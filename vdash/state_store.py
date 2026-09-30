"""The dashboard's own small state database (never the warehouse).

Holds, per tenant: data version, data date range, KPI availability and the
last refresh status; plus an append-only audit log of who viewed what.
Demo: a local SQLite file under ``runtime/``. Production: point it at a small
Postgres schema (same two tables) so several dashboard workers share it.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path


class StateStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._con() as con:
            con.executescript("""
            CREATE TABLE IF NOT EXISTS tenant_state (
                tenant_id TEXT PRIMARY KEY, data_version TEXT, min_date TEXT, max_date TEXT,
                last_loaded_at TEXT, availability_json TEXT, status TEXT, error TEXT,
                refreshed_at REAL, probe_ms REAL);
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, username TEXT, tenant_id TEXT,
                action TEXT, detail TEXT);
            CREATE INDEX IF NOT EXISTS ix_audit_ts ON audit_log (ts);
            CREATE TABLE IF NOT EXISTS alert_runs (
                tenant_id TEXT PRIMARY KEY, data_version TEXT, prev_version TEXT, asof TEXT, computed_at REAL,
                checked INTEGER, errors_json TEXT);
            CREATE TABLE IF NOT EXISTS alert_state (
                tenant_id TEXT, rule_id TEXT, severity TEXT, active INTEGER,
                first_seen REAL, first_version TEXT, last_seen REAL, last_version TEXT, resolved_at REAL,
                payload_json TEXT, PRIMARY KEY (tenant_id, rule_id));
            """)

    def _con(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=10)
        con.row_factory = sqlite3.Row
        return con

    def get(self, tenant_id: str) -> dict | None:
        with self._con() as con:
            r = con.execute("SELECT * FROM tenant_state WHERE tenant_id = ?", (tenant_id,)).fetchone()
        if r is None:
            return None
        d = dict(r)
        d["availability"] = json.loads(d.pop("availability_json") or "{}")
        return d

    def put(self, tenant_id: str, **fields) -> None:
        if "availability" in fields:
            fields["availability_json"] = json.dumps(fields.pop("availability"))
        fields["refreshed_at"] = time.time()
        with self._lock, self._con() as con:
            cur = con.execute("SELECT 1 FROM tenant_state WHERE tenant_id = ?", (tenant_id,)).fetchone()
            if cur is None:
                cols = ["tenant_id", *fields]
                con.execute(f"INSERT INTO tenant_state ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                            [tenant_id, *fields.values()])
            else:
                con.execute(f"UPDATE tenant_state SET {', '.join(f'{k} = ?' for k in fields)} WHERE tenant_id = ?",
                            [*fields.values(), tenant_id])

    # ------------------------------------------------------------ early warnings (v9)
    def alert_run(self, tenant_id: str) -> dict | None:
        with self._con() as con:
            r = con.execute("SELECT * FROM alert_runs WHERE tenant_id = ?", (tenant_id,)).fetchone()
        if r is None:
            return None
        d = dict(r)
        d["errors"] = json.loads(d.pop("errors_json") or "[]")
        return d

    def save_warnings(self, tenant_id: str, data_version: str, asof: str, warnings: list[dict], checked: int,
                      errors: list[dict]) -> None:
        """Store one computation per data version. A warning keeps its first-seen time while it stays active,
        so it is announced as new only in the load where it first appeared; one that stops firing is marked
        resolved (kept as history)."""
        now = time.time()
        with self._lock, self._con() as con:
            prev = con.execute("SELECT data_version FROM alert_runs WHERE tenant_id = ?", (tenant_id,)).fetchone()
            prev_version = prev["data_version"] if prev else None
            if prev_version == data_version:
                row = con.execute("SELECT prev_version FROM alert_runs WHERE tenant_id = ?", (tenant_id,)).fetchone()
                prev_version = row["prev_version"]
            con.execute("DELETE FROM alert_runs WHERE tenant_id = ?", (tenant_id,))
            con.execute("INSERT INTO alert_runs (tenant_id, data_version, prev_version, asof, computed_at, checked, "
                        "errors_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (tenant_id, data_version, prev_version, asof, now, checked, json.dumps(errors)))
            existing = {r["rule_id"]: dict(r) for r in con.execute(
                "SELECT * FROM alert_state WHERE tenant_id = ?", (tenant_id,))}
            fired = {w["rule"]: w for w in warnings}
            for rid, w in fired.items():
                old = existing.get(rid)
                if old and old["active"]:
                    con.execute("UPDATE alert_state SET severity = ?, last_seen = ?, last_version = ?, payload_json = ? "
                                "WHERE tenant_id = ? AND rule_id = ?",
                                (w.get("severity"), now, data_version, json.dumps(w), tenant_id, rid))
                else:
                    con.execute("INSERT OR REPLACE INTO alert_state (tenant_id, rule_id, severity, active, first_seen, "
                                "first_version, last_seen, last_version, resolved_at, payload_json) "
                                "VALUES (?, ?, ?, 1, ?, ?, ?, ?, NULL, ?)",
                                (tenant_id, rid, w.get("severity"), now, data_version, now, data_version, json.dumps(w)))
            for rid, old in existing.items():
                if rid not in fired and old["active"]:
                    con.execute("UPDATE alert_state SET active = 0, resolved_at = ? WHERE tenant_id = ? AND rule_id = ?",
                                (now, tenant_id, rid))

    def active_warnings(self, tenant_id: str) -> list[dict]:
        with self._con() as con:
            rows = con.execute("SELECT * FROM alert_state WHERE tenant_id = ? AND active = 1", (tenant_id,)).fetchall()
        out = []
        for r in rows:
            w = json.loads(r["payload_json"] or "{}")
            w.update(first_seen=r["first_seen"], first_version=r["first_version"])
            out.append(w)
        return out

    def audit(self, username: str, tenant_id: str | None, action: str, detail: str = "") -> None:
        try:
            with self._lock, self._con() as con:
                con.execute("INSERT INTO audit_log (ts, username, tenant_id, action, detail) VALUES (?, ?, ?, ?, ?)",
                            (time.time(), username, tenant_id, action, detail[:500]))
        except sqlite3.Error:  # auditing must never break a request
            pass

    def recent_audit(self, limit: int = 100) -> list[dict]:
        with self._con() as con:
            return [dict(r) for r in con.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,))]
