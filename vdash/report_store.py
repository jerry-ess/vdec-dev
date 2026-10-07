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
                        Table, Text, and_, create_engine, delete, func, insert, or_, select, update)
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
    Column("last_status", String(16)),                     # created | sent | failed | skipped  (v44: 'created' = report made)
    Column("last_error", Text),                            # v44: for 'skipped' / 'failed': a short reason code
    Column("lang", String(8)),                             # v44: report language (NULL = the business's own language)
    Column("last_report_id", String(36)),                  # v44: the report the last run made
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

# v45: who may receive report mails. A new address must confirm (a link in a mail to it) before anything is sent to it.
# One row per business and address (lower case). Only the hash of the confirmation token is stored.
report_recipients = Table(
    "report_recipients", md,
    Column("tenant_id", String(128), nullable=False),
    Column("address", String(254), nullable=False),
    Column("status", String(10), nullable=False),          # pending | confirmed | revoked
    Column("token_hash", String(64)),
    Column("token_expires", Float),
    Column("lang", String(8)),                             # language of the confirmation mail and pages
    Column("requested_by", String(128), nullable=False),
    Column("requested_at", Float, nullable=False),
    Column("requests_today", Integer, nullable=False, default=0),   # confirmation mails sent in the last day (limit)
    Column("confirmed_at", Float),
    Column("revoked_at", Float),
    PrimaryKeyConstraint("tenant_id", "address"),
)
Index("ix_report_recipients_token", report_recipients.c.token_hash)

# v45: one row per mail a scheduled report has to send (one per recipient and report); the scheduler works through them.
report_deliveries = Table(
    "report_deliveries", md,
    Column("id", String(36), primary_key=True),
    Column("tenant_id", String(128), nullable=False),
    Column("schedule_id", String(36), nullable=False),
    Column("report_id", String(36), nullable=False),
    Column("created_by", String(128), nullable=False),     # the report's owner (whose "My reports" holds it)
    Column("address", String(254), nullable=False),
    Column("status", String(10), nullable=False),          # queued | sending | sent | failed | skipped
    Column("reason", String(40)),                          # why failed / skipped (short code)
    Column("attempts", Integer, nullable=False, default=0),
    Column("next_try", Float, nullable=False),
    Column("claimed_at", Float),
    Column("provider_id", String(64)),                     # the mail provider's id of the sent mail
    Column("created_at", Float, nullable=False),
    Column("sent_at", Float),
)
Index("ix_report_deliveries_due", report_deliveries.c.status, report_deliveries.c.next_try)
Index("ix_report_deliveries_run", report_deliveries.c.schedule_id, report_deliveries.c.report_id, report_deliveries.c.address,
      unique=True)

# v50: share links - who may open which stored report without signing in, until when (only the secret's hash is kept)
report_shares = Table(
    "report_shares", md,
    Column("id", String(36), primary_key=True),
    Column("tenant_id", String(128), nullable=False),
    Column("report_id", String(36), nullable=False),       # report_documents.id
    Column("created_by", String(128), nullable=False),     # the report's owner
    Column("token_hash", String(64), nullable=False),
    Column("created_at", Float, nullable=False),
    Column("expires_at", Float, nullable=False),
    Column("revoked_at", Float),
    Column("views", Integer, nullable=False, default=0),
    Column("last_view", Float),
)
Index("ix_report_shares_token", report_shares.c.token_hash, unique=True)
Index("ix_report_shares_report", report_shares.c.tenant_id, report_shares.c.report_id)

