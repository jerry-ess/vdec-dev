"""Report mail: who may receive it (v46, AI plan Step 24 part 2).

Public pages (no sign-in; the link in a mail is the proof):
  GET  /mail/confirm/{token}       a page with a button (opening the link alone changes nothing)
  POST /mail/confirm/{token}       confirms the address (the token works once)
  GET  /mail/unsubscribe/{token}   a page with a button
  POST /mail/unsubscribe/{token}   stops the mails (also the one-click List-Unsubscribe of mail programs)
Signed in, role tenant_admin or higher:
  GET    /api/tenants/{tid}/report-recipients
  POST   /api/tenants/{tid}/report-recipients          {"address", "lang"} - sends the confirmation mail
  DELETE /api/tenants/{tid}/report-recipients/{address}
"""
from __future__ import annotations

import html as _html
import logging

from fastapi import Depends, FastAPI
from fastapi.responses import Response
from pydantic import BaseModel, Field

from .i18n import bundle, norm_lang
from .report_store import ReportStoreError
from .reports.recipients import (RecipientError, hash_token, load_config, normalize_address, request_confirmation,
                                 unsub_key, verify_unsubscribe)
from .services import ServiceError

log = logging.getLogger("vdash.mail")
STATUS = {"bad_address": 400, "recipients_limit": 429, "too_many_requests": 429, "no_public_url": 503,
          "mail_not_configured": 503, "mail_failed": 502}
_CSP = "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"


class RecipientBody(BaseModel):
    address: str = Field(max_length=254)
    lang: str = "en"


def _page(lang: str, title: str, text: str, button: str | None = None, action: str = "") -> Response:
    e = _html.escape
    btn = (f'<form method="post" action="{e(action)}"><button type="submit" style="background:#0969da;color:#fff;border:0;'
           f'border-radius:6px;padding:10px 18px;font-size:15px;font-weight:600;cursor:pointer">{e(button)}</button></form>'
           if button else "")
    body = (f'<!doctype html><html lang="{e(lang)}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<meta name="robots" content="noindex"><title>{e(title)}</title></head><body style="margin:0;background:#f6f8fa;'
            'font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;color:#24292f"><div style="max-width:480px;margin:48px auto;padding:0 16px">'
            '<div style="background:#fff;border:1px solid #d0d7de;border-radius:8px;padding:24px">'
            f'<h1 style="font-size:20px;margin:0 0 12px">{e(title)}</h1><p style="margin:0 0 20px;line-height:1.5">{e(text)}</p>{btn}'
            '</div></div></body></html>')
    return Response(body, media_type="text/html; charset=utf-8",
                    headers={"Content-Security-Policy": _CSP, "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
                             "X-Frame-Options": "DENY"})


def _human(exc: RecipientError) -> str:
    return {"bad_address": "That is not an e-mail address.",
            "recipients_limit": "This business has reached the number of addresses it may have.",
            "too_many_requests": "This address was asked to confirm several times today - try again tomorrow.",
            "no_public_url": "Sending mail is not set up yet (the address of this dashboard is missing).",
            "mail_not_configured": "Sending mail is not set up yet.",
            "mail_failed": "The confirmation mail could not be sent - please try again later."}.get(exc.code, "Not possible.")


