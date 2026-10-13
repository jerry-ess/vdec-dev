"""Report limits per payment plan (v34) - config/report_plans.json.

* A business (tenant) has one plan: ``plan`` in tenants.json, else ``default_plan``.
* ``reports_per_month``: reports the business may CREATE per calendar month, counted in the business's own time
  zone (resets on the 1st, 00:00 local time). ``null`` = no limit.
* ``keep_per_user``: how many of each person's created reports stay in "My reports"; the oldest are removed
  first (silently) when a new one is created.
* ``ai_reports_per_month`` (v41): how many of those reports may get the AI-written story; ``null`` = all. When it is
  used up, reports are still created (counted as reports) with the rule-written texts and a note. A report whose
  AI writing failed is not counted against it.
* ``ask_per_month`` (talk-to-data Step 1): how many AI-answered questions to stored reports ("Ask this report") the
  business may use per calendar month; ``null`` = no limit. Only a real AI call counts; repeated questions (cache),
  rule-written answers and failed calls are free. When used up, questions still get a rule-written answer.
"""
from __future__ import annotations

import json
import threading
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from ..config import config_dir

_LOCK = threading.Lock()
_CACHE: dict[str, tuple[float, dict]] = {}


def plans_path() -> Path:
    return config_dir() / "report_plans.json"


def load_plans(path: Path | None = None) -> dict:
    path = path or plans_path()
    if not path.is_file():
        return {"default_plan": "unlimited", "plans": {"unlimited": {"title": "Unlimited", "reports_per_month": None,
                                                                      "keep_per_user": 20}}}
    mtime = path.stat().st_mtime
    with _LOCK:
        hit = _CACHE.get(str(path))
        if hit and hit[0] == mtime:
            return hit[1]
        raw = json.loads(path.read_text(encoding="utf-8"))
        _CACHE[str(path)] = (mtime, raw)
        return raw


def validate_plans(raw: dict) -> list[str]:
    errs = []
    plans = raw.get("plans")
    if not isinstance(plans, dict) or not plans:
        return ["plans: missing or empty"]
    if raw.get("default_plan") not in plans:
        errs.append(f"default_plan {raw.get('default_plan')!r} is not one of the plans")
    for pid, p in plans.items():
        lim = p.get("reports_per_month")
        if lim is not None and (not isinstance(lim, int) or isinstance(lim, bool) or lim < 0):
            errs.append(f"{pid}: reports_per_month must be a whole number >= 0 or null")
        ai = p.get("ai_reports_per_month")
        if ai is not None and (not isinstance(ai, int) or isinstance(ai, bool) or ai < 0):
            errs.append(f"{pid}: ai_reports_per_month must be a whole number >= 0 or null")
        ak = p.get("ask_per_month")
        if ak is not None and (not isinstance(ak, int) or isinstance(ak, bool) or ak < 0):
            errs.append(f"{pid}: ask_per_month must be a whole number >= 0 or null")
        ml = p.get("mails_per_month")
        if ml is not None and (not isinstance(ml, int) or isinstance(ml, bool) or ml < 0):
            errs.append(f"{pid}: mails_per_month must be a whole number >= 0 or null")
        keep = p.get("keep_per_user")
        if not isinstance(keep, int) or isinstance(keep, bool) or keep < 1:
            errs.append(f"{pid}: keep_per_user must be a whole number >= 1")
    return errs


def plan_for(tc) -> dict:
    raw = load_plans()
    plans = raw.get("plans", {})
    pid = getattr(tc, "plan", "") or raw.get("default_plan")
    if pid not in plans:
        pid = raw.get("default_plan")
    p = plans.get(pid, {})
    return {"id": pid, "title": p.get("title", pid), "reports_per_month": p.get("reports_per_month"),
            "ai_reports_per_month": p.get("ai_reports_per_month"),
            "mails_per_month": p.get("mails_per_month"), "ask_per_month": p.get("ask_per_month"),
            "keep_per_user": int(p.get("keep_per_user") or 20)}


def _local_now(tc, now: datetime | None = None) -> datetime:
    try:
        tz = ZoneInfo(getattr(tc, "timezone", "UTC") or "UTC")
    except Exception:   # noqa: BLE001 - unknown zone name: count in UTC
        tz = ZoneInfo("UTC")
    return (now or datetime.now(tz=ZoneInfo("UTC"))).astimezone(tz)


def month_key(tc, now: datetime | None = None) -> str:
    return _local_now(tc, now).strftime("%Y-%m")


def resets_on(tc, now: datetime | None = None) -> str:
    d = _local_now(tc, now).date()
    return (date(d.year + 1, 1, 1) if d.month == 12 else date(d.year, d.month + 1, 1)).isoformat()
