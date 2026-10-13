"""Warehouse connectors.

One small interface (``DataSource.query`` / ``DataSource.columns``) with two
implementations:

* ``SQLAlchemySource`` - SQLite (local demo stand-in) and PostgreSQL
  (production), plus any other SQLAlchemy dialect.
* ``ClickHouseSource`` - ClickHouse (production) through the official
  ``clickhouse-connect`` HTTP client.
* ``GenBiApiSource`` (v78) - the PostgreSQL warehouse on the VM, reached through its read-only HTTPS gateway
  (dialect ``postgresql_api``). Used because the hosting provider does not expose the Postgres port to the internet
  (Render cannot connect directly); the gateway runs the SQL as a read-only role.

KPI SQL in the catalog is written once in a portable ANSI subset with
``:name`` bind parameters; connectors translate the binding style when needed.
The dashboard only ever READS from the warehouse: SQLite is opened read-only,
and production credentials should be a read-only role (see ARCHITECTURE.md).
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.request
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


def warn_if_no_stats(path: str) -> None:
    """The dashboard only reads the warehouse. Read performance (covering indexes + planner statistics) is part of
    the warehouse itself: the Step 4.1 loader applies it after every load, and its "Optimize warehouse" button does it
    for a warehouse loaded earlier. This only logs a warning when the statistics are missing."""
    import logging
    try:
        con = sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True, timeout=5)
        try:
            ok = con.execute("SELECT 1 FROM sqlite_master WHERE name = 'sqlite_stat1'").fetchone() and \
                con.execute("SELECT 1 FROM sqlite_stat1 LIMIT 1").fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return
    if not ok:
        logging.getLogger("vdash.datasources").warning(
            "warehouse %s has no query statistics/indexes for reading - dashboards over large data will be slow. "
            "Run 'Optimize warehouse for the dashboard' in Step 4.1.", path)


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
            warn_if_no_stats(path)

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
            self._stmt_limit_s = float(cfg.get("statement_timeout_seconds", 180))
            self._gate = threading.BoundedSemaphore(int(cfg.get("max_concurrent_queries", 4)))
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

        gate = getattr(self, "_gate", None)
        if gate is not None:
            gate.acquire()   # v52: at most N warehouse queries at a time (more only thrash the page cache)
        try:
            with self.engine.connect() as con:
                raw = con.connection.driver_connection if self.dialect == "sqlite" else None
                if raw is not None and self._stmt_limit_s > 0:
                    deadline = time.monotonic() + self._stmt_limit_s

                    def _stop() -> int:
                        return 1 if time.monotonic() > deadline else 0   # non-zero aborts the query
                    raw.set_progress_handler(_stop, 200000)
                try:
                    res = con.execute(text(sql), params or {})
                    cols = list(res.keys())
                    return [{c: _norm(v) for c, v in zip(cols, row)} for row in res]
                finally:
                    if raw is not None and self._stmt_limit_s > 0:
                        raw.set_progress_handler(None, 0)
        finally:
            if gate is not None:
                gate.release()

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


class GenBiApiSource(DataSource):
    """v78: PostgreSQL through the Gen-BI Database Gateway (read-only HTTPS, ``POST <base>/api/v1/query``).

    * ``:name`` binds become the gateway's positional ``$1, $2, ...`` parameters (a repeated name reuses its number).
    * The gateway types parameters strictly and JSON has no date type, so a ``date`` / ``datetime`` bind is sent as
      an ISO string and cast in the SQL: ``CAST(CAST($n AS text) AS date)``. ``dialect`` is ``"postgresql"`` so the
      engine passes real dates (not ISO strings, as for SQLite).
    * The gateway returns at most 5000 rows per response; a larger result is fetched page by page (``_page_all``), so
      per-customer lists and full exports are complete, not silently cut.
    * Config: ``base_url`` / ``api_key`` (normally ``${GENBI_API_BASE_URL}`` / ``${GENBI_API_KEY}``), optional
      ``schema``, ``max_rows`` (<= 5000), ``max_concurrent_queries``, ``statement_timeout_seconds``, ``retries``.
    """
    dialect = "postgresql"
    parallel_probe = True   # refresher.probe_availability runs its probes side by side (v78)
    MAX_ROWS = 5000
    _RETRY_CODES = {429, 502, 503, 504}

    def __init__(self, ds_id: str, cfg: dict, timeout_s: int = 30):
        super().__init__(ds_id, cfg)
        base = str(cfg.get("base_url") or os.environ.get("GENBI_API_BASE_URL") or "").strip().rstrip("/")
        key = str(cfg.get("api_key") or os.environ.get("GENBI_API_KEY") or "").strip()
        if not base or not key:
            raise RuntimeError(
                f"Datasource '{ds_id}': set GENBI_API_BASE_URL and GENBI_API_KEY (config/local.env locally, "
                f"the service's Environment settings on Render).")
        if not base.startswith(("https://", "http://")):
            base = "https://" + base
        if base.endswith("/api/v1/query"):
            self.url = base
        elif base.endswith("/api/v1"):
            self.url = base + "/query"
        else:
            self.url = base + "/api/v1/query"
        self._key = key
        self.schema = cfg.get("schema") or "public"
        self.max_rows = max(1, min(int(cfg.get("max_rows", self.MAX_ROWS)), self.MAX_ROWS))
        self._http_timeout = max(float(timeout_s), float(cfg.get("statement_timeout_seconds", 60)))
        self._retries = int(cfg.get("retries", 2))
        self._gate = threading.BoundedSemaphore(int(cfg.get("max_concurrent_queries", 6)))
        self.truncated_queries = 0
        self._log = logging.getLogger("vdash.datasources")

    @staticmethod
    def to_positional(sql: str, params: dict | None) -> tuple[str, list]:
        """':name' binds -> '$n' (+ the values list). Names without a value are left untouched."""
        params = params or {}
        slots: dict[str, int] = {}
        kinds: dict[str, str] = {}
        values: list = []

        def repl(m: re.Match) -> str:
            name = m.group(1)
            if name not in params:
                return m.group(0)
            if name not in slots:
                v = params[name]
                kind = ""
                if isinstance(v, datetime):
                    v, kind = v.isoformat(sep=" "), "timestamp"
                elif isinstance(v, date):
                    v, kind = v.isoformat(), "date"
                elif isinstance(v, Decimal):
                    v = float(v)
                values.append(v)
                slots[name], kinds[name] = len(values), kind
            n, kind = slots[name], kinds[name]
            return f"CAST(CAST(${n} AS text) AS {kind})" if kind else f"${n}"

        return _BIND_RE.sub(repl, sql), values

    def _post(self, body: dict) -> dict:
        data = json.dumps(body, default=str).encode("utf-8")
        last: Exception | None = None
        for attempt in range(self._retries + 1):
            req = urllib.request.Request(self.url, data=data, method="POST", headers={
                "X-API-Key": self._key, "Content-Type": "application/json", "User-Agent": "vdash/78"})
            try:
                with urllib.request.urlopen(req, timeout=self._http_timeout) as r:
                    return json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:600]
                try:
                    detail = json.loads(detail).get("detail", detail)
                except Exception:
                    pass
                last = RuntimeError(f"warehouse gateway HTTP {e.code}: {detail}")
                if e.code not in self._RETRY_CODES:
                    raise last
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                last = RuntimeError(f"warehouse gateway unreachable: {e}")
            if attempt < self._retries:
                time.sleep(0.5 * (3 ** attempt))
        raise last  # type: ignore[misc]

    @staticmethod
    def _top_level(sql: str) -> str:
        """The SQL with string literals and everything inside parentheses blanked out (to look for a top-level
        ORDER BY / LIMIT without being fooled by subqueries)."""
        out, depth, i, n = [], 0, 0, len(sql)
        while i < n:
            ch = sql[i]
            if ch == "'":
                i += 1
                while i < n and not (sql[i] == "'" and sql[i + 1:i + 2] != "'"):
                    i += 2 if sql[i] == "'" else 1
                i += 1
                out.append(" ")
                continue
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            out.append(ch if depth == 0 and ch != ")" else " ")
            i += 1
        return "".join(out)

    def _call(self, text_sql: str, values: list, limit: int | None = None) -> dict:
        body: dict[str, Any] = {"sql": text_sql, "max_rows": limit or self.max_rows}
        if values:
            body["params"] = values
        with self._gate:
            res = self._post(body)
        if not res.get("success", False):
            raise RuntimeError(f"warehouse gateway error: {res.get('detail') or res}")
        return res

    def query(self, sql: str, params: dict | None = None) -> list[dict]:
        text_sql, values = self.to_positional(sql, params)
        res = self._call(text_sql, values)
        rows = res.get("rows", [])
        if res.get("truncated"):
            rows = self._page_all(text_sql, values, res)
        return [{c: _norm(v) for c, v in row.items()} for row in rows]

    def _page_all(self, text_sql: str, values: list, first: dict) -> list[dict]:
        """The gateway caps a response at 5000 rows. Per-entity lists (e.g. revenue per customer, for the 'top 20%
        share') and full-list exports can be larger, and a silently cut list gives WRONG numbers, so fetch the rest page
        by page: LIMIT/OFFSET on the statement itself when it already has a top-level ORDER BY (and no LIMIT), else on a
        wrapper ordered by every column (a total order, so the pages never overlap or skip). A statement with its own
        top-level LIMIT is deliberately bounded and is not paged."""
        top = self._top_level(text_sql).upper()
        has_limit = re.search(r"\bLIMIT\s+\d+\s*$", top.strip()) is not None
        has_order = re.search(r"\bORDER\s+BY\b", top) is not None
        if has_limit:
            self.truncated_queries += 1
            self._log.warning("warehouse result truncated at %s rows (gateway cap): %s",
                              first.get("row_count"), " ".join(text_sql.split())[:160])
            return first.get("rows", [])
        size, max_pages = self.max_rows, int(self.cfg.get("max_pages", 60))
        if has_order:
            base, rows, offset = text_sql.rstrip().rstrip(";"), list(first.get("rows", [])), size
        else:
            ncol = max(1, len(first.get("columns") or []))
            base = f"SELECT * FROM ({text_sql.rstrip().rstrip(';')}) AS _q ORDER BY {', '.join(str(i) for i in range(1, ncol + 1))}"
            rows, offset = [], 0
        for _ in range(max_pages):
            res = self._call(f"{base} LIMIT {size} OFFSET {offset}", values)
            page = res.get("rows", [])
            rows.extend(page)
            if len(page) < size:
                return rows
            offset += size
        self.truncated_queries += 1
        self._log.warning("warehouse result still truncated after %s pages: %s", max_pages, " ".join(text_sql.split())[:160])
        return rows

    def _introspect(self) -> dict[str, set[str]]:
        rows = self.query(
            "SELECT table_name AS t, string_agg(column_name, ',') AS c FROM information_schema.columns "
            "WHERE table_schema = :schema GROUP BY table_name", {"schema": self.schema})
        return {r["t"]: set(str(r["c"]).split(",")) for r in rows}


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
            elif cfg["dialect"] == "postgresql_api":
                src = GenBiApiSource(ds_id, cfg, self.timeout_s)
            else:
                src = SQLAlchemySource(ds_id, cfg, self.timeout_s)
            self._open[ds_id] = src
            return src

    def close_all(self) -> None:
        with self._lock:
            for s in self._open.values():
                s.close()
            self._open.clear()
