"""Share links for stored reports (v50, AI plan Step 27).

Signed in (the report's owner; a business administrator may also list and stop every link of the business):
  POST   /api/tenants/{tid}/reports/mine/{rid}/share     make a link -> {"id", "path", "url", "expires_at"}; the secret is
                                                         shown this once (only its hash is stored)
  GET    /api/tenants/{tid}/report-shares[?report=rid]   the live links (no secrets)
  DELETE /api/tenants/{tid}/report-shares/{sid}          stop sharing (works at once)
Public (no sign-in - the secret in the address is the proof):
  GET    /r/{secret}                                     the stored report, read-only, until the link expires or is stopped

A shared page is the report exactly as its owner saw it when it was made, including any customer names the owner could
see; nothing on it can change data. It is sent with its own strict content-security policy, never cached, never
indexed and without a referrer. Wrong addresses are slowed down per visitor (``failed_opens_per_10_min``).
"""
from __future__ import annotations

import logging
import secrets
import threading
import time
from datetime import datetime
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Request
from fastapi.responses import Response

from .i18n import LOCALES, bundle, ui
from .report_store import ReportStoreError
from .reports.html import render_html
from .reports.recipients import RecipientError, hash_token, public_url
from .reports.shares import TOKEN_RE, load_config, new_secret
from .services import ServiceError

log = logging.getLogger("vdash.share")
_REFUSED = {"report_limit": "This report already has the most links it may have - stop one first",
            "business_limit": "This business has reached its limit of share links - stop one first"}


