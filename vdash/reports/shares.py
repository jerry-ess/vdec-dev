"""Share links for stored reports (v50, AI plan Step 27).

A link is ``/r/<secret>``: the secret is 32 random bytes (never stored - only its SHA-256), it works for
``valid_days`` and can be stopped earlier. Settings: config/report_share.json. The routes are in
``vdash/share_routes.py``; the rows in ``report_store.report_shares``.
"""
from __future__ import annotations

import json
import re
import secrets
import threading
from pathlib import Path

from ..config import config_dir
from .recipients import hash_token

_CACHE: dict[str, tuple[float, dict]] = {}
_LOCK = threading.Lock()
TOKEN_RE = re.compile(r"^[A-Za-z0-9_\-]{40,64}$")


def config_path() -> Path:
    return config_dir() / "report_share.json"


def load_config(path: Path | None = None) -> dict:
    path = path or config_path()
    mtime = path.stat().st_mtime
    with _LOCK:
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
    num("valid_days", 1, 30)
    num("max_active_per_report", 1, 50)
    num("max_active_per_business", 1, 1000)
    num("failed_opens_per_10_min", 1, 1000)
    return errs


def new_secret() -> tuple[str, str]:
    """(secret for the link, hash to store)."""
    raw = secrets.token_urlsafe(32)
    return raw, hash_token(raw)
