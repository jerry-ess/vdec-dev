"""Configuration loading for the Veridex BI dashboard service.

All configuration lives in ``<root>/config/*.json``. String values may contain
``${ENV_VAR}`` or ``${ENV_VAR:-default}`` placeholders (defaults may nest:
``${VDASH_PORT:-${PORT:-8050}}``), so secrets (database passwords, session
secret) never have to be written into the JSON files.

Local development vs online (v19)
---------------------------------
The same code and the same committed config files run everywhere; only
environment variables differ.

* ``config/local.env`` (never committed, see ``local.env.example``) holds this
  machine's values as ``KEY=VALUE`` lines. It is read once at import time and
  only fills variables that are NOT already set, so a real environment
  variable (e.g. on Render) always wins. ``VDASH_ENV_FILE`` points elsewhere.
* ``VDASH_ENV`` picks a profile: ``local`` or ``online``. If unset it is
  ``online`` when the ``RENDER`` variable exists (Render sets it), else ``local``.
  For every config file ``<name>.json`` an optional ``<name>.<profile>.json``
  is deep-merged on top (e.g. ``settings.online.json``).

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
# innermost placeholder first: its default contains no further "${" / "}"
_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^${}]*))?\}")
_TRUE, _FALSE = {"1", "true", "yes", "on"}, {"0", "false", "no", "off", ""}


def as_bool(v: Any, default: bool = False) -> bool:
    """v19: config booleans may be strings after ${VAR} expansion - bool("false") would be True."""
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    s = str(v).strip().lower()
    if s in _TRUE:
        return True
    if s in _FALSE:
        return False
    raise ValueError(f"not a true/false value: {v!r}")


def load_env_file(path: Path | None = None) -> dict[str, str]:
    """Read KEY=VALUE lines (# comments, optional quotes, optional 'export ') into os.environ,
    without overriding variables that are already set. Returns what was applied."""
    path = Path(path or os.environ.get("VDASH_ENV_FILE") or ROOT / "config" / "local.env")
    applied: dict[str, str] = {}
    if not path.is_file():
        return applied
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, val = (x.strip() for x in line.split("=", 1))
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "'\"":
            val = val[1:-1]
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) and key not in os.environ:
            os.environ[key] = val
            applied[key] = val
    return applied


LOCAL_ENV_APPLIED = load_env_file()


def env_profile() -> str:
    p = (os.environ.get("VDASH_ENV") or ("online" if os.environ.get("RENDER") else "local")).strip().lower()
    if not re.fullmatch(r"[a-z0-9_-]+", p):
        raise ValueError(f"VDASH_ENV must be a simple name like local / online, got {p!r}")
    return p


def _deep_merge(base: Any, over: Any) -> Any:
    if isinstance(base, dict) and isinstance(over, dict):
        out = dict(base)
        for k, v in over.items():
            out[k] = _deep_merge(base.get(k), v) if k in base else v
        return out
    return over


def _expand(value: Any) -> Any:
    """Recursively expand ${VAR} / ${VAR:-default} in strings (nested defaults allowed)."""
    if isinstance(value, str):
        def repl(m: re.Match) -> str:
            return os.environ.get(m.group(1), m.group(2) if m.group(2) is not None else "")
        for _ in range(10):
            new = _ENV_RE.sub(repl, value)
            if new == value:
                break
            value = new
        return value
    if isinstance(value, list):
        return [_expand(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    return value


def config_dir() -> Path:
    return Path(os.environ.get("VDASH_CONFIG_DIR", ROOT / "config"))


def load_json(name: str) -> Any:
    """<name> from the config folder, with the profile overlay <stem>.<VDASH_ENV>.json merged on top (v19)."""
    path = config_dir() / name
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    overlay = path.with_name(f"{path.stem}.{env_profile()}{path.suffix}")
    if overlay.is_file():
        with open(overlay, "r", encoding="utf-8") as fh:
            data = _deep_merge(data, json.load(fh))
    return _expand(data)


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
    cache_ttl_minutes: int = 0          # v20: 0 = no expiry (keys carry the data version)
    cache_persist_path: Path | None = ROOT / "runtime" / "result_cache.db"
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
        s.cookie_secure = as_bool(sess.get("cookie_secure"), s.cookie_secure)
        s.login_max_failures = int(sess.get("login_max_failures", s.login_max_failures))
        s.login_lockout_minutes = int(sess.get("login_lockout_minutes", s.login_lockout_minutes))
        ref = raw.get("refresh", {})
        s.refresh_enabled = as_bool(ref.get("enabled"), s.refresh_enabled)
        s.refresh_interval_minutes = int(ref.get("interval_minutes", s.refresh_interval_minutes))
        s.prewarm_presets = list(ref.get("prewarm_presets", s.prewarm_presets))
        cache = raw.get("cache", {})
        s.cache_max_entries = int(cache.get("max_entries", s.cache_max_entries))
        s.cache_ttl_minutes = int(cache.get("ttl_minutes", s.cache_ttl_minutes))
        if "persist_path" in cache:
            s.cache_persist_path = resolve_path(cache["persist_path"]) if cache["persist_path"] else None
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
    plan: str = ""                               # v34: payment plan (config/report_plans.json); "" = default plan
    ai_reports: bool = True                      # v41: platform admins can switch AI-written reports off per business


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
            enabled=as_bool(t.get("enabled"), True),
            alerts=dict(t.get("alerts") or {}),
            plan=str(t.get("plan") or ""),
            ai_reports=as_bool(t.get("ai_reports"), True),
        )
    return out
