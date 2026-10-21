"""Scheduled refresh: the dashboard pulls from the warehouse on its own clock.

For every enabled tenant, each cycle:
1. reads a cheap *data version* (latest ``_loaded_at`` / ``_built_at``),
2. if it changed (or on first run / forced): re-probes which KPIs have data
   (the availability map that hides KPIs without data), stores the tenant's
   data date range, drops that tenant's cached results,
3. computes the early warnings for the new data (v9, stored with history),
4. pre-computes the default views (configured presets x sections) so the
   first viewer gets instant charts.

Runs as a background thread inside the web process (demo) or as a separate
worker: ``python -m vdash.refresher --once`` from cron / a k8s CronJob.
"""
from __future__ import annotations

import argparse
import logging
import threading
import time
from datetime import date

from .catalog import Catalog, Req, load_catalog
from .config import Settings, TenantConfig, load_tenants
from .datasources import DataSource, DataSourceRegistry
from .engine import KpiEngine, PeriodRequest, ResultCache, kpi_sig, presets
from .state_store import StateStore

log = logging.getLogger("vdash.refresher")

FRESHNESS_PROBES = [  # (table, timestamp column) - max() of these = the tenant's data version
    ("fact_sales_transaction_line", "_loaded_at"),
    ("fact_sales_order_header", "_loaded_at"),
    ("mart_product_performance_daily", "_built_at"),
    ("mart_customer_rfm_summary", "_built_at"),
    # v90: external data linked by Step 4.2 (weather, economy) changes which KPIs are available without a sales load
    ("fact_weather_daily", "_loaded_at"),
    ("fact_economic_indicator", "_loaded_at"),
]
DATE_RANGE_SQL = ("SELECT MIN(d.full_date) AS min_d, MAX(d.full_date) AS max_d FROM fact_sales_transaction_line f "
                  "JOIN dim_date d ON d.dim_date_sk = f.date_sk AND d.tenant_id = f.tenant_id "
                  "WHERE f.tenant_id = :tenant_id")


def probe_requirement(ds: DataSource, tenant_id: str, req: Req) -> tuple[bool, str]:
    cols = ds.columns()
    if req.table not in cols:
        return False, f"table {req.table} not in warehouse"
    if req.column not in cols[req.table]:
        return False, f"column {req.table}.{req.column} not in warehouse"
    cond = f"{req.column} IS NOT NULL" + (f" AND {req.column} <> 0" if req.nonzero else "") + \
        (f" AND ({req.where})" if req.where else "")
    sql = (f"SELECT COUNT(*) AS n FROM (SELECT DISTINCT {req.column} AS val FROM {req.table} "
           f"WHERE tenant_id = :tenant_id AND {cond} LIMIT {int(req.min_distinct)}) z")
    try:
        n = int(ds.query(sql, {"tenant_id": tenant_id})[0]["n"] or 0)
    except Exception as exc:
        return False, f"probe failed: {type(exc).__name__}"
    return (n >= req.min_distinct), ("" if n >= req.min_distinct else req.describe())


REQ_SIG_KEY = "meta:requirements"


def requirements_signature(catalog: Catalog) -> str:
    """v71: hash of every KPI / export / rule-data requirement. When an upgrade changes what a KPI needs (same id,
    new requirements) the stored probe is stale even though the data did not change, so it must be redone."""
    import hashlib
    parts = [f"{k.id}:{sorted(r.key() for r in k.requires)}:{sorted(r.key() for r in k.requires_any)}:{getattr(k, 'gate', '')}"
             for k in (*catalog.kpis, *catalog.ask_kpis)]
    parts += [f"export:{e.id}:{sorted(r.key() for r in e.requires)}" for e in catalog.exports]
    parts += [f"ruledata:{n}:{sorted(r.key() for r in rd.requires)}" for n, rd in sorted(catalog.rule_data.items())]
    return hashlib.sha1("|".join(sorted(parts)).encode()).hexdigest()[:16]


