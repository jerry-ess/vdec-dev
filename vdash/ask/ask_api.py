"""HTTP routes for "Ask this report" (talk-to-data Step 1). Registered by ``reports_api.register``; every route goes
through ``tenant_guard`` and only reaches the signed-in person's own stored reports.

POST /api/tenants/{tid}/reports/mine/{rid}/ask            {"question": "..."}
    Answers from that report's fact sheet only. Counted against the plan's ``ask_per_month`` only when a real AI call
    is made (cache hits, rule-written answers and failed calls are free). When the limit is used up the answer is still
    given, written by rules, with ``limited: true``.
POST /api/tenants/{tid}/reports/mine/{rid}/ask/{aid}/feedback   {"rating": 1 | -1, "note": "..."}   thumbs up / down
GET  /api/tenants/{tid}/reports/mine/{rid}/ask            the person's last questions on this report + the month's usage
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections import OrderedDict, deque

from fastapi import Depends, FastAPI
from pydantic import BaseModel, Field

from ..catalog import load_catalog
from ..i18n import bundle
from ..reports.facts import build_fact_sheet
from ..reports.plans import month_key, plan_for, resets_on
from ..services import ServiceError
from . import embeddings, report_ask

log = logging.getLogger("vdash.ask")


_LLM_CACHE: dict = {}
from ..llm import MODEL_ALIASES   # noqa: E402,F401  (short names -> the API model ids; kept here for old imports)


def ask_llm(dash):
    """The AI client for questions. ``settings.json -> ask.llm`` ({"provider", "model", "thinking", ...}) overrides the
    report writer's ``llm`` block for questions only (e.g. a cheaper model); without it the shared client is used."""
    raw = getattr(getattr(dash.svc, "settings", None), "raw", None) or {}
    over = (raw.get("ask") or {}).get("llm")
    if not isinstance(over, dict) or not any(v not in (None, "") for v in over.values()):
        return dash.svc.llm
    key = json.dumps(over, sort_keys=True, default=str)
    cli = _LLM_CACHE.get(key)
    if cli is None:
        from ..llm import llm_from_settings
        base = dict(raw.get("llm") or {})
        base.pop("fallback", None)                     # v88: the report writer's backup is not the questions' backup
        if over.get("provider") and str(over["provider"]).strip().lower() != str(base.get("provider") or "deepseek").strip().lower():
            base.pop("model", None)                    # the other provider's default model unless one is named
        merged = {**base, **{k: v for k, v in over.items() if v not in (None, "")}}
        cli = _LLM_CACHE[key] = llm_from_settings({"llm": merged})   # model aliases and the "fallback" are resolved there
    return cli


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


class FeedbackRequest(BaseModel):
    rating: int
    note: str | None = Field(default=None, max_length=500)


