"""Regular reports: the schedule screen's API (v48/v49, AI plan Step 25).

Signed in, role tenant_admin or higher (the report is made and mailed on behalf of the person who sets it up):
  GET    /api/tenants/{tid}/report-schedules               the schedules, their next runs, recipients and last outcome
  POST   /api/tenants/{tid}/report-schedules               make one: the ticked figures + when + who
  PATCH  /api/tenants/{tid}/report-schedules/{sid}         change when / who / language / on-off / (optionally) the figures
  DELETE /api/tenants/{tid}/report-schedules/{sid}
  POST   /api/tenants/{tid}/report-schedules/{sid}/run     make and mail the last complete period now (once per 10 minutes)
  POST   /api/tenants/{tid}/report-schedules/preview       the next runs and the period each one reports, for the form

A schedule is a saved report setup (``report_configs``) plus a time rule (``report_schedules``); the scheduler
(``reports/scheduler.py``) does the rest. Addresses are stored on the schedule, but a mail only goes to an address
that confirmed itself by link (``report_recipients``); adding an address here asks it to confirm.
"""
from __future__ import annotations

import logging
import re
import time

from fastapi import Depends, FastAPI
from pydantic import BaseModel, Field

from .catalog import load_catalog
from .i18n import norm_lang
from .mailer import settings_from_env
from .report_store import ReportStoreError
from .reports import scheduler as S
from .reports.blocks import all_block_ids, load_spec
from .reports.recipients import (RecipientError, load_config as mail_config, normalize_address, public_url,
                                 request_confirmation)
from .services import ServiceError

log = logging.getLogger("vdash.schedules")
MAX_BLOCKS = 400
RUN_COOLDOWN_SECONDS = 600
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
RECIPIENT_ERRORS = {"bad_address": "Not an e-mail address", "recipients_limit": "The business has reached its limit of addresses",
                    "too_many_requests": "This address was asked too often today - try again tomorrow",
                    "no_public_url": "Sending mail is not set up yet", "mail_not_configured": "Sending mail is not set up yet",
                    "mail_failed": "The confirmation mail could not be sent"}


class ScheduleBody(BaseModel):
    name: str | None = Field(default=None, max_length=80)
    blocks: list[str] | None = Field(default=None, max_length=MAX_BLOCKS)
    profile: str | None = Field(default=None, max_length=40)
    frequency: str = Field(max_length=16)
    day_of_week: int | None = None
    day_of_month: int | None = None
    send_time: str = "08:00"
    lang: str | None = Field(default=None, max_length=8)
    recipients: list[str] = Field(default_factory=list, max_length=100)
    enabled: bool = True


class SchedulePatch(BaseModel):
    name: str | None = Field(default=None, max_length=80)
    blocks: list[str] | None = Field(default=None, max_length=MAX_BLOCKS)
    profile: str | None = Field(default=None, max_length=40)
    frequency: str | None = Field(default=None, max_length=16)
    day_of_week: int | None = None
    day_of_month: int | None = None
    send_time: str | None = None
    lang: str | None = Field(default=None, max_length=8)
    recipients: list[str] | None = Field(default=None, max_length=100)
    enabled: bool | None = None


class PreviewBody(BaseModel):
    frequency: str = Field(max_length=16)
    day_of_week: int | None = None
    day_of_month: int | None = None
    send_time: str = "08:00"


def _when(frequency, day_of_week, day_of_month, send_time) -> dict:
    """Validated time rule: only the day that belongs to the frequency is kept."""
    if frequency not in S.FREQUENCIES:
        raise ServiceError(400, "Choose weekly, monthly or quarterly")
    if not isinstance(send_time, str) or not _HHMM.match(send_time):
        raise ServiceError(400, "The time must look like 08:00")
    if frequency == "weekly":
        if day_of_week is not None and not 0 <= day_of_week <= 6:
            raise ServiceError(400, "Choose a weekday")
        return {"frequency": frequency, "day_of_week": day_of_week, "day_of_month": None, "send_time": send_time}
    if day_of_month is not None and not 1 <= day_of_month <= 28:
        raise ServiceError(400, "The day of the month must be from 1 to 28")
    return {"frequency": frequency, "day_of_week": None, "day_of_month": day_of_month, "send_time": send_time}


