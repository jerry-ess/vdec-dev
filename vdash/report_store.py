"""Durable store for reports: saved report settings, schedules and report history (v25).

Unlike ``state_store`` (availability, warnings, audit - rebuilt on every deploy), this data is created by users and
must survive restarts and redeploys. So it can live in an external PostgreSQL database (Aiven, Neon, ...) while the
dashboard itself keeps running on a server with a temporary disk (Render free).

* ``report_store.url`` empty  -> SQLite file ``report_store.sqlite_path`` (default ``runtime/reports.db``; local use).
* ``report_store.url`` = ``postgres://user:password@host:port/db?sslmode=require`` -> PostgreSQL. With a CA
  certificate (``ca_cert`` = file path, or ``ca_cert_pem`` = the certificate text, e.g. a Render environment
  variable) the connection is encrypted AND the server's certificate and host name are checked (``verify-full``).

The store never blocks start-up: the database is first contacted when a report feature needs it; a failure
raises ``ReportStoreError`` with a plain message and ``status()`` reports it to platform admins.

Every read and write is scoped by ``tenant_id`` (defence in depth on top of the API's tenant guard).
Schedules are claimed atomically (``claim_schedule``) so two dashboard processes never send the same report twice.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import zlib

from sqlalchemy import (Boolean, Column, Float, Index, Integer, LargeBinary, MetaData, PrimaryKeyConstraint, String,
                        Table, Text, and_, create_engine, delete, func, insert, select, update)
from sqlalchemy.engine import Engine

log = logging.getLogger("vdash.reports")
SCHEMA_VERSION = 1

md = MetaData()
report_meta = Table("report_meta", md, Column("key", String(64), primary_key=True), Column("value", Text))

report_configs = Table(
    "report_configs", md,
    Column("id", String(36), primary_key=True),
    Column("tenant_id", String(128), nullable=False),
    Column("created_by", String(128), nullable=False),
    Column("name", String(200), nullable=False),
    Column("profile", String(64)),                         # base profile or 'custom'
    Column("period_preset", String(64)),                   # 'last_30_days', 'prev_week', 'custom', ...
    Column("period_start", String(10)),                    # YYYY-MM-DD, custom only
    Column("period_end", String(10)),
    Column("compare", String(16), nullable=False, default="previous"),   # previous | yoy | none
    Column("blocks_json", Text, nullable=False),           # JSON list of block ids
    Column("use_llm_narrative", Boolean, nullable=False, default=False),
    Column("use_llm_playbook", Boolean, nullable=False, default=False),
    Column("output_format", String(8), nullable=False, default="pdf"),     # pdf | html | xlsx
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
)
Index("ix_report_configs_tenant", report_configs.c.tenant_id)

report_schedules = Table(
    "report_schedules", md,
    Column("id", String(36), primary_key=True),
    Column("config_id", String(36), nullable=False),
    Column("tenant_id", String(128), nullable=False),
    Column("created_by", String(128), nullable=False),     # role is looked up at send time, never stored
    Column("frequency", String(16), nullable=False),       # weekly | monthly | quarterly
    Column("day_of_week", Integer),                        # 0=Mon..6=Sun (weekly)
    Column("day_of_month", Integer),                       # 1..28 (monthly / quarterly)
    Column("send_time", String(5), nullable=False, default="08:00"),   # HH:MM, tenant time zone
    Column("recipients_json", Text, nullable=False),       # JSON [{"type": "email", "address": ...}]
    Column("enabled", Boolean, nullable=False, default=True),
    Column("next_run", Float),                             # epoch seconds
    Column("last_run", Float),
    Column("last_status", String(16)),                     # sent | failed | skipped
    Column("last_error", Text),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
)
Index("ix_report_schedules_due", report_schedules.c.enabled, report_schedules.c.next_run)
Index("ix_report_schedules_tenant", report_schedules.c.tenant_id)

report_history = Table(
    "report_history", md,
    Column("id", String(36), primary_key=True),
    Column("tenant_id", String(128), nullable=False),
    Column("config_id", String(36)),                       # NULL for one-off reports
    Column("schedule_id", String(36)),
    Column("period_start", String(10), nullable=False),
    Column("period_end", String(10), nullable=False),
    Column("language", String(8), nullable=False, default="en"),
    Column("data_version", String(128)),                   # warehouse version the report was made from
    Column("spec_json", Text, nullable=False),             # everything needed to make the same report again
    Column("blocks_json", Text),                           # blocks actually included
    Column("findings_json", Text),                         # finding / warning ids, for "changes since last report"
    Column("generated_at", Float, nullable=False),
    Column("generated_by", String(128), nullable=False),   # user name or 'scheduler'
    Column("status", String(16), nullable=False, default="completed"),  # completed | failed | generating
    Column("error", Text),
    Column("llm_used", Boolean, nullable=False, default=False),
    Column("llm_cost", Float, nullable=False, default=0.0),
)
Index("ix_report_history_tenant_time", report_history.c.tenant_id, report_history.c.generated_at)

# v34: the reports a user created, kept exactly as made (the composed document, compressed) - at most
# ``keep`` per user and business, oldest removed first. One row per report_history row of the same id.
report_documents = Table(
    "report_documents", md,
    Column("id", String(36), primary_key=True),            # = report_history.id
    Column("tenant_id", String(128), nullable=False),
    Column("created_by", String(128), nullable=False),
    Column("seq", Integer, nullable=False),                # the user's running report number in this business
    Column("name", String(300), nullable=False),           # automatic, in the report's language
    Column("spec_hash", String(64), nullable=False),       # same report (parts, period, comparison, language)?
    Column("doc_gz", LargeBinary, nullable=False),         # zlib-compressed document JSON
    Column("size", Integer, nullable=False),
    Column("created_at", Float, nullable=False),
)
Index("ix_report_documents_owner", report_documents.c.tenant_id, report_documents.c.created_by,
      report_documents.c.created_at)

# v34: reports created per business and month (the business's own time zone) - for the plan's monthly limit.
# Never decreases when old reports are removed, so the count stays accurate.
report_usage = Table(
    "report_usage", md,
    Column("tenant_id", String(128), nullable=False),
    Column("month", String(7), nullable=False),            # YYYY-MM
    Column("count", Integer, nullable=False, default=0),
    PrimaryKeyConstraint("tenant_id", "month"),
)

# v37: AI answers, kept so the same request (same report, data, language, prompt, model) is never paid twice,
# and AI usage per business and month (calls, tokens, cost) for plans and cost tracking.
report_llm_cache = Table(
    "report_llm_cache", md,
    Column("key", String(64), primary_key=True),
    Column("tenant_id", String(128), nullable=False),
    Column("answer_json", Text, nullable=False),
    Column("created_at", Float, nullable=False),
)
Index("ix_report_llm_cache_time", report_llm_cache.c.created_at)

report_llm_usage = Table(
    "report_llm_usage", md,
    Column("tenant_id", String(128), nullable=False),
    Column("month", String(7), nullable=False),
    Column("calls", Integer, nullable=False, default=0),
    Column("failures", Integer, nullable=False, default=0),
    Column("tokens_in", Integer, nullable=False, default=0),
    Column("tokens_out", Integer, nullable=False, default=0),
    Column("cost_usd", Float, nullable=False, default=0.0),
    PrimaryKeyConstraint("tenant_id", "month"),
)
LLM_CACHE_DAYS = 120

_JSON = {"blocks_json": "blocks", "recipients_json": "recipients", "spec_json": "spec", "findings_json": "findings"}


class ReportStoreError(RuntimeError):
    """The report database could not be used; the message is safe to show to an administrator."""


def _now() -> float:
    return time.time()


def _row(r) -> dict:
    d = dict(r._mapping)
    for col, key in _JSON.items():
        if col in d:
            raw = d.pop(col)
            d[key] = json.loads(raw) if raw else None
    return d


def _to_db(fields: dict) -> dict:
    out = {}
    for k, v in fields.items():
        col = next((c for c, key in _JSON.items() if key == k), None)
        out[col or k] = json.dumps(v, ensure_ascii=False) if col else v
    return out


def normalize_url(url: str) -> str:
    """postgres:// and postgresql:// -> SQLAlchemy's psycopg 3 driver; anything else unchanged."""
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def redact(url: str) -> str:
    """URL without the password, for logs and the admin page."""
    try:
        p = urlsplit(url)
        if p.password:
            netloc = p.netloc.replace(":" + p.password + "@", ":*****@")
            return urlunsplit((p.scheme, netloc, p.path, p.query, p.fragment))
    except ValueError:
        pass
    return url


