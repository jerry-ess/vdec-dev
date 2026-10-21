"""Talk-to-data Step 5: one chat turn, from the question to the stored answer.

``run_turn`` is plain Python (no HTTP): it plans, runs the tools, writes the answer, charges the AI use and stores both
messages, and reports progress through ``emit(event, data)``. ``chat_api`` turns those events into a server-sent
stream; tests call it directly.

Events, in order:  status (understanding) -> status (looking) -> evidence -> status (writing) -> answer -> done
The answer text is released only after the writer's number guard has passed (``write_answer`` checks every figure
before it returns), so the stream never shows a number that was not computed by a tool.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from typing import Callable

from ..ai_credits import ai_action, current_action
from ..llm import LLMError
from ..reports.plans import month_key, plan_for, resets_on
from .answer import write_answer
from .chat_messages import cmsg
from .tools import ToolContext, registry
from .understand.context import PlanningContext
from .understand.planner import plan_question, update_state
from .understand.state import ConversationState

log = logging.getLogger("vdash.ask.chat")
LANGS = ("en", "vi", "de")
Emit = Callable[[str, dict], None]


def chat_lang(lang: str | None) -> str:
    """The chat understands English, Vietnamese and German; anything else (e.g. Chinese UI) is answered in English."""
    lang = (lang or "en").lower()[:2]
    return lang if lang in LANGS else "en"


# ----------------------------------------------------------------------------------------------- AI use, per call
class LedgerLLM:
    """Wraps the AI client for one part of a turn ("planner" or "writer"): every provider call becomes one row in the
    AI usage ledger of the current action (with that purpose), and the turn's tokens and cost are added up. A failed call
    is recorded as failed and re-raised, so the planner / writer fall back to rules exactly as before."""

    def __init__(self, llm, purpose: str, totals: dict):
        self._llm, self.purpose, self.totals = llm, purpose, totals

    def __getattr__(self, name):
        return getattr(self._llm, name)

    def _record_failed_attempts(self, act, attempts, kw) -> None:
        """v88 fail-over: each model that failed before a backup answered is one failed row (never charged)."""
        part = self.totals.setdefault(self.purpose, {"calls": 0, "failed": 0, "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0, "ms": 0})
        for a in attempts or []:
            part["failed"] += 1
            part["tokens_in"] += a.get("tokens_in", 0) or 0
            part["tokens_out"] += a.get("tokens_out", 0) or 0
            part["cost_usd"] += a.get("cost_usd", 0.0) or 0.0
            if act is not None:
                act.record(status="failed", tokens_in=a.get("tokens_in", 0), tokens_out=a.get("tokens_out", 0),
                           cost_usd=a.get("cost_usd", 0.0), error_kind=a.get("kind"), llm=self._llm, provider=a.get("provider"),
                           model=a.get("model"), temperature=kw.get("temperature"), max_output_tokens=kw.get("max_tokens"),
                           thinking=kw.get("thinking"), purpose=self.purpose)

    def complete_json(self, system: str, user: str, **kw):
        act = current_action()
        t0 = time.perf_counter()
        part = self.totals.setdefault(self.purpose, {"calls": 0, "failed": 0, "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0, "ms": 0})
        part["calls"] += 1
        try:
            res = self._llm.complete_json(system, user, **kw)
        except LLMError as e:
            part["failed"] += 1
            part["tokens_in"] += getattr(e, "tokens_in", 0) or 0
            part["tokens_out"] += getattr(e, "tokens_out", 0) or 0
            part["cost_usd"] += getattr(e, "cost_usd", 0.0) or 0.0
            part["ms"] += int((time.perf_counter() - t0) * 1000)
            self._record_failed_attempts(act, getattr(e, "failed_attempts", None), kw)
            if act is not None:
                act.record(status="failed", tokens_in=getattr(e, "tokens_in", 0), tokens_out=getattr(e, "tokens_out", 0),
                           cost_usd=getattr(e, "cost_usd", 0.0), latency_ms=int((time.perf_counter() - t0) * 1000),
                           error_kind=e.kind, llm=self._llm, temperature=kw.get("temperature"),
                           max_output_tokens=kw.get("max_tokens"), thinking=kw.get("thinking"), purpose=self.purpose,
                           provider=getattr(e, "provider", None), model=getattr(e, "model", None))
            raise
        self._record_failed_attempts(act, getattr(res, "failed_attempts", None), kw)
        part["tokens_in"] += res.tokens_in
        part["tokens_out"] += res.tokens_out
        part["cost_usd"] += res.cost_usd
        part["ms"] += res.ms
        if act is not None:
            res.call_id = act.record(status="ok", tokens_in=res.tokens_in, tokens_out=res.tokens_out, tokens_reasoning=res.thinking_tokens,
                                     cost_usd=res.cost_usd, latency_ms=res.ms, llm=self._llm, temperature=kw.get("temperature"),
                                     max_output_tokens=kw.get("max_tokens"), thinking=kw.get("thinking"), provider=res.provider,
                                     model=res.model, purpose=self.purpose)
        return res


# ----------------------------------------------------------------------------------------------- planning context cache
class PlanningCache:
    """The planning context (KPI catalog, tools, synonyms) is the same for everyone of a business until its data changes,
    so it is built once per (business, data version) and only given the asker's role and language."""

    def __init__(self, size: int = 16):
        self._lock = threading.Lock()
        self._items: OrderedDict[tuple, PlanningContext] = OrderedDict()
        self.size = size
        self.builds = 0

    def get(self, ctx: ToolContext) -> PlanningContext:
        key = (ctx.tc.tenant_id, ctx.st.get("data_version"), ctx.tc.catalog)
        with self._lock:
            base = self._items.get(key)
            if base is not None:
                self._items.move_to_end(key)
        if base is None:
            base = PlanningContext.from_tool_context(ctx)
            with self._lock:
                self._items[key] = base
                self.builds += 1
                while len(self._items) > self.size:
                    self._items.popitem(last=False)
        return base.with_role(ctx.user.role, ctx.lang)


