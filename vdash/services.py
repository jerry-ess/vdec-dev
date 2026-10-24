"""Dashboard business logic, independent of HTTP (v22).

Everything the API routes need beyond request parsing lives here, so other features (the report engine, the
scheduler) can reuse exactly the same rules without importing FastAPI or going through HTTP:

* ``Services``          - the long-lived objects (settings, users, warehouse registry, state store, result cache,
                          KPI engine, refresher, query thread pool, report store (v25)). One per process.
* ``DashboardService``  - what a user may see and the data behind it: visible KPIs, period validation, KPI
                          results, business evaluation, early warnings, role filtering and translation.
* ``ServiceError``      - a refusal with an HTTP-like status (400 bad input, 404 not available, 503 still
                          preparing). The API turns it into the same JSON error response as before.

Moved unchanged from the closures inside ``api.create_app()`` (v21); behaviour is identical.
"""
from __future__ import annotations

import copy
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

from .catalog import Kpi, load_catalog
from .config import Settings, TenantConfig
from .datasources import DataSourceRegistry
from .engine import COMPARE_MODES, GRAINS, KpiEngine, PeriodRequest, ResultCache
from .i18n import t, translate_result
from . import evaluation as ev
from .refresher import Refresher, warnings_version
from .report_store import from_settings as report_store_from_settings
from .security import LoginThrottle, SessionSigner, User, UserStore
from .state_store import StateStore

log = logging.getLogger("vdash.api")   # same logger name as before: slow-request lines keep their prefix

MAX_RANGE_DAYS = 3700
PANEL_SECTION = {"evaluation": "overview", "warnings": "ai"}   # panel -> section it is shown in
RULE_PANEL = {"evaluation": "evaluation", "warnings": "warning"}   # panel -> rules-file panel name


class ServiceError(Exception):
    """A request the service refuses. ``status`` follows HTTP (400 / 404 / 503); ``detail`` is shown to the user."""

    def __init__(self, status: int, detail: str, headers: dict | None = None):
        super().__init__(detail)
        self.status, self.detail, self.headers = status, detail, headers


class Services:
    def __init__(self) -> None:
        self.settings = Settings.load()
        self.users = UserStore()
        self.signer = SessionSigner(self.settings.session_secret, self.settings.session_ttl_minutes)
        self.throttle = LoginThrottle(self.settings.login_max_failures, self.settings.login_lockout_minutes)
        self.registry = DataSourceRegistry(self.settings.query_timeout_seconds)
        self.state = StateStore(self.settings.state_store_path)
        self.cache = ResultCache(self.settings.cache_max_entries, self.settings.cache_ttl_minutes * 60,
                                 self.settings.cache_persist_path)
        self.engine = KpiEngine(self.cache)
        self.refresher = Refresher(self.settings, self.registry, self.state, self.engine, self.cache)
        self.pool = ThreadPoolExecutor(max_workers=int(self.settings.raw.get("query", {}).get("parallel", 4)))
        # v25: durable report data (SQLite locally, external PostgreSQL online); connects on first use only
        self.reports = report_store_from_settings(self.settings.raw)
        # v37: AI writer for reports (key from the environment only; nothing is contacted at start-up)
        from .llm import LLMClient, llm_from_settings      # v88: llm_from_settings = the default model + its backup
        try:
            self.llm = llm_from_settings(self.settings.raw)
        except ValueError as exc:
            log.error("llm settings: %s - AI writing disabled", exc)
            self.llm = LLMClient(enabled=False)

    @property
    def tenants(self) -> dict[str, TenantConfig]:
        return self.refresher.tenants