class ReportStore:
    def __init__(self, url: str = "", sqlite_path: Path | None = None, ca_cert: str = "", ca_cert_pem: str = "",
                 sslmode: str = "verify-full", pool_size: int = 3, connect_timeout: int = 10):
        self._lock = threading.RLock()   # ensure_schema -> engine take it in turn
        self._ready = False
        self._engine: Engine | None = None
        self.error: str | None = None
        if url:
            self.backend = "postgresql"
            self.url = normalize_url(url)
            if not self.url.startswith("postgresql+psycopg://"):
                raise ReportStoreError("report_store.url must be a PostgreSQL URL (postgres://...)")
            self._connect_args, self._url = self._ssl(self.url, ca_cert, ca_cert_pem, sslmode, connect_timeout)
            self._engine_kw = dict(pool_size=pool_size, max_overflow=2, pool_pre_ping=True, pool_recycle=300,
                                   connect_args=self._connect_args)
        else:
            self.backend = "sqlite"
            path = Path(sqlite_path or "runtime/reports.db")
            path.parent.mkdir(parents=True, exist_ok=True)
            self._url = f"sqlite:///{path.as_posix()}"
            self.url = self._url
            self._engine_kw = dict(connect_args={"timeout": 10, "check_same_thread": False})

    # ------------------------------------------------------------ connection
    @staticmethod
    def _ssl(url: str, ca_cert: str, ca_cert_pem: str, sslmode: str, connect_timeout: int) -> tuple[dict, str]:
        """Strip sslmode/sslrootcert from the URL and pass them explicitly, so the configured CA always applies."""
        p = urlsplit(url)
        q = dict(parse_qsl(p.query))
        url_mode = q.pop("sslmode", None)
        q.pop("sslrootcert", None)
        args: dict[str, Any] = {"connect_timeout": connect_timeout, "application_name": "veridex-dashboard"}
        ca_file = ""
        if ca_cert_pem and ca_cert_pem.strip():
            pem = ca_cert_pem.replace("\\n", "\n").strip() + "\n"   # env vars may carry the PEM with literal \n
            if "BEGIN CERTIFICATE" not in pem:
                raise ReportStoreError("report_store.ca_cert_pem does not look like a PEM certificate")
            digest = hashlib.sha256(pem.encode()).hexdigest()[:12]
            ca_file = os.path.join(tempfile.gettempdir(), f"vdash-report-db-ca-{digest}.pem")
            if not os.path.exists(ca_file):
                with open(ca_file, "w", encoding="ascii") as fh:
                    fh.write(pem)
        elif ca_cert:
            from .config import resolve_path
            path = resolve_path(ca_cert)
            if not path.is_file():
                raise ReportStoreError(f"CA certificate file not found: {path}")
            ca_file = str(path)
        if ca_file:
            args["sslmode"] = sslmode or "verify-full"
            args["sslrootcert"] = ca_file
        else:
            # no CA: still encrypted, but the server is not verified
            args["sslmode"] = url_mode or "require"
            log.warning("report_store: no CA certificate configured - the connection is encrypted (sslmode=%s) "
                        "but the server's identity is not checked", args["sslmode"])
        return args, urlunsplit((p.scheme, p.netloc, p.path, urlencode(q), p.fragment))

    @property
    def engine(self) -> Engine:
        if self._engine is None:
            with self._lock:
                if self._engine is None:
                    self._engine = create_engine(self._url, future=True, **self._engine_kw)
        return self._engine

    def ensure_schema(self) -> None:
        """Create missing tables (idempotent). Called automatically before the first read or write."""
        if self._ready:
            return
        with self._lock:
            if self._ready:
                return
            try:
                md.create_all(self.engine, checkfirst=True)
                with self.engine.begin() as con:
                    have = con.execute(select(report_meta.c.value).where(report_meta.c.key == "schema_version")).scalar()
                    if have is None:
                        con.execute(insert(report_meta).values(key="schema_version", value=str(SCHEMA_VERSION)))
                    elif int(have) > SCHEMA_VERSION:
                        raise ReportStoreError(f"the report database has schema version {have}; this dashboard "
                                               f"understands up to {SCHEMA_VERSION} - update the dashboard")
                self._ready, self.error = True, None
            except ReportStoreError as exc:
                self.error = str(exc)
                raise
            except Exception as exc:   # noqa: BLE001 - driver errors vary; report them plainly
                self.error = f"{type(exc).__name__}: {str(exc).splitlines()[0][:300] if str(exc) else ''}"
                log.error("report_store %s unavailable: %s", redact(self.url), self.error)
                raise ReportStoreError(f"The report database is not reachable ({self.error})") from exc

    def _begin(self):
        self.ensure_schema()
        return self.engine.begin()

    def status(self) -> dict:
        """For /api/admin/status: backend, where (no password), whether it works now."""
        out = {"backend": self.backend, "url": redact(self.url),
               "verified_tls": bool(getattr(self, "_connect_args", {}).get("sslrootcert"))}
        try:
            t0 = time.perf_counter()
            with self._begin() as con:
                out["configs"] = con.execute(select(func.count()).select_from(report_configs)).scalar()
                out["schedules"] = con.execute(select(func.count()).select_from(report_schedules)).scalar()
                out["history"] = con.execute(select(func.count()).select_from(report_history)).scalar()
                out["documents"] = con.execute(select(func.count()).select_from(report_documents)).scalar()
            out.update(ok=True, ms=round((time.perf_counter() - t0) * 1000))
        except ReportStoreError as exc:
            out.update(ok=False, error=str(exc))
        return out

    def close(self) -> None:
        if self._engine is not None:
            self._engine.dispose()

    # ------------------------------------------------------------ saved report settings
    CONFIG_FIELDS = ("name", "profile", "period_preset", "period_start", "period_end", "compare", "blocks",
                     "use_llm_narrative", "use_llm_playbook", "output_format")

    def create_config(self, tenant_id: str, created_by: str, **fields) -> dict:
        bad = set(fields) - set(self.CONFIG_FIELDS)
        if bad:
            raise ValueError(f"unknown report config field(s): {', '.join(sorted(bad))}")
        if not fields.get("name") or not isinstance(fields.get("blocks"), list):
            raise ValueError("a report config needs a name and a list of blocks")
        now = _now()
        row = {"id": str(uuid.uuid4()), "tenant_id": tenant_id, "created_by": created_by, "created_at": now,
               "updated_at": now, "compare": "previous", "output_format": "pdf", "use_llm_narrative": False,
               "use_llm_playbook": False, **fields}
        with self._begin() as con:
            con.execute(insert(report_configs).values(**_to_db(row)))
        return self.get_config(tenant_id, row["id"])

    def get_config(self, tenant_id: str, config_id: str) -> dict | None:
        with self._begin() as con:
            r = con.execute(select(report_configs).where(and_(report_configs.c.tenant_id == tenant_id,
                                                              report_configs.c.id == config_id))).first()
        return _row(r) if r else None

    def list_configs(self, tenant_id: str) -> list[dict]:
        with self._begin() as con:
            rs = con.execute(select(report_configs).where(report_configs.c.tenant_id == tenant_id)
                             .order_by(report_configs.c.name)).all()
        return [_row(r) for r in rs]

    def update_config(self, tenant_id: str, config_id: str, **fields) -> dict | None:
        bad = set(fields) - set(self.CONFIG_FIELDS)
        if bad:
            raise ValueError(f"unknown report config field(s): {', '.join(sorted(bad))}")
        with self._begin() as con:
            n = con.execute(update(report_configs).where(and_(report_configs.c.tenant_id == tenant_id,
                                                              report_configs.c.id == config_id))
                            .values(**_to_db(fields), updated_at=_now())).rowcount
        return self.get_config(tenant_id, config_id) if n else None

    def delete_config(self, tenant_id: str, config_id: str) -> bool:
        """Deletes the config and its schedules (history rows are kept, with their own copy of the spec)."""
        with self._begin() as con:
            con.execute(delete(report_schedules).where(and_(report_schedules.c.tenant_id == tenant_id,
                                                            report_schedules.c.config_id == config_id)))
            return con.execute(delete(report_configs).where(and_(report_configs.c.tenant_id == tenant_id,
                                                                 report_configs.c.id == config_id))).rowcount > 0

    # ------------------------------------------------------------ schedules
    SCHEDULE_FIELDS = ("frequency", "day_of_week", "day_of_month", "send_time", "recipients", "enabled", "next_run")

    def create_schedule(self, tenant_id: str, config_id: str, created_by: str, **fields) -> dict:
        bad = set(fields) - set(self.SCHEDULE_FIELDS)
        if bad:
            raise ValueError(f"unknown schedule field(s): {', '.join(sorted(bad))}")
        if self.get_config(tenant_id, config_id) is None:
            raise ValueError("report config not found for this tenant")
        if fields.get("frequency") not in ("weekly", "monthly", "quarterly"):
            raise ValueError("frequency must be weekly, monthly or quarterly")
        now = _now()
        row = {"id": str(uuid.uuid4()), "config_id": config_id, "tenant_id": tenant_id, "created_by": created_by,
               "send_time": "08:00", "recipients": [], "enabled": True, "created_at": now, "updated_at": now, **fields}
        with self._begin() as con:
            con.execute(insert(report_schedules).values(**_to_db(row)))
        return self.get_schedule(tenant_id, row["id"])

    def get_schedule(self, tenant_id: str, schedule_id: str) -> dict | None:
        with self._begin() as con:
            r = con.execute(select(report_schedules).where(and_(report_schedules.c.tenant_id == tenant_id,
                                                                report_schedules.c.id == schedule_id))).first()
        return _row(r) if r else None

    def list_schedules(self, tenant_id: str) -> list[dict]:
        with self._begin() as con:
            rs = con.execute(select(report_schedules).where(report_schedules.c.tenant_id == tenant_id)
                             .order_by(report_schedules.c.created_at)).all()
        return [_row(r) for r in rs]

    def update_schedule(self, tenant_id: str, schedule_id: str, **fields) -> dict | None:
        bad = set(fields) - set(self.SCHEDULE_FIELDS)
        if bad:
            raise ValueError(f"unknown schedule field(s): {', '.join(sorted(bad))}")
        with self._begin() as con:
            n = con.execute(update(report_schedules).where(and_(report_schedules.c.tenant_id == tenant_id,
                                                                report_schedules.c.id == schedule_id))
                            .values(**_to_db(fields), updated_at=_now())).rowcount
        return self.get_schedule(tenant_id, schedule_id) if n else None

    def delete_schedule(self, tenant_id: str, schedule_id: str) -> bool:
        with self._begin() as con:
            return con.execute(delete(report_schedules).where(and_(report_schedules.c.tenant_id == tenant_id,
                                                                   report_schedules.c.id == schedule_id))).rowcount > 0

    def due_schedules(self, now: float | None = None) -> list[dict]:
        """Enabled schedules whose next run is due (all tenants - for the scheduler only)."""
        now = _now() if now is None else now
        with self._begin() as con:
            rs = con.execute(select(report_schedules).where(and_(report_schedules.c.enabled.is_(True),
                                                                 report_schedules.c.next_run.is_not(None),
                                                                 report_schedules.c.next_run <= now))
                             .order_by(report_schedules.c.next_run)).all()
        return [_row(r) for r in rs]

    def claim_schedule(self, schedule_id: str, expected_next_run: float, new_next_run: float) -> bool:
        """Atomically move a due schedule to its next run. True for exactly one caller - that one sends it."""
        with self._begin() as con:
            return con.execute(update(report_schedules)
                               .where(and_(report_schedules.c.id == schedule_id,
                                           report_schedules.c.next_run == expected_next_run))
                               .values(next_run=new_next_run, last_run=_now(), updated_at=_now())).rowcount == 1

    def record_schedule_result(self, schedule_id: str, status: str, error: str | None = None) -> None:
        with self._begin() as con:
            con.execute(update(report_schedules).where(report_schedules.c.id == schedule_id)
                        .values(last_status=status, last_error=error, updated_at=_now()))

    # ------------------------------------------------------------ history
    HISTORY_FIELDS = ("config_id", "schedule_id", "period_start", "period_end", "language", "data_version", "spec",
                      "blocks", "findings", "status", "error", "llm_used", "llm_cost")

    def add_history(self, tenant_id: str, generated_by: str, **fields) -> dict:
        bad = set(fields) - set(self.HISTORY_FIELDS)
        if bad:
            raise ValueError(f"unknown history field(s): {', '.join(sorted(bad))}")
        if not isinstance(fields.get("spec"), dict):
            raise ValueError("history needs the report spec (dict)")
        row = {"id": str(uuid.uuid4()), "tenant_id": tenant_id, "generated_by": generated_by, "generated_at": _now(),
               "language": "en", "status": "completed", "llm_used": False, "llm_cost": 0.0, **fields}
        with self._begin() as con:
            con.execute(insert(report_history).values(**_to_db(row)))
        return self.get_history(tenant_id, row["id"])

    def get_history(self, tenant_id: str, history_id: str) -> dict | None:
        with self._begin() as con:
            r = con.execute(select(report_history).where(and_(report_history.c.tenant_id == tenant_id,
                                                              report_history.c.id == history_id))).first()
        return _row(r) if r else None

    def list_history(self, tenant_id: str, limit: int = 50, config_id: str | None = None) -> list[dict]:
        q = select(report_history).where(report_history.c.tenant_id == tenant_id)
        if config_id:
            q = q.where(report_history.c.config_id == config_id)
        with self._begin() as con:
            rs = con.execute(q.order_by(report_history.c.generated_at.desc()).limit(limit)).all()
        return [_row(r) for r in rs]

    def update_history(self, tenant_id: str, history_id: str, **fields) -> bool:
        bad = set(fields) - set(self.HISTORY_FIELDS)
        if bad:
            raise ValueError(f"unknown history field(s): {', '.join(sorted(bad))}")
        with self._begin() as con:
            return con.execute(update(report_history).where(and_(report_history.c.tenant_id == tenant_id,
                                                                 report_history.c.id == history_id))
                               .values(**_to_db(fields))).rowcount == 1


    # ------------------------------------------------------------ v34: created reports + monthly usage
    def _insert_ignore(self, table, **values):
        name = self.engine.dialect.name
        if name == "postgresql":
            from sqlalchemy.dialects.postgresql import insert as _ins
        elif name == "sqlite":
            from sqlalchemy.dialects.sqlite import insert as _ins
        else:   # pragma: no cover - only SQLite and PostgreSQL are supported
            raise ReportStoreError(f"unsupported database {name}")
        return _ins(table).values(**values).on_conflict_do_nothing()

    def usage(self, tenant_id: str, month: str) -> int:
        with self._begin() as con:
            return int(con.execute(select(report_usage.c.count).where(and_(report_usage.c.tenant_id == tenant_id,
                                                                           report_usage.c.month == month))).scalar() or 0)

    def reserve_report(self, tenant_id: str, month: str, limit: int | None) -> bool:
        """Count one report for the business and month - atomically, only while below ``limit`` (None = no limit).
        False = the limit is reached. Two requests at the same moment can never both take the last report."""
        with self._begin() as con:
            con.execute(self._insert_ignore(report_usage, tenant_id=tenant_id, month=month, count=0))
            cond = and_(report_usage.c.tenant_id == tenant_id, report_usage.c.month == month)
            if limit is not None:
                cond = and_(cond, report_usage.c.count < int(limit))
            return con.execute(update(report_usage).where(cond).values(count=report_usage.c.count + 1)).rowcount == 1

    def release_report(self, tenant_id: str, month: str) -> None:
        """Give back a reserved report when it could not be made after all."""
        with self._begin() as con:
            con.execute(update(report_usage).where(and_(report_usage.c.tenant_id == tenant_id, report_usage.c.month == month,
                                                        report_usage.c.count > 0)).values(count=report_usage.c.count - 1))

    def find_report(self, tenant_id: str, created_by: str, spec_hash: str, data_version: str | None) -> dict | None:
        """The user's stored report with the same parts, period, comparison and language made from the same data."""
        q = (select(report_documents.c.id).select_from(report_documents.join(report_history, report_history.c.id == report_documents.c.id))
             .where(and_(report_documents.c.tenant_id == tenant_id, report_documents.c.created_by == created_by,
                         report_documents.c.spec_hash == spec_hash,
                         report_history.c.data_version.is_(None) if data_version is None
                         else report_history.c.data_version == data_version))
             .order_by(report_documents.c.created_at.desc()).limit(1))
        with self._begin() as con:
            rid = con.execute(q).scalar()
        return self.get_report(tenant_id, created_by, rid, with_doc=False) if rid else None

    def save_report(self, tenant_id: str, created_by: str, doc: dict, spec_hash: str, name_for, keep: int,
                    **history) -> tuple[dict, int]:
        """Store a created report (history row + compressed document) and keep only the user's newest ``keep``.
        ``name_for(seq)`` gives the automatic name. Returns (the report, number of old reports removed)."""
        raw = json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        gz = zlib.compress(raw, 6)
        rid, now = str(uuid.uuid4()), _now()
        hrow = {"id": rid, "tenant_id": tenant_id, "generated_by": created_by, "generated_at": now, "language": "en",
                "status": "completed", "llm_used": False, "llm_cost": 0.0, **history}
        removed = 0
        with self._begin() as con:
            seq = int(con.execute(select(func.max(report_documents.c.seq)).where(and_(
                report_documents.c.tenant_id == tenant_id, report_documents.c.created_by == created_by))).scalar() or 0) + 1
            con.execute(insert(report_history).values(**_to_db(hrow)))
            con.execute(insert(report_documents).values(id=rid, tenant_id=tenant_id, created_by=created_by, seq=seq,
                                                        name=str(name_for(seq))[:300], spec_hash=spec_hash, doc_gz=gz,
                                                        size=len(gz), created_at=now))
            old = [r[0] for r in con.execute(
                select(report_documents.c.id).where(and_(report_documents.c.tenant_id == tenant_id,
                                                         report_documents.c.created_by == created_by))
                .order_by(report_documents.c.created_at.desc(), report_documents.c.seq.desc()).offset(max(int(keep), 1)))]
            if old:   # first in, first out - silently
                con.execute(delete(report_documents).where(and_(report_documents.c.tenant_id == tenant_id,
                                                                report_documents.c.id.in_(old))))
                con.execute(delete(report_history).where(and_(report_history.c.tenant_id == tenant_id,
                                                              report_history.c.id.in_(old))))
                removed = len(old)
        return self.get_report(tenant_id, created_by, rid, with_doc=False), removed

    _REPORT_COLS = (report_documents.c.id, report_documents.c.seq, report_documents.c.name, report_documents.c.size,
                    report_documents.c.created_at, report_documents.c.created_by, report_history.c.period_start,
                    report_history.c.period_end, report_history.c.language, report_history.c.data_version,
                    report_history.c.spec_json, report_history.c.blocks_json)

    def list_reports(self, tenant_id: str, created_by: str, limit: int = 100) -> list[dict]:
        """The user's own stored reports, newest first (without the documents)."""
        q = (select(*self._REPORT_COLS).select_from(report_documents.join(report_history, report_history.c.id == report_documents.c.id))
             .where(and_(report_documents.c.tenant_id == tenant_id, report_documents.c.created_by == created_by))
             .order_by(report_documents.c.created_at.desc(), report_documents.c.seq.desc()).limit(limit))
        with self._begin() as con:
            return [_row(r) for r in con.execute(q)]

    def get_report(self, tenant_id: str, created_by: str, report_id: str, with_doc: bool = True) -> dict | None:
        """One of the user's stored reports - only its creator gets it (None otherwise)."""
        cols = self._REPORT_COLS + ((report_documents.c.doc_gz,) if with_doc else ())
        q = (select(*cols).select_from(report_documents.join(report_history, report_history.c.id == report_documents.c.id))
             .where(and_(report_documents.c.tenant_id == tenant_id, report_documents.c.created_by == created_by,
                         report_documents.c.id == report_id)))
        with self._begin() as con:
            r = con.execute(q).first()
        if not r:
            return None
        d = _row(r)
        if with_doc:
            d["doc"] = json.loads(zlib.decompress(d.pop("doc_gz")).decode("utf-8"))
        return d


    # ------------------------------------------------------------ v37: AI answer cache + usage
    def get_llm_cache(self, key: str) -> dict | None:
        with self._begin() as con:
            raw = con.execute(select(report_llm_cache.c.answer_json).where(report_llm_cache.c.key == key)).scalar()
        return json.loads(raw) if raw else None

    def put_llm_cache(self, key: str, tenant_id: str, answer: dict) -> None:
        now = _now()
        with self._begin() as con:
            con.execute(self._insert_ignore(report_llm_cache, key=key, tenant_id=tenant_id,
                                            answer_json=json.dumps(answer, ensure_ascii=False), created_at=now))
            con.execute(delete(report_llm_cache).where(report_llm_cache.c.created_at < now - LLM_CACHE_DAYS * 86400))

    def add_llm_usage(self, tenant_id: str, month: str, tokens_in: int = 0, tokens_out: int = 0, cost_usd: float = 0.0,
                      ok: bool = True) -> None:
        with self._begin() as con:
            con.execute(self._insert_ignore(report_llm_usage, tenant_id=tenant_id, month=month, calls=0, failures=0,
                                            tokens_in=0, tokens_out=0, cost_usd=0.0))
            u = report_llm_usage.c
            con.execute(update(report_llm_usage).where(and_(u.tenant_id == tenant_id, u.month == month)).values(
                calls=u.calls + 1, failures=u.failures + (0 if ok else 1), tokens_in=u.tokens_in + int(tokens_in or 0),
                tokens_out=u.tokens_out + int(tokens_out or 0), cost_usd=u.cost_usd + float(cost_usd or 0)))

    def llm_usage(self, tenant_id: str | None = None, month: str | None = None) -> list[dict]:
        q = select(report_llm_usage)
        if tenant_id:
            q = q.where(report_llm_usage.c.tenant_id == tenant_id)
        if month:
            q = q.where(report_llm_usage.c.month == month)
        with self._begin() as con:
            return [dict(r._mapping) for r in con.execute(q.order_by(report_llm_usage.c.month.desc(),
                                                                       report_llm_usage.c.tenant_id))]


def from_settings(raw: dict) -> ReportStore:
    """Build from settings.json → ``report_store`` (already ${VAR}-expanded)."""
    from .config import resolve_path
    rs = raw.get("report_store", {}) or {}
    return ReportStore(url=(rs.get("url") or "").strip(),
                       sqlite_path=resolve_path(rs.get("sqlite_path") or "runtime/reports.db"),
                       ca_cert=(rs.get("ca_cert") or "").strip(), ca_cert_pem=rs.get("ca_cert_pem") or "",
                       sslmode=rs.get("sslmode") or "verify-full", pool_size=int(rs.get("pool_size", 3)),
                       connect_timeout=int(rs.get("connect_timeout_seconds", 10)))