# ----------------------------------------------------------------------------------------------- warm-up
class Warmer:
    """The first "why did revenue change" of a loaded data version is slow (14-27 s: the engine builds the bridge and the
    movers); asked again it is instant, because results are cached by data version. When someone opens the chat, the
    common questions are run once in the background (per business and data version, per process) so that the first real
    question finds them ready. Failures are ignored: warming never changes an answer."""

    def __init__(self):
        self._lock = threading.Lock()
        self._done: set = set()
        self.runs = 0

    def start(self, build_ctx: Callable[[], ToolContext], pcache: "PlanningCache", calls: list[dict], sync: bool = False) -> bool:
        try:
            ctx = build_ctx()
        except Exception:   # noqa: BLE001 - the data is not ready yet: nothing to warm
            return False
        key = (ctx.tc.tenant_id, ctx.st.get("data_version"))
        with self._lock:
            if key in self._done:
                return False
            self._done.add(key)

        def run():
            t0 = time.time()
            try:
                pcache.get(ctx)
                for c in calls:
                    registry.call(ctx, c["tool"], c.get("args") or {})
                self.runs += 1
                log.info("chat warm-up for %s took %.1fs", key[0], time.time() - t0)
            except Exception:   # noqa: BLE001
                log.exception("chat warm-up failed for %s", key[0])
        if sync:
            run()
        else:
            threading.Thread(target=run, name="ask-warm", daemon=True).start()
        return True


# ----------------------------------------------------------------------------------------------- evidence
def evidence_of(plan_steps: list[dict], results: list[dict]) -> list[dict]:
    """What the answer is based on, shown before it: one entry per tool call (what was looked at, for which period)."""
    out = []
    for i, r in enumerate(results):
        r = r or {}
        meta = r.get("meta") or {}
        kpi = meta.get("kpi") or {}
        per = meta.get("period") or {}
        out.append({"tool": (plan_steps[i].get("tool") if i < len(plan_steps) else None) or r.get("tool"),
                    "ok": bool(r.get("ok")), "error": None if r.get("ok") else (r.get("error") or "failed"),
                    "kpi": kpi.get("id"), "title": kpi.get("title"), "period": per.get("text"),
                    "compare": per.get("compare") or None,
                    "data_through": meta.get("data_through")})
    return out


def _tool_names(steps: list[dict]) -> str:
    return ",".join(s["tool"] for s in steps) or "-"