def register(app: FastAPI, dash, tenant_guard) -> None:
    store = lambda: dash.svc.reports   # noqa: E731 - replaced in tests

    def ui(lang: str):
        lg = norm_lang(lang)
        return lg, {**bundle("en")["ui"], **bundle(lg)["ui"]}

    def db(call):
        try:
            return call()
        except ReportStoreError as exc:
            log.warning("report store: %s", exc)
            raise ServiceError(503, "Not available right now - please try again in a few minutes") from None

    def fill(t: str, **kw) -> str:
        for k, v in kw.items():
            t = t.replace("{" + k + "}", v)
        return t

    def business(tid: str) -> str:
        t = dash.svc.tenants.get(tid)
        return t.display_name if t else tid

    def invalid():
        lg, u = ui("en")
        return _page(lg, "VDec BI", u["page_invalid"])

    # ------------------------------------------------------------ public: confirm
    @app.get("/mail/confirm/{token}")
    def confirm_page(token: str):
        row = db(lambda: store().peek_recipient_token(hash_token(token))) if len(token) < 200 else None
        if not row:
            return invalid()
        lg, u = ui(row.get("lang") or "en")
        kw = {"business": business(row["tenant_id"]), "address": row["address"]}
        return _page(lg, fill(u["page_confirm_title"], **kw), fill(u["page_confirm_text"], **kw), u["page_confirm_btn"],
                     f"/mail/confirm/{token}")

    @app.post("/mail/confirm/{token}")
    def confirm_do(token: str):
        row = db(lambda: store().confirm_recipient(hash_token(token))) if len(token) < 200 else None
        if not row:
            return invalid()
        lg, u = ui(row.get("lang") or "en")
        kw = {"business": business(row["tenant_id"]), "address": row["address"]}
        dash.svc.state.audit("recipient", row["tenant_id"], "report_recipient_confirmed", row["address"])
        return _page(lg, fill(u["page_confirm_title"], **kw), fill(u["page_confirmed"], **kw))

    # ------------------------------------------------------------ public: unsubscribe
    def unsub_data(token: str):
        return verify_unsubscribe(unsub_key(dash.svc.signer.key), token)

    @app.get("/mail/unsubscribe/{token}")
    def unsub_page(token: str):
        d = unsub_data(token)
        if not d:
            return invalid()
        lg, u = ui(d.get("l") or "en")
        kw = {"business": business(d["t"]), "address": d["a"]}
        return _page(lg, fill(u["page_unsub_title"], **kw), fill(u["page_unsub_text"], **kw), u["page_unsub_btn"],
                     f"/mail/unsubscribe/{token}")

    @app.post("/mail/unsubscribe/{token}")
    def unsub_do(token: str):
        d = unsub_data(token)
        if not d:
            return invalid()
        db(lambda: store().revoke_recipient(d["t"], d["a"]))
        dash.svc.state.audit("recipient", d["t"], "report_recipient_unsubscribed", d["a"])
        lg, u = ui(d.get("l") or "en")
        kw = {"business": business(d["t"]), "address": d["a"]}
        return _page(lg, fill(u["page_unsub_title"], **kw), fill(u["page_unsubscribed"], **kw))

    # ------------------------------------------------------------ signed in: manage the list
    def admin(g):
        user, tc = g
        if not user.has_role("tenant_admin"):
            raise ServiceError(403, "Only a business administrator can manage report recipients")
        return user, tc

    @app.get("/api/tenants/{tid}/report-recipients")
    def recipients_list(g=Depends(tenant_guard)):
        user, tc = admin(g)
        c = load_config()
        return {"recipients": db(lambda: store().list_recipients(tc.tenant_id)),
                "limits": {"max_recipients": c["max_recipients_per_business"], "confirm_valid_days": c["confirm_valid_days"]}}

    @app.post("/api/tenants/{tid}/report-recipients")
    def recipients_add(body: RecipientBody, g=Depends(tenant_guard)):
        user, tc = admin(g)
        try:
            ans = db(lambda: request_confirmation(store(), tc.tenant_id, tc.display_name, body.address, user.username,
                                                  body.lang))
        except RecipientError as exc:
            raise ServiceError(STATUS.get(exc.code, 400), _human(exc)) from None
        dash.svc.state.audit(user.username, tc.tenant_id, "report_recipient_request", f"{ans['address']} {ans['state']}")
        return ans

    @app.delete("/api/tenants/{tid}/report-recipients/{address}")
    def recipients_remove(address: str, g=Depends(tenant_guard)):
        user, tc = admin(g)
        try:
            a = normalize_address(address)
        except RecipientError:
            raise ServiceError(400, "Not an e-mail address") from None
        ok = db(lambda: store().revoke_recipient(tc.tenant_id, a))
        dash.svc.state.audit(user.username, tc.tenant_id, "report_recipient_removed", a)
        return {"removed": bool(ok)}