class _Limiter:
    """Per person: at most N questions a minute and M an hour (protects the AI budget from loops and scripts)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._hits: dict[str, deque] = {}

    def allow(self, key: str, per_minute: int, per_hour: int, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        with self._lock:
            q = self._hits.setdefault(key, deque())
            while q and q[0] < now - 3600:
                q.popleft()
            if len(q) >= per_hour or sum(1 for t in q if t >= now - 60) >= per_minute:
                return False
            q.append(now)
            return True


def _credits_view(bal: dict) -> dict:
    """The few balance numbers a screen shows next to an answer (credits, not millicredits)."""
    from ..ai_credits_api import level_of
    level, pct = level_of(bal, {})
    return {"mode": bal["mode"], "unlimited": bal["unlimited"], "available": int(round(bal["available_mc"] / 1000)),
            "used_this_month": int(round(bal["used_this_month_mc"] / 1000)), "resets_at": bal["resets_at"],
            "level": level, "percent": pct}


def register(app: FastAPI, dash, tenant_guard, store, db) -> None:
    """``store()`` -> the report store, ``db(call)`` -> runs a store call (an unreachable database becomes a 503)."""
    sheets: OrderedDict[tuple, dict] = OrderedDict()
    sheets_lock = threading.Lock()
    limiter = _Limiter()
    app.state.ask_limiter = limiter

    def sheet_of(tc, rid: str, doc: dict) -> dict:
        key = (tc.tenant_id, rid)
        with sheets_lock:
            if key in sheets:
                sheets.move_to_end(key)
                return sheets[key]
        sh = build_fact_sheet(doc, load_catalog(tc.catalog))
        with sheets_lock:
            sheets[key] = sh
            while len(sheets) > 32:
                sheets.popitem(last=False)
        return sh

    def _credits_now(tc) -> dict | None:
        try:
            return _credits_view(db(lambda: store().credits.balance(tc)))
        except Exception:   # noqa: BLE001 - a balance problem never loses an answer
            return None

    def usage_of(tc, counted: bool) -> dict:
        plan, month = plan_for(tc), month_key(tc)
        lim = plan.get("ask_per_month")
        used = db(lambda: store().usage(tc.tenant_id, month, ask=True))
        return {"limit": lim, "used": used, "remaining": None if lim is None else max(lim - used, 0),
                "resets_on": resets_on(tc), "counted": counted}

    @app.post("/api/tenants/{tid}/reports/mine/{rid}/ask")
    def ask(rid: str, body: AskRequest, g=Depends(tenant_guard)):
        user, tc = g
        dash.visible(user, tc)
        prompt = report_ask.load_prompt()
        question = " ".join(body.question.split())
        if not question:
            raise ServiceError(400, "Please type a question")
        if len(question) > int(prompt.get("max_question_chars", 300)):
            raise ServiceError(400, f"Please keep the question under {prompt.get('max_question_chars', 300)} characters")
        rl = prompt.get("rate_limit") or {}
        if not limiter.allow(f"{tc.tenant_id}:{user.username}", int(rl.get("per_minute", 10)), int(rl.get("per_hour", 60))):
            raise ServiceError(429, "Too many questions in a short time - please wait a moment")
        r = db(lambda: store().get_report(tc.tenant_id, user.username, rid))
        if r is None:
            raise ServiceError(404, "Report not found")
        doc = r["doc"]
        lg = doc["meta"]["lang"]
        msgs = {**bundle("en")["ui"], **bundle(lg)["ui"]}
        sheet = sheet_of(tc, rid, doc)
        plan, month = plan_for(tc), month_key(tc)
        counted = not user.is_platform_admin               # VDec staff (support) never use a customer's allowance
        llm = ask_llm(dash)
        allow_ai = bool(tc.ai_reports)
        limit = plan.get("ask_per_month")
        from ..ai_credits import ai_action

        with ai_action(store(), tc, user, "ask_report", ref_type="report", ref_id=rid, language=lg) as act:
            # v83: with credits enforced a question is paid in credits (hold before the call, charge the accepted
            # answer after); otherwise the plan's ask_per_month count applies as before
            def reserve() -> bool:
                if act.enforcing:
                    return db(lambda: act.hold())
                return (not counted) or db(lambda: store().reserve_report(tc.tenant_id, month, limit, ask=True))

            def release() -> None:
                if act.enforcing or not counted:
                    return
                try:
                    store().release_report(tc.tenant_id, month, ask=True)
                except Exception:   # noqa: BLE001
                    log.error("ask: could not give back a reserved question for %s %s", tc.tenant_id, month)

            res = report_ask.answer_question(doc, sheet, question, llm, store(), tc.tenant_id, month, report_id=rid,
                                             msgs=msgs, allow_ai=allow_ai, reserve=reserve, release=release,
                                             embedder=embeddings.from_settings(getattr(getattr(dash.svc, "settings", None), "raw", None)))
            act.settle("last" if res["source"] == "ai" else "none")
        if act.hold_failed:      # v83: no credits left - AI features are blocked until a top-up
            bal = db(lambda: store().credits.balance(tc))
            dash.svc.state.audit(user.username, tc.tenant_id, "report_ask", f"{rid} blocked=no_credits")
            return {"id": None, "blocked": "no_credits", "answer": "", "in_scope": True, "source": "blocked", "facts": [],
                    "evidence": [], "follow_ups": [], "limited": True, "note": msgs.get("ai_credits_blocked"),
                    "credits": _credits_view(bal), "usage": usage_of(tc, counted)}
        m = res["meta"]
        try:
            aid = store().add_ask(tc.tenant_id, rid, user.username, question, res["answer"], res["source"], res["reason"],
                                  res["in_scope"], res["facts"], res["charged"], m.get("tokens_in", 0), m.get("tokens_out", 0),
                                  m.get("cost_usd", 0.0), m.get("ms", 0))
        except Exception as exc:   # noqa: BLE001 - a log problem never loses the answer
            log.warning("ask log not written: %s", exc)
            aid = None
        dash.svc.state.audit(user.username, tc.tenant_id, "report_ask",
                             f"{rid} source={res['source']} reason={res['reason'] or '-'} charged={int(res['charged'])} "
                             f"scope={int(res['in_scope'])}")
        limited = res["reason"] == "quota"
        note = msgs.get("ask_limit_note") if limited else (msgs.get("ask_rules_note") if res["source"] == "rules" else None)
        return {"id": aid, "answer": res["answer"], "in_scope": res["in_scope"], "source": res["source"], "facts": res["facts"],
                "evidence": res["evidence"], "follow_ups": res["follow_ups"], "limited": limited, "note": note,
                "usage": usage_of(tc, counted), "credits": _credits_now(tc)}

    @app.post("/api/tenants/{tid}/reports/mine/{rid}/ask/{aid}/feedback")
    def ask_feedback(rid: str, aid: str, body: FeedbackRequest, g=Depends(tenant_guard)):
        user, tc = g
        if body.rating not in (1, -1):
            raise ServiceError(400, "rating must be 1 or -1")
        ok = db(lambda: store().rate_ask(tc.tenant_id, rid, user.username, aid, body.rating, body.note))
        if not ok:
            raise ServiceError(404, "Answer not found")
        dash.svc.state.audit(user.username, tc.tenant_id, "report_ask_feedback", f"{rid} {aid} {body.rating}")
        return {"ok": True}

    @app.get("/api/tenants/{tid}/reports/mine/{rid}/ask")
    def ask_history(rid: str, g=Depends(tenant_guard)):
        user, tc = g
        dash.visible(user, tc)
        rows = db(lambda: store().list_asks(tc.tenant_id, rid, user.username, 20))
        return {"asks": [{"id": x["id"], "question": x["question"], "answer": x["answer"], "source": x["source"],
                          "rating": x["rating"], "created_at": x["created_at"]} for x in rows],
                "usage": usage_of(tc, not user.is_platform_admin)}