# ----------------------------------------------------------------------------------------------- the turn
def run_turn(*, dash, store, user, tc, ctx: ToolContext, pcache: PlanningCache, base_llm, allow_ai: bool, cid: str, question: str,
             lang: str, state: ConversationState, title: str | None, emit: Emit, extras: Callable[[], dict] | None = None) -> dict:
    """One question, start to finish. Never raises: a failure becomes an ``error`` event and a stored apology."""
    t_start = time.time()
    emit("status", {"stage": "understanding"})
    plan_cfg, month = plan_for(tc), month_key(tc)
    counted = not user.is_platform_admin           # VDec staff (support) never use a customer's allowance
    totals: dict = {}
    limited: str | None = None
    reserved = False
    answer: dict = {}
    mid = None
    try:
        store().chat_add_message(tc.tenant_id, user.username, cid, "user", question, title=title)
        with ai_action(store(), tc, user, "talk_to_data", ref_type="conversation", ref_id=cid, language=lang) as act:
            use_ai = bool(allow_ai and base_llm is not None and getattr(base_llm, "enabled", True) and getattr(base_llm, "key", True))
            if not use_ai:
                limited = "ai_off"
            else:
                if act.enforcing:
                    if not act.hold():
                        use_ai, limited = False, "no_credits"
                elif counted:
                    reserved = bool(store().reserve_report(tc.tenant_id, month, plan_cfg.get("ask_per_month"), ask=True))
                    if not reserved:
                        use_ai, limited = False, "quota"
            planner_llm = LedgerLLM(base_llm, "planner", totals) if use_ai else None
            writer_llm = LedgerLLM(base_llm, "writer", totals) if use_ai else None

            prev_turn = state.brief()
            pctx = pcache.get(ctx)
            plan = plan_question(question, pctx, llm=planner_llm, state=state)
            steps = plan.steps if plan.behavior == "answer" else []
            evidence: list[dict] = []
            if plan.behavior == "answer":
                emit("status", {"stage": "looking"})
                results = [registry.call(ctx, s["tool"], s.get("args") or {}) for s in steps]
                evidence = evidence_of(steps, results)
                emit("evidence", {"items": evidence})
                emit("status", {"stage": "writing"})
                ans = write_answer(question, results, lang, writer_llm, steps=steps, previous=prev_turn)
                update_state(state, question, plan)
                answer = {"text": ans.text, "source": ans.source, "kind": ans.kind, "table": ans.table, "chart": ans.chart,
                          "follow_ups": ans.follow_ups, "caveats": ans.caveats,
                          "headline": ans.headline, "points": ans.points, "verdict": ans.verdict}
            else:                                       # clarify / refuse / adhoc: fixed wording, nothing to look up
                emit("evidence", {"items": []})
                answer = {"text": plan.message, "source": "fixed", "kind": plan.behavior, "table": None, "chart": None,
                          "follow_ups": [], "caveats": [], "headline": plan.message, "points": [], "verdict": None}
            ai_ok = any(c.get("status") == "ok" for c in act.calls)
            act.settle("all" if ai_ok else "none")
            charged_mc = act.charged_mc
        if reserved and not ai_ok:
            try:
                store().release_report(tc.tenant_id, month, ask=True)    # a question the AI did not answer is free
            except Exception:   # noqa: BLE001
                log.error("chat: could not give back a reserved question for %s %s", tc.tenant_id, month)
        reserved_used = reserved and ai_ok

        note = None
        if limited == "quota":
            note = cmsg("quota", lang, date=resets_on(tc))
        elif limited == "no_credits":
            note = cmsg("no_credits", lang)
        elif limited == "ai_off":
            note = cmsg("ai_off", lang)
        answer.update({"behavior": plan.behavior, "limited": limited, "note": note, "evidence": evidence,
                       "tools": [s["tool"] for s in steps], "planner": plan.source})
        tin = sum(p["tokens_in"] for p in totals.values())
        tout = sum(p["tokens_out"] for p in totals.values())
        cost = sum(p["cost_usd"] for p in totals.values())
        ms = int((time.time() - t_start) * 1000)
        payload = {k: answer[k] for k in ("table", "chart", "follow_ups", "caveats", "evidence", "tools", "note", "kind", "planner", "headline", "points", "verdict")}
        mid = store().chat_add_message(tc.tenant_id, user.username, cid, "assistant", answer["text"], payload=payload,
                                       behavior=plan.behavior, source=answer["source"], limited=limited, tokens_in=tin,
                                       tokens_out=tout, cost_usd=cost, ms=ms, state=state.to_dict())
        answer.update({"id": mid, "conversation_id": cid, "ms": ms})
        emit("answer", answer)
        dash.svc.state.audit(user.username, tc.tenant_id, "ask",
                             f"conv={cid[:8]} behavior={plan.behavior} source={answer['source']} planner={plan.source} "
                             f"tools={_tool_names(steps)} limited={limited or '-'} ai_calls={sum(p['calls'] for p in totals.values())} "
                             f"cost_usd={cost:.5f} ms={ms}")
        try:
            more = extras() if extras else {}
        except Exception:   # noqa: BLE001 - a balance problem never loses an answer
            more = {}
        done = {**more, "id": mid, "ms": ms, "ai_used": bool(ai_ok), "counted": bool(reserved_used),
                "cost": {"tokens_in": tin, "tokens_out": tout, "cost_usd": round(cost, 6),
                         **{k: {kk: (round(vv, 6) if isinstance(vv, float) else vv) for kk, vv in v.items()} for k, v in totals.items()}},
                "credits_charged": round(charged_mc / 1000, 3)}
        emit("done", done)
        return {**answer, "done": done}
    except Exception:   # noqa: BLE001 - a turn never takes the server down; the person gets an apology they can retry
        log.exception("chat turn failed (conversation %s)", cid)
        if reserved:
            try:
                store().release_report(tc.tenant_id, month, ask=True)
            except Exception:   # noqa: BLE001
                pass
        text = cmsg("error", lang)
        try:
            mid = store().chat_add_message(tc.tenant_id, user.username, cid, "assistant", text, behavior="error", source="fixed",
                                           payload={"kind": "error"}, state=state.to_dict())
        except Exception:   # noqa: BLE001
            mid = None
        try:
            dash.svc.state.audit(user.username, tc.tenant_id, "ask", f"conv={cid[:8]} behavior=error")
        except Exception:   # noqa: BLE001
            pass
        err = {"text": text, "id": mid, "conversation_id": cid, "kind": "error", "behavior": "error", "source": "fixed",
               "table": None, "chart": None, "follow_ups": [], "caveats": [], "evidence": [], "limited": None, "note": None,
               "headline": text, "points": [], "verdict": None}
        emit("answer", err)
        emit("done", {"id": mid, "ms": int((time.time() - t_start) * 1000), "ai_used": False, "counted": False, "error": True})
        return err