def register(app: FastAPI, dash, tenant_guard) -> None:
    store = lambda: dash.svc.reports   # noqa: E731 - replaced in tests

    def db(call):
        try:
            return call()
        except ReportStoreError as exc:
            log.warning("report store: %s", exc)
            raise ServiceError(503, "Not available right now - please try again in a few minutes") from None

    def admin(g):
        user, tc = g
        if not user.has_role("tenant_admin"):
            raise ServiceError(403, "Only a business administrator can manage regular reports")
        return user, tc

    def cfg() -> dict:
        return S.load_config()

    def mail_ready() -> dict:
        key, sender = settings_from_env()
        try:
            public_url()
            url = True
        except RecipientError:
            url = False
        return {"configured": bool(key and sender), "public_url": url}

    def check_blocks(tc, blocks: list[str]) -> list[str]:
        ids = all_block_ids(load_catalog(tc.catalog), load_spec())
        out = list(dict.fromkeys(str(b) for b in blocks))
        bad = [b for b in out if b not in ids]
        if not out:
            raise ServiceError(400, "Tick at least one figure for the report")
        if bad:
            raise ServiceError(400, "Some of the chosen figures do not exist any more - reload the page and choose again")
        return out

    def add_recipients(user, tc, addresses: list[str], lang: str, *, ask: list[str]) -> tuple[list[dict], list[dict]]:
        """Normalised, de-duplicated addresses of a schedule (as stored) and notes about those that could not be asked."""
        mc, seen, notes = mail_config(), [], []
        for a in addresses:
            try:
                a = normalize_address(a)
            except RecipientError as exc:
                raise ServiceError(400, RECIPIENT_ERRORS.get(exc.code, "Not an e-mail address") + f": {str(a)[:60]}") from None
            if a not in seen:
                seen.append(a)
        if len(seen) > int(mc["max_per_schedule"]):
            raise ServiceError(400, f"At most {int(mc['max_per_schedule'])} addresses per regular report")
        for a in seen:
            if a not in ask:
                continue
            try:
                db(lambda: request_confirmation(store(), tc.tenant_id, tc.display_name, a, user.username, lang))
            except RecipientError as exc:
                notes.append({"address": a, "code": exc.code, "message": RECIPIENT_ERRORS.get(exc.code, "Not possible")})
        return [{"type": "email", "address": a} for a in seen], notes

    def lang_of(tc, lang) -> str:
        return norm_lang(lang) if lang else S.business_lang(tc)

    def view(s: dict, tc, config: dict | None, recs: dict, now: float) -> dict:
        addrs = []
        for r in s.get("recipients") or []:
            a = str((r.get("address") if isinstance(r, dict) else r) or "").lower()
            if a:
                rec = recs.get(a)
                addrs.append({"address": a, "state": rec["status"] if rec else "unknown"})
        try:
            nxt = S.preview(s, tc, now, 3, cfg()) if s.get("enabled") else []
        except ValueError:
            nxt = []
        return {"id": s["id"], "name": (config or {}).get("name") or "", "profile": (config or {}).get("profile"),
                "blocks": len((config or {}).get("blocks") or []), "frequency": s["frequency"],
                "day_of_week": s.get("day_of_week"), "day_of_month": s.get("day_of_month"), "send_time": s["send_time"],
                "lang": s.get("lang") or S.business_lang(tc), "enabled": bool(s.get("enabled")), "recipients": addrs,
                "next_runs": nxt, "last_run": s.get("last_run"), "last_status": s.get("last_status"),
                "last_error": s.get("last_error"), "last_report_id": s.get("last_report_id"),
                "can_run": bool(s.get("enabled")) and (now - (s.get("last_run") or 0)) >= RUN_COOLDOWN_SECONDS,
                "created_by": s.get("created_by")}

    def listing(tc) -> dict:
        now = time.time()
        rows = store().list_schedules(tc.tenant_id)
        configs = {c["id"]: c for c in store().list_configs(tc.tenant_id)}
        recs = {r["address"]: r for r in store().list_recipients(tc.tenant_id)}
        mc, c = mail_config(), cfg()
        return {"schedules": [view(s, tc, configs.get(s["config_id"]), recs, now) for s in rows],
                "mail": mail_ready(), "timezone": getattr(tc, "timezone", "UTC") or "UTC",
                "defaults": c["default_day"], "business_lang": S.business_lang(tc),
                "limits": {"schedules": int(c.get("max_schedules_per_business", 10)),
                           "recipients_per_schedule": int(mc["max_per_schedule"])}}

    # ------------------------------------------------------------ reading
    @app.get("/api/tenants/{tid}/report-schedules")
    def schedules_list(g=Depends(tenant_guard)):
        user, tc = admin(g)
        return db(lambda: listing(tc))

    @app.post("/api/tenants/{tid}/report-schedules/preview")
    def schedules_preview(body: PreviewBody, g=Depends(tenant_guard)):
        user, tc = admin(g)
        w = _when(body.frequency, body.day_of_week, body.day_of_month, body.send_time)
        return {"runs": S.preview(w, tc, None, 3, cfg()), "timezone": getattr(tc, "timezone", "UTC") or "UTC"}

    # ------------------------------------------------------------ making
    @app.post("/api/tenants/{tid}/report-schedules")
    def schedules_create(body: ScheduleBody, g=Depends(tenant_guard)):
        user, tc = admin(g)
        w = _when(body.frequency, body.day_of_week, body.day_of_month, body.send_time)
        blocks = check_blocks(tc, body.blocks or [])
        lg = lang_of(tc, body.lang)
        if len(db(lambda: store().list_schedules(tc.tenant_id))) >= int(cfg().get("max_schedules_per_business", 10)):
            raise ServiceError(429, "This business has reached its limit of regular reports - delete one first")
        recipients, notes = add_recipients(user, tc, body.recipients, lg, ask=[a for a in body.recipients])
        name = (body.name or "").strip() or {"weekly": "Weekly report", "monthly": "Monthly report",
                                              "quarterly": "Quarterly report"}[w["frequency"]]
        try:
            conf = db(lambda: store().create_config(tc.tenant_id, user.username, name=name, blocks=blocks,
                                                    profile=body.profile or "custom", compare=cfg().get("compare", "previous"),
                                                    period_preset=f"schedule:{w['frequency']}"))
            s = db(lambda: store().create_schedule(tc.tenant_id, conf["id"], user.username, recipients=recipients,
                                                   enabled=body.enabled, lang=lg, **w))
        except ValueError as exc:
            raise ServiceError(400, str(exc)) from None
        dash.svc.state.audit(user.username, tc.tenant_id, "report_schedule_create",
                             f"{s['id']} {w['frequency']} {w['send_time']} {len(blocks)} blocks {len(recipients)} addresses")
        out = db(lambda: listing(tc))
        out["created"] = s["id"]
        out["notes"] = notes
        return out

    # ------------------------------------------------------------ changing
    def own(tc, sid: str) -> tuple[dict, dict | None]:
        s = db(lambda: store().get_schedule(tc.tenant_id, sid))
        if s is None:
            raise ServiceError(404, "Regular report not found")
        return s, db(lambda: store().get_config(tc.tenant_id, s["config_id"]))

    @app.patch("/api/tenants/{tid}/report-schedules/{sid}")
    def schedules_update(sid: str, body: SchedulePatch, g=Depends(tenant_guard)):
        user, tc = admin(g)
        s, conf = own(tc, sid)
        given = body.model_dump(exclude_unset=True)
        w = _when(given.get("frequency", s["frequency"]),
                  given.get("day_of_week", s.get("day_of_week")), given.get("day_of_month", s.get("day_of_month")),
                  given.get("send_time", s["send_time"]))
        fields: dict = dict(w)
        retime = any(w[k] != s.get(k) for k in w)
        if "lang" in given:
            fields["lang"] = lang_of(tc, given["lang"])
        if "enabled" in given and given["enabled"] is not None:
            fields["enabled"] = bool(given["enabled"])
            retime = retime or (fields["enabled"] and not s.get("enabled"))
        notes: list[dict] = []
        if "recipients" in given and given["recipients"] is not None:
            old = {str((r.get("address") if isinstance(r, dict) else r) or "").lower() for r in s.get("recipients") or []}
            lg = fields.get("lang") or s.get("lang") or S.business_lang(tc)
            new = [a for a in given["recipients"] if str(a).strip().lower() not in old]
            fields["recipients"], notes = add_recipients(user, tc, given["recipients"], lg, ask=new)
        if retime:
            fields["next_run"] = None                         # the scheduler gives it its next run from the new rule
        cf: dict = {}
        if given.get("name") and given["name"].strip():
            cf["name"] = given["name"].strip()
        if given.get("blocks") is not None:
            cf["blocks"] = check_blocks(tc, given["blocks"])
            cf["profile"] = given.get("profile") or "custom"
        if cf and conf is not None:
            db(lambda: store().update_config(tc.tenant_id, conf["id"], **cf))
        db(lambda: store().update_schedule(tc.tenant_id, sid, **fields))
        dash.svc.state.audit(user.username, tc.tenant_id, "report_schedule_update", f"{sid} {sorted(set(fields) | set(cf))}")
        out = db(lambda: listing(tc))
        out["notes"] = notes
        return out

    @app.delete("/api/tenants/{tid}/report-schedules/{sid}")
    def schedules_delete(sid: str, g=Depends(tenant_guard)):
        user, tc = admin(g)
        s, conf = own(tc, sid)
        db(lambda: store().delete_schedule(tc.tenant_id, sid))
        if conf is not None and not any(x["config_id"] == conf["id"] for x in db(lambda: store().list_schedules(tc.tenant_id))):
            db(lambda: store().delete_config(tc.tenant_id, conf["id"]))      # the setup was made for this schedule only
        dash.svc.state.audit(user.username, tc.tenant_id, "report_schedule_delete", sid)
        return db(lambda: listing(tc))

    @app.post("/api/tenants/{tid}/report-schedules/{sid}/run")
    def schedules_run(sid: str, g=Depends(tenant_guard)):
        """Make the last complete period's report now and mail it: the schedule is just due at once (the scheduler's
        next pass, within a minute, does the normal work - period, data check, quota, mails) and then continues as before."""
        user, tc = admin(g)
        s, conf = own(tc, sid)
        now = time.time()
        if not s.get("enabled"):
            raise ServiceError(409, "Switch the regular report on first")
        if conf is None or not conf.get("blocks"):
            raise ServiceError(409, "This regular report has no figures any more - choose them again")
        if now - (s.get("last_run") or 0) < RUN_COOLDOWN_SECONDS:
            raise ServiceError(429, "It ran a moment ago - wait a few minutes before running it again")
        db(lambda: store().update_schedule(tc.tenant_id, sid, next_run=now - 1))
        dash.svc.state.audit(user.username, tc.tenant_id, "report_schedule_run_now", sid)
        out = db(lambda: listing(tc))
        out["started"] = sid
        return out
