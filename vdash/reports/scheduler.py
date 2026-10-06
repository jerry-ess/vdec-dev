"""Regular reports: the scheduler (v44, AI plan Step 23).

A schedule (``report_schedules``: frequency, day, time, owner, saved report settings) makes a report by itself:

* **Which period.** Always the last *complete* unit before the run date, in the business's time zone: weekly = the
  last full Monday-Sunday week, monthly = the last full calendar month, quarterly = the last full quarter. Never a
  rolling window - so two consecutive reports never overlap and "Since your last report" (Step 21) can compare them.
* **Who runs it.** The schedule's owner, with the role the owner has *now* (no role is stored). A report is made
  exactly like a manual one (same limits, same story writer, kept in the owner's "My reports", counted in the
  plan). If the owner or the business is gone, the schedule is switched off.
* **When the data is late.** The run waits (``data_grace_days``) until the data reaches the end of the period;
  then it is skipped with the reason ``data_not_ready``. A report with a half period is never made.
* **After downtime** (the server slept or was redeployed): only the latest missed run is made, and only if it is not
  older than ``max_late_hours`` (else ``missed``). Older missed runs are dropped, never made in a burst.
* **No duplicates.** A run is claimed with one conditional UPDATE (``claim_schedule``) before anything is made, so
  with several workers exactly one makes it. The price: if the process dies between the claim and the report, that
  one run is lost (shown as the schedule's last run with no result) - it is never made twice.

Outcomes are kept on the schedule (``last_status``: created / skipped / failed, ``last_error``: a short reason code,
``last_report_id``) and in the audit log. Sending the report (e-mail, Telegram) is the next steps: they start from
a schedule whose ``last_status`` is ``created``.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from ..config import config_dir, load_tenants
from ..i18n import norm_lang
from ..mailer import MailError, send_resend, settings_from_env
from ..report_store import ReportStoreError
from ..services import ServiceError

log = logging.getLogger("vdash.reports")
FREQUENCIES = ("weekly", "monthly", "quarterly")
_CACHE: dict[str, tuple[float, dict]] = {}


# ------------------------------------------------------------------ settings
def config_path() -> Path:
    return config_dir() / "report_schedule.json"


def load_config(path: Path | None = None) -> dict:
    path = path or config_path()
    mtime = path.stat().st_mtime
    hit = _CACHE.get(str(path))
    if hit and hit[0] == mtime:
        return hit[1]
    raw = json.loads(path.read_text(encoding="utf-8"))
    _CACHE[str(path)] = (mtime, raw)
    return raw


def validate_config(c: dict) -> list[str]:
    errs = []

    def num(path, v, lo, hi):
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not lo <= v <= hi:
            errs.append(f"{path}: a number from {lo} to {hi}")
    num("tick_seconds", c.get("tick_seconds"), 5, 3600)
    num("data_grace_days", c.get("data_grace_days"), 0, 14)
    num("max_runs_per_tick", c.get("max_runs_per_tick"), 1, 50)
    late, grace = c.get("max_late_hours") or {}, c.get("data_grace_days")
    for f in FREQUENCIES:
        num(f"max_late_hours.{f}", late.get(f), 1, 24 * 60)
        if isinstance(late.get(f), (int, float)) and isinstance(grace, (int, float)) and late[f] < grace * 24:
            errs.append(f"max_late_hours.{f} must be at least data_grace_days x 24 (a run waiting for data would be 'missed')")
    day = c.get("default_day") or {}
    num("default_day.weekly", day.get("weekly"), 0, 6)
    num("default_day.monthly", day.get("monthly"), 1, 28)
    num("default_day.quarterly", day.get("quarterly"), 1, 28)
    num("max_schedules_per_business", c.get("max_schedules_per_business", 10), 1, 100)
    if c.get("compare") not in ("previous", "yoy", "none"):
        errs.append("compare: previous, yoy or none")
    return errs


# ------------------------------------------------------------------ time
def _tz(tc) -> ZoneInfo:
    try:
        return ZoneInfo(getattr(tc, "timezone", "UTC") or "UTC")
    except Exception:   # noqa: BLE001 - unknown zone name: UTC
        return ZoneInfo("UTC")


def _hhmm(v) -> tuple[int, int]:
    try:
        h, m = str(v or "08:00").split(":")
        return max(0, min(23, int(h))), max(0, min(59, int(m)))
    except ValueError:
        return 8, 0


def _day(s: dict, cfg: dict) -> int:
    f = s["frequency"]
    v = s.get("day_of_week") if f == "weekly" else s.get("day_of_month")
    return int(cfg["default_day"][f] if v is None else v)


def occurrence(s: dict, tc, after: float, cfg: dict | None = None) -> float:
    """The first run time of the schedule strictly after ``after`` (epoch seconds), in the business's time zone."""
    cfg = cfg or load_config()
    tz, (hh, mm), day = _tz(tc), _hhmm(s.get("send_time")), _day(s, cfg)
    start = datetime.fromtimestamp(after, tz).date()
    for i in range(0, 400):
        d = start + timedelta(days=i)
        ok = (d.weekday() == day if s["frequency"] == "weekly" else
              d.day == day and (s["frequency"] == "monthly" or d.month in (1, 4, 7, 10)))
        if ok:
            ts = datetime(d.year, d.month, d.day, hh, mm, tzinfo=tz).timestamp()
            if ts > after:
                return ts
    raise ValueError("no run time found")           # unreachable for valid schedules


