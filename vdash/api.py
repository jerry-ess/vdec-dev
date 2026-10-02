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
GET  /api/tenants/{tid}/reports/...   report blocks etc. (v26+, see vdash/reports_api.py)

Every tenant-scoped route goes through ``tenant_guard``: the session user must
be allowed on that tenant, otherwise 403 (and the attempt is audited).
"""
from __future__ import annotations

import logging
import time
from urllib.parse import quote
from contextlib import asynccontextmanager
from datetime import date

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, Response as RawResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .catalog import load_catalog
from .config import ROOT, TenantConfig
from .engine import presets
from .exports import build_export_sql, filter_options, included_joins, kpi_table, safe_filename, to_csv, to_xlsx
from .i18n import LANGS, LOCALES, bundle, norm_lang, t, translate_kpi_public, ui
from . import evaluation as ev
from .security import User
from .services import MAX_RANGE_DAYS, PANEL_SECTION, DashboardService, ServiceError, Services  # noqa: F401 (re-exported)

log = logging.getLogger("vdash.api")
COOKIE = "vdash_session"
WEB = ROOT / "web"


class LoginBody(BaseModel):
    username: str
    password: str


def create_app(start_refresher: bool | None = None) -> FastAPI:
    svc = Services()
    dash = DashboardService(svc)   # v22: business logic shared with reports / scheduler
    # v19: say which environment profile / local settings are in use, and warn about online-only pitfalls
    from .config import LOCAL_ENV_APPLIED, env_profile
    prof = env_profile()
    log.info("config profile '%s'%s", prof,
             f"; from config/local.env: {', '.join(sorted(LOCAL_ENV_APPLIED))}" if LOCAL_ENV_APPLIED else "")
    cs = svc.cache.stats()
    if "disk_entries" in cs:   # v20: >0 here on Render means the build step pre-computed the results
        log.info("result cache file %s: %d stored result(s)%s", cs["disk_path"], cs["disk_entries"],
                 "" if cs["disk_enabled"] else " (disabled)")
    if prof != "local" and not svc.settings.session_secret:
        log.warning("VDASH_SESSION_SECRET is not set: a new session key is created whenever runtime/ is empty "
                    "(every deploy/restart on Render), which signs everybody out. Set it in the service environment.")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        run_bg = svc.settings.refresh_enabled if start_refresher is None else start_refresher
        if run_bg:
            svc.refresher.start()  # first cycle runs immediately in the background
        yield
        svc.refresher.stop()
        svc.registry.close_all()
        svc.reports.close()

    app = FastAPI(title="Veridex BI Dashboard", version="1.0", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.svc = svc
    app.state.dash = dash

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        resp: Response = await call_next(request)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers.setdefault("X-Frame-Options", "DENY")   # v29: a report preview may set SAMEORIGIN
        resp.headers["Referrer-Policy"] = "same-origin"
        resp.headers.setdefault("Content-Security-Policy", (   # v29: a report page sets its own (nonce) policy
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline' "
            "https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; "
            "img-src 'self' data: blob:; connect-src 'self'; manifest-src 'self'; worker-src 'self'; "
            "frame-ancestors 'none'"))
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

    @app.get("/api/tenants/{tid}/context")
    def context(lang: str = "en", g=Depends(tenant_guard)):
        user, tc = g
        lg = norm_lang(lang)
        shown, hidden, role_hidden, st = dash.visible(user, tc)
        catalog = load_catalog(tc.catalog)
        panels = dash.panels_for(user, tc, st)
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
                   for e in dash.available_exports(user, tc, st)]
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

    @app.get("/api/tenants/{tid}/sections/{sid}")
    def section_data(sid: str, start: str | None = None, end: str | None = None, grain: str = "auto",
                     compare: str = "previous", lang: str = "en", g=Depends(tenant_guard)):
        user, tc = g
        shown, _, _, st = dash.visible(user, tc)
        kpis = [k for k in shown if sid in k.sections]
        has_panel = any(sec == sid and on for p, sec in PANEL_SECTION.items() for on in [dash.panels_for(user, tc, st)[p]])
        if not kpis and not has_panel:
            raise HTTPException(404, "Section not available for this tenant")
        req = dash.period(start, end, grain, compare, st)
        t0 = time.perf_counter()
        results = dash.run_many(user, tc, kpis, req, st)
        dash.log_slow(f"section {sid}", tc.tenant_id, req, time.perf_counter() - t0, results)
        lg = norm_lang(lang)
        by_id = {k.id: k for k in kpis}
        return {"period": {"start": req.start.isoformat(), "end": req.end.isoformat(), "grain": req.resolved_grain(),
                           "compare": req.compare},
                "results": {kid: dash.translate(by_id[kid], r, lg, user) for kid, r in results.items()}}

    @app.get("/api/tenants/{tid}/kpis/{kid}")
    def kpi_data(kid: str, start: str | None = None, end: str | None = None, grain: str = "auto",
                 compare: str = "previous", lang: str = "en", g=Depends(tenant_guard)):
        user, tc = g
        k, res, _ = dash.one_kpi(user, tc, kid, start, end, grain, compare)
        return dash.translate(k, res, norm_lang(lang), user)

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
        k, res, req = dash.one_kpi(user, tc, kid, start, end, grain, compare)
        if res.get("type") == "error":
            raise HTTPException(500, res["error"] if user.is_platform_admin else "This chart could not be calculated right now")
        cols, rows = kpi_table(k, dash.translate(k, res, lg), lg)
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
        shown, _, _, st = dash.visible(user, tc)
        k = next((k for k in shown if k.id == kid), None)
        if k is None or k.drill is None:
            raise HTTPException(404, "No detail rows for this KPI")
        try:
            clicked = _json.loads(f)
            if not isinstance(clicked, dict) or len(f) > 500:
                raise ValueError
        except ValueError:
            raise HTTPException(400, "f must be a small JSON object")
        req = dash.period(start, end, grain, "none", st)
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
        exp = next((e for e in dash.available_exports(user, tc, st) if e.id == eid), None)
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
        _, _, _, st = dash.visible(user, tc)
        if not dash.panels_for(user, tc, st)["evaluation"]:
            raise HTTPException(404, "No evaluation available for this business")
        req = dash.period(start, end, grain, compare, st)
        lg = norm_lang(lang)
        res = dash.evaluation_result(user, tc, st, req)
        return {"period": {"start": req.start.isoformat(), "end": req.end.isoformat(), "compare": req.compare},
                "checked": len(dash.user_rules(user, tc, st, "evaluation")),
                "positive": dash.present(res["positive"], user, tc, lg, max_items),
                "negative": dash.present(res["negative"], user, tc, lg, max_items),
                "errors": res["errors"] if user.is_platform_admin else []}

    @app.get("/api/tenants/{tid}/warnings")
    def warnings(lang: str = "en", g=Depends(tenant_guard), max_items: int = 100):
        """Early warnings on the tenant's latest data (AI Insights; not filtered by the selected period)."""
        user, tc = g
        _, _, _, st = dash.visible(user, tc)
        if not dash.panels_for(user, tc, st)["warnings"]:
            return {"asof": st.get("max_date"), "warnings": [], "counts": {"urgent": 0, "watch": 0, "new": 0},
                    "checked": 0, "available": False}
        res = dash.warnings_result(user, tc, st)
        ws = dash.present(res["warnings"], user, tc, norm_lang(lang), max_items)
        counts = {"urgent": sum(w["severity"] == "urgent" for w in ws), "watch": sum(w["severity"] == "watch" for w in ws),
                  "new": sum(bool(w.get("is_new")) for w in ws)}
        return {"asof": res["asof"], "computed_at": res["computed_at"], "warnings": ws, "counts": counts,
                "checked": len(dash.user_rules(user, tc, st, "warnings")), "available": True,
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
            text = ev.render_text(ui(lg, f["msg"]), f["args"], tc.currency, lg)
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
                             "c": ev.render_text(ui(lg, f["msg"]), f["args"], tc.currency, lg)})
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
        llm = svc.llm.status()
        try:
            from datetime import datetime, timezone
            llm["usage_this_month"] = svc.reports.llm_usage(month=datetime.now(timezone.utc).strftime("%Y-%m"))
        except Exception as exc:   # noqa: BLE001 - status must always answer
            llm["usage_this_month"] = f"unavailable: {exc}"
        return {"tenants": out, "cache": svc.cache.stats(), "rules": rules, "report_store": svc.reports.status(),
                "llm": llm, "audit": svc.state.recent_audit(50)}

    @app.post("/api/admin/llm/test")
    @app.get("/api/admin/llm/test")
    def admin_llm_test(user: User = Depends(current_user)):
        """v37: one tiny AI request to check provider, key and network (platform admins; about 100 tokens).
        v37.1: also GET, so it can be opened in the browser's address bar."""
        if not user.is_platform_admin:
            raise HTTPException(403, "Platform admin only")
        from datetime import datetime, timezone
        from .llm import LLMError
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        try:
            r = svc.llm.complete_json("You answer with JSON only.",
                                      'Reply with {"ok": true, "answer": <the sum of 17 and 25 as a number>}.',
                                      max_tokens=50, temperature=0)
        except LLMError as e:
            try:
                svc.reports.add_llm_usage("_platform", month, getattr(e, "tokens_in", 0), getattr(e, "tokens_out", 0),
                                          getattr(e, "cost_usd", 0.0), ok=False)
            except Exception:   # noqa: BLE001
                pass
            svc.state.audit(user.username, "-", "llm_test", f"failed {e.kind}")
            return {"ok": False, "kind": e.kind, "error": str(e), **svc.llm.status()}
        try:
            svc.reports.add_llm_usage("_platform", month, r.tokens_in, r.tokens_out, r.cost_usd, ok=True)
        except Exception:   # noqa: BLE001
            pass
        svc.state.audit(user.username, "-", "llm_test", f"ok {r.ms} ms")
        return {"ok": r.data.get("answer") == 42, "answer": r.data, **r.meta()}

    @app.get("/api/health")
    @app.head("/api/health")
    def health():
        return {"ok": True}
    

    # ------------------------------------------------------------ reports (v26+)
    from .reports_api import register as register_reports
    register_reports(app, dash, tenant_guard)

    # ------------------------------------------------------------ frontend

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

    # The page and the manifest are served with ?v=<pwa_version> on every icon URL, so a new icon set in
    # web/icons/ (which changes the hash above) is fetched fresh by browsers, desktop installs and PWAs.
    @app.get("/")
    def index():
        html = (WEB / "index.html").read_text(encoding="utf-8").replace("__ASSET_V__", pwa_version)
        # X-Veridex-App lets the service worker tell this page from a host's "starting up" page (v20)
        return RawResponse(html, media_type="text/html; charset=utf-8",
                           headers={"Cache-Control": "no-cache", "X-Veridex-App": pwa_version})

    @app.get("/manifest.webmanifest")
    def manifest():
        import json as _json
        man = _json.loads((WEB / "manifest.json").read_text(encoding="utf-8"))
        def _v(icons):
            for ic in icons or []:
                if ic.get("src", "").startswith("/static/icons/") and "?" not in ic["src"]:
                    ic["src"] += f"?v={pwa_version}"
        _v(man.get("icons"))
        for sc in man.get("shortcuts") or []:
            _v(sc.get("icons"))
        return RawResponse(_json.dumps(man, ensure_ascii=False), media_type="application/manifest+json",
                           headers={"Cache-Control": "no-cache"})

    @app.get("/favicon.ico")
    def favicon():
        return FileResponse(WEB / "icons" / "favicon-32.png", media_type="image/png")

    app.mount("/static", StaticFiles(directory=str(WEB)), name="static")

    @app.exception_handler(HTTPException)
    async def http_err(request: Request, exc: HTTPException):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code, headers=getattr(exc, "headers", None))

    @app.exception_handler(ServiceError)
    async def service_err(request: Request, exc: ServiceError):   # v22: refusals from vdash.services
        return JSONResponse({"error": exc.detail}, status_code=exc.status, headers=exc.headers)

    return app
