"""Personal settings and the Talk to Data access point (v51).

  GET /api/me/settings   the signed-in person's saved settings (language, theme, default view)
  PUT /api/me/settings   change some of them; a value of null puts that one back to standard

Settings belong to the person, not to a business, and are stored in the report database (``user_prefs``) so
they follow the person to other devices. Only the keys below can be saved, each with a fixed set of allowed
values, so a stored setting can never carry anything else. If the database is down the page keeps working
with what the browser remembers; saving then says so plainly.

``talk_to_data_feature`` builds what the page needs for the "Talk to Data" menu entry. The chat itself is a
separate application (it works on the same warehouse data); this service only links to it. Nothing is sent to
that application except what the link itself carries: the business id and the language.
"""
from __future__ import annotations

import logging
from datetime import date
from urllib.parse import quote, urlsplit

from fastapi import Body, Depends, FastAPI

from .engine import presets
from .i18n import LANGS
from .report_store import ReportStoreError
from .services import ServiceError

log = logging.getLogger("vdash.settings")

THEMES = ("light", "dark", "system")
COMPARES = ("previous", "yoy", "none")
GRAINS = ("auto", "day", "week", "month", "quarter")
TRENDS = ("ma", "linear", "off")
# every preset the engine can offer, whatever the data range (a business that lacks one falls back to 30 days)
PRESET_IDS = tuple(p["id"] for p in presets(date(2030, 1, 1), date(2000, 1, 1)))

ALLOWED: dict[str, tuple[str, ...]] = {
    "lang": tuple(LANGS), "theme": THEMES, "preset": PRESET_IDS, "compare": COMPARES, "grain": GRAINS, "trend": TRENDS,
}


def clean(raw: dict) -> dict:
    """The stored settings with anything unknown or no longer allowed dropped (old rows stay harmless)."""
    return {k: v for k, v in (raw or {}).items() if k in ALLOWED and v in ALLOWED[k]}


def validate_update(body: dict) -> dict:
    """The requested changes as {key: value-or-None}; raises ServiceError(400) for anything not allowed."""
    if not isinstance(body, dict) or not body:
        raise ServiceError(400, "Nothing to save")
    out: dict = {}
    for key, value in body.items():
        if key not in ALLOWED:
            raise ServiceError(400, f"Unknown setting: {str(key)[:40]}")
        if value is None:
            out[key] = None
        elif isinstance(value, str) and value in ALLOWED[key]:
            out[key] = value
        else:
            raise ServiceError(400, f"Not an allowed value for {key}")
    return out


def talk_to_data_feature(raw_settings: dict, user, tenant_id: str, lang: str) -> dict:
    """What the menu needs: whether to show the entry and, if the chat application is connected, where it is.

    settings.json -> talk_to_data: ``enabled`` (show the entry), ``url`` (empty = not connected yet; may contain
    {tenant} and {lang}), ``min_role``. The URL must be http(s); anything else is ignored.
    """
    from .config import as_bool
    cfg = raw_settings.get("talk_to_data") or {}
    try:
        enabled = as_bool(cfg.get("enabled"), False)
    except ValueError:
        enabled = False
    min_role = cfg.get("min_role") or "viewer"
    if not enabled or not user.has_role(min_role):
        return {"enabled": False, "url": None}
    url = str(cfg.get("url") or "").strip()
    if url:
        url = url.replace("{tenant}", quote(tenant_id, safe="")).replace("{lang}", quote(lang, safe=""))
        p = urlsplit(url)
        if p.scheme not in ("http", "https") or not p.netloc:
            log.warning("talk_to_data.url ignored: it must start with http:// or https://")
            url = ""
    return {"enabled": True, "url": url or None}


def register(app: FastAPI, dash, current_user) -> None:
    store = lambda: dash.svc.reports   # noqa: E731 - replaced in tests

    @app.get("/api/me/settings")
    def settings_get(user=Depends(current_user)):
        try:
            prefs = clean(store().get_user_prefs(user.username))
            return {"prefs": prefs, "available": True}
        except ReportStoreError as exc:
            log.warning("settings unavailable: %s", exc)
            return {"prefs": {}, "available": False}

    @app.put("/api/me/settings")
    def settings_put(body: dict = Body(...), user=Depends(current_user)):
        changes = validate_update(body)
        try:
            prefs = clean(store().get_user_prefs(user.username))
            for k, v in changes.items():
                if v is None:
                    prefs.pop(k, None)
                else:
                    prefs[k] = v
            store().set_user_prefs(user.username, prefs)
        except ReportStoreError as exc:
            log.warning("settings not saved: %s", exc)
            raise ServiceError(503, "Could not save to your account right now - your choice is kept on this device") from None
        dash.svc.state.audit(user.username, None, "update_settings", ",".join(sorted(changes)))
        return {"prefs": prefs, "available": True}
