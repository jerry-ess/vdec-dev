"""HTTP routes for reports (v26+). Registered by ``api.create_app``; every route goes through ``tenant_guard``.

GET /api/tenants/{tid}/reports/blocks?start&end&compare&lang
    All report blocks (every KPI + summary blocks) by layer and chapter, each with its state for this user,
    business and period: available / locked / no_data / historical / needs_comparison, a plain-language reason,
    and whether it is ticked by default.

POST /api/tenants/{tid}/reports/compose   {"blocks": [...], "start", "end", "compare", "lang"}
    The complete report document (JSON, data only): every number from the dashboard's own results for this user,
    every sentence from translated templates. Blocks that cannot be used now are listed under "skipped".

POST /api/tenants/{tid}/reports/create   (same body)   v34
    Creates a report: composed once, stored exactly as made (an automatic name, "#7 · Executive summary · period"),
    counted against the business's monthly plan limit (config/report_plans.json). The same report (parts, period,
    comparison, language) made again from the same data opens the stored copy instead - not counted again.
    429 when the monthly limit is reached. Each person keeps their newest ``keep_per_user`` reports (oldest removed).
GET /api/tenants/{tid}/reports/mine                 the person's own created reports + the month's usage
GET /api/tenants/{tid}/reports/mine/{id}?download=0|1   one of them as an HTML page (the stored copy, unchanged)

POST /api/tenants/{tid}/reports/render?download=0|1   (same body)   - or GET ...?spec=<the body as JSON>
    Platform admins only (support, tests): builds the report live, not stored or counted. The report as an HTML page: download=1 -> one self-contained file (charts, styles, data inside; opens
    offline, prints to PDF); download=0 -> in-app preview (scripts from /static). Strict nonce-based policy.
"""
from __future__ import annotations

import logging
import secrets
from urllib.parse import quote

from fastapi import Depends, FastAPI
from fastapi.responses import Response
from pydantic import BaseModel, Field

from .catalog import load_catalog
from .i18n import LOCALES, bundle, norm_lang, ui
from .reports.blocks import BlockResolver, load_profiles, load_spec, validate
from .report_store import ReportStoreError
from .reports.composer import compose
from .reports.html import render_html
from .services import DashboardService, ServiceError

log = logging.getLogger("vdash.reports")


class ReportRequest(BaseModel):
    blocks: list[str] = Field(default_factory=list, max_length=500)
    start: str | None = None
    end: str | None = None
    compare: str = "previous"
    lang: str = "en"
    # kept with a created report (names it; not used to build it)
    profile: str | None = Field(default=None, max_length=40)
    period_preset: str | None = Field(default=None, max_length=40)


def _nonce_of(page: str) -> str:
    import re
    m = re.search(r'nonce="([^"]+)"', page)
    return m.group(1) if m else secrets.token_urlsafe(16)


def spec_hash(doc: dict) -> str:
    """Identity of a report: parts, period, comparison, language (data version is compared separately)."""
    import hashlib
    import json as _json
    m = doc["meta"]
    key = {"blocks": m["blocks_requested"], "start": m["period"]["start"], "end": m["period"]["end"],
           "compare": m["period"]["compare"], "lang": m["lang"]}
    return hashlib.sha256(_json.dumps(key, sort_keys=True).encode()).hexdigest()