class DashboardService:
    """Per-user views of a tenant's data. Stateless apart from ``svc``; safe to share between threads."""

    def __init__(self, svc: Services) -> None:
        self.svc = svc
        self._scope_names: dict = {}

    # ------------------------------------------------------------ visibility
    def visible(self, user: User, t: TenantConfig) -> tuple[list[Kpi], list[dict], int, dict]:
        """KPIs this user sees for this tenant = role allows it AND the tenant has data for it."""
        svc = self.svc
        catalog = load_catalog(t.catalog)
        st = svc.state.get(t.tenant_id)
        if st is None:
            # v20: never run the refresh inside a request (it pre-computes every common period and took minutes on
            # a small server). Start it in the background; the page shows "preparing the data" and retries.
            svc.refresher.kick()
            raise ServiceError(503, "preparing", headers={"Retry-After": "3"})
        avail = st.get("availability", {})
        shown, hidden, role_hidden = [], [], 0
        sds = self.ds_for(user, t) if user.is_scoped else None
        for k in catalog.kpis:
            if not user.has_role(k.min_role):
                role_hidden += 1
                continue
            if sds is not None and not sds.allows(k.sql, getattr(k, "spark_sql", None)):
                hidden.append({"id": k.id, "title": k.title, "section": k.sections[0],
                               "reasons": ["cannot be shown for a single store"], "template_kpis": k.template_kpis})
                continue
            a = avail.get(k.id, {"available": False, "reasons": ["not probed yet"]})
            if a["available"]:
                shown.append(k)
            else:
                hidden.append({"id": k.id, "title": k.title, "section": k.sections[0], "reasons": a["reasons"],
                               "template_kpis": k.template_kpis})
        return shown, hidden, role_hidden, st

    def display_name_for(self, user: User, t: TenantConfig) -> str:
        """v99: a store-scoped user sees "Business - Store name" (the real store names), everyone else the business name."""
        if user is None or not user.is_scoped:
            return t.display_name
        key = (t.tenant_id, tuple(sorted(user.locations)))
        names = self._scope_names.get(key)
        if names is None:
            try:
                ids = ", ".join("'" + i.replace("'", "") + "'" for i in key[1])
                rows = self.svc.registry.get(t.datasource).query(
                    "SELECT dim_location_id AS i, MAX(location_name) AS n FROM dim_location WHERE tenant_id = :tenant_id "
                    f"AND dim_location_id IN ({ids}) GROUP BY dim_location_id", {"tenant_id": t.tenant_id})
                by = {r["i"]: r["n"] for r in rows}
                names = [by.get(i) or i for i in key[1]]
                self._scope_names[key] = names
            except Exception:      # noqa: BLE001 - the name is a nicety; the id alone is still right
                names = list(key[1])
        return f"{t.display_name} - {', '.join(names)}"

    def available_exports(self, user: User, t: TenantConfig, st: dict) -> list:
        if user.is_scoped:     # v99: customer-level lists cannot be split by store
            return []
        avail = st.get("availability", {})
        return [e for e in load_catalog(t.catalog).exports
                if user.has_role(e.min_role) and avail.get(f"export:{e.id}", {}).get("available")]

    # ------------------------------------------------------------ evaluation + early warnings (v9)
    def user_rules(self, user: User, tc: TenantConfig, st: dict, panel: str) -> list:
        catalog = load_catalog(tc.catalog)
        return [r for r in ev.active_rules(catalog, RULE_PANEL[panel], st.get("availability", {}), tc.alerts)
                if user.has_role(ev.rule_role(r, catalog))]

    def panels_for(self, user: User, tc: TenantConfig, st: dict) -> dict:
        return {p: bool(st.get("max_date")) and bool(self.user_rules(user, tc, st, p)) for p in PANEL_SECTION}

    def present(self, findings: list[dict], user: User, tc: TenantConfig, lg: str, max_items: int = 0) -> list[dict]:
        """Role filter (rule role, item role) + translation. The cached/stored originals are never mutated."""
        catalog = load_catalog(tc.catalog)
        rules = catalog.rules_by_id
        vis = {x.id for x in self.visible(user, tc)[0]}
        out = []
        for f in ev.for_role([f for f in findings if f["rule"] in rules],
                             lambda rid: ev.rule_role(rules[rid], catalog), user.has_role,
                             lambda rid: rules[rid].items_min_role):
            f = copy.deepcopy(f)
            if max_items and len(f.get("items") or []) > max_items:
                f["items"] = f["items"][:max_items]
            f["title"] = rules[f["rule"]].title_i18n.get(lg) or t(lg, rules[f["rule"]].title)   # current rule-file title
            for a in f["args"].values():
                if a.get("fmt") == "label":
                    a["v"] = t(lg, a["v"])
            text_cols = [c["key"] for c in f.get("items_cols", []) if c.get("fmt", "text") == "text"]
            for c in f.get("items_cols", []):
                c["label"] = t(lg, c["label"])
            for row in f.get("items", []):
                for key in text_cols:
                    row[key] = t(lg, row.get(key))
            if f.get("link_kpi"):
                k = catalog.by_id.get(f["link_kpi"])
                f["link"] = ({"kpi": k.id, "section": k.sections[0], "title": t(lg, k.title)}
                             if k is not None and k.id in vis else None)
            out.append(f)
        return out

    def log_slow(self, what: str, tenant_id: str, req: PeriodRequest, secs: float, results: dict) -> None:
        """v16: every request slower than query.log_slow_seconds (default 1 s) is logged with its slowest KPIs,
        so a slow machine / drive shows up in the server log instead of only as a spinner."""
        limit = float(self.svc.settings.raw.get("query", {}).get("log_slow_seconds", 1.0))
        if secs < limit:
            return
        cached = sum(1 for r in results.values() if r.get("cached"))
        slow = sorted(((r.get("ms") or 0, k) for k, r in results.items() if not r.get("cached")), reverse=True)[:5]
        log.info("slow %s for %s %s..%s: %.2f s (%d KPIs, %d from cache); slowest: %s", what, tenant_id, req.start,
                 req.end, secs, len(results), cached, ", ".join(f"{k} {ms:.0f} ms" for ms, k in slow) or "-")

    def evaluation_result(self, user: User, tc: TenantConfig, st: dict, req: PeriodRequest) -> dict:
        svc = self.svc
        catalog = load_catalog(tc.catalog)
        version = st.get("data_version") or ""
        ds = self.ds_for(user, tc)
        key = ev.eval_cache_key(tc.tenant_id, version + getattr(ds, "scope_key", ""), req, catalog, tc.alerts)
        res = svc.cache.get(key)
        t0 = time.perf_counter()
        if res is None:
            res = ev.evaluate_period(catalog, ds, tc.tenant_id, svc.engine, version,
                                     st.get("availability", {}), tc.alerts, req, pool=svc.pool)
            if not res["errors"]:
                svc.cache.put(key, res)
            self.log_slow("evaluation", tc.tenant_id, req, time.perf_counter() - t0, {})
        return res

    def warnings_result(self, user: User, tc: TenantConfig, st: dict) -> dict:
        svc = self.svc
        catalog = load_catalog(tc.catalog)
        ds = self.ds_for(user, tc)
        version = st.get("data_version") or ""
        if user.is_scoped:      # v99: stored warnings are chain-wide - a store user gets live checks on the store only
            live = ev.evaluate_warnings(catalog, ds, tc.tenant_id, svc.engine, version, st.get("availability", {}),
                                        tc.alerts, date.fromisoformat(st["max_date"]), tc.timezone, live=True)
            for w in live["warnings"]:
                w.update(first_seen=None, is_new=False)
            return {"asof": st.get("max_date"), "computed_at": None, "checked": live["checked"],
                    "errors": live["errors"], "warnings": ev.sort_warnings(live["warnings"])}
        run = svc.state.alert_run(tc.tenant_id)
        if run is None or run.get("data_version") != warnings_version(version, catalog):
            run = svc.refresher.compute_warnings(tc, ds, catalog, version, st.get("availability", {}), st["max_date"])
        active_ids = {r.id for r in ev.active_rules(catalog, "warning", st.get("availability", {}), tc.alerts)}
        stored = [w for w in svc.state.active_warnings(tc.tenant_id) if w["rule"] in active_ids]
        for w in stored:
            # new = first seen in the latest computation (same save timestamp), and not the very first computation
            w["is_new"] = (run.get("prev_version") is not None and w.get("first_seen") is not None
                           and abs(w["first_seen"] - (run.get("computed_at") or 0)) < 1e-6)
        live = ev.evaluate_warnings(catalog, ds, tc.tenant_id, svc.engine, version, st.get("availability", {}),
                                    tc.alerts, date.fromisoformat(st["max_date"]), tc.timezone, live=True)
        for w in live["warnings"]:
            w.update(first_seen=None, is_new=False)
        return {"asof": run.get("asof"), "computed_at": run.get("computed_at"),
                "checked": (run.get("checked") or 0) + live["checked"],
                "errors": (run.get("errors") or []) + live["errors"],
                "warnings": ev.sort_warnings(stored + live["warnings"])}

    # ------------------------------------------------------------ periods + KPI results
    def period(self, start: str | None, end: str | None, grain: str, compare: str, st: dict) -> PeriodRequest:
        try:
            max_d = date.fromisoformat(st["max_date"]) if st.get("max_date") else date.today()
            e = date.fromisoformat(end) if end else max_d
            s = date.fromisoformat(start) if start else e - timedelta(days=29)
        except ValueError:
            raise ServiceError(400, "Dates must be YYYY-MM-DD")
        if s > e:
            raise ServiceError(400, "Start date is after end date")
        if (e - s).days > MAX_RANGE_DAYS:
            raise ServiceError(400, "Period too long (max ~10 years)")
        if grain not in (*GRAINS, "auto"):
            raise ServiceError(400, f"grain must be one of auto, {', '.join(GRAINS)}")
        if compare not in COMPARE_MODES:
            raise ServiceError(400, f"compare must be one of {', '.join(COMPARE_MODES)}")
        return PeriodRequest(s, e, grain, compare)

    def ds_for(self, user: User, t: TenantConfig):
        """v99: the warehouse connection for this user - restricted to the user's stores when users.json lists ``locations``."""
        ds = self.svc.registry.get(t.datasource)
        if user is not None and user.is_scoped:
            from .scope import ScopedSource
            return ScopedSource(ds, t.tenant_id, user.locations)
        return ds

    def run_many(self, user: User, t: TenantConfig, kpis: list[Kpi], req: PeriodRequest, st: dict) -> dict:
        svc = self.svc
        ds = self.ds_for(user, t)
        version = st.get("data_version") or ""
        svc.engine.touch()
        futures = {k.id: svc.pool.submit(svc.engine.run, ds, t.tenant_id, k, req, version) for k in kpis}
        out = {kid: f.result() for kid, f in futures.items()}
        svc.engine.touch()
        return out

    @staticmethod
    def translate(k: Kpi, res: dict, lang: str, user: User | None = None) -> dict:
        out = translate_result(res, lang, {c.key for c in k.columns if c.fmt == "text"})
        if out.get("type") == "error" and not (user and user.is_platform_admin):
            out["error"] = ""   # v11: technical error text (SQL, table names) only for platform admins
        return out

    def one_kpi(self, user: User, tc: TenantConfig, kid: str, start, end, grain, compare
                ) -> tuple[Kpi, dict, PeriodRequest]:
        shown, _, _, st = self.visible(user, tc)
        k = next((k for k in shown if k.id == kid), None)
        if k is None:
            raise ServiceError(404, "KPI not available")
        req = self.period(start, end, grain, compare, st)
        return k, self.run_many(user, tc, [k], req, st)[kid], req