def probe_availability(ds: DataSource, tenant_id: str, catalog: Catalog) -> dict[str, dict]:
    ds.columns(refresh=True)
    memo: dict[str, tuple[bool, str]] = {}

    def check(r: Req) -> tuple[bool, str]:
        if r.key() not in memo:
            memo[r.key()] = probe_requirement(ds, tenant_id, r)
        return memo[r.key()]

    if getattr(ds, "parallel_probe", False):
        # v78: over the HTTPS gateway every probe is a network round trip, hundreds of them one after the other take
        # minutes - run the distinct probes side by side (the connector limits how many run at once).
        from concurrent.futures import ThreadPoolExecutor
        reqs: dict[str, Req] = {}
        for k in (*catalog.kpis, *catalog.ask_kpis):
            for r in (*k.requires, *k.requires_any):
                reqs.setdefault(r.key(), r)
        for e in catalog.exports:
            for r in e.requires:
                reqs.setdefault(r.key(), r)
        for rd in catalog.rule_data.values():
            for r in rd.requires:
                reqs.setdefault(r.key(), r)
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(check, reqs.values()))

    out: dict[str, dict] = {}
    verdicts = None   # v91: external-factor verdicts, computed once and only when a KPI is gated by one
    for k in (*catalog.kpis, *catalog.ask_kpis):
        reasons = [msg for ok, msg in map(check, k.requires) if not ok]
        if k.requires_any:
            any_res = [check(r) for r in k.requires_any]
            if not any(ok for ok, _ in any_res):
                reasons.append("none of: " + "; ".join(msg for _, msg in any_res))
        if getattr(k, "gate", "") and not reasons:
            if verdicts is None:
                from . import drivers
                verdicts = drivers.compute(lambda sql: ds.query(sql, {"tenant_id": tenant_id}))
            ok, msg = drivers.is_clear(verdicts, k.gate)
            if not ok:
                reasons.append(msg)
        out[k.id] = {"available": not reasons, "reasons": reasons}
    for e in catalog.exports:  # full-list downloads use the same probe, keyed "export:<id>"
        reasons = [msg for ok, msg in map(check, e.requires) if not ok]
        out[f"export:{e.id}"] = {"available": not reasons, "reasons": reasons}
    for name, rd in catalog.rule_data.items():  # data sets the evaluation/warning checks read (v12), "ruledata:<name>"
        reasons = [msg for ok, msg in map(check, rd.requires) if not ok]
        out[f"ruledata:{name}"] = {"available": not reasons, "reasons": reasons}
    return out


def warnings_version(data_version: str, catalog: Catalog) -> str:
    """Warnings are stored per data version AND rule-file version: editing the rule file recomputes them."""
    return f"{data_version}#rules:{catalog.rules_version}"


def data_version(ds: DataSource, tenant_id: str) -> str:
    cols = ds.columns()
    parts = []
    for table, col in FRESHNESS_PROBES:
        if col in cols.get(table, set()):
            try:
                v = ds.query(f"SELECT MAX({col}) AS m FROM {table} WHERE tenant_id = :tenant_id",
                             {"tenant_id": tenant_id})[0]["m"]
            except Exception:
                v = None
            parts.append(str(v))
    # v97: the signal registry and this tenant's saved Step 5 verdicts change what the Market pages show without any
    # sales load - a new signal or a new verdict starts a refresh and clears the cached results
    for label, sql, params in (
            ("reg", "SELECT COUNT(*) AS n, MAX(created_at) AS m FROM ext_signal_map WHERE enabled", {}),
            ("xst", "SELECT COUNT(*) AS n, MAX(evaluated_at) AS m FROM tenant_external_factor_status WHERE tenant_id = :tenant_id",
             {"tenant_id": tenant_id})):
        try:
            r = ds.query(sql, params)[0]
            parts.append(f"{label}:{r['n']}:{str(r['m'])[:19]}")
        except Exception:
            pass       # registry not installed (older warehouse): version unchanged from before
    return "|".join(parts)


