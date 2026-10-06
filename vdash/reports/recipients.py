"""Who may receive report mails (v45, AI plan Step 24).

* An address receives **nothing** until its owner has confirmed it: ``request_confirmation`` sends one mail to the
  address with a link; the link opens a page with a button (so mail scanners that open links do not confirm by
  accident); only the button confirms. The link is single-use, expires (``confirm_valid_days``) and only its hash is
  stored.
* Limits (``config/report_mail.json``): addresses per business, confirmation mails per address and day.
* Every report mail carries an unsubscribe link (and the one-click ``List-Unsubscribe`` header): a signed token, so
  nothing has to be stored; it revokes the address for that business.
* The address of this dashboard for the links (``VDASH_PUBLIC_URL``), the Resend key and the sender are environment
  variables - without the address no mail with a link is sent.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import html as _html
import json
import os
import re
import secrets
import time
from pathlib import Path

from ..config import config_dir
from ..i18n import norm_lang
from ..mailer import MailError, send_resend, settings_from_env

_CACHE: dict[str, tuple[float, dict]] = {}
_ADDR = re.compile(r"^[A-Za-z0-9._%+\-']+@[A-Za-z0-9\-]+(\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}$")


class RecipientError(RuntimeError):
    """A refusal with a short ``code`` (recipients_limit, too_many_requests, bad_address, no_public_url, mail_failed ...)."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


# ------------------------------------------------------------------ settings
def config_path() -> Path:
    return config_dir() / "report_mail.json"


def load_config(path: Path | None = None) -> dict:
    path = path or config_path()
    mtime = path.stat().st_mtime
    hit = _CACHE.get(str(path))
    if hit and hit[0] == mtime:
        return hit[1]
    raw = json.loads(path.read_text(encoding="utf-8"))
    _CACHE[str(path)] = (mtime, raw)
    return raw


def validate_config(c: dict) -> list[str]:
    errs = []

    def num(k, lo, hi):
        v = c.get(k)
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not lo <= v <= hi:
            errs.append(f"{k}: a number from {lo} to {hi}")
    num("max_recipients_per_business", 1, 1000)
    num("max_per_schedule", 1, 100)
    num("confirm_valid_days", 1, 30)
    num("max_confirmation_mails_per_address_per_day", 1, 20)
    num("max_mails_per_tick", 1, 500)
    num("report_wait_hours", 1, 72)
    rs = c.get("retry_seconds")
    if not isinstance(rs, list) or not all(isinstance(x, (int, float)) and not isinstance(x, bool) and 1 <= x <= 86400 for x in rs):
        errs.append("retry_seconds: a list of numbers from 1 to 86400")
    return errs


def public_url() -> str:
    u = os.environ.get("VDASH_PUBLIC_URL", "").strip().rstrip("/")
    if not re.match(r"^https?://[^\s/]+", u):
        raise RecipientError("no_public_url", "VDASH_PUBLIC_URL (the address of this dashboard, for the links in mails) is not set")
    return u


# ------------------------------------------------------------------ addresses and tokens
def normalize_address(value: str) -> str:
    a = (value or "").strip().lower()
    if len(a) > 254 or not _ADDR.match(a):
        raise RecipientError("bad_address", f"'{(value or '')[:60]}' is not an e-mail address")
    return a


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def unsub_key(session_key: bytes) -> bytes:
    """A key of its own for unsubscribe links, derived from the session key (a session cookie is never a valid link)."""
    return hmac.new(session_key, b"veridex-unsubscribe", hashlib.sha256).digest()


def sign_unsubscribe(key: bytes, tenant_id: str, address: str, lang: str) -> str:
    body = base64.urlsafe_b64encode(json.dumps({"t": tenant_id, "a": address, "l": lang, "p": "unsub"},
                                               separators=(",", ":")).encode()).decode().rstrip("=")
    return f"{body}.{hmac.new(key, body.encode(), hashlib.sha256).hexdigest()}"