# v45: mails sent per business and month (the plan's mails_per_month limit)
report_mail_usage = Table(
    "report_mail_usage", md,
    Column("tenant_id", String(128), nullable=False),
    Column("month", String(7), nullable=False),
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

# v41 (AI plan Step 17): reports are written in the background. A job holds the composed document (made when the
# user clicked, so the numbers are those of that moment) until the story is written and the report is stored
# under the job's id. ``heartbeat`` is renewed while a process works on it; a job whose heartbeat is old (the
# process restarted or died) is claimed again by any process and resumed, or failed after ``max_attempts``.
report_jobs = Table(
    "report_jobs", md,
    Column("id", String(36), primary_key=True),            # = the report's id once stored
    Column("tenant_id", String(128), nullable=False),
    Column("created_by", String(128), nullable=False),
    Column("status", String(16), nullable=False),          # writing | done | failed
    Column("step", String(16), nullable=False, default="queued"),   # queued | story | saving | done
    Column("spec_hash", String(64), nullable=False),
    Column("month", String(7), nullable=False),            # the month the report was counted in
    Column("counted", Boolean, nullable=False, default=True),
    Column("ai_reserved", Boolean, nullable=False, default=False),
    Column("doc_gz", LargeBinary, nullable=False),         # the composed document (zlib JSON)
    Column("params_json", Text, nullable=False),           # name, keep, history fields
    Column("error", Text),
    Column("attempts", Integer, nullable=False, default=1),
    Column("worker", String(36)),
    Column("heartbeat", Float, nullable=False),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
)
Index("ix_report_jobs_owner", report_jobs.c.tenant_id, report_jobs.c.created_by, report_jobs.c.status)
Index("ix_report_jobs_status", report_jobs.c.status, report_jobs.c.heartbeat)

# v41: AI-written reports per business and month - the plan's ``ai_reports_per_month``.
report_ai_usage = Table(
    "report_ai_usage", md,
    Column("tenant_id", String(128), nullable=False),
    Column("month", String(7), nullable=False),
    Column("count", Integer, nullable=False, default=0),
    PrimaryKeyConstraint("tenant_id", "month"),
)
# v51: each person's own settings (language, theme, default view) so they follow the person to other devices.
# Not tied to a business: the same person opens several businesses with the same preferences.
user_prefs = Table(
    "user_prefs", md,
    Column("username", String(128), primary_key=True),
    Column("prefs_json", Text, nullable=False),
    Column("updated_at", Float, nullable=False),
)
JOB_KEEP_SECONDS = 2 * 86400            # finished / failed jobs are kept 2 days (the "My reports" list shows failures)

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
        args: dict[str, Any] = {"connect_timeout": connect_timeout, "application_name": "vdec-dashboard"}
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
                self._add_missing_columns()
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

    # columns added after v25: an existing database gets them with ALTER TABLE (nullable, so older code keeps working)
    _ADDED = {"report_schedules": (("lang", "VARCHAR(8)"), ("last_report_id", "VARCHAR(36)"))}

    def _add_missing_columns(self) -> None:
        from sqlalchemy import inspect, text
        insp = inspect(self.engine)
        for table, cols in self._ADDED.items():
            have = {c["name"] for c in insp.get_columns(table)}
            for name, ddl in cols:
                if name not in have:
                    with self.engine.begin() as con:
                        con.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
                    log.info("report_store: added column %s.%s", table, name)

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
    SCHEDULE_FIELDS = ("frequency", "day_of_week", "day_of_month", "send_time", "recipients", "enabled", "next_run", "lang")

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

    def schedules_without_next_run(self) -> list[dict]:
        """Enabled schedules that have no next run yet (the scheduler gives them one)."""
        with self._begin() as con:
            rs = con.execute(select(report_schedules).where(and_(report_schedules.c.enabled.is_(True),
                                                                 report_schedules.c.next_run.is_(None)))).all()
        return [_row(r) for r in rs]

    def set_next_run(self, schedule_id: str, expected: float | None, new: float | None) -> bool:
        """Set the next run only if it still is ``expected`` (None = not set) - one process wins."""
        cond = report_schedules.c.next_run.is_(None) if expected is None else report_schedules.c.next_run == expected
        with self._begin() as con:
            return con.execute(update(report_schedules).where(and_(report_schedules.c.id == schedule_id, cond))
                               .values(next_run=new, updated_at=_now())).rowcount == 1

    def record_schedule_result(self, schedule_id: str, status: str, error: str | None = None,
                               report_id: str | None = None, only_if_report: str | None = None) -> None:
        """Last outcome of a schedule. ``only_if_report``: a late answer about a report (a background job finishing)
        is written only when that report is still the schedule's last one."""
        vals: dict = {"last_status": status, "last_error": error, "updated_at": _now()}
        if report_id is not None:
            vals["last_report_id"] = report_id
        cond = [report_schedules.c.id == schedule_id]
        if only_if_report is not None:
            cond.append(report_schedules.c.last_report_id == only_if_report)
        with self._begin() as con:
            con.execute(update(report_schedules).where(and_(*cond)).values(**vals))

    # ------------------------------------------------------------ v45: mail recipients (confirmed before use)
    def get_recipient(self, tenant_id: str, address: str) -> dict | None:
        with self._begin() as con:
            r = con.execute(select(report_recipients).where(and_(report_recipients.c.tenant_id == tenant_id,
                                                                 report_recipients.c.address == address.lower()))).first()
        return dict(r._mapping) if r else None

    def list_recipients(self, tenant_id: str) -> list[dict]:
        with self._begin() as con:
            rs = con.execute(select(report_recipients).where(report_recipients.c.tenant_id == tenant_id)
                             .order_by(report_recipients.c.requested_at)).all()
        return [{k: v for k, v in dict(r._mapping).items() if k != "token_hash"} for r in rs]

    def request_recipient(self, tenant_id: str, address: str, requested_by: str, token_hash: str, expires: float,
                          lang: str, max_per_day: int, max_recipients: int) -> tuple[str, str | None]:
        """Ask an address to confirm. Returns (state, reason): ``("pending", None)`` = send the confirmation mail;
        ``("confirmed", None)`` = nothing to do; ``("refused", "recipients_limit" | "too_many_requests")``."""
        a, now = address.lower(), _now()
        with self._begin() as con:
            t = report_recipients
            cur = con.execute(select(t).where(and_(t.c.tenant_id == tenant_id, t.c.address == a))).first()
            if cur is not None and cur.status == "confirmed":
                return "confirmed", None
            live = con.execute(select(func.count()).select_from(t).where(and_(t.c.tenant_id == tenant_id,
                                                                              t.c.status != "revoked"))).scalar() or 0
            if cur is None and live >= max_recipients:
                return "refused", "recipients_limit"
            if cur is not None and cur.status == "pending" and (now - cur.requested_at) < 86400 and cur.requests_today >= max_per_day:
                return "refused", "too_many_requests"
            fresh_day = cur is None or (now - cur.requested_at) >= 86400
            n = 1 if fresh_day else cur.requests_today + 1
            vals = dict(status="pending", token_hash=token_hash, token_expires=expires, lang=lang, requested_by=requested_by,
                        requested_at=now if fresh_day else cur.requested_at, requests_today=n, revoked_at=None)
            if cur is None:
                con.execute(insert(t).values(tenant_id=tenant_id, address=a, **vals))
            else:
                con.execute(update(t).where(and_(t.c.tenant_id == tenant_id, t.c.address == a)).values(**vals))
        return "pending", None

    def confirm_recipient(self, token_hash: str) -> dict | None:
        """Confirm the address that owns this token (once; the token is then void). None = unknown / expired / used."""
        now, t = _now(), report_recipients
        with self._begin() as con:
            row = con.execute(select(t).where(and_(t.c.token_hash == token_hash, t.c.status == "pending",
                                                   t.c.token_expires >= now))).first()
            if row is None:
                return None
            n = con.execute(update(t).where(and_(t.c.tenant_id == row.tenant_id, t.c.address == row.address,
                                                 t.c.token_hash == token_hash, t.c.status == "pending"))
                            .values(status="confirmed", confirmed_at=now, token_hash=None, token_expires=None)).rowcount
        if n != 1:
            return None                     # someone else used the token at the same moment
        return {k: v for k, v in dict(row._mapping).items() if k != "token_hash"} | {"status": "confirmed", "confirmed_at": now}

    def peek_recipient_token(self, token_hash: str) -> dict | None:
        """The pending request behind a token (for the confirmation page); None when unknown, used or expired."""
        t = report_recipients
        with self._begin() as con:
            r = con.execute(select(t).where(and_(t.c.token_hash == token_hash, t.c.status == "pending",
                                                 t.c.token_expires >= _now()))).first()
        return {k: v for k, v in dict(r._mapping).items() if k != "token_hash"} if r else None

    def revoke_recipient(self, tenant_id: str, address: str) -> bool:
        t = report_recipients
        with self._begin() as con:
            return con.execute(update(t).where(and_(t.c.tenant_id == tenant_id, t.c.address == address.lower(),
                                                    t.c.status != "revoked"))
                               .values(status="revoked", revoked_at=_now(), token_hash=None, token_expires=None)).rowcount == 1

    # ------------------------------------------------------------ v45: deliveries (one mail per recipient and report)
    def add_delivery(self, tenant_id: str, schedule_id: str, report_id: str, created_by: str, address: str,
                     status: str = "queued", reason: str | None = None, now: float | None = None) -> bool:
        """Queue (or record as skipped) one mail. False when this run already has a row for the address."""
        now = _now() if now is None else now
        with self._begin() as con:
            return con.execute(self._insert_ignore(
                report_deliveries, id=str(uuid.uuid4()), tenant_id=tenant_id, schedule_id=schedule_id, report_id=report_id,
                created_by=created_by, address=address.lower(), status=status, reason=reason, attempts=0, next_try=now,
                created_at=now)).rowcount == 1

    def claim_deliveries(self, now: float, limit: int, stale_after: float = 600.0) -> list[dict]:
        """Take up to ``limit`` mails that are due (queued, or 'sending' by a process that died). Each one goes to
        exactly one caller (conditional UPDATE)."""
        t = report_deliveries
        with self._begin() as con:
            due = con.execute(select(t.c.id, t.c.status, t.c.claimed_at).where(or_(
                and_(t.c.status == "queued", t.c.next_try <= now),
                and_(t.c.status == "sending", t.c.claimed_at <= now - stale_after))).order_by(t.c.next_try).limit(limit)).all()
        out = []
        for d in due:
            cond = [t.c.id == d.id, t.c.status == d.status]
            if d.status == "sending":
                cond.append(t.c.claimed_at == d.claimed_at)
            with self._begin() as con:
                if con.execute(update(t).where(and_(*cond)).values(status="sending", claimed_at=now,
                                                                   attempts=t.c.attempts + 1)).rowcount == 1:
                    out.append(dict(con.execute(select(t).where(t.c.id == d.id)).first()._mapping))
        return out

    def finish_delivery(self, delivery_id: str, status: str, reason: str | None = None, provider_id: str | None = None,
                        next_try: float | None = None, keep_attempt: bool = False) -> None:
        """status: sent | failed | skipped, or queued (try again at ``next_try``). ``keep_attempt``: the try did not
        count (the report was not ready yet)."""
        vals: dict = {"status": status, "reason": reason, "claimed_at": None}
        if keep_attempt:
            vals["attempts"] = case((report_deliveries.c.attempts > 0, report_deliveries.c.attempts - 1), else_=0)
        if provider_id:
            vals["provider_id"] = provider_id
        if status == "sent":
            vals["sent_at"] = _now()
        if next_try is not None:
            vals["next_try"] = next_try
        with self._begin() as con:
            con.execute(update(report_deliveries).where(report_deliveries.c.id == delivery_id).values(**vals))

    def run_deliveries(self, schedule_id: str, report_id: str) -> list[dict]:
        with self._begin() as con:
            rs = con.execute(select(report_deliveries).where(and_(report_deliveries.c.schedule_id == schedule_id,
                                                                  report_deliveries.c.report_id == report_id))
                             .order_by(report_deliveries.c.address)).all()
        return [dict(r._mapping) for r in rs]

    # ------------------------------------------------------------ v50: share links
    def add_share(self, tenant_id: str, report_id: str, created_by: str, token_hash: str, expires_at: float,
                  max_per_report: int, max_per_business: int, now: float | None = None) -> tuple[str, dict | str]:
        """Make a link: ``("ok", row)`` or ``("refused", "report_limit" | "business_limit")``."""
        now = _now() if now is None else now
        t = report_shares
        live = and_(t.c.tenant_id == tenant_id, t.c.revoked_at.is_(None), t.c.expires_at > now)
        with self._begin() as con:
            con.execute(delete(t).where(t.c.expires_at < now - 30 * 86400))            # long dead rows go
            if (con.execute(select(func.count()).select_from(t).where(and_(live, t.c.report_id == report_id))).scalar() or 0) >= max_per_report:
                return "refused", "report_limit"
            if (con.execute(select(func.count()).select_from(t).where(live)).scalar() or 0) >= max_per_business:
                return "refused", "business_limit"
            row = {"id": str(uuid.uuid4()), "tenant_id": tenant_id, "report_id": report_id, "created_by": created_by,
                   "token_hash": token_hash, "created_at": now, "expires_at": expires_at, "views": 0}
            con.execute(insert(t).values(**row))
        return "ok", {k: v for k, v in row.items() if k != "token_hash"}

    def list_shares(self, tenant_id: str, created_by: str | None = None, report_id: str | None = None,
                    now: float | None = None) -> list[dict]:
        """Live links (not stopped, not expired) with the report's name; ``created_by`` None = the whole business."""
        now = _now() if now is None else now
        t, d = report_shares, report_documents
        cond = [t.c.tenant_id == tenant_id, t.c.revoked_at.is_(None), t.c.expires_at > now]
        if created_by is not None:
            cond.append(t.c.created_by == created_by)
        if report_id is not None:
            cond.append(t.c.report_id == report_id)
        q = (select(t.c.id, t.c.report_id, t.c.created_by, t.c.created_at, t.c.expires_at, t.c.views, t.c.last_view,
                    d.c.name.label("report_name"), d.c.seq.label("report_seq"))
             .select_from(t.join(d, d.c.id == t.c.report_id)).where(and_(*cond)).order_by(t.c.created_at.desc()))
        with self._begin() as con:
            return [dict(r._mapping) for r in con.execute(q).all()]

    def revoke_share(self, tenant_id: str, share_id: str, created_by: str | None = None, now: float | None = None) -> bool:
        t = report_shares
        cond = [t.c.tenant_id == tenant_id, t.c.id == share_id, t.c.revoked_at.is_(None)]
        if created_by is not None:
            cond.append(t.c.created_by == created_by)
        with self._begin() as con:
            return con.execute(update(t).where(and_(*cond)).values(revoked_at=_now() if now is None else now)).rowcount == 1

    def resolve_share(self, token_hash: str, now: float | None = None) -> dict | None:
        """The live link with this secret hash and its stored report; None = unknown, stopped, expired or report gone."""
        now = _now() if now is None else now
        t, d = report_shares, report_documents
        q = (select(t.c.id, t.c.tenant_id, t.c.report_id, t.c.created_by, t.c.expires_at, d.c.name, d.c.seq, d.c.doc_gz)
             .select_from(t.join(d, d.c.id == t.c.report_id))
             .where(and_(t.c.token_hash == token_hash, t.c.revoked_at.is_(None), t.c.expires_at > now)))
        with self._begin() as con:
            r = con.execute(q).first()
        if not r:
            return None
        row = dict(r._mapping)
        row["doc"] = json.loads(zlib.decompress(row.pop("doc_gz")).decode("utf-8"))
        return row

    def touch_share(self, share_id: str, now: float | None = None) -> None:
        with self._begin() as con:
            con.execute(update(report_shares).where(report_shares.c.id == share_id)
                        .values(views=report_shares.c.views + 1, last_view=_now() if now is None else now))

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

    @staticmethod
    def _counter(ai: bool = False, mail: bool = False):
        return report_mail_usage if mail else report_ai_usage if ai else report_usage

    def usage(self, tenant_id: str, month: str, ai: bool = False, mail: bool = False) -> int:
        t = self._counter(ai, mail)
        with self._begin() as con:
            return int(con.execute(select(t.c.count).where(and_(t.c.tenant_id == tenant_id,
                                                                t.c.month == month))).scalar() or 0)

    def reserve_report(self, tenant_id: str, month: str, limit: int | None, ai: bool = False, mail: bool = False) -> bool:
        """Count one report (``ai``: one AI-written report) for the business and month - atomically, only while
        below ``limit`` (None = no limit). False = the limit is reached. Two requests at the same moment can never
        both take the last one. ``mail``: count one mail instead (v45)."""
        t = self._counter(ai, mail)
        with self._begin() as con:
            con.execute(self._insert_ignore(t, tenant_id=tenant_id, month=month, count=0))
            cond = and_(t.c.tenant_id == tenant_id, t.c.month == month)
            if limit is not None:
                cond = and_(cond, t.c.count < int(limit))
            return con.execute(update(t).where(cond).values(count=t.c.count + 1)).rowcount == 1

    def release_report(self, tenant_id: str, month: str, ai: bool = False, mail: bool = False) -> None:
        """Give back a reserved report when it could not be made after all (``ai``: not written by AI after all)."""
        t = self._counter(ai, mail)
        with self._begin() as con:
            con.execute(update(t).where(and_(t.c.tenant_id == tenant_id, t.c.month == month,
                                             t.c.count > 0)).values(count=t.c.count - 1))

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
                    report_id: str | None = None, **history) -> tuple[dict, int]:
        """Store a created report (history row + compressed document) and keep only the user's newest ``keep``.
        ``name_for(seq)`` gives the automatic name. Returns (the report, number of old reports removed)."""
        raw = json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        gz = zlib.compress(raw, 6)
        rid, now = report_id or str(uuid.uuid4()), _now()
        if report_id:      # v41: a resumed job may already have stored its report - never store it twice
            have = self.get_report(tenant_id, created_by, report_id, with_doc=False)
            if have:
                return have, 0
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


    # ------------------------------------------------------------ v41: background report jobs
    _JOB_COLS = (report_jobs.c.id, report_jobs.c.tenant_id, report_jobs.c.created_by, report_jobs.c.status,
                 report_jobs.c.step, report_jobs.c.spec_hash, report_jobs.c.month, report_jobs.c.counted,
                 report_jobs.c.ai_reserved, report_jobs.c.params_json, report_jobs.c.error, report_jobs.c.attempts,
                 report_jobs.c.worker, report_jobs.c.heartbeat, report_jobs.c.created_at, report_jobs.c.updated_at)

    @staticmethod
    def _job(r) -> dict:
        d = dict(r._mapping)
        d["params"] = json.loads(d.pop("params_json") or "{}")
        if "doc_gz" in d:
            d["doc"] = json.loads(zlib.decompress(d.pop("doc_gz")).decode("utf-8"))
        return d

    def create_job(self, tenant_id: str, created_by: str, doc: dict, spec_hash: str, month: str, counted: bool,
                   params: dict, worker: str) -> dict:
        now = _now()
        jid = str(uuid.uuid4())
        gz = zlib.compress(json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), 6)
        with self._begin() as con:
            con.execute(insert(report_jobs).values(
                id=jid, tenant_id=tenant_id, created_by=created_by, status="writing", step="queued", spec_hash=spec_hash,
                month=month, counted=bool(counted), ai_reserved=False, doc_gz=gz,
                params_json=json.dumps(params, ensure_ascii=False), attempts=1, worker=worker, heartbeat=now,
                created_at=now, updated_at=now))
        return self.get_job(tenant_id, created_by, jid)

    def get_job(self, tenant_id: str | None, created_by: str | None, job_id: str, with_doc: bool = False) -> dict | None:
        """A job - only for its creator in its business (``None`` for both = internal use by the job runner)."""
        cond = report_jobs.c.id == job_id
        if tenant_id is not None:
            cond = and_(cond, report_jobs.c.tenant_id == tenant_id, report_jobs.c.created_by == created_by)
        cols = self._JOB_COLS + ((report_jobs.c.doc_gz,) if with_doc else ())
        with self._begin() as con:
            r = con.execute(select(*cols).where(cond)).first()
        return self._job(r) if r else None

    def list_jobs(self, tenant_id: str, created_by: str, since: float) -> list[dict]:
        """The person's jobs still being written, and those finished or failed since ``since`` (newest first)."""
        q = (select(*self._JOB_COLS).where(and_(report_jobs.c.tenant_id == tenant_id, report_jobs.c.created_by == created_by,
                                                (report_jobs.c.status == "writing") | (report_jobs.c.updated_at >= since)))
             .order_by(report_jobs.c.created_at.desc()).limit(50))
        with self._begin() as con:
            return [self._job(r) for r in con.execute(q)]

    def find_job(self, tenant_id: str, created_by: str, spec_hash: str) -> dict | None:
        """The person's job for the same report that is still being written (a second click joins it)."""
        q = (select(*self._JOB_COLS).where(and_(report_jobs.c.tenant_id == tenant_id, report_jobs.c.created_by == created_by,
                                                report_jobs.c.spec_hash == spec_hash, report_jobs.c.status == "writing"))
             .order_by(report_jobs.c.created_at.desc()).limit(1))
        with self._begin() as con:
            r = con.execute(q).first()
        return self._job(r) if r else None

    def update_job(self, job_id: str, worker: str | None = None, **fields) -> bool:
        """Change a job; with ``worker`` only while that worker still owns it (a job taken over elsewhere is left
        alone). Returns whether the job was changed."""
        cond = report_jobs.c.id == job_id
        if worker is not None:
            cond = and_(cond, report_jobs.c.worker == worker)
        with self._begin() as con:
            return con.execute(update(report_jobs).where(cond).values(updated_at=_now(), **fields)).rowcount == 1

    def beat_jobs(self, job_ids: list[str], worker: str) -> None:
        if not job_ids:
            return
        with self._begin() as con:
            con.execute(update(report_jobs).where(and_(report_jobs.c.id.in_(job_ids), report_jobs.c.worker == worker,
                                                       report_jobs.c.status == "writing")).values(heartbeat=_now()))

    def claim_stale_jobs(self, worker: str, stale_after: float, limit: int = 5) -> list[dict]:
        """Jobs whose worker stopped (old heartbeat): taken over atomically one by one - two processes never
        take the same job. Returns the claimed jobs (attempts already increased)."""
        cutoff, out = _now() - stale_after, []
        with self._begin() as con:
            rows = con.execute(select(report_jobs.c.id, report_jobs.c.heartbeat, report_jobs.c.attempts).where(
                and_(report_jobs.c.status == "writing", report_jobs.c.heartbeat < cutoff)).limit(limit)).all()
        for jid, hb, att in rows:
            with self._begin() as con:
                ok = con.execute(update(report_jobs).where(and_(
                    report_jobs.c.id == jid, report_jobs.c.status == "writing", report_jobs.c.heartbeat == hb))
                    .values(worker=worker, heartbeat=_now(), attempts=att + 1, updated_at=_now())).rowcount == 1
            if ok:
                out.append(self.get_job(None, None, jid))
        return [j for j in out if j]

    def prune_jobs(self, older_than: float = JOB_KEEP_SECONDS) -> int:
        with self._begin() as con:
            return con.execute(delete(report_jobs).where(and_(report_jobs.c.status != "writing",
                                                              report_jobs.c.updated_at < _now() - older_than))).rowcount

    # ------------------------------------------------------------ v51: personal settings
    def get_user_prefs(self, username: str) -> dict:
        with self._begin() as con:
            raw = con.execute(select(user_prefs.c.prefs_json).where(user_prefs.c.username == username)).scalar()
        try:
            d = json.loads(raw) if raw else {}
        except ValueError:
            d = {}
        return d if isinstance(d, dict) else {}

    def set_user_prefs(self, username: str, prefs: dict) -> None:
        """Replace the person's settings (the caller merges and validates). An empty dict removes the row."""
        with self._begin() as con:
            if not prefs:
                con.execute(delete(user_prefs).where(user_prefs.c.username == username))
                return
            body = json.dumps(prefs, ensure_ascii=False, sort_keys=True)
            n = con.execute(update(user_prefs).where(user_prefs.c.username == username)
                            .values(prefs_json=body, updated_at=_now())).rowcount
            if not n:   # first save; a parallel first save of the same person simply loses the race harmlessly
                con.execute(self._insert_ignore(user_prefs, username=username, prefs_json=body, updated_at=_now()))

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