def register(app: FastAPI, dash: DashboardService, tenant_guard) -> None:
    checked: dict[tuple[str, float], list[str]] = {}

    def spec_for(tc):
        spec = load_spec()
        key = (tc.catalog, spec.raw.get("version", 0), id(spec))
        if key not in checked:
            checked[key] = validate(spec, load_catalog(tc.catalog))
            for e in checked[key]:
                log.error("config/report_blocks.json: %s", e)
        return spec, checked[key]

    @app.get("/api/tenants/{tid}/reports/blocks")
    def report_blocks(start: str | None = None, end: str | None = None, compare: str = "previous", lang: str = "en",
                      g=Depends(tenant_guard)):
        user, tc = g
        _, _, _, st = dash.visible(user, tc)          # 503 "preparing" until the tenant's data is checked
        req = dash.period(start, end, "auto", compare, st)
        spec, errors = spec_for(tc)
        if errors:
            raise ServiceError(500, "; ".join(errors) if user.is_platform_admin
                               else "Report settings are being updated - please try again later")
        return BlockResolver(dash, user, tc, st, spec).resolve(req.start, req.end, req.compare, norm_lang(lang))

    @app.post("/api/tenants/{tid}/reports/compose")
    def report_compose(body: ReportRequest, g=Depends(tenant_guard)):
        user, tc = g
        _, errors = spec_for(tc)
        if errors:
            raise ServiceError(500, "; ".join(errors) if user.is_platform_admin
                               else "Report settings are being updated - please try again later")
        doc = compose(dash, user, tc, body.model_dump())
        p = doc["meta"]["period"]
        dash.svc.state.audit(user.username, tc.tenant_id, "report_compose",
                             f"{p['start']}..{p['end']} {len(doc['meta']['blocks_requested'])} blocks lang={doc['meta']['lang']}")
        return doc

    @app.get("/api/tenants/{tid}/reports/render")
    def report_render_get(spec: str, download: bool = False, g=Depends(tenant_guard)):
        """Same as POST, with the request as JSON in ?spec= - so the builder can show it in a frame or link it."""
        import json as _json
        if len(spec) > 20_000:
            raise ServiceError(400, "Report request too long")
        try:
            body = ReportRequest.model_validate(_json.loads(spec))
        except Exception:   # noqa: BLE001 - any malformed input is the same 400
            raise ServiceError(400, "Invalid report request")
        return report_render(body, download, g)

    @app.post("/api/tenants/{tid}/reports/render")
    def report_render(body: ReportRequest, download: bool = False, g=Depends(tenant_guard)):
        user, tc = g
        if not user.is_platform_admin:   # v34: users create reports (counted, kept) - see /reports/create
            raise ServiceError(403, "Create the report from the Reports page")
        _, errors = spec_for(tc)
        if errors:
            raise ServiceError(500, "; ".join(errors) if user.is_platform_admin
                               else "Report settings are being updated - please try again later")
        doc = compose(dash, user, tc, body.model_dump())
        lg = doc["meta"]["lang"]
        texts = {**bundle("en")["ui"], **bundle(lg)["ui"]}
        nonce = secrets.token_urlsafe(16)
        page = render_html(doc, texts, LOCALES[lg], standalone=download, nonce=nonce)
        p = doc["meta"]["period"]
        dash.svc.state.audit(user.username, tc.tenant_id, "report_download" if download else "report_preview",
                             f"{p['start']}..{p['end']} {len(doc['meta']['blocks_requested'])} blocks lang={lg}")
        return html_response(page, download, f"report_{tc.tenant_id}_{p['start']}_{p['end']}_{lg}.html")

    def html_response(page: str, download: bool, name: str, nonce: str | None = None) -> Response:
        nonce = nonce or _nonce_of(page)
        headers = {
            "Content-Security-Policy": (f"default-src 'none'; script-src 'self' 'nonce-{nonce}'; style-src 'unsafe-inline'; "
                                        "img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'self'"),
            "X-Frame-Options": "SAMEORIGIN",
        }
        if download:
            headers["Content-Disposition"] = f"attachment; filename=\"{name}\"; filename*=UTF-8''{quote(name)}"
        return Response(page, media_type="text/html; charset=utf-8", headers=headers)

    # ------------------------------------------------------------ v34: created reports, kept and counted
    from .reports.plans import month_key, plan_for, resets_on

    store = lambda: dash.svc.reports   # noqa: E731 - replaced in tests
    STORE_DOWN = "Reports cannot be created or opened right now - please try again in a few minutes"

    def db(call):
        """Run a report-store call; an unreachable database is a 503 with a plain message, never a crash."""
        try:
            return call()
        except ReportStoreError as exc:
            log.warning("report store: %s", exc)
            raise ServiceError(503, STORE_DOWN)

    def usage_out(user, tc) -> dict:
        plan, month = plan_for(tc), month_key(tc)
        used = db(lambda: store().usage(tc.tenant_id, month))
        lim = plan["reports_per_month"]
        return {"plan": plan["title"], "used": used, "limit": lim,
                "remaining": None if lim is None else max(lim - used, 0), "resets_on": resets_on(tc),
                "keep": plan["keep_per_user"], "counted": not user.is_platform_admin}

    def report_out(r: dict, st: dict) -> dict:
        return {"id": r["id"], "seq": r["seq"], "name": r["name"], "created_at": r["created_at"],
                "period_start": r["period_start"], "period_end": r["period_end"], "language": r["language"],
                "parts": len(r.get("blocks") or []), "data_changed": r.get("data_version") != st.get("data_version")}

    def report_name(doc: dict, profile: str | None):
        lg = doc["meta"]["lang"]
        profiles = {p["id"]: p for p in load_profiles().get("profiles", [])}
        title = ui(lg, profiles[profile]["title_key"]) if profile in profiles else ui(lg, "rb_custom")
        return lambda seq: f"#{seq} · {title} · {doc['cover']['period_text']}"

    @app.post("/api/tenants/{tid}/reports/create")
    def report_create(body: ReportRequest, g=Depends(tenant_guard)):
        user, tc = g
        _, _, _, st = dash.visible(user, tc)
        _, errors = spec_for(tc)
        if errors:
            raise ServiceError(500, "; ".join(errors) if user.is_platform_admin
                               else "Report settings are being updated - please try again later")
        if not body.blocks:
            raise ServiceError(400, "Choose at least one part of the report")
        doc = compose(dash, user, tc, body.model_dump())
        included = [b["id"] for ly in doc["layers"] for c in ly["chapters"] for b in c["blocks"]]
        if not included:                                   # nothing to show: not a report, not counted
            raise ServiceError(400, "None of the chosen parts have data for this period")
        h, m = spec_hash(doc), doc["meta"]
        same = db(lambda: store().find_report(tc.tenant_id, user.username, h, m["data_version"]))
        if same:                                           # identical report from the same data: open the copy
            return {"report": report_out(same, st), "reused": True, "usage": usage_out(user, tc)}
        plan, month = plan_for(tc), month_key(tc)
        counted = not user.is_platform_admin               # Veridex staff (support) never use a customer's reports
        if counted and not db(lambda: store().reserve_report(tc.tenant_id, month, plan["reports_per_month"])):
            u = usage_out(user, tc)
            raise ServiceError(429, f"Monthly report limit reached ({u['used']} of {u['limit']}, plan {u['plan']}). "
                                    f"New reports can be created again from {u['resets_on']}.")
        try:
            row, removed = store().save_report(
                tc.tenant_id, user.username, doc, h, report_name(doc, body.profile), plan["keep_per_user"],
                period_start=m["period"]["start"], period_end=m["period"]["end"], language=m["lang"],
                data_version=m["data_version"], blocks=included, findings=doc.get("finding_ids", []),
                spec={"blocks": m["blocks_requested"], "start": m["period"]["start"], "end": m["period"]["end"],
                      "compare": m["period"]["compare"], "lang": m["lang"], "profile": body.profile,
                      "period_preset": body.period_preset})
        except ReportStoreError as exc:
            if counted:
                try:
                    store().release_report(tc.tenant_id, month)
                except ReportStoreError:
                    log.error("report store: could not give back a reserved report for %s %s", tc.tenant_id, month)
            log.warning("report store: %s", exc)
            raise ServiceError(503, STORE_DOWN)
        dash.svc.state.audit(user.username, tc.tenant_id, "report_create",
                             f"{row['id']} {m['period']['start']}..{m['period']['end']} {len(included)} parts "
                             f"lang={m['lang']} counted={counted} removed_old={removed}")
        return {"report": report_out(row, st), "reused": False, "usage": usage_out(user, tc)}

    @app.get("/api/tenants/{tid}/reports/mine")
    def reports_mine(g=Depends(tenant_guard)):
        user, tc = g
        _, _, _, st = dash.visible(user, tc)
        rows = db(lambda: store().list_reports(tc.tenant_id, user.username, limit=plan_for(tc)["keep_per_user"]))
        return {"reports": [report_out(r, st) for r in rows], "usage": usage_out(user, tc)}

    @app.get("/api/tenants/{tid}/reports/mine/{rid}")
    def report_open(rid: str, download: bool = False, g=Depends(tenant_guard)):
        user, tc = g
        _, _, _, st = dash.visible(user, tc)
        r = db(lambda: store().get_report(tc.tenant_id, user.username, rid))
        if r is None:
            raise ServiceError(404, "Report not found")
        doc = r["doc"]
        lg = doc["meta"]["lang"]
        if doc["meta"].get("data_version") != st.get("data_version"):
            doc["cover"]["data_note"] = ui(lg, "rpt_copy_note")
        texts = {**bundle("en")["ui"], **bundle(lg)["ui"]}
        nonce = secrets.token_urlsafe(16)
        page = render_html(doc, texts, LOCALES[lg], standalone=download, nonce=nonce)
        dash.svc.state.audit(user.username, tc.tenant_id, "report_open", f"{rid} download={int(download)}")
        p = doc["meta"]["period"]
        return html_response(page, download, f"report_{tc.tenant_id}_{p['start']}_{p['end']}_{lg}_{r['seq']}.html", nonce)
