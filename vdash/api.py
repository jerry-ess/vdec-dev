"""HTTP API + static frontend (FastAPI).

Endpoints
---------
POST /api/login                         username/password -> signed HttpOnly session cookie
POST /api/logout
GET  /api/me                            user + the tenants this user may open
GET  /api/tenants/{tid}/context         tenant meta, data range, presets, visible sections/KPIs, hidden KPIs
GET  /api/tenants/{tid}/sections/{sid}  results for every visible KPI of a section for a period
GET  /api/tenants/{tid}/kpis/{kid}      one KPI for a period
GET  /api/tenants/{tid}/evaluation      business evaluation for a period (v9: going well / needs attention)
GET  /api/tenants/{tid}/warnings        early warnings on the latest data (v9)
GET  /api/tenants/{tid}/evaluation/export, /warnings/export   the same as CSV / Excel (one finding's rows with ?rule=)
POST /api/tenants/{tid}/refresh         re-probe availability now (tenant_admin / platform_admin)
GET  /api/admin/status                  refresh status of all tenants + cache stats (platform_admin)
GET  /api/health

Every tenant-scoped route goes through ``tenant_guard``: the session user must
be allowed on that tenant, otherwise 403 (and the attempt is audited).
"""
from __future__ import annotations

import logging
import time
from urllib.parse import quote
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import date, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, Response as RawResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .catalog import Kpi, load_catalog
from .config import ROOT, Settings, TenantConfig
from .datasources import DataSourceRegistry
from .engine import COMPARE_MODES, GRAINS, KpiEngine, PeriodRequest, ResultCache, presets
from .exports import build_export_sql, filter_options, included_joins, kpi_table, safe_filename, to_csv, to_xlsx
from .i18n import LANGS, LOCALES, bundle, norm_lang, t, translate_kpi_public, translate_result, ui
from . import evaluation as ev
from .refresher import Refresher
from .security import LoginThrottle, SessionSigner, User, UserStore
from .state_store import StateStore

log = logging.getLogger("vdash.api")
COOKIE = "vdash_session"
WEB = ROOT / "web"
MAX_RANGE_DAYS = 3700


class LoginBody(BaseModel):
    username: str
    password: str


class Services:
    def __init__(self) -> None:
        self.settings = Settings.load()
        self.users = UserStore()
        self.signer = SessionSigner(self.settings.session_secret, self.settings.session_ttl_minutes)
        self.throttle = LoginThrottle(self.settings.login_max_failures, self.settings.login_lockout_minutes)
        self.registry = DataSourceRegistry(self.settings.query_timeout_seconds)
        self.state = StateStore(self.settings.state_store_path)
        self.cache = ResultCache(self.settings.cache_max_entries, self.settings.cache_ttl_minutes * 60)
        self.engine = KpiEngine(self.cache)
        self.refresher = Refresher(self.settings, self.registry, self.state, self.engine, self.cache)
        self.pool = ThreadPoolExecutor(max_workers=int(self.settings.raw.get("query", {}).get("parallel", 4)))

    @property
    def tenants(self) -> dict[str, TenantConfig]:
        return self.refresher.tenants


