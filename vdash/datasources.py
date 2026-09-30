"""Warehouse connectors.

One small interface (``DataSource.query`` / ``DataSource.columns``) with two
implementations:

* ``SQLAlchemySource`` - SQLite (local demo stand-in) and PostgreSQL
  (production), plus any other SQLAlchemy dialect.
* ``ClickHouseSource`` - ClickHouse (production) through the official
  ``clickhouse-connect`` HTTP client.

KPI SQL in the catalog is written once in a portable ANSI subset with
``:name`` bind parameters; connectors translate the binding style when needed.
The dashboard only ever READS from the warehouse: SQLite is opened read-only,
and production credentials should be a read-only role (see ARCHITECTURE.md).
"""
from __future__ import annotations

import re
import sqlite3
import threading
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from .config import as_bool, load_json, resolve_path

_BIND_RE = re.compile(r"(?<![:\w]):([A-Za-z_][A-Za-z0-9_]*)")


def _norm(v: Any) -> Any:
    """Normalise driver-specific values to JSON-friendly Python types."""
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, datetime):
        return v.isoformat(sep=" ")
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace")
    return v


class DataSource:
    dialect = "generic"

    def __init__(self, ds_id: str, cfg: dict):
        self.ds_id = ds_id
        self.cfg = cfg
        self._columns: dict[str, set[str]] | None = None
        self._lock = threading.Lock()

    def query(self, sql: str, params: dict | None = None) -> list[dict]:
        raise NotImplementedError

    def _introspect(self) -> dict[str, set[str]]:
        raise NotImplementedError

    def columns(self, refresh: bool = False) -> dict[str, set[str]]:
        """{table_name: {column, ...}} for every table/view the connection can see."""
        with self._lock:
            if self._columns is None or refresh:
                self._columns = self._introspect()
            return self._columns

    def close(self) -> None:  # pragma: no cover - trivial
        pass


class SQLAlchemySource(DataSource):
    def __init__(self, ds_id: str, cfg: dict, timeout_s: int = 30):
        super().__init__(ds_id, cfg)
        from sqlalchemy import create_engine

        self.dialect = cfg["dialect"]
        self.schema = cfg.get("schema") or None
        if self.dialect == "sqlite":
            path = resolve_path(cfg["path"])
            if not Path(path).exists():
                raise FileNotFoundError(f"SQLite warehouse not found: {path}")
            uri = Path(path).as_uri() + "?mode=ro"  # read-only: the dashboard never writes to the DW

            # v16: keep the warehouse pages in memory once read. On a slow or synced drive (e.g. a Windows data
            # folder that a cloud client or virus scanner watches) every uncached page read costs a lot; the default
            # 2 MB page cache per connection meant the same pages were read from disk again and again.
            cache_mb = int(cfg.get("sqlite_cache_mb", 64))
            mmap_mb = int(cfg.get("sqlite_mmap_mb", 256))

            def _connect() -> sqlite3.Connection:
                con = sqlite3.connect(uri, uri=True, check_same_thread=False, timeout=timeout_s)
                con.execute(f"PRAGMA cache_size = -{cache_mb * 1024}")   # KiB
                con.execute(f"PRAGMA mmap_size = {mmap_mb * 1024 * 1024}")
                con.execute("PRAGMA temp_store = MEMORY")
                con.execute("PRAGMA query_only = 1")
                return con

            # Explicit QueuePool: a bare "sqlite://" URL would get SingletonThreadPool, which closes connections
            # still in use by other threads once more than pool_size threads query (crashes under parallel load).
            from sqlalchemy.pool import QueuePool
            self.engine = create_engine("sqlite://", creator=_connect, poolclass=QueuePool, pool_size=8,
                                        max_overflow=8, pool_timeout=timeout_s)
        else:
            connect_args: dict[str, Any] = {}
            if self.dialect.startswith("postgres"):
                opts = [f"-c statement_timeout={int(timeout_s) * 1000}",
                        "-c default_transaction_read_only=on"]
                if self.schema:
                    opts.append(f"-c search_path={self.schema}")
                connect_args["options"] = " ".join(opts)
            self.engine = create_engine(cfg["url"], connect_args=connect_args,
                                        pool_pre_ping=True, pool_size=int(cfg.get("pool_size", 5)))

    def query(self, sql: str, params: dict | None = None) -> list[dict]:
        from sqlalchemy import text

        with self.engine.connect() as con:
            res = con.execute(text(sql), params or {})
            cols = list(res.keys())
            return [{c: _norm(v) for c, v in zip(cols, row)} for row in res]

    def _introspect(self) -> dict[str, set[str]]:
        from sqlalchemy import inspect

        insp = inspect(self.engine)
        out: dict[str, set[str]] = {}
        names = insp.get_table_names(schema=self.schema) + insp.get_view_names(schema=self.schema)
        for t in names:
            try:
                out[t] = {c["name"] for c in insp.get_columns(t, schema=self.schema)}
            except Exception:  # pragma: no cover - permissions on exotic objects
                continue
        return out

    def close(self) -> None:
        self.engine.dispose()


