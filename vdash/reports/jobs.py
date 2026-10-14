"""Background report writing (v41, AI plan Step 17).

``POST /reports/create`` composes the report at once (numbers of that moment, limits checked, the report counted)
and stores a *job* with the composed document (``report_jobs``). A worker thread of the same process then writes
the story (AI when allowed - 1 to 3 minutes - otherwise rule-written) and stores the finished report under the
job's id. The builder polls ``GET /reports/jobs/{id}`` and opens the report when it is done.

Never a half report:
* the report is stored in one transaction at the end (``save_report``) under the job id - a resumed job that had
  already stored it finds it and does not store it twice;
* while a process works on a job it renews the job's heartbeat (``beat_every`` seconds). A job whose heartbeat is
  older than ``stale_after`` (the process restarted, was redeployed or died) is taken over atomically by any
  running process (``claim_stale_jobs``) and written again from its stored document - the AI answer usually comes
  from the AI cache, so a resume rarely pays twice. After ``max_attempts`` the job fails cleanly: the reserved
  report (and AI report) are given back, the user sees "could not be created - not counted".

AI quota (``ai_reports_per_month`` of the plan): reserved just before the story is written; given back when the
story ended up rule-written because the AI failed (or was not reachable). When the quota is used up, or AI is
switched off for the business (``ai_reports: false`` in tenants.json), the report gets the rule-written texts
and a note on the cover (quota, failure). Platform admins (support) are never counted.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from ..catalog import load_catalog
from ..config import load_tenants
from ..fmt import fmt_bucket
from ..i18n import ui
from ..report_store import ReportStoreError
from .plans import plan_for, resets_on

log = logging.getLogger("vdash.reports")

AI_FAILED = ("ai_error", "checks_failed", "no_answer")   # story reasons that mean "the AI was tried and failed"


class ReportJobs:
    def __init__(self, dash, store_fn=None, max_workers: int = 2, stale_after: float = 180.0, beat_every: float = 20.0,
                 max_attempts: int = 3, inline: bool = False):
        self.dash = dash
        self.store_fn = store_fn or (lambda: dash.svc.reports)
        self.worker = str(uuid.uuid4())               # this process
        self.stale_after, self.beat_every, self.max_attempts = stale_after, beat_every, max_attempts
        self.inline = inline                          # tests: run the job inside the request
        self.pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="report-job")
        self.running: set[str] = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._ticker: threading.Thread | None = None

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        """Heartbeats for this process's jobs, and taking over jobs whose process stopped (also right now:
        jobs left over from before a restart)."""
        if self._ticker:
            return
        self._ticker = threading.Thread(target=self._tick_loop, name="report-jobs", daemon=True)
        self._ticker.start()

    def stop(self) -> None:
        self._stop.set()
        self.pool.shutdown(wait=False, cancel_futures=True)

    def _tick_loop(self) -> None:
        last_sweep = 0.0
        while not self._stop.is_set():
            try:
                with self._lock:
                    ids = list(self.running)
                self.store_fn().beat_jobs(ids, self.worker)
                if time.time() - last_sweep >= max(self.beat_every, 30):
                    self.sweep()
                    last_sweep = time.time()
            except ReportStoreError as exc:
                log.warning("report jobs: %s", exc)
            except Exception:   # noqa: BLE001 - the ticker must keep running
                log.exception("report jobs ticker")
            self._stop.wait(self.beat_every)

    def sweep(self) -> int:
        """Take over stale jobs (resume, or fail after max_attempts) and remove old finished ones."""
        store = self.store_fn()
        n = 0
        for job in store.claim_stale_jobs(self.worker, self.stale_after):
            n += 1
            if job["attempts"] > self.max_attempts:
                log.warning("report job %s failed after %s attempts", job["id"], job["attempts"] - 1)
                self._fail(job, "The report could not be finished. It was not counted - please create it again.")
            else:
                log.info("report job %s resumed (attempt %s)", job["id"], job["attempts"])
                self.submit(job["id"])
        try:
            store.prune_jobs()
        except ReportStoreError:
            pass
        return n

    # ------------------------------------------------------------ running a job
    def submit(self, job_id: str) -> None:
        with self._lock:
            if job_id in self.running:
                return
            self.running.add(job_id)
        if self.inline:
            self._run(job_id)
        else:
            self.pool.submit(self._run, job_id)

    def _run(self, job_id: str) -> None:
        try:
            self.run(job_id)
        except Exception:   # noqa: BLE001 - logged; the job is failed cleanly in run()
            log.exception("report job %s", job_id)
        finally:
            with self._lock:
                self.running.discard(job_id)

    def run(self, job_id: str) -> None:
        store = self.store_fn()
        job = store.get_job(None, None, job_id, with_doc=True)
        if not job or job["status"] != "writing" or job["worker"] != self.worker:
            return                                           # finished meanwhile, or taken over elsewhere
        try:
            self._write(store, job)
        except Exception as exc:   # noqa: BLE001 - anything: fail cleanly, give the counts back
            log.exception("report job %s failed", job_id)
            self._fail(job, "The report could not be created. It was not counted - please try again."
                       if not isinstance(exc, ReportStoreError) else
                       "Reports cannot be created right now - please try again in a few minutes. It was not counted.")

    def _write(self, store, job: dict) -> None:
        from ..ai_credits import ai_action
        tc = load_tenants().get(job["tenant_id"])
        if tc is None:
            raise RuntimeError(f"business {job['tenant_id']} no longer exists")
        lg = (job["doc"].get("meta") or {}).get("lang")
        # v83: one AI action per report - every model call is written to the usage ledger; with credits enforced the
        # report's AI story is paid in credits (admin / demo businesses are logged, never charged)
        with ai_action(store, tc, job["created_by"], "report_story", ref_type="report", ref_id=job["id"], language=lg,
                       exempt=not job["counted"]) as act:
            self._write_inner(store, job, act)

    def _write_inner(self, store, job: dict, act) -> None:
        from .story import write_story
        doc, p = job["doc"], job["params"]
        tenants = load_tenants()
        tc = tenants.get(job["tenant_id"])
        if tc is None:
            raise RuntimeError(f"business {job['tenant_id']} no longer exists")
        plan, month, counted = plan_for(tc), job["month"], job["counted"]
        lg = doc["meta"]["lang"]
        # ---- AI allowed for this report?
        allow_ai, reason, ai_reserved = True, None, job["ai_reserved"]
        if not tc.ai_reports:
            allow_ai, reason = False, "ai_off"
        elif act.enforcing:                                  # v83: credits replace the count quota
            if not act.hold():
                allow_ai, reason = False, "ai_credits"
        elif counted and not ai_reserved:
            if store.reserve_report(tc.tenant_id, month, plan["ai_reports_per_month"], ai=True):
                ai_reserved = True
                if not store.update_job(job["id"], worker=self.worker, ai_reserved=True):
                    store.release_report(tc.tenant_id, month, ai=True)   # taken over meanwhile - not ours
                    return
            else:
                allow_ai, reason = False, "ai_quota"
        store.update_job(job["id"], worker=self.worker, step="story")
        from .changes import attach_changes             # v43: 'since your last report' (never fails the report)
        attach_changes(doc, store, tc.tenant_id, job["created_by"])
        try:
            story = write_story(doc, load_catalog(tc.catalog), self.dash.svc.llm, store, tc.tenant_id, month,
                                allow_ai=allow_ai)
        except Exception:   # noqa: BLE001 - a story problem never costs the user the report
            log.exception("story writer failed for %s", tc.tenant_id)
            story = None
        if story is not None:
            if reason:
                story["meta"]["reason"] = reason
            doc["story"] = story
        src = (story or {}).get("source", "rules")
        why = str((story or {}).get("meta", {}).get("reason") or "")
        act.settle("last" if src in ("ai", "mixed") else "none")   # v83: charge the AI story actually used, nothing else
        # ---- AI quota: only an AI-written (ai / mixed) story counts
        if ai_reserved and src == "rules":
            store.release_report(tc.tenant_id, month, ai=True)
            store.update_job(job["id"], worker=self.worker, ai_reserved=False)
            ai_reserved = False
        if story is not None:
            story["meta"]["ai_counted"] = bool(ai_reserved)
        # ---- a note on the cover when the AI story is missing for a reason the user should know
        if reason == "ai_quota":
            used = store.usage(tc.tenant_id, month, ai=True)
            doc["cover"]["story_note"] = ui(lg, "story_note_quota").replace("{used}", str(used)).replace(
                "{limit}", str(plan["ai_reports_per_month"])).replace("{date}", fmt_bucket(resets_on(tc), "day", lg))
        elif reason == "ai_credits":
            doc["cover"]["story_note"] = ui(lg, "story_note_credits")
        elif src == "rules" and (story is None or why.startswith(AI_FAILED)):
            doc["cover"]["story_note"] = ui(lg, "story_note_failed")
        # ---- store the finished report (one transaction, under the job id)
        store.update_job(job["id"], worker=self.worker, step="saving")
        from ..reports_api import report_namer
        row, removed = store.save_report(tc.tenant_id, job["created_by"], doc, job["spec_hash"],
                                         report_namer(doc, p.get("profile")), int(p.get("keep") or plan["keep_per_user"]),
                                         report_id=job["id"], **p["history"])
        store.update_job(job["id"], worker=self.worker, status="done", step="done")
        m = doc["meta"]
        self.dash.svc.state.audit(job["created_by"], tc.tenant_id, "report_create",
                                  f"{row['id']} {m['period']['start']}..{m['period']['end']} "
                                  f"{len(p['history'].get('blocks') or [])} parts story={src}"
                                  f"{('/' + why) if why else ''} ai_counted={ai_reserved} lang={m['lang']} "
                                  f"counted={counted} removed_old={removed} attempt={job['attempts']}")

    def _fail(self, job: dict, message: str) -> None:
        store = self.store_fn()
        try:
            if store.get_report(job["tenant_id"], job["created_by"], job["id"], with_doc=False):
                store.update_job(job["id"], worker=self.worker, status="done", step="done")   # it was stored after all
                return
            if not store.update_job(job["id"], worker=self.worker, status="failed", step="done", error=message):
                return                                       # someone else owns it now
            if job.get("counted"):
                store.release_report(job["tenant_id"], job["month"])
            fresh = store.get_job(None, None, job["id"]) or job
            if fresh.get("ai_reserved"):
                store.release_report(job["tenant_id"], job["month"], ai=True)
                store.update_job(job["id"], ai_reserved=False)
            self.dash.svc.state.audit(job["created_by"], job["tenant_id"], "report_failed", f"{job['id']} {message}")
            sid = ((job.get("params") or {}).get("history") or {}).get("schedule_id")
            if sid:                                          # v44: a scheduled report that could not be finished
                store.record_schedule_result(sid, "failed", "report_failed", only_if_report=job["id"])
        except ReportStoreError as exc:
            log.error("report job %s: could not record the failure: %s", job["id"], exc)