def create_app(start_refresher: bool | None = None) -> FastAPI:
    svc = Services()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        run_bg = svc.settings.refresh_enabled if start_refresher is None else start_refresher
        if run_bg:
            svc.refresher.start()  # first cycle runs immediately in the background
        yield
        svc.refresher.stop()
        svc.registry.close_all()

    app = FastAPI(title="Veridex BI Dashboard", version="1.0", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.svc = svc

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        resp: Response = await call_next(request)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "same-origin"
        resp.headers["Content-Security-Policy"] = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline' "
                                                   "https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; "
                                                   "img-src 'self' data: blob:; connect-src 'self'; manifest-src 'self'; worker-src 'self'; "
                                                   "frame-ancestors 'none'")
        if request.url.path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    # ------------------------------------------------------------ auth helpers
    def current_user(request: Request) -> User:
        username = svc.signer.verify(request.cookies.get(COOKIE))
        user = svc.users.get(username) if username else None
        if user is None or not user.enabled:
            raise HTTPException(401, "Not signed in")
        return user

    def tenant_guard(tid: str, user: User = Depends(current_user)) -> tuple[User, TenantConfig]:
        t = svc.tenants.get(tid)
        if t is None or not t.enabled or not user.can_access_tenant(tid):
            svc.state.audit(user.username, tid, "denied", "tenant access refused")
            raise HTTPException(403, "You do not have access to this tenant")
        return user, t

    def visible(user: User, t: TenantConfig) -> tuple[list[Kpi], list[dict], int, dict]:
        """KPIs this user sees for this tenant = role allows it AND the tenant has data for it."""
        catalog = load_catalog(t.catalog)
        st = svc.state.get(t.tenant_id)
        if st is None:
            svc.refresher.refresh_tenant(t.tenant_id)
            st = svc.state.get(t.tenant_id) or {}
        avail = st.get("availability", {})
        shown, hidden, role_hidden = [], [], 0
        for k in catalog.kpis:
            if not user.has_role(k.min_role):
                role_hidden += 1
                continue
            a = avail.get(k.id, {"available": False, "reasons": ["not probed yet"]})
            if a["available"]:
                shown.append(k)
            else:
                hidden.append({"id": k.id, "title": k.title, "section": k.sections[0], "reasons": a["reasons"],
                               "template_kpis": k.template_kpis})
        return shown, hidden, role_hidden, st

    # ------------------------------------------------------------ evaluation + early warnings (v9)
    PANEL_SECTION = {"evaluation": "overview", "warnings": "ai"}
    RULE_PANEL = {"evaluation": "evaluation", "warnings": "warning"}

    def user_rules(user: User, tc: TenantConfig, st: dict, panel: str) -> list:
        catalog = load_catalog(tc.catalog)
        return [r for r in ev.active_rules(catalog, RULE_PANEL[panel], st.get("availability", {}), tc.alerts)
                if user.has_role(ev.rule_role(r, catalog))]

    def panels_for(user: User, tc: TenantConfig, st: dict) -> dict:
        return {p: bool(st.get("max_date")) and bool(user_rules(user, tc, st, p)) for p in PANEL_SECTION}

    def present(findings: list[dict], user: User, tc: TenantConfig, lg: str, max_items: int = 0) -> list[dict]:
        """Role filter (rule role, item role) + translation. The cached/stored originals are never mutated."""
        import copy
        catalog = load_catalog(tc.catalog)
        rules = catalog.rules_by_id
        vis = {x.id for x in visible(user, tc)[0]}
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

    def _log_slow(what: str, tenant_id: str, req: PeriodRequest, secs: float, results: dict) -> None:
        """v16: every request slower than query.log_slow_seconds (default 1 s) is logged with its slowest KPIs,
        so a slow machine / drive shows up in the server log instead of only as a spinner."""
        limit = float(svc.settings.raw.get("query", {}).get("log_slow_seconds", 1.0))
        if secs < limit:
            return
        cached = sum(1 for r in results.values() if r.get("cached"))
        slow = sorted(((r.get("ms") or 0, k) for k, r in results.items() if not r.get("cached")), reverse=True)[:5]
        log.info("slow %s for %s %s..%s: %.2f s (%d KPIs, %d from cache); slowest: %s", what, tenant_id, req.start,
                 req.end, secs, len(results), cached, ", ".join(f"{k} {ms:.0f} ms" for ms, k in slow) or "-")

    def evaluation_result(user: User, tc: TenantConfig, st: dict, req: PeriodRequest) -> dict:
        catalog = load_catalog(tc.catalog)
        version = st.get("data_version") or ""
        key = ev.eval_cache_key(tc.tenant_id, version, req, catalog, tc.alerts)
        res = svc.cache.get(key)
        t0 = time.perf_counter()
        if res is None:
            res = ev.evaluate_period(catalog, svc.registry.get(tc.datasource), tc.tenant_id, svc.engine, version,
                                     st.get("availability", {}), tc.alerts, req, pool=svc.pool)
            if not res["errors"]:
                svc.cache.put(key, res)
            _log_slow("evaluation", tc.tenant_id, req, time.perf_counter() - t0, {})
        return res

    def warnings_result(user: User, tc: TenantConfig, st: dict) -> dict:
        catalog = load_catalog(tc.catalog)
        ds = svc.registry.get(tc.datasource)
        version = st.get("data_version") or ""
        from .refresher import warnings_version
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

    def period(start: str | None, end: str | None, grain: str, compare: str, st: dict) -> PeriodRequest:
        try:
            max_d = date.fromisoformat(st["max_date"]) if st.get("max_date") else date.today()
            e = date.fromisoformat(end) if end else max_d
            s = date.fromisoformat(start) if start else e - timedelta(days=29)
        except ValueError:
            raise HTTPException(400, "Dates must be YYYY-MM-DD")
        if s > e:
            raise HTTPException(400, "Start date is after end date")
        if (e - s).days > MAX_RANGE_DAYS:
            raise HTTPException(400, "Period too long (max ~10 years)")
        if grain not in (*GRAINS, "auto"):
            raise HTTPException(400, f"grain must be one of auto, {', '.join(GRAINS)}")
        if compare not in COMPARE_MODES:
            raise HTTPException(400, f"compare must be one of {', '.join(COMPARE_MODES)}")
        return PeriodRequest(s, e, grain, compare)

    # ------------------------------------------------------------ routes
    @app.post("/api/login")
    def login(body: LoginBody, response: Response):
        if svc.throttle.blocked(body.username):
            raise HTTPException(429, "Too many failed attempts - try again in a few minutes")
        user = svc.users.authenticate(body.username, body.password)
        if user is None:
            svc.throttle.fail(body.username)
            svc.state.audit(body.username, None, "login_failed")
            raise HTTPException(401, "Invalid username or password")
        svc.throttle.reset(body.username)
        response.set_cookie(COOKIE, svc.signer.issue(user.username), httponly=True, samesite="lax",
                            secure=svc.settings.cookie_secure, max_age=svc.settings.session_ttl_minutes * 60)
        svc.state.audit(user.username, None, "login")
        return {"ok": True, "user": user.public()}

    @app.post("/api/logout")
    def logout(response: Response):
        response.delete_cookie(COOKIE)
        return {"ok": True}

    @app.get("/api/me")
    def me(user: User = Depends(current_user)):
        tenants = [{"id": t.tenant_id, "display_name": t.display_name, "industry": t.industry}
                   for t in svc.tenants.values() if t.enabled and user.can_access_tenant(t.tenant_id)]
        return {"user": user.public(), "tenants": tenants}

    @app.get("/api/i18n/{lang}")
    def i18n_bundle(lang: str):
        lg = norm_lang(lang)
        return {"lang": lg, "locale": LOCALES[lg], "languages": LANGS, "ui": bundle(lg)["ui"]}

    def available_exports(user: User, t: TenantConfig, st: dict) -> list:
        avail = st.get("availability", {})
        return [e for e in load_catalog(t.catalog).exports
                if user.has_role(e.min_role) and avail.get(f"export:{e.id}", {}).get("available")]

    @app.get("/api/tenants/{tid}/context")
    def context(lang: str = "en", g=Depends(tenant_guard)):
        user, tc = g
        lg = norm_lang(lang)
        shown, hidden, role_hidden, st = visible(user, tc)
        catalog = load_catalog(tc.catalog)
        panels = panels_for(user, tc, st)
        sections = []
        for s in catalog.sections:
            ks = [translate_kpi_public(k.to_public(), lg) for k in shown if s.id in k.sections]
            panel = next((p for p, sec in PANEL_SECTION.items() if sec == s.id and panels[p]), None)
            if ks or panel:
                sections.append({"id": s.id, "title": t(lg, s.title), "icon": s.icon, "desc": t(lg, s.desc), "kpis": ks,
                                 "panel": panel})
        for h in hidden:
            h["title"] = t(lg, h["title"])
            if not user.is_platform_admin:  # business admins: which data unlocks it, in plain words (v11)
                h["reasons"] = sorted({t(lg, catalog.data_area(r)) for r in h["reasons"]})
                h.pop("template_kpis", None)
        if not user.is_platform_admin:      # internal KPI codes stay internal
            for s_ in sections:
                for k_ in s_["kpis"]:
                    k_["template_kpis"] = []
        exports = [{**e.to_public(), "title": t(lg, e.title), "desc": t(lg, e.desc)}
                   for e in available_exports(user, tc, st)]
        svc.state.audit(user.username, tc.tenant_id, "open_dashboard")
        max_d, min_d = st.get("max_date"), st.get("min_date")
        return {
            "tenant": {"id": tc.tenant_id, "display_name": tc.display_name, "currency": tc.currency, "locale": tc.locale,
                       "industry": tc.industry, "template_id": catalog.template_id if user.is_platform_admin else None},
            "exports": exports,
            "panels": panels,
            "data": {"min_date": min_d, "max_date": max_d, "last_loaded_at": st.get("last_loaded_at"),
                     "refreshed_at": st.get("refreshed_at"), "status": st.get("status"), "error": st.get("error")},
            "presets": presets(date.fromisoformat(max_d), date.fromisoformat(min_d)) if max_d and min_d else [],
            "sections": sections,
            "kpi_counts": {"catalog": len(catalog.kpis), "shown": len(shown), "no_data": len(hidden),
                           "role_restricted": role_hidden},
            # which KPIs are hidden for lack of data (helps the demo story "connect X to unlock Y")
            "hidden": hidden if user.has_role("tenant_admin") else [],
        }

    def _run_many(user: User, t: TenantConfig, kpis: list[Kpi], req: PeriodRequest, st: dict) -> dict:
        ds = svc.registry.get(t.datasource)
        version = st.get("data_version") or ""
        futures = {k.id: svc.pool.submit(svc.engine.run, ds, t.tenant_id, k, req, version) for k in kpis}
        return {kid: f.result() for kid, f in futures.items()}

    def _tr(k: Kpi, res: dict, lang: str, user: User | None = None) -> dict:
        out = translate_result(res, lang, {c.key for c in k.columns if c.fmt == "text"})
        if out.get("type") == "error" and not (user and user.is_platform_admin):
            out["error"] = ""   # v11: technical error text (SQL, table names) only for platform admins
        return out

    @app.get("/api/tenants/{tid}/sections/{sid}")
    def section_data(sid: str, start: str | None = None, end: str | None = None, grain: str = "auto",
                     compare: str = "previous", lang: str = "en", g=Depends(tenant_guard)):
        user, tc = g
        shown, _, _, st = visible(user, tc)
        kpis = [k for k in shown if sid in k.sections]
        has_panel = any(sec == sid and on for p, sec in PANEL_SECTION.items() for on in [panels_for(user, tc, st)[p]])
        if not kpis and not has_panel:
            raise HTTPException(404, "Section not available for this tenant")
        req = period(start, end, grain, compare, st)
        t0 = time.perf_counter()
        results = _run_many(user, tc, kpis, req, st)
        _log_slow(f"section {sid}", tc.tenant_id, req, time.perf_counter() - t0, results)
        lg = norm_lang(lang)
        by_id = {k.id: k for k in kpis}
        return {"period": {"start": req.start.isoformat(), "end": req.end.isoformat(), "grain": req.resolved_grain(),
                           "compare": req.compare},
                "results": {kid: _tr(by_id[kid], r, lg, user) for kid, r in results.items()}}

    def _one(user: User, tc: TenantConfig, kid: str, start, end, grain, compare) -> tuple[Kpi, dict, PeriodRequest]:
        shown, _, _, st = visible(user, tc)
        k = next((k for k in shown if k.id == kid), None)
        if k is None:
            raise HTTPException(404, "KPI not available")
        req = period(start, end, grain, compare, st)
        return k, _run_many(user, tc, [k], req, st)[kid], req

    @app.get("/api/tenants/{tid}/kpis/{kid}")
    def kpi_data(kid: str, start: str | None = None, end: str | None = None, grain: str = "auto",
                 compare: str = "previous", lang: str = "en", g=Depends(tenant_guard)):
        user, tc = g
        k, res, _ = _one(user, tc, kid, start, end, grain, compare)
        return _tr(k, res, norm_lang(lang), user)

    def _file(data: bytes, name: str, fmt: str) -> RawResponse:
        media = ("text/csv; charset=utf-8" if fmt == "csv"
                 else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        return RawResponse(data, media_type=media, headers={
            "Content-Disposition": f"attachment; filename=\"{name}.{fmt}\"; filename*=UTF-8''{quote(name)}.{fmt}"})

    @app.get("/api/tenants/{tid}/kpis/{kid}/export")
    def kpi_export(kid: str, format: str = "xlsx", start: str | None = None, end: str | None = None,
                   grain: str = "auto", compare: str = "previous", lang: str = "en", g=Depends(tenant_guard)):
        """One chart's data as CSV or Excel (same role/tenant checks as viewing it)."""
        user, tc = g
        if format not in ("csv", "xlsx"):
            raise HTTPException(400, "format must be csv or xlsx")
        lg = norm_lang(lang)
        k, res, req = _one(user, tc, kid, start, end, grain, compare)
        if res.get("type") == "error":
            raise HTTPException(500, res["error"] if user.is_platform_admin else "This chart could not be calculated right now")
        cols, rows = kpi_table(k, _tr(k, res, lg), lg)
        title = t(lg, k.title)
        svc.state.audit(user.username, tc.tenant_id, "export_kpi", f"{kid} {format} {req.start}..{req.end}")
        name = safe_filename(tc.tenant_id, k.id, req.start.isoformat(), req.end.isoformat())
        if format == "csv":
            return _file(to_csv(cols, rows), name, "csv")
        about = [("Business", tc.display_name), ("Chart", title), ("Period", f"{req.start} - {req.end}"),
                 ("Grain", req.resolved_grain())]
        return _file(to_xlsx(title, cols, rows, about), name, "xlsx")

    @app.get("/api/tenants/{tid}/kpis/{kid}/drill")
    def kpi_drill(kid: str, f: str = "{}", start: str | None = None, end: str | None = None, grain: str = "auto",
                  format: str = "json", lang: str = "en", g=Depends(tenant_guard)):
        """Rows behind one chart element. ``f`` (JSON) = what was clicked: {"bucket", "grain"} | {"key"} |
        {"x" | "x_range", "y"}. json -> first 200 rows + totals; csv/xlsx -> all rows (audited)."""
        import json as _json
        from .drill import MAX_EXPORT, MAX_PREVIEW, DrillError, build, drop_empty_columns
        user, tc = g
        if format not in ("json", "csv", "xlsx"):
            raise HTTPException(400, "format must be json, csv or xlsx")
        lg = norm_lang(lang)
        shown, _, _, st = visible(user, tc)
        k = next((k for k in shown if k.id == kid), None)
        if k is None or k.drill is None:
            raise HTTPException(404, "No detail rows for this KPI")
        try:
            clicked = _json.loads(f)
            if not isinstance(clicked, dict) or len(f) > 500:
                raise ValueError
        except ValueError:
            raise HTTPException(400, "f must be a small JSON object")
        req = period(start, end, grain, "none", st)
        catalog = load_catalog(tc.catalog)
        src = catalog.details[k.drill.source]
        ds = svc.registry.get(tc.datasource)
        try:
            rows_sql, sum_sql, params, cols, rng = build(k, src, clicked, req.start, req.end, ds.dialect, tc.tenant_id,
                                                         show_pii=user.has_role("manager"), available=ds.columns())
        except DrillError as exc:
            raise HTTPException(400, str(exc))
        summary = ds.query(sum_sql, params)[0]
        limit = MAX_PREVIEW if format == "json" else MAX_EXPORT
        rows = ds.query(f"{rows_sql} LIMIT {limit}", params)
        cols = drop_empty_columns(cols, rows)
        for r in rows:
            for c in cols:
                if c.fmt == "text":
                    r[c.key] = t(lg, r.get(c.key))
        out_cols = [{"key": c.key, "label": t(lg, c.label), "fmt": c.fmt} for c in cols]
        rows = [{c.key: r.get(c.key) for c in cols} for r in rows]
        if format == "json":
            return {"kpi": k.id, "range": rng, "columns": out_cols, "rows": rows, "total_rows": int(summary["n"] or 0),
                    "total_value": summary.get("total_v"), "total_label": t(lg, next(
                        (c.label for c in src.columns if c.expr == src.total_expr), "Total")),
                    "truncated": int(summary["n"] or 0) > len(rows)}
        svc.state.audit(user.username, tc.tenant_id, "export_drill", f"{kid} {format} {clicked} rows={len(rows)}")
        name = safe_filename(tc.tenant_id, kid, "rows", *rng.split(".."))
        if format == "csv":
            return _file(to_csv(out_cols, rows), name, "csv")
        about = [("Business", tc.display_name), ("Chart", t(lg, k.title)), ("Period", rng.replace("..", " - ")),
                 ("Clicked", _json.dumps(clicked, ensure_ascii=False))]
        return _file(to_xlsx(t(lg, k.title), out_cols, rows, about), name, "xlsx")

    def _export_def(user: User, tc: TenantConfig, eid: str):
        st = svc.state.get(tc.tenant_id) or {}
        exp = next((e for e in available_exports(user, tc, st) if e.id == eid), None)
        if exp is None:
            raise HTTPException(404 if not any(e.id == eid for e in load_catalog(tc.catalog).exports) else 403,
                                "This list is not available for your role or this business")
        return exp

    @app.get("/api/tenants/{tid}/exports/{eid}/options")
    def export_options(eid: str, lang: str = "en", g=Depends(tenant_guard)):
        user, tc = g
        lg = norm_lang(lang)
        exp = _export_def(user, tc, eid)
        ds = svc.registry.get(tc.datasource)
        filters = filter_options(exp, ds, tc.tenant_id)
        for f in filters:
            f["label"] = t(lg, f["label"])
            for o in f["options"]:
                o["label"] = t(lg, o["value"])
        total = ds.query(f"SELECT COUNT(*) AS n FROM {exp.base_from} WHERE {exp.base_where}", {"tenant_id": tc.tenant_id})
        return {"id": exp.id, "title": t(lg, exp.title), "desc": t(lg, exp.desc), "filters": filters,
                "total_rows": int(total[0]["n"]) if total else None}

    @app.get("/api/tenants/{tid}/exports/{eid}")
    def export_list(eid: str, request: Request, format: str = "xlsx", lang: str = "en", g=Depends(tenant_guard)):
        """Complete list (e.g. every customer with its segment), optionally filtered to one segment."""
        user, tc = g
        if format not in ("csv", "xlsx"):
            raise HTTPException(400, "format must be csv or xlsx")
        lg = norm_lang(lang)
        exp = _export_def(user, tc, eid)
        ds = svc.registry.get(tc.datasource)
        chosen = {f.param: request.query_params.get(f"f_{f.param}") or None for f in exp.filters}
        sql, params, cols = build_export_sql(exp, ds, chosen)
        params["tenant_id"] = tc.tenant_id
        rows = ds.query(sql, params)
        text_cols = {c.key for c in cols if c.fmt == "text"}
        for r in rows:
            for key in text_cols:
                r[key] = t(lg, r.get(key))
        out_cols = [{"key": c.key, "label": t(lg, c.label), "fmt": c.fmt} for c in cols]
        picked = {k: v for k, v in chosen.items() if v}
        svc.state.audit(user.username, tc.tenant_id, "export_list", f"{eid} {format} {picked} rows={len(rows)}")
        name = safe_filename(tc.tenant_id, eid, *[str(v) for v in picked.values()],
                             (st := svc.state.get(tc.tenant_id) or {}).get("max_date") or "")
        if format == "csv":
            return _file(to_csv(out_cols, rows), name, "csv")
        labels = {f.param: t(lg, f.label) for f in exp.filters}
        about = [("Business", tc.display_name), ("List", t(lg, exp.title)), ("Description", t(lg, exp.desc)),
                 ("Filters", "; ".join(f"{labels[k]} = {t(lg, v)}" for k, v in picked.items()) or "-"),
                 ("Data through", st.get("max_date") or "-")]
        return _file(to_xlsx(t(lg, exp.title), out_cols, rows, about), name, "xlsx")

    @app.get("/api/tenants/{tid}/evaluation")
    def evaluation(start: str | None = None, end: str | None = None, grain: str = "auto", compare: str = "previous",
                   lang: str = "en", g=Depends(tenant_guard), max_items: int = 100):
        """Business evaluation for the selected period vs its comparison (Executive Overview)."""
        user, tc = g
        _, _, _, st = visible(user, tc)
        if not panels_for(user, tc, st)["evaluation"]:
            raise HTTPException(404, "No evaluation available for this business")
        req = period(start, end, grain, compare, st)
        lg = norm_lang(lang)
        res = evaluation_result(user, tc, st, req)
        return {"period": {"start": req.start.isoformat(), "end": req.end.isoformat(), "compare": req.compare},
                "checked": len(user_rules(user, tc, st, "evaluation")),
                "positive": present(res["positive"], user, tc, lg, max_items),
                "negative": present(res["negative"], user, tc, lg, max_items),
                "errors": res["errors"] if user.is_platform_admin else []}

    @app.get("/api/tenants/{tid}/warnings")
    def warnings(lang: str = "en", g=Depends(tenant_guard), max_items: int = 100):
        """Early warnings on the tenant's latest data (AI Insights; not filtered by the selected period)."""
        user, tc = g
        _, _, _, st = visible(user, tc)
        if not panels_for(user, tc, st)["warnings"]:
            return {"asof": st.get("max_date"), "warnings": [], "counts": {"urgent": 0, "watch": 0, "new": 0},
                    "checked": 0, "available": False}
        res = warnings_result(user, tc, st)
        ws = present(res["warnings"], user, tc, norm_lang(lang), max_items)
        counts = {"urgent": sum(w["severity"] == "urgent" for w in ws), "watch": sum(w["severity"] == "watch" for w in ws),
                  "new": sum(bool(w.get("is_new")) for w in ws)}
        return {"asof": res["asof"], "computed_at": res["computed_at"], "warnings": ws, "counts": counts,
                "checked": len(user_rules(user, tc, st, "warnings")), "available": True,
                "errors": res["errors"] if user.is_platform_admin else []}

    def _findings_file(findings: list[dict], rule: str | None, fmt: str, lg: str, tc: TenantConfig, title: str,
                       about: list, name_parts: list[str], panel: str):
        if fmt not in ("csv", "xlsx"):
            raise HTTPException(400, "format must be csv or xlsx")
        if rule:
            f = next((x for x in findings if x["rule"] == rule), None)
            if f is None:
                raise HTTPException(404, "This finding is not in the current results")
            if f.get("items_hidden"):
                raise HTTPException(403, "Your role cannot download this list")
            cols = f.get("items_cols") or []
            rows = f.get("items") or []
            text = ev.render_text(ui(lg, f["msg"]), f["args"], tc.currency)
            title, about = f["title"], about + [("Finding", text)]
        else:
            first = ui(lg, "col_side") if panel == "evaluation" else ui(lg, "col_severity")
            cols = [{"key": "a", "label": first, "fmt": "text"}, {"key": "b", "label": ui(lg, "col_check"), "fmt": "text"},
                    {"key": "c", "label": ui(lg, "col_finding"), "fmt": "text"}]
            if panel == "warnings":
                cols.insert(1, {"key": "basis", "label": ui(lg, "col_basis"), "fmt": "text"})
            rows = []
            for f in findings:
                a = ui(lg, "eval_good" if f.get("side") == "positive" else "eval_bad") if panel == "evaluation" \
                    else ui(lg, "sev_" + (f.get("severity") or "watch"))
                rows.append({"a": a, "basis": ui(lg, "basis_" + (f.get("basis") or "trend")), "b": f["title"],
                             "c": ev.render_text(ui(lg, f["msg"]), f["args"], tc.currency)})
        name = safe_filename(tc.tenant_id, *name_parts, *( [rule] if rule else []))
        if fmt == "csv":
            return _file(to_csv(cols, rows), name, "csv")
        return _file(to_xlsx(title, cols, rows, about), name, "xlsx")

    @app.get("/api/tenants/{tid}/evaluation/export")
    def evaluation_export(format: str = "xlsx", rule: str | None = None, start: str | None = None,
                          end: str | None = None, grain: str = "auto", compare: str = "previous", lang: str = "en",
                          g=Depends(tenant_guard)):
        user, tc = g
        lg = norm_lang(lang)
        res = evaluation(start, end, grain, compare, lg, g, max_items=0)
        p = res["period"]
        svc.state.audit(user.username, tc.tenant_id, "export_evaluation", f"{format} {rule or 'all'} {p['start']}..{p['end']}")
        about = [("Business", tc.display_name), ("Period", f"{p['start']} - {p['end']}"), ("Compare", p["compare"])]
        return _findings_file(res["positive"] + res["negative"], rule, format, lg, tc, ui(lg, "eval_title"), about,
                              ["evaluation", p["start"], p["end"]], "evaluation")

    @app.get("/api/tenants/{tid}/warnings/export")
    def warnings_export(format: str = "xlsx", rule: str | None = None, lang: str = "en", g=Depends(tenant_guard)):
        user, tc = g
        lg = norm_lang(lang)
        res = warnings(lg, g, max_items=0)
        svc.state.audit(user.username, tc.tenant_id, "export_warnings", f"{format} {rule or 'all'} asof={res.get('asof')}")
        about = [("Business", tc.display_name), ("Data through", res.get("asof") or "-")]
        return _findings_file(res["warnings"], rule, format, lg, tc, ui(lg, "warn_title"), about,
                              ["warnings", res.get("asof") or ""], "warnings")

    @app.post("/api/tenants/{tid}/refresh")
    def refresh_now(g=Depends(tenant_guard)):
        user, t = g
        if not user.has_role("tenant_admin"):
            raise HTTPException(403, "Only administrators can force a refresh")
        svc.state.audit(user.username, t.tenant_id, "force_refresh")
        return svc.refresher.refresh_tenant(t.tenant_id, force=True)

    @app.get("/api/admin/status")
    def admin_status(user: User = Depends(current_user)):
        if not user.is_platform_admin:
            raise HTTPException(403, "Platform admin only")
        out = {}
        for tid in svc.tenants:
            st = svc.state.get(tid) or {}
            av = st.get("availability", {})
            out[tid] = {k: st.get(k) for k in ("status", "error", "min_date", "max_date", "data_version", "refreshed_at",
                                               "probe_ms")}
            out[tid]["kpis_available"] = sum(1 for k, a in av.items() if a.get("available") and ":" not in k)
        rules = {}
        for name in {t_.catalog for t_ in svc.tenants.values()}:
            cat = load_catalog(name)
            rules[name] = {"version": cat.rules_version, "rules": len(cat.rules), "errors": cat.rules_error}
        return {"tenants": out, "cache": svc.cache.stats(), "rules": rules, "audit": svc.state.recent_audit(50)}

    @app.get("/api/health")
    @app.head("/api/health")
    def health():
        return {"ok": True}
    

    # ------------------------------------------------------------ frontend
    @app.get("/")
    def index():
        return FileResponse(WEB / "index.html", headers={"Cache-Control": "no-cache"})

    # ------------------------------------------------------------ PWA
    # Version = app version + hash of the web files: any deployed change gives the service worker a new
    # cache name, so installed apps offer "Reload" and never keep running an old frontend.
    import hashlib
    from . import __version__
    _h = hashlib.sha256()
    for f in sorted(p for p in WEB.rglob("*") if p.is_file()):
        _h.update(f.relative_to(WEB).as_posix().encode())
        _h.update(f.read_bytes())
    pwa_version = f"{__version__}-{_h.hexdigest()[:10]}"
    sw_source = (WEB / "sw.js").read_text(encoding="utf-8").replace("__VERSION__", pwa_version)

    @app.get("/sw.js")
    def service_worker():
        return RawResponse(sw_source, media_type="application/javascript",
                           headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})

    @app.get("/manifest.webmanifest")
    def manifest():
        return FileResponse(WEB / "manifest.json", media_type="application/manifest+json",
                            headers={"Cache-Control": "no-cache"})

    @app.get("/favicon.ico")
    def favicon():
        return FileResponse(WEB / "icons" / "favicon-32.png", media_type="image/png")

    app.mount("/static", StaticFiles(directory=str(WEB)), name="static")

    @app.exception_handler(HTTPException)
    async def http_err(request: Request, exc: HTTPException):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)

    return app
