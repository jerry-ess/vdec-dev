"""HTTP routes for reports (v26+). Registered by ``api.create_app``; every route goes through ``tenant_guard``.

GET /api/tenants/{tid}/reports/blocks?start&end&compare&lang
    All report blocks (every KPI + summary blocks) by layer and chapter, each with its state for this user,
    business and period: available / locked / no_data / historical / needs_comparison, a plain-language reason,
    and whether it is ticked by default.

POST /api/tenants/{tid}/reports/compose   {"blocks": [...], "start", "end", "compare", "lang"}
    The complete report document (JSON, data only): every number from the dashboard's own results for this user,
    every sentence from translated templates. Blocks that cannot be used now are listed under "skipped".

POST /api/tenants/{tid}/reports/render?download=0|1   (same body)   - or GET ...?spec=<the body as JSON>
    The report as an HTML page: download=1 -> one self-contained file (charts, styles, data inside; opens
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
from .i18n import LOCALES, bundle, norm_lang
from .reports.blocks import BlockResolver, load_spec, validate
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
        headers = {
            "Content-Security-Policy": (f"default-src 'none'; script-src 'self' 'nonce-{nonce}'; style-src 'unsafe-inline'; "
                                        "img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'self'"),
            "X-Frame-Options": "SAMEORIGIN",
        }
        if download:
            name = f"report_{tc.tenant_id}_{p['start']}_{p['end']}_{lg}.html"
            headers["Content-Disposition"] = f"attachment; filename=\"{name}\"; filename*=UTF-8''{quote(name)}"
        return Response(page, media_type="text/html; charset=utf-8", headers=headers)
