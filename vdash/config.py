"""Configuration loading for the Veridex BI dashboard service.

All configuration lives in ``<root>/config/*.json``. String values may contain
``${ENV_VAR}`` or ``${ENV_VAR:-default}`` placeholders, so secrets (database
passwords, session secret) never have to be written into the JSON files.

The dashboard is an independent system: nothing here imports or reads the
Step 1-9 pipeline. It only needs a database connection and its own config.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _expand(value: Any) -> Any:
    """Recursively expand ${VAR} / ${VAR:-default} in strings."""
    if isinstance(value, str):
        def repl(m: re.Match) -> str:
            return os.environ.get(m.group(1), m.group(2) if m.group(2) is not None else "")
        return _ENV_RE.sub(repl, value)
    if isinstance(value, list):
        return [_expand(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    return value


def config_dir() -> Path:
    return Path(os.environ.get("VDASH_CONFIG_DIR", ROOT / "config"))


def load_json(name: str) -> Any:
    path = config_dir() / name
    with open(path, "r", encoding="utf-8") as fh:
        return _expand(json.load(fh))


def resolve_path(p: str) -> Path:
    """Resolve a path from config: absolute stays absolute, relative is relative to ROOT."""
    path = Path(p)
    return path if path.is_absolute() else (ROOT / path).resolve()


@dataclass
class Settings:
    host: str = "127.0.0.1"
    port: int = 8050
    session_secret: str = ""
    session_ttl_minutes: int = 480
    cookie_secure: bool = False
    refresh_enabled: bool = True
    refresh_interval_minutes: int = 15
    prewarm_presets: list[str] = field(default_factory=lambda: ["last_30_days", "last_12_months"])
    cache_max_entries: int = 2000
    cache_ttl_minutes: int = 60
    state_store_path: Path = ROOT / "runtime" / "dashboard_state.db"
    query_timeout_seconds: int = 30
    login_max_failures: int = 5
    login_lockout_minutes: int = 5
    raw: dict = field(default_factory=dict)

    @classmethod
    def load(cls) -> "Settings":
        raw = load_json("settings.json")
        s = cls(raw=raw)
        srv = raw.get("server", {})
        s.host = srv.get("host", s.host)
        s.port = int(srv.get("port", s.port))
        sess = raw.get("session", {})
        s.session_secret = sess.get("secret", "")
        s.session_ttl_minutes = int(sess.get("ttl_minutes", s.session_ttl_minutes))
        s.cookie_secure = bool(sess.get("cookie_secure", s.cookie_secure))
        s.login_max_failures = int(sess.get("login_max_failures", s.login_max_failures))
        s.login_lockout_minutes = int(sess.get("login_lockout_minutes", s.login_lockout_minutes))
        ref = raw.get("refresh", {})
        s.refresh_enabled = bool(ref.get("enabled", s.refresh_enabled))
        s.refresh_interval_minutes = int(ref.get("interval_minutes", s.refresh_interval_minutes))
        s.prewarm_presets = list(ref.get("prewarm_presets", s.prewarm_presets))
        cache = raw.get("cache", {})
        s.cache_max_entries = int(cache.get("max_entries", s.cache_max_entries))
        s.cache_ttl_minutes = int(cache.get("ttl_minutes", s.cache_ttl_minutes))
        st = raw.get("state_store", {})
        if st.get("path"):
            s.state_store_path = resolve_path(st["path"])
        q = raw.get("query", {})
        s.query_timeout_seconds = int(q.get("timeout_seconds", s.query_timeout_seconds))
        return s


@dataclass
class TenantConfig:
    tenant_id: str
    display_name: str
    datasource: str
    catalog: str = "retail_trade_004"
    currency: str = "USD"
    locale: str = "en-US"
    timezone: str = "UTC"
    industry: str = ""
    enabled: bool = True
    alerts: dict = field(default_factory=dict)   # {"disabled": [rule ids], "params": {rule id: {param: value}}}


def load_tenants() -> dict[str, TenantConfig]:
    raw = load_json("tenants.json")
    out: dict[str, TenantConfig] = {}
    for tid, t in raw.get("tenants", {}).items():
        out[tid] = TenantConfig(
            tenant_id=tid,
            display_name=t.get("display_name", tid),
            datasource=t["datasource"],
            catalog=t.get("catalog", "retail_trade_004"),
            currency=t.get("currency", "USD"),
            locale=t.get("locale", "en-US"),
            timezone=t.get("timezone", "UTC"),
            industry=t.get("industry", ""),
            enabled=bool(t.get("enabled", True)),
            alerts=dict(t.get("alerts") or {}),
        )
    return out