class Refresher:
    def __init__(self, settings: Settings, registry: DataSourceRegistry, state: StateStore, engine: KpiEngine,
                 cache: ResultCache):
        self.settings, self.registry, self.state, self.engine, self.cache = settings, registry, state, engine, cache
        self.tenants: dict[str, TenantConfig] = load_tenants()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._run_lock = threading.Lock()
        self._warn_lock = threading.Lock()
        self._kick_lock = threading.Lock()
        from .rules_config import validate_tenant_alerts   # v12: report bad per-tenant rule overrides at start-up
        for t in self.tenants.values():
            for err in validate_tenant_alerts(load_catalog(t.catalog), t.alerts, f"tenants.json {t.tenant_id}"):
                log.warning("%s (ignored)", err)

    def refresh_tenant(self, tenant_id: str, force: bool = False) -> dict:
        t = self.tenants[tenant_id]
        t0 = time.perf_counter()
        try:
            ds = self.registry.get(t.datasource)
            ds.columns(refresh=True)
            version = data_version(ds, tenant_id)
            prev = self.state.get(tenant_id)
            catalog = load_catalog(t.catalog)
            expected = ({k.id for k in (*catalog.kpis, *catalog.ask_kpis)} | {f"export:{e.id}" for e in catalog.exports}
                        | {f"ruledata:{n}" for n in catalog.rule_data})
            # unchanged data AND the stored probe covers the current catalog (a dashboard upgrade adds KPIs) -> skip
            if (prev and not force and prev.get("data_version") == version and prev.get("status") == "ok"
                    and expected <= set(prev.get("availability", {}))
                    and prev.get("availability", {}).get(REQ_SIG_KEY, {}).get("sig") == requirements_signature(catalog)):
                run = self.state.alert_run(tenant_id)
                if prev.get("max_date") and (run is None or run.get("data_version") != warnings_version(version, catalog)):
                    self.compute_warnings(t, ds, catalog, version, prev["availability"], prev["max_date"])
                # v16: the result cache lives in memory — after a server restart or when entries expire it is empty
                # although the data did not change, so the common periods are warmed again (only what is missing)
                if prev.get("max_date") and prev.get("min_date"):
                    self.prewarm(t, ds, catalog, version, prev["availability"], date.fromisoformat(prev["min_date"]),
                                 date.fromisoformat(prev["max_date"]))
                return {"tenant_id": tenant_id, "changed": False}
            avail = probe_availability(ds, tenant_id, catalog)
            avail[REQ_SIG_KEY] = {"available": False, "reasons": [], "sig": requirements_signature(catalog)}
            rng = ds.query(DATE_RANGE_SQL, {"tenant_id": tenant_id})
            min_d = str(rng[0]["min_d"])[:10] if rng and rng[0]["min_d"] else None
            max_d = str(rng[0]["max_d"])[:10] if rng and rng[0]["max_d"] else None
            self.state.put(tenant_id, data_version=version, min_date=min_d, max_date=max_d,
                           last_loaded_at=max([v for v in version.split("|") if v not in ("None", "") and not v.startswith(("reg:", "xst:"))], default=None), availability=avail,
                           status="ok" if max_d else "no_data", error=None,
                           probe_ms=round((time.perf_counter() - t0) * 1000, 1))
            self.cache.invalidate_tenant(tenant_id)
            n_avail = sum(1 for k, a in avail.items() if a["available"] and ":" not in k)
            log.info("tenant %s refreshed: %d/%d KPIs available, data %s..%s", tenant_id, n_avail, len(catalog.kpis), min_d, max_d)
            if max_d:
                self.compute_warnings(t, ds, catalog, version, avail, max_d, force=True)
                self.prewarm(t, ds, catalog, version, avail, date.fromisoformat(min_d), date.fromisoformat(max_d))
            return {"tenant_id": tenant_id, "changed": True, "available": n_avail, "total": len(catalog.kpis)}
        except Exception as exc:
            log.exception("refresh failed for %s", tenant_id)
            self.state.put(tenant_id, status="error", error=f"{type(exc).__name__}: {exc}"[:500])
            return {"tenant_id": tenant_id, "error": str(exc)}

    def compute_warnings(self, t: TenantConfig, ds: DataSource, catalog: Catalog, version: str, avail: dict,
                         max_d: str, force: bool = False) -> dict:
        """Early warnings (v9): once per data version, as of the tenant's last data date. Stored with first-seen
        history so the page marks a warning "new" only in the load where it appeared."""
        from .evaluation import evaluate_warnings
        wv = warnings_version(version, catalog)
        with self._warn_lock:
            run = self.state.alert_run(t.tenant_id)
            if run and run.get("data_version") == wv and not force:
                return run
            res = evaluate_warnings(catalog, ds, t.tenant_id, self.engine, version, avail, t.alerts,
                                    date.fromisoformat(max_d), t.timezone, live=False)
            self.state.save_warnings(t.tenant_id, wv, max_d, res["warnings"], res["checked"], res["errors"])
            if res["errors"]:
                log.warning("tenant %s: %d warning rule(s) failed: %s", t.tenant_id, len(res["errors"]), res["errors"])
            log.info("tenant %s: %d early warning(s) from %d rule(s)", t.tenant_id, len(res["warnings"]), res["checked"])
            return self.state.alert_run(t.tenant_id) or {}

    def prewarm(self, t: TenantConfig, ds: DataSource, catalog: Catalog, version: str, avail: dict,
                min_d: date, max_d: date) -> None:
        """Compute the common periods ahead of time so switching to them is instant. v16: every section by default
        ("all"), the period evaluation too, and only entries the cache does not hold (cheap when already warm)."""
        from .evaluation import eval_cache_key, evaluate_period
        cfg = self.settings.raw.get("refresh", {}).get("prewarm_sections", "all")
        sections = None if cfg in ("all", ["all"]) else set(cfg)
        ranges = {p["id"]: p for p in presets(max_d, min_d)}
        t0, n = time.perf_counter(), 0
        for pid in self.settings.prewarm_presets:
            if self._stop.is_set():
                return
            p = ranges.get(pid)
            if not p:
                continue
            req = PeriodRequest(date.fromisoformat(p["start"]), date.fromisoformat(p["end"]), "auto", "previous")
            for k in catalog.kpis:
                if avail.get(k.id, {}).get("available") and (sections is None or sections.intersection(k.sections)):
                    if self.cache.get(f"{t.tenant_id}|{version}|{k.id}.{kpi_sig(k)}|{req.key()}") is None:
                        self.engine.wait_idle(stop=self._stop)   # v56: pre-warm yields to viewers
                        if self._stop.is_set():
                            return
                        self.engine.run(ds, t.tenant_id, k, req, version)
                        n += 1
            key = eval_cache_key(t.tenant_id, version, req, catalog, t.alerts)
            if self.cache.get(key) is None:
                res = evaluate_period(catalog, ds, t.tenant_id, self.engine, version, avail, t.alerts, req)
                if not res["errors"]:
                    self.cache.put(key, res)
        if n:
            log.info("tenant %s: prewarmed %d KPI result(s) for %s in %.1f s", t.tenant_id, n,
                     ", ".join(self.settings.prewarm_presets), time.perf_counter() - t0)

    def kick(self) -> bool:
        """v20: start a refresh in the background (used when a request finds no stored state, instead of making
        that request wait for the whole refresh). No-op while a refresh is already running or queued."""
        if self._run_lock.locked() or not self._kick_lock.acquire(blocking=False):
            return False

        def run() -> None:
            try:
                self.refresh_all()
            finally:
                self._kick_lock.release()
        threading.Thread(target=run, name="vdash-refresh-kick", daemon=True).start()
        return True

    def refresh_all(self, force: bool = False) -> list[dict]:
        with self._run_lock:
            return [self.refresh_tenant(tid, force) for tid, t in self.tenants.items() if t.enabled]

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.refresh_all()
            self._stop.wait(self.settings.refresh_interval_minutes * 60)

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="vdash-refresher", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()


def main() -> None:  # pragma: no cover - CLI
    ap = argparse.ArgumentParser(description="Refresh tenant availability + pre-warm dashboard caches")
    ap.add_argument("--once", action="store_true", help="run one cycle and exit (for cron / CronJob)")
    ap.add_argument("--force", action="store_true", help="re-probe even when the data version is unchanged")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    s = Settings.load()
    cache = ResultCache(s.cache_max_entries, s.cache_ttl_minutes * 60, s.cache_persist_path)
    r = Refresher(s, DataSourceRegistry(s.query_timeout_seconds), StateStore(s.state_store_path), KpiEngine(cache), cache)
    if args.once:
        for res in r.refresh_all(force=args.force):
            print(res)
    else:
        r._loop()


if __name__ == "__main__":  # pragma: no cover
    main()