def verify_unsubscribe(key: bytes, token: str) -> dict | None:
    if not token or "." not in token or len(token) > 1000:
        return None
    body, sig = token.rsplit(".", 1)
    if not hmac.compare_digest(sig, hmac.new(key, body.encode(), hashlib.sha256).hexdigest()):
        return None
    try:
        d = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except Exception:   # noqa: BLE001
        return None
    return d if d.get("p") == "unsub" and d.get("t") and d.get("a") else None


def unsubscribe_url(key: bytes, tenant_id: str, address: str, lang: str) -> str:
    return f"{public_url()}/mail/unsubscribe/{sign_unsubscribe(key, tenant_id, address, lang)}"


# ------------------------------------------------------------------ the confirmation mail
def _ui(lang: str) -> dict:
    from ..i18n import bundle
    return {**bundle("en")["ui"], **bundle(norm_lang(lang))["ui"]}


def confirmation_mail(business: str, address: str, lang: str, link: str, days: int) -> dict:
    ui, e = _ui(lang), _html.escape
    f = lambda k: ui[k].replace("{business}", business).replace("{days}", str(days))   # noqa: E731
    html = ('<!doctype html><html><head><meta charset="utf-8"></head><body style="margin:0;background:#f6f8fa">'
            '<div style="max-width:560px;margin:0 auto;padding:24px 16px;font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif">'
            '<div style="background:#fff;border:1px solid #d0d7de;border-radius:8px;padding:24px;color:#24292f;font-size:15px;line-height:1.5">'
            f'<p style="margin:0 0 16px">{e(f("mail_confirm_intro"))}</p>'
            f'<p style="margin:0 0 20px"><a href="{e(link)}" style="display:inline-block;background:#0969da;color:#fff;text-decoration:none;'
            f'padding:10px 18px;border-radius:6px;font-weight:600">{e(f("mail_confirm_button"))}</a></p>'
            f'<p style="margin:0 0 6px;font-size:13px;color:#57606a">{e(f("mail_confirm_valid"))}</p>'
            f'<p style="margin:0;font-size:13px;color:#57606a">{e(f("mail_confirm_ignore"))}</p>'
            '</div></div></body></html>')
    text = "\n".join([f("mail_confirm_intro"), "", link, "", f("mail_confirm_valid"), f("mail_confirm_ignore")])
    return {"subject": f("mail_confirm_subject"), "html": html, "text": text}


def request_confirmation(store, tenant_id: str, business: str, address: str, requested_by: str, lang: str,
                         cfg: dict | None = None, send=None) -> dict:
    """Ask ``address`` to confirm. Returns {"address", "state": "pending" | "confirmed"}; raises RecipientError."""
    cfg = cfg or load_config()
    a, lg = normalize_address(address), norm_lang(lang)
    base = public_url()
    key, sender = settings_from_env()
    if not key or not sender:
        raise RecipientError("mail_not_configured", "RESEND_API_KEY / RESEND_FROM are not set")
    raw = secrets.token_urlsafe(32)
    state, why = store.request_recipient(tenant_id, a, requested_by, hash_token(raw),
                                         time.time() + cfg["confirm_valid_days"] * 86400, lg,
                                         int(cfg["max_confirmation_mails_per_address_per_day"]),
                                         int(cfg["max_recipients_per_business"]))
    if state == "refused":
        raise RecipientError(why or "refused")
    if state == "confirmed":
        return {"address": a, "state": "confirmed"}
    mail = confirmation_mail(business, a, lg, f"{base}/mail/confirm/{raw}", int(cfg["confirm_valid_days"]))
    try:
        (send or send_resend)(key, sender, [a], mail["subject"], mail["html"], mail["text"], idempotency_key=hash_token(raw)[:40])
    except MailError as exc:
        raise RecipientError("mail_failed", str(exc)) from None
    return {"address": a, "state": "pending"}
