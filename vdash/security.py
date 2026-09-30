"""Authentication + tenant-level access control.

Model
-----
* A user has one ``role`` and a list of ``tenants`` ("*" = every tenant; only
  for ``platform_admin``).
* Roles are ordered: viewer < manager < tenant_admin < platform_admin.
  Each KPI declares a ``min_role`` (finance, workforce and customer-level
  tables need ``manager`` or above).
* The tenant id used in every warehouse query is taken from the URL path and
  checked against the signed session - it is never trusted from the browser
  alone, and the KPI engine refuses to run SQL that does not bind
  ``:tenant_id``.

Demo implementation: users live in ``config/users.json`` with PBKDF2-SHA256
password hashes, and sessions are HMAC-signed cookies (stdlib only). For
production the same ``User`` object is produced from an OIDC/SAML identity
provider instead (see ARCHITECTURE.md) - the rest of the code is unchanged.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass, field

from .config import ROOT, load_json

ROLE_ORDER = {"viewer": 1, "manager": 2, "tenant_admin": 3, "platform_admin": 4}
PBKDF2_ITERATIONS = 240_000


# ---------------------------------------------------------------- passwords
def hash_password(password: str, iterations: int = PBKDF2_ITERATIONS) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "pbkdf2_sha256${}${}${}".format(
        iterations, base64.b64encode(salt).decode(), base64.b64encode(dk).decode())


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt_b64, dk_b64 = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), base64.b64decode(salt_b64), int(iters))
        return hmac.compare_digest(dk, base64.b64decode(dk_b64))
    except Exception:
        return False


# ---------------------------------------------------------------- users
@dataclass
class User:
    username: str
    display_name: str
    role: str
    tenants: list[str] = field(default_factory=list)
    password_hash: str = ""
    enabled: bool = True

    @property
    def is_platform_admin(self) -> bool:
        return self.role == "platform_admin"

    def can_access_tenant(self, tenant_id: str) -> bool:
        if not self.enabled:
            return False
        if "*" in self.tenants:
            return self.is_platform_admin
        return tenant_id in self.tenants

    def has_role(self, min_role: str) -> bool:
        return ROLE_ORDER.get(self.role, 0) >= ROLE_ORDER.get(min_role, 99)

    def public(self) -> dict:
        return {"username": self.username, "display_name": self.display_name, "role": self.role}


class UserStore:
    def __init__(self) -> None:
        self.reload()

    def reload(self) -> None:
        raw = load_json("users.json")
        self.users: dict[str, User] = {}
        for u in raw.get("users", []):
            role = u.get("role", "viewer")
            if role not in ROLE_ORDER:
                raise ValueError(f"users.json: unknown role '{role}' for {u.get('username')}")
            tenants = list(u.get("tenants", []))
            if "*" in tenants and role != "platform_admin":
                raise ValueError(f"users.json: only platform_admin may use tenants ['*'] ({u.get('username')})")
            self.users[u["username"].lower()] = User(
                username=u["username"], display_name=u.get("display_name", u["username"]), role=role,
                tenants=tenants, password_hash=u.get("password_hash", ""), enabled=bool(u.get("enabled", True)))

    def get(self, username: str) -> User | None:
        return self.users.get((username or "").lower())

    def authenticate(self, username: str, password: str) -> User | None:
        u = self.get(username)
        if u is None or not u.enabled or not u.password_hash:
            verify_password(password, hash_password("timing-equaliser", 1000))
            return None
        return u if verify_password(password, u.password_hash) else None


# ---------------------------------------------------------------- sessions
def _load_or_create_secret(configured: str) -> bytes:
    if configured:
        return configured.encode("utf-8")
    key_file = ROOT / "runtime" / "session_secret.key"
    key_file.parent.mkdir(parents=True, exist_ok=True)
    if key_file.exists():
        return key_file.read_bytes().strip()
    key = secrets.token_hex(32).encode()
    key_file.write_bytes(key)
    try:
        os.chmod(key_file, 0o600)
    except OSError:  # pragma: no cover - Windows
        pass
    return key


class SessionSigner:
    def __init__(self, secret: str, ttl_minutes: int):
        self.key = _load_or_create_secret(secret)
        self.ttl = ttl_minutes * 60

    def issue(self, username: str) -> str:
        payload = json.dumps({"u": username, "exp": int(time.time()) + self.ttl, "n": secrets.token_hex(8)},
                             separators=(",", ":")).encode()
        body = base64.urlsafe_b64encode(payload).decode().rstrip("=")
        sig = hmac.new(self.key, body.encode(), hashlib.sha256).hexdigest()
        return f"{body}.{sig}"

    def verify(self, token: str | None) -> str | None:
        if not token or "." not in token:
            return None
        body, sig = token.rsplit(".", 1)
        expected = hmac.new(self.key, body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        try:
            payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        except Exception:
            return None
        if int(payload.get("exp", 0)) < time.time():
            return None
        return payload.get("u")


class LoginThrottle:
    """Simple in-process brute-force guard: N failures per user -> lockout window."""

    def __init__(self, max_failures: int, lockout_minutes: int):
        self.max_failures = max_failures
        self.window = lockout_minutes * 60
        self._fails: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def blocked(self, username: str) -> bool:
        now = time.time()
        with self._lock:
            hits = [t for t in self._fails.get(username.lower(), []) if now - t < self.window]
            self._fails[username.lower()] = hits
            return len(hits) >= self.max_failures

    def fail(self, username: str) -> None:
        with self._lock:
            self._fails.setdefault(username.lower(), []).append(time.time())

    def reset(self, username: str) -> None:
        with self._lock:
            self._fails.pop(username.lower(), None)
