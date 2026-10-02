"""HTTP routes for reports (v26+). Registered by ``api.create_app``; every route goes through ``tenant_guard``.

GET /api/tenants/{tid}/reports/blocks?start&end&compare&lang
    All report blocks (every KPI + summary blocks) by layer and chapter, each with its state for this user,
    business and period: available / locked / no_data / historical / needs_comparison, a plain-language reason,
    and whether it is ticked by default.

POST /api/tenants/{tid}/reports/compose   {"blocks": [...], "start", "end", "compare", "lang"}
    The complete report document (JSON, data only): every number from the dashboard's own results for this user,
    every sentence from translated templates. Blocks that cannot be used now are listed under "skipped".

GET/POST/PUT/DELETE /api/tenants/{tid}/reports/configs[/{cid}]   saved report settings (v32, shared by the business;
    changed or deleted only by their creator, a business admin or a platform admin)
GET /api/tenants/{tid}/reports/history          reports made (v32): own ones; managers and above see the business's

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
from .reports.blocks import BlockResolver, all_block_ids, load_profiles, load_spec, validate
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
    # v32: context kept in the history (not used to build the report)
    profile: str | None = Field(default=None, max_length=40)
    period_preset: str | None = Field(default=None, max_length=40)
    config_id: str | None = Field(default=None, max_length=36)
    as_of_version: str | None = Field(default=None, max_length=128)   # re-opened from history: note if data changed
    record: bool = True                                               # False: do not add a history entry


class ConfigBody(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    profile: str = Field(default="custom", max_length=40)
    period_preset: str = Field(default="last_30_days", max_length=40)
    period_start: str | None = None
    period_end: str | None = None
    compare: str = "previous"
    blocks: list[str] = Field(default_factory=list, max_length=500)


MAX_CONFIGS_PER_TENANT = 100
HISTORY_DEDUP_SECONDS = 15 * 60


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
        if body.record:
            record_history(user, tc, body, doc)
        headers = {
            "Content-Security-Policy": (f"default-src 'none'; script-src 'self' 'nonce-{nonce}'; style-src 'unsafe-inline'; "
                                        "img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'self'"),
            "X-Frame-Options": "SAMEORIGIN",
        }
        if download:
            name = f"report_{tc.tenant_id}_{p['start']}_{p['end']}_{lg}.html"
            headers["Content-Disposition"] = f"attachment; filename=\"{name}\"; filename*=UTF-8''{quote(name)}"
        return Response(page, media_type="text/html; charset=utf-8", headers=headers)

    # ------------------------------------------------------------ v32: saved report settings + history
    store = lambda: dash.svc.reports   # noqa: E731 - replaced in tests

    def db(call):
        """Run a report-store call; an unreachable database is a 503 with a plain message, never a crash."""
        try:
            return call()
        except ReportStoreError as exc:
            log.warning("report store: %s", exc)
            raise ServiceError(503, "Saved reports are not available right now - please try again later")

    def may_change(user, row: dict) -> bool:
        return row["created_by"] == user.username or user.has_role("tenant_admin")

    def check_config(tc, st: dict, body: ConfigBody) -> dict:
        from datetime import date as _date
        catalog = load_catalog(tc.catalog)
        unknown = [b for b in body.blocks if b not in all_block_ids(catalog)]
        if unknown:
            raise ServiceError(400, f"Unknown report block(s): {', '.join(unknown[:5])}")
        profiles = {p["id"] for p in load_profiles().get("profiles", [])}
        if body.profile != "custom" and body.profile not in profiles:
            raise ServiceError(400, "Unknown report profile")
        if body.profile == "custom" and not body.blocks:
            raise ServiceError(400, "Choose at least one part of the report")
        if body.compare not in ("previous", "yoy", "none"):
            raise ServiceError(400, "compare must be previous, yoy or none")
        if body.period_preset == "custom":
            try:
                s_, e_ = _date.fromisoformat(body.period_start or ""), _date.fromisoformat(body.period_end or "")
            except ValueError:
                raise ServiceError(400, "A custom period needs start and end dates (YYYY-MM-DD)")
            if s_ > e_:
                raise ServiceError(400, "Start date is after end date")
        else:
            from .engine import presets
            if st.get("max_date") and st.get("min_date"):
                ids = {x["id"] for x in presets(_date.fromisoformat(st["max_date"]), _date.fromisoformat(st["min_date"]))}
                if body.period_preset not in ids:
                    raise ServiceError(400, "Unknown period")
        d = body.model_dump()
        d["name"] = d["name"].strip()
        if not d["name"]:
            raise ServiceError(400, "Give the report a name")
        if d["period_preset"] != "custom":
            d["period_start"] = d["period_end"] = None
        return d

    def config_out(user, row: dict) -> dict:
        u = dash.svc.users.get(row["created_by"])
        return {k: row.get(k) for k in ("id", "name", "profile", "period_preset", "period_start", "period_end", "compare",
                                        "blocks", "created_at", "updated_at")} | {
            "created_by": u.display_name if u else row["created_by"], "can_change": may_change(user, row)}

    @app.get("/api/tenants/{tid}/reports/configs")
    def configs_list(g=Depends(tenant_guard)):
        user, tc = g
        rows = db(lambda: store().list_configs(tc.tenant_id))
        return {"configs": [config_out(user, r) for r in rows]}

    @app.post("/api/tenants/{tid}/reports/configs")
    def configs_create(body: ConfigBody, g=Depends(tenant_guard)):
        user, tc = g
        _, _, _, st = dash.visible(user, tc)
        d = check_config(tc, st, body)
        rows = db(lambda: store().list_configs(tc.tenant_id))
        if len(rows) >= MAX_CONFIGS_PER_TENANT:
            raise ServiceError(400, f"At most {MAX_CONFIGS_PER_TENANT} saved reports per business - delete one first")
        if any(r["name"].casefold() == d["name"].casefold() for r in rows):
            raise ServiceError(409, "A saved report with this name already exists")
        row = db(lambda: store().create_config(tc.tenant_id, user.username, **d))
        dash.svc.state.audit(user.username, tc.tenant_id, "report_config_create", f"{row['id']} {d['name']}")
        return config_out(user, row)

    @app.put("/api/tenants/{tid}/reports/configs/{cid}")
    def configs_update(cid: str, body: ConfigBody, g=Depends(tenant_guard)):
        user, tc = g
        _, _, _, st = dash.visible(user, tc)
        row = db(lambda: store().get_config(tc.tenant_id, cid))
        if row is None:
            raise ServiceError(404, "Saved report not found")
        if not may_change(user, row):
            raise ServiceError(403, "Only the person who saved this report or an administrator can change it")
        d = check_config(tc, st, body)
        rows = db(lambda: store().list_configs(tc.tenant_id))
        if any(r["id"] != cid and r["name"].casefold() == d["name"].casefold() for r in rows):
            raise ServiceError(409, "A saved report with this name already exists")
        row = db(lambda: store().update_config(tc.tenant_id, cid, **d))
        dash.svc.state.audit(user.username, tc.tenant_id, "report_config_update", f"{cid} {d['name']}")
        return config_out(user, row)

    @app.delete("/api/tenants/{tid}/reports/configs/{cid}")
    def configs_delete(cid: str, g=Depends(tenant_guard)):
        user, tc = g
        row = db(lambda: store().get_config(tc.tenant_id, cid))
        if row is None:
            raise ServiceError(404, "Saved report not found")
        if not may_change(user, row):
            raise ServiceError(403, "Only the person who saved this report or an administrator can delete it")
        db(lambda: store().delete_config(tc.tenant_id, cid))
        dash.svc.state.audit(user.username, tc.tenant_id, "report_config_delete", f"{cid} {row['name']}")
        return {"ok": True}

    def record_history(user, tc, body: ReportRequest, doc: dict) -> None:
        """One history row per report made. Never breaks the report: a database problem is only logged.
        The same report by the same person within 15 minutes is recorded once."""
        import time as _time
        m = doc["meta"]
        spec = {"blocks": m["blocks_requested"], "start": m["period"]["start"], "end": m["period"]["end"],
                "compare": m["period"]["compare"], "lang": m["lang"], "profile": body.profile,
                "period_preset": body.period_preset}
        try:
            for h in store().list_history(tc.tenant_id, limit=20):
                if (h["generated_by"] == user.username and h["spec"] == spec and h.get("data_version") == m["data_version"]
                        and _time.time() - h["generated_at"] < HISTORY_DEDUP_SECONDS):
                    return
            store().add_history(tc.tenant_id, user.username, config_id=body.config_id, period_start=spec["start"],
                                period_end=spec["end"], language=m["lang"], data_version=m["data_version"], spec=spec,
                                blocks=[b["id"] for ly in doc["layers"] for c in ly["chapters"] for b in c["blocks"]],
                                findings=doc.get("finding_ids", []))
        except ReportStoreError as exc:
            log.warning("report history not recorded: %s", exc)

    @app.get("/api/tenants/{tid}/reports/history")
    def history_list(limit: int = 30, g=Depends(tenant_guard)):
        user, tc = g
        _, _, _, st = dash.visible(user, tc)
        limit = max(1, min(int(limit), 100))
        rows = db(lambda: store().list_history(tc.tenant_id, limit=limit if user.has_role("manager") else 200))
        if not user.has_role("manager"):
            rows = [r for r in rows if r["generated_by"] == user.username][:limit]
        out = []
        for r in rows:
            u = dash.svc.users.get(r["generated_by"])
            out.append({"id": r["id"], "generated_at": r["generated_at"], "generated_by": u.display_name if u else r["generated_by"],
                        "mine": r["generated_by"] == user.username, "period_start": r["period_start"], "period_end": r["period_end"],
                        "language": r["language"], "parts": len(r.get("blocks") or []), "spec": r["spec"],
                        "data_version": r.get("data_version"), "data_changed": r.get("data_version") != st.get("data_version"),
                        "config_id": r.get("config_id")})
        return {"history": out}