def register(app: FastAPI, dash, tenant_guard) -> None:
    store = lambda: dash.svc.reports   # noqa: E731 - replaced in tests
    fails: dict[str, list[float]] = {}
    lock = threading.Lock()

    def db(call):
        try:
            return call()
        except ReportStoreError as exc:
            log.warning("report store: %s", exc)
            raise ServiceError(503, "Not available right now - please try again in a few minutes") from None

    def client_of(request: Request) -> str:
        fwd = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
        return fwd or (request.client.host if request.client else "?")

    def slowed(who: str, now: float, add: bool = False) -> bool:
        limit = int(load_config().get("failed_opens_per_10_min", 30))
        with lock:
            hits = [t for t in fails.get(who, []) if now - t < 600]
            if add:
                hits.append(now)
            fails[who] = hits
            if len(fails) > 5000:                                    # never grows without bound
                for k in [k for k, v in fails.items() if not v or now - v[-1] >= 600]:
                    fails.pop(k, None)
            return len(hits) >= limit

    def page(title: str, text: str, status: int) -> Response:
        import html as _h
        e = _h.escape
        body = (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
                f'<meta name="robots" content="noindex"><title>{e(title)}</title></head><body style="margin:0;background:#f6f8fa;'
                'font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;color:#24292f"><div style="max-width:480px;margin:48px auto;padding:0 16px">'
                '<div style="background:#fff;border:1px solid #d0d7de;border-radius:8px;padding:24px">'
                f'<h1 style="font-size:20px;margin:0 0 12px">{e(title)}</h1><p style="margin:0;line-height:1.5">{e(text)}</p></div></div></body></html>')
        return Response(body, status_code=status, media_type="text/html; charset=utf-8", headers={
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
            "Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY", "X-Robots-Tag": "noindex"})

    def out(s: dict, mine_only: bool, user) -> dict:
        return {"id": s["id"], "report_id": s["report_id"], "report_name": s["report_name"], "created_at": s["created_at"],
                "expires_at": s["expires_at"], "views": s["views"], "last_view": s["last_view"],
                "created_by": s["created_by"], "mine": s["created_by"] == user.username}

    # ------------------------------------------------------------ signed in
    @app.post("/api/tenants/{tid}/reports/mine/{rid}/share")
    def share_create(rid: str, g=Depends(tenant_guard)):
        user, tc = g
        r = db(lambda: store().get_report(tc.tenant_id, user.username, rid, with_doc=False))
        if r is None:
            raise ServiceError(404, "Report not found")
        c = load_config()
        raw, h = new_secret()
        exp = time.time() + float(c["valid_days"]) * 86400
        state, row = db(lambda: store().add_share(tc.tenant_id, rid, user.username, h, exp,
                                                  int(c["max_active_per_report"]), int(c["max_active_per_business"])))
        if state == "refused":
            raise ServiceError(429, _REFUSED.get(row, "Not possible"))
        path = f"/r/{raw}"
        try:
            url = public_url() + path
        except RecipientError:
            url = None                                               # the page builds it from its own address
        dash.svc.state.audit(user.username, tc.tenant_id, "report_share_create", f"{rid} share={row['id']} valid_days={c['valid_days']}")
        return {"id": row["id"], "path": path, "url": url, "expires_at": exp, "valid_days": int(c["valid_days"])}

    @app.get("/api/tenants/{tid}/report-shares")
    def share_list(report: str | None = None, g=Depends(tenant_guard)):
        user, tc = g
        everyone = user.has_role("tenant_admin")
        rows = db(lambda: store().list_shares(tc.tenant_id, None if everyone else user.username, report))
        return {"shares": [out(s, not everyone, user) for s in rows], "all": everyone,
                "valid_days": int(load_config()["valid_days"])}

    @app.delete("/api/tenants/{tid}/report-shares/{sid}")
    def share_stop(sid: str, g=Depends(tenant_guard)):
        user, tc = g
        ok = db(lambda: store().revoke_share(tc.tenant_id, sid, None if user.has_role("tenant_admin") else user.username))
        if not ok:
            raise ServiceError(404, "Link not found")
        dash.svc.state.audit(user.username, tc.tenant_id, "report_share_stop", sid)
        return {"stopped": True}

    # ------------------------------------------------------------ public
    @app.get("/r/{secret}")
    def shared_report(secret: str, request: Request):
        now, who = time.time(), client_of(request)
        if slowed(who, now):
            return page("Too many tries", "Please wait a few minutes and try again.", 429)
        gone = lambda: page("This link is not valid any more",                      # noqa: E731
                            "It may have expired or been stopped. Ask the person who sent it for a new one.", 404)
        if not TOKEN_RE.match(secret):
            slowed(who, now, add=True)
            return gone()
        try:
            row = store().resolve_share(hash_token(secret), now)
        except ReportStoreError as exc:
            log.warning("report store: %s", exc)
            return page("Not available right now", "Please try again in a few minutes.", 503)
        tc = dash.svc.tenants.get(row["tenant_id"]) if row else None
        if row is None or tc is None or not tc.enabled:
            slowed(who, now, add=True)
            return gone()
        doc = row["doc"]
        lg = doc["meta"]["lang"]
        try:
            tz = ZoneInfo(getattr(tc, "timezone", "UTC") or "UTC")
        except Exception:    # noqa: BLE001
            tz = ZoneInfo("UTC")
        until = datetime.fromtimestamp(row["expires_at"], tz).strftime("%Y-%m-%d %H:%M")
        doc["cover"]["data_note"] = ui(lg, "rpt_share_note").replace("{date}", until)
        texts = {**bundle("en")["ui"], **bundle(lg)["ui"]}
        nonce = secrets.token_urlsafe(16)
        html = render_html(doc, texts, LOCALES[lg], standalone=False, nonce=nonce, excel_url=None)
        try:
            store().touch_share(row["id"], now)
        except ReportStoreError:
            pass                                                     # the count is a nicety; the report still opens
        dash.svc.state.audit("share-link", row["tenant_id"], "report_share_open", f"{row['report_id']} share={row['id']}")
        return Response(html, media_type="text/html; charset=utf-8", headers={
            "Content-Security-Policy": (f"default-src 'none'; script-src 'self' 'nonce-{nonce}'; style-src 'unsafe-inline'; "
                                        "img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"),
            "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer", "X-Robots-Tag": "noindex, nofollow",
            "Cache-Control": "no-store"})