class ClickHouseSource(DataSource):
    dialect = "clickhouse"

    def __init__(self, ds_id: str, cfg: dict, timeout_s: int = 30):
        super().__init__(ds_id, cfg)
        import clickhouse_connect  # imported lazily: only needed for ClickHouse deployments

        self.database = cfg.get("database", "default")
        self.client = clickhouse_connect.get_client(
            host=cfg["host"], port=int(cfg.get("port", 8443)), username=cfg.get("username", "default"),
            password=cfg.get("password", ""), database=self.database, secure=as_bool(cfg.get("secure"), True),
            settings={"max_execution_time": int(timeout_s), "readonly": 1,
                      # ANSI semantics: SUM/AVG over zero rows -> NULL (shown as "no data"), same as SQLite/Postgres
                      "aggregate_functions_null_for_empty": 1,
                      # LEFT JOIN misses -> NULL (not 0 / ''), as in SQL; used by the full-list exports
                      "join_use_nulls": 1},
        )
        self._client_lock = threading.Lock()

    @staticmethod
    def to_pyformat(sql: str) -> str:
        """':name' binds -> '%(name)s' (clickhouse-connect client-side binding)."""
        return _BIND_RE.sub(lambda m: f"%({m.group(1)})s", sql.replace("%", "%%"))

    def query(self, sql: str, params: dict | None = None) -> list[dict]:
        with self._client_lock:  # clickhouse-connect clients are not thread-safe for concurrent queries
            res = self.client.query(self.to_pyformat(sql), parameters=params or {})
        cols = list(res.column_names)
        return [{c: _norm(v) for c, v in zip(cols, row)} for row in res.result_rows]

    def _introspect(self) -> dict[str, set[str]]:
        rows = self.query("SELECT table, name FROM system.columns WHERE database = :db", {"db": self.database})
        out: dict[str, set[str]] = {}
        for r in rows:
            out.setdefault(r["table"], set()).add(r["name"])
        return out


class DataSourceRegistry:
    """Lazily opens one connector per configured datasource id."""

    def __init__(self, timeout_s: int = 30):
        self.timeout_s = timeout_s
        self.cfg = load_json("datasources.json").get("datasources", {})
        self._open: dict[str, DataSource] = {}
        self._lock = threading.Lock()

    def get(self, ds_id: str) -> DataSource:
        with self._lock:
            if ds_id in self._open:
                return self._open[ds_id]
            cfg = self.cfg.get(ds_id)
            if cfg is None:
                raise KeyError(f"Unknown datasource '{ds_id}'")
            if not as_bool(cfg.get("enabled"), True):
                raise RuntimeError(f"Datasource '{ds_id}' is disabled in datasources.json")
            if cfg["dialect"] == "clickhouse":
                src: DataSource = ClickHouseSource(ds_id, cfg, self.timeout_s)
            else:
                src = SQLAlchemySource(ds_id, cfg, self.timeout_s)
            self._open[ds_id] = src
            return src

    def close_all(self) -> None:
        with self._lock:
            for s in self._open.values():
                s.close()
            self._open.clear()