def latest_occurrence(s: dict, tc, first: float, now: float, cfg: dict | None = None) -> float:
    """Of the runs from ``first`` up to ``now``, the latest one (the earlier ones were missed and are dropped)."""
    cur = first
    for _ in range(2000):
        nxt = occurrence(s, tc, cur, cfg)
        if nxt > now:
            return cur
        cur = nxt
    return cur


def period_for(frequency: str, run_date: date) -> tuple[date, date]:
    """The last complete week / month / quarter before ``run_date``."""
    if frequency == "weekly":
        end = run_date - timedelta(days=run_date.weekday() + 1)           # the Sunday before this week
        return end - timedelta(days=6), end
    if frequency == "monthly":
        end = run_date.replace(day=1) - timedelta(days=1)
        return end.replace(day=1), end
    q0 = date(run_date.year, 3 * ((run_date.month - 1) // 3) + 1, 1)
    end = q0 - timedelta(days=1)
    return date(end.year, 3 * ((end.month - 1) // 3) + 1, 1), end


def business_lang(tc) -> str:
    return norm_lang(str(getattr(tc, "locale", "") or "en").split("-")[0])


def preview(s: dict, tc, now: float | None = None, n: int = 3, cfg: dict | None = None) -> list[dict]:
    """The next ``n`` runs with the period each will report - for the schedule screen (Step 25)."""
    cfg, now = cfg or load_config(), time.time() if now is None else now
    out, cur = [], now
    for _ in range(n):
        cur = occurrence(s, tc, cur, cfg)
        d = datetime.fromtimestamp(cur, _tz(tc)).date()
        a, b = period_for(s["frequency"], d)
        out.append({"run_at": cur, "run_date": d.isoformat(), "period_start": a.isoformat(), "period_end": b.isoformat()})
    return out


# ------------------------------------------------------------------ the scheduler
class ReportScheduler:
    """``create`` = ``create(user, tc, body, schedule_id) -> {"report", "job", "reused"}`` (raises ServiceError)."""

    def __init__(self, dash, create, store_fn=None, cfg: dict | None = None, mail_cfg: dict | None = None,
                 send=None):
        self.dash, self.create = dash, create
        self.mail_cfg = mail_cfg
        self.send = send or send_resend
        self.store_fn = store_fn or (lambda: dash.svc.reports)
        self.cfg = cfg
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_tick: float | None = None
        self.last_error: str | None = None
        self.runs = 0

    def conf(self) -> dict:
        return self.cfg or load_config()

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self._thread:
            return
        self._thread = threading.Thread(target=self._loop, name="report-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
                self.last_error = None
            except ReportStoreError as exc:
                self.last_error = str(exc)
                log.warning("report scheduler: %s", exc)
            except Exception as exc:   # noqa: BLE001 - the scheduler must keep running
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.exception("report scheduler")
            self._stop.wait(float(self.conf()["tick_seconds"]))

    def status(self) -> dict:
        return {"running": bool(self._thread and self._thread.is_alive()), "last_tick": self.last_tick,
                "last_error": self.last_error, "runs": self.runs, "tick_seconds": self.conf()["tick_seconds"]}

    # ------------------------------------------------------------ one pass
    def tick(self, now: float | None = None) -> list[dict]:
        """Give new schedules their first run time, then make every due report (at most ``max_runs_per_tick``).
        Returns what happened: [{schedule, status, reason}]."""
        now = time.time() if now is None else now
        store, cfg, tenants = self.store_fn(), self.conf(), load_tenants()
        self.last_tick = now
        for s in store.schedules_without_next_run():
            tc = tenants.get(s["tenant_id"])
            if tc is not None:
                store.set_next_run(s["id"], None, occurrence(s, tc, now, cfg))
        out = []
        for s in store.due_schedules(now)[: int(cfg["max_runs_per_tick"])]:
            try:
                r = self.process(s, now, store, cfg, tenants)
            except ReportStoreError:
                raise
            except Exception as exc:   # noqa: BLE001 - one schedule never stops the others
                log.exception("schedule %s", s["id"])
                r = {"status": "failed", "reason": "error"}
                self._record(store, s, "failed", f"error: {type(exc).__name__}")
            if r:
                out.append({"schedule": s["id"], **r})
                if r.get("status") == "created":
                    try:
                        self._enqueue(store, s, r["report_id"], tenants, now)
                    except ReportStoreError:
                        raise
                    except Exception:   # noqa: BLE001 - the report exists; only its mails are in doubt
                        log.exception("queue mails of schedule %s", s["id"])
        try:
            self.deliver(now, store, tenants)
        except ReportStoreError:
            raise
        except Exception:   # noqa: BLE001
            log.exception("deliver report mails")
        return out

    # ------------------------------------------------------------ mails (v46, Step 24 part 2)
    def mconf(self) -> dict:
        from .recipients import load_config as mail_config
        return self.mail_cfg or mail_config()

    def _enqueue(self, store, s: dict, report_id: str, tenants: dict, now: float | None = None) -> None:
        """One queued mail per confirmed address of the schedule; every other address is recorded as skipped."""
        mc = self.mconf()
        seen, tid = [], s["tenant_id"]
        for a in s.get("recipients") or []:
            a = str((a.get("address") if isinstance(a, dict) else a) or "").strip().lower()
            if a and a not in seen:
                seen.append(a)
        for a in seen[: int(mc["max_per_schedule"])]:
            rec = store.get_recipient(tid, a)
            ok = bool(rec and rec["status"] == "confirmed")
            store.add_delivery(tid, s["id"], report_id, s["created_by"], a, "queued" if ok else "skipped",
                               None if ok else ("unsubscribed" if rec and rec["status"] == "revoked" else "not_confirmed"), now=now)
        self._summarize(store, s["id"], report_id, s)

    def _summarize(self, store, schedule_id: str, report_id: str, s: dict | None = None) -> None:
        """When nothing of this run is waiting any more, say how the mailing went on the schedule."""
        rows = store.run_deliveries(schedule_id, report_id)
        if not rows or any(r["status"] in ("queued", "sending") for r in rows):
            return
        sent = sum(r["status"] == "sent" for r in rows)
        failed = sum(r["status"] == "failed" for r in rows)
        if failed:
            store.record_schedule_result(schedule_id, "failed", "mail_failed", only_if_report=report_id)
        elif sent:
            store.record_schedule_result(schedule_id, "sent", None, only_if_report=report_id)

    def deliver(self, now: float, store=None, tenants: dict | None = None) -> int:
        """Send the queued report mails (at most ``max_mails_per_tick``). Returns how many were sent."""
        from .mail import build_mail
        from .plans import month_key, plan_for
        from .recipients import RecipientError, public_url, unsub_key, unsubscribe_url
        store = store or self.store_fn()
        tenants = tenants if tenants is not None else load_tenants()
        mc = self.mconf()
        claimed = store.claim_deliveries(now, int(mc["max_mails_per_tick"]))
        if not claimed:
            return 0
        retry = [float(x) for x in mc["retry_seconds"]]
        key, sender = settings_from_env()
        docs: dict[str, tuple] = {}
        sent = 0
        for d in claimed:
            rid, sid, tid = d["report_id"], d["schedule_id"], d["tenant_id"]
            fin = lambda status, reason=None, **kw: store.finish_delivery(d["id"], status, reason, **kw)   # noqa: E731
            try:
                tc = tenants.get(tid)
                if tc is None or not tc.enabled:
                    fin("skipped", "business_off")
                elif rid not in docs and not self._load(store, docs, d):
                    job = store.get_job(None, None, rid)
                    if job and job["status"] in ("queued", "running", "pending") and \
                            now - d["created_at"] < mc["report_wait_hours"] * 3600:
                        fin("queued", "waiting_for_report", next_try=now + 30, keep_attempt=True)
                    else:
                        fin("skipped", "report_failed" if job and job["status"] == "failed" else "report_missing")
                else:
                    rec = store.get_recipient(tid, d["address"])
                    if not rec or rec["status"] != "confirmed":
                        fin("skipped", "unsubscribed" if rec and rec["status"] == "revoked" else "not_confirmed")
                    elif not key or not sender:
                        fin("failed", "mail_not_configured")
                    else:
                        sent += self._send_one(store, d, tc, docs[rid], key, sender, retry, now, fin, month_key,
                                               plan_for, build_mail, public_url, unsubscribe_url, unsub_key)
            except RecipientError as exc:
                fin("failed", exc.code)
            except ReportStoreError:
                store.finish_delivery(d["id"], "queued", "store_unavailable", next_try=now + 60, keep_attempt=True)
                raise
            except Exception as exc:   # noqa: BLE001 - one mail never stops the others
                log.exception("delivery %s", d["id"])
                fin("failed", f"error: {type(exc).__name__}"[:40])
            self._summarize(store, sid, rid)
        return sent

    def _load(self, store, docs: dict, d: dict) -> bool:
        full = store.get_report(d["tenant_id"], d["created_by"], d["report_id"])
        if not full:
            return False
        docs[d["report_id"]] = (full["doc"], full["name"], {})        # the third item keeps the PDF made for this report
        return True

    def _send_one(self, store, d, tc, loaded, key, sender, retry, now, fin, month_key, plan_for, build_mail,
                  public_url, unsubscribe_url, unsub_key) -> int:
        doc, name, cache = loaded
        public_url()
        limit = plan_for(tc).get("mails_per_month")
        month = month_key(tc)
        if not store.reserve_report(tc.tenant_id, month, limit, mail=True):
            fin("skipped", "mail_quota")
            return 0
        rec = store.get_recipient(tc.tenant_id, d["address"])
        lang = (rec or {}).get("lang") or doc["meta"]["lang"]
        try:
            if "pdf" not in cache:                       # the PDF is made once per report, not once per address
                first = build_mail(doc, name, with_pdf=True)
                cache["pdf"] = first["attachments"][0]["content"] if first["attachments"] else None
            unsub = unsubscribe_url(unsub_key(self.dash.svc.signer.key), tc.tenant_id, d["address"], lang)
            mail = build_mail(doc, name, with_pdf=bool(cache["pdf"]), pdf_bytes=cache["pdf"], unsubscribe_url=unsub)
            ans = self.send(key, sender, [d["address"]], mail["subject"], mail["html"], mail["text"], mail["attachments"],
                            idempotency_key=d["id"],
                            mail_headers={"List-Unsubscribe": f"<{unsub}>",
                                          "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"})
        except MailError as exc:
            store.release_report(tc.tenant_id, month, mail=True)
            n = int(d["attempts"])
            if exc.retriable and n <= len(retry):
                fin("queued", "retry", next_try=now + retry[n - 1])
            else:
                fin("failed", "mail_failed" if exc.retriable else "mail_refused")
            return 0
        except Exception:
            store.release_report(tc.tenant_id, month, mail=True)
            raise
        fin("sent", None, provider_id=str((ans or {}).get("id") or "")[:64] or None)
        return 1

    def _audit(self, s: dict, action: str, detail: str) -> None:
        try:
            self.dash.svc.state.audit("scheduler", s["tenant_id"], action, f"{s['id']} {detail}")
        except Exception:   # noqa: BLE001 - the audit log never blocks a report
            log.exception("audit")

    def _record(self, store, s: dict, status: str, reason: str | None, report_id: str | None = None) -> None:
        store.record_schedule_result(s["id"], status, reason, report_id=report_id)
        self._audit(s, "report_schedule_" + status, f"{reason or ''} {report_id or ''}".strip())

    def _off(self, store, s: dict, reason: str) -> dict:
        store.update_schedule(s["tenant_id"], s["id"], enabled=False)
        self._record(store, s, "failed", reason)
        return {"status": "failed", "reason": reason}

    def process(self, s: dict, now: float, store, cfg: dict, tenants: dict) -> dict | None:
        tc = tenants.get(s["tenant_id"])
        user = self.dash.svc.users.get(s["created_by"])
        if tc is None or not tc.enabled:
            return self._off(store, s, "business_off")
        if user is None or not user.enabled or not user.can_access_tenant(tc.tenant_id):
            return self._off(store, s, "owner_gone")
        config = store.get_config(tc.tenant_id, s["config_id"])
        if config is None or not config.get("blocks"):
            return self._off(store, s, "settings_gone")
        due = s["next_run"]
        run_at = latest_occurrence(s, tc, due, now, cfg)
        run_date = datetime.fromtimestamp(run_at, _tz(tc)).date()
        start, end = period_for(s["frequency"], run_date)
        late = now - run_at
        new_next = occurrence(s, tc, now, cfg)
        # ---- is the data there? (wait up to data_grace_days, then give up on this run)
        ready = self._data_ready(user, tc, end)
        if not ready and late < cfg["data_grace_days"] * 86400:
            return None                                          # not claimed: look again at the next tick
        reason = ("missed" if late > cfg["max_late_hours"][s["frequency"]] * 3600 else
                  None if ready else "data_not_ready")
        if not store.claim_schedule(s["id"], due, new_next):
            return None                                          # another worker has it
        if reason:
            self._record(store, s, "skipped", reason)
            return {"status": "skipped", "reason": reason}
        body = {"blocks": config["blocks"], "start": start.isoformat(), "end": end.isoformat(),
                "compare": config.get("compare") or cfg["compare"], "lang": s.get("lang") or business_lang(tc),
                "profile": config.get("profile"), "period_preset": f"schedule:{s['frequency']}"}
        try:
            res = self.create(user, tc, body, s["id"])
        except ServiceError as exc:
            if exc.status in (502, 503):                    # store / data not usable right now: try again soon
                store.set_next_run(s["id"], new_next, due)
                return {"status": "retry", "reason": "unavailable"}
            why = {429: "quota", 400: "nothing_to_show"}.get(exc.status, "refused")
            self._record(store, s, "skipped", why)
            return {"status": "skipped", "reason": why}
        except ReportStoreError:
            store.set_next_run(s["id"], new_next, due)
            raise
        rid = (res.get("report") or {}).get("id") or (res.get("job") or {}).get("id")
        if (res.get("job") or {}).get("status") == "failed":     # it failed before we could record it as created
            self._record(store, s, "failed", "report_failed", report_id=rid)
            return {"status": "failed", "reason": "report_failed"}
        self.runs += 1
        self._record(store, s, "created", None, report_id=rid)
        return {"status": "created", "report_id": rid, "period": [start.isoformat(), end.isoformat()],
                "reused": bool(res.get("reused"))}

    def _data_ready(self, user, tc, period_end: date) -> bool:
        try:
            _, _, _, st = self.dash.visible(user, tc)
            return bool(st.get("max_date")) and date.fromisoformat(str(st["max_date"])[:10]) >= period_end
        except (ServiceError, ValueError):
            return False
