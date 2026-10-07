"""Sending e-mail through Resend's HTTP API (v45, AI plan Step 24).

Why HTTP and not SMTP: Render's free servers block outgoing SMTP ports; an HTTPS call always works.

* The key comes from the environment (``RESEND_API_KEY``, e.g. in ``config/local.env``, which is never committed
  or synced) - it is never written to a file, a log or an error message.
* ``send_resend`` sends one mail (HTML + plain text + attachments). A retry after a network error or a 5xx answer
  uses the same ``Idempotency-Key``, so Resend never delivers the mail twice.
* Every problem is a ``MailError`` with a plain message; nothing else escapes.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request

log = logging.getLogger("vdash.mail")
API = "https://api.resend.com/emails"
USER_AGENT = "vdec-dashboard/9.24 (+report mail)"      # Resend's firewall refuses clients without a user agent
MAX_ATTACHMENT_BYTES = 30 * 1024 * 1024                   # Resend: 40 MB per mail after encoding; stay well under
_ADDR = re.compile(r"^[^@\s<>,;\"']+@[^@\s<>,;\"']+\.[^@\s<>,;\"']+$")


class MailError(RuntimeError):
    """The mail could not be sent; the message is safe to show (it never contains the key).
    ``retriable``: a temporary problem (network, server busy) - trying again later may work."""

    def __init__(self, message: str, retriable: bool = False):
        super().__init__(message)
        self.retriable = retriable


def _address(value: str, what: str) -> str:
    v = (value or "").strip()
    if re.search(r"[\r\n]", v):
        raise MailError(f"{what}: invalid address")
    m = re.match(r"^(?P<name>[^<>]*)<(?P<addr>[^<>]+)>$", v)          # "Name <a@b.c>"
    addr = (m.group("addr") if m else v).strip()
    if not _ADDR.match(addr):
        raise MailError(f"{what}: '{v[:80]}' is not an e-mail address")
    return v


def send_resend(api_key: str, sender: str, to: list[str], subject: str, html: str, text: str | None = None,
                attachments: list[dict] | None = None, reply_to: str | None = None, idempotency_key: str | None = None,
                timeout: float = 30.0, api: str | None = None, retries: int = 1, mail_headers: dict | None = None) -> dict:
    """Send one mail. ``attachments``: [{"filename": "report.pdf", "content": bytes}]. Returns Resend's answer
    ({"id": ...}). Raises MailError (``retriable`` for temporary problems).
    ``mail_headers``: extra mail headers, e.g. {"List-Unsubscribe": "<https://...>"}."""
    api = api or os.environ.get("RESEND_API_URL", "").strip() or API      # RESEND_API_URL: tests / a proxy
    if not api_key:
        raise MailError("No Resend key: set RESEND_API_KEY")
    if not to:
        raise MailError("No recipient")
    body: dict = {"from": _address(sender, "from"), "to": [_address(a, "to") for a in to],
                  "subject": re.sub(r"[\r\n]+", " ", subject or "").strip()[:250], "html": html}
    if text:
        body["text"] = text
    if reply_to:
        body["reply_to"] = _address(reply_to, "reply_to")
    if mail_headers:                                  # e.g. List-Unsubscribe (one-click unsubscribe)
        for k, v in mail_headers.items():
            if re.search(r"[\r\n]", f"{k}{v}"):
                raise MailError("invalid mail header")
        body["headers"] = dict(mail_headers)
    size = 0
    if attachments:
        out = []
        for a in attachments:
            raw = a["content"]
            size += len(raw)
            out.append({"filename": re.sub(r"[\r\n\\/]+", "_", a["filename"])[:120],
                        "content": base64.b64encode(raw).decode("ascii")})
        if size > MAX_ATTACHMENT_BYTES:
            raise MailError(f"The attachment is too large ({size // 1024 // 1024} MB; the limit is "
                            f"{MAX_ATTACHMENT_BYTES // 1024 // 1024} MB)")
        body["attachments"] = out
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "User-Agent": USER_AGENT}
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key[:256]
    last = "unknown error"
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(api, data=data, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as r:
                ans = json.loads(r.read().decode("utf-8") or "{}")
            if not ans.get("id"):
                raise MailError("Resend did not confirm the mail")
            return ans
        except urllib.error.HTTPError as exc:
            msg = ""
            try:
                j = json.loads(exc.read().decode("utf-8"))
                msg = str(j.get("message") or j.get("error") or "")[:300]
            except Exception:   # noqa: BLE001 - an unreadable error body is not worth a second error
                pass
            last = f"Resend refused the mail (HTTP {exc.code}){': ' + msg if msg else ''}"
            if exc.code < 500 and exc.code != 429:
                raise MailError(last) from None                 # a wrong key / address / sender is not retried
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = f"Resend could not be reached ({type(exc).__name__})"
        if attempt < retries:
            time.sleep(1.5)
    log.warning("mail: %s", last)
    raise MailError(last, retriable=True)


def settings_from_env() -> tuple[str, str]:
    """(API key, sender) from RESEND_API_KEY / RESEND_FROM - empty strings when not set."""
    import os
    return os.environ.get("RESEND_API_KEY", "").strip(), os.environ.get("RESEND_FROM", "").strip()
