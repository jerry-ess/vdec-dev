"""HTTP routes of the talk-to-data chat (Step 5). Registered by ``reports_api.register`` (it shares the report store);
every route goes through ``tenant_guard`` and only reaches the signed-in person's own conversations.

POST   /api/tenants/{tid}/ask                       {"question", "conversation_id"?, "lang"?}   ?stream=false for one JSON reply
         -> text/event-stream:  status -> status -> evidence -> status -> answer -> done   (see ``vdash.ask.chat``)
GET    /api/tenants/{tid}/ask/config                starter questions, limits, this month's usage, credits
GET    /api/tenants/{tid}/ask/conversations         the person's conversations, newest first
GET    /api/tenants/{tid}/ask/conversations/{cid}   one conversation with its messages
DELETE /api/tenants/{tid}/ask/conversations/{cid}   delete one (the person's right at any time)
DELETE /api/tenants/{tid}/ask/conversations         delete all of the person's conversations
POST   /api/tenants/{tid}/ask/messages/{mid}/feedback   {"rating": 1 | -1, "reason"?, "note"?}
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
from datetime import datetime, timezone

from fastapi import Depends, FastAPI
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..config import config_dir
from ..reports.plans import month_key, plan_for, resets_on
from ..services import ServiceError
from . import chat as chatlib
from .ask_api import _credits_view, _Limiter, ask_llm
from .chat_messages import cmsg
from .tools import ToolContext
from .understand.state import ConversationState

log = logging.getLogger("vdash.ask.chat")
_CFG_CACHE: dict = {}


def load_chat_config(path=None) -> dict:
    path = path or (config_dir() / "ask_chat.json")
    if not path.is_file():
        return {"enabled": False}
    m = path.stat().st_mtime
    hit = _CFG_CACHE.get(str(path))
    if hit and hit[0] == m:
        return hit[1]
    raw = json.loads(path.read_text(encoding="utf-8"))
    _CFG_CACHE[str(path)] = (m, raw)
    return raw


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    conversation_id: str | None = Field(default=None, max_length=64)
    lang: str | None = Field(default=None, max_length=8)


class ChatFeedback(BaseModel):
    rating: int
    reason: str | None = Field(default=None, max_length=32)
    note: str | None = Field(default=None, max_length=500)


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


def _message_out(m: dict) -> dict:
    p = m.get("payload") or {}
    out = {"id": m["id"], "role": m["role"], "content": m["content"], "created_at": m["created_at"]}
    if m["role"] == "assistant":
        out.update({"behavior": m.get("behavior"), "source": m.get("source"), "limited": m.get("limited"),
                    "rating": m.get("rating"), "rating_reason": m.get("rating_reason"),
                    **{k: p.get(k) for k in ("table", "chart", "follow_ups", "caveats", "evidence", "note", "kind", "headline", "points", "verdict")}})
    return out


def register(app: FastAPI, dash, tenant_guard, store, db) -> None:
    limiter = _Limiter()
    pcache = chatlib.PlanningCache()
    warmer = chatlib.Warmer()
    app.state.chat_limiter, app.state.chat_planning_cache, app.state.chat_warmer = limiter, pcache, warmer
    running: set = set()
    running_lock = threading.Lock()

    def usage_of(tc, counted: bool) -> dict:
        plan, month = plan_for(tc), month_key(tc)
        lim = plan.get("ask_per_month")
        used = db(lambda: store().usage(tc.tenant_id, month, ask=True))
        return {"limit": lim, "used": used, "remaining": None if lim is None else max(lim - used, 0),
                "resets_on": resets_on(tc), "counted": counted}

    def credits_now(tc) -> dict | None:
        try:
            return _credits_view(db(lambda: store().credits.balance(tc)))
        except Exception:   # noqa: BLE001
            return None

    def day_start() -> float:
        return datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

    def enabled(tc, cfg: dict) -> bool:
        return bool(cfg.get("enabled")) and getattr(tc, "talk_to_data", None) is not False

    @app.get("/api/tenants/{tid}/ask/config")
    def ask_config(lang: str | None = None, g=Depends(tenant_guard)):
        user, tc = g
        cfg = load_chat_config()
        lg = chatlib.chat_lang(lang)
        counted = not user.is_platform_admin
        if enabled(tc, cfg) and cfg.get("prewarm"):                 # opening the chat starts the slow first queries
            warmer.start(lambda: ToolContext.build(dash, user, tc, lg), pcache, cfg["prewarm"], sync=bool(cfg.get("prewarm_sync")))
        return {"enabled": enabled(tc, cfg), "lang": lg, "starters": (cfg.get("starters") or {}).get(lg) or [],
                "deeper": {k: v.get(lg) or v.get("en") for k, v in (cfg.get("deeper") or {}).items() if isinstance(v, dict)},
                "max_question_chars": int(cfg.get("max_question_chars", 400)), "history_days": int(cfg.get("history_days", 90)),
                "rate": cfg.get("rate") or {}, "feedback_reasons": cfg.get("feedback_reasons") or [],
                "ai": bool(tc.ai_reports), "usage": usage_of(tc, counted), "credits": credits_now(tc)}

    @app.post("/api/tenants/{tid}/ask")
    def ask(body: ChatRequest, stream: bool = True, g=Depends(tenant_guard)):
        user, tc = g
        cfg = load_chat_config()
        lang = chatlib.chat_lang(body.lang)
        if not enabled(tc, cfg):
            raise ServiceError(404, cmsg("disabled", lang))
        question = " ".join(body.question.split())
        if not question:
            raise ServiceError(400, cmsg("empty", lang))
        mx = int(cfg.get("max_question_chars", 400))
        if len(question) > mx:
            raise ServiceError(400, cmsg("too_long", lang, n=mx))
        conv = None
        if body.conversation_id:
            conv = db(lambda: store().chat_get(tc.tenant_id, user.username, body.conversation_id))
            if conv is None:
                raise ServiceError(404, cmsg("not_found", lang))
            if body.lang is None:
                lang = chatlib.chat_lang(conv.get("lang"))
            if int(conv.get("turns") or 0) * 2 >= int(cfg.get("max_messages_per_conversation", 80)):
                raise ServiceError(400, cmsg("long_chat", lang))
        rate = cfg.get("rate") or {}
        key = f"{tc.tenant_id}:{user.username}"
        if not limiter.allow(key, int(rate.get("per_user_per_minute", 8)), int(rate.get("per_user_per_hour", 80))):
            raise ServiceError(429, cmsg("rate_minute", lang), headers={"Retry-After": "30"})
        day_max = rate.get("per_tenant_per_day")
        if day_max is not None and db(lambda: store().chat_questions_since(tc.tenant_id, day_start())) >= int(day_max):
            raise ServiceError(429, cmsg("rate_day", lang))
        ctx = ToolContext.build(dash, user, tc, lang)           # 404 / 503 "preparing" before anything is stored
        with running_lock:
            if key in running:
                raise ServiceError(429, cmsg("busy", lang), headers={"Retry-After": "5"})
            running.add(key)
        try:
            if conv is None:
                cid = db(lambda: store().chat_create(tc.tenant_id, user.username, lang))
                state = ConversationState()
            else:
                cid, state = conv["id"], ConversationState.from_dict(conv.get("state"))
        except Exception:
            with running_lock:
                running.discard(key)
            raise
        title = " ".join(question.split())[:60]
        counted = not user.is_platform_admin
        q: queue.Queue = queue.Queue()

        def work(emit):
            try:
                return chatlib.run_turn(dash=dash, store=store, user=user, tc=tc, ctx=ctx, pcache=pcache, base_llm=ask_llm(dash),
                                        allow_ai=bool(tc.ai_reports), cid=cid, question=question, lang=lang, state=state,
                                        title=title, emit=emit,
                                        extras=lambda: {"usage": usage_of(tc, counted), "credits": credits_now(tc)})
            finally:
                with running_lock:
                    running.discard(key)

        if not stream:
            events: list = []
            res = work(lambda e, d: events.append((e, d)))
            done = next((d for e, d in events if e == "done"), {})
            return {**{k: v for k, v in res.items() if k != "done"}, "done": done}

        def worker():
            try:
                work(lambda e, d: q.put((e, d)))
            except Exception:   # noqa: BLE001 - run_turn catches its own; this is the last net
                log.exception("chat worker crashed")
                q.put(("error", {"message": cmsg("error", lang)}))
            finally:
                q.put(None)

        threading.Thread(target=worker, name="ask-chat", daemon=True).start()

        def gen():
            yield _sse("start", {"conversation_id": cid})
            while True:
                try:
                    item = q.get(timeout=10)
                except queue.Empty:
                    yield ": keepalive\n\n"
                    continue
                if item is None:
                    return
                yield _sse(*item)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no", "Connection": "keep-alive"})

    @app.get("/api/tenants/{tid}/ask/conversations")
    def conversations(g=Depends(tenant_guard)):
        user, tc = g
        rows = db(lambda: store().chat_list(tc.tenant_id, user.username, 30))
        return {"conversations": rows, "retention_days": int(load_chat_config().get("history_days", 90))}

    @app.get("/api/tenants/{tid}/ask/conversations/{cid}")
    def conversation(cid: str, g=Depends(tenant_guard)):
        user, tc = g
        conv = db(lambda: store().chat_get(tc.tenant_id, user.username, cid))
        if conv is None:
            raise ServiceError(404, cmsg("not_found", "en"))
        msgs = db(lambda: store().chat_messages(tc.tenant_id, user.username, cid))
        return {"conversation": {k: conv[k] for k in ("id", "title", "lang", "turns", "created_at", "updated_at")},
                "messages": [_message_out(m) for m in msgs]}

    @app.delete("/api/tenants/{tid}/ask/conversations/{cid}")
    def conversation_delete(cid: str, g=Depends(tenant_guard)):
        user, tc = g
        if not db(lambda: store().chat_delete(tc.tenant_id, user.username, cid)):
            raise ServiceError(404, cmsg("not_found", "en"))
        dash.svc.state.audit(user.username, tc.tenant_id, "ask_delete", f"conv={cid[:8]}")
        return {"ok": True}

    @app.delete("/api/tenants/{tid}/ask/conversations")
    def conversations_delete(g=Depends(tenant_guard)):
        user, tc = g
        n = db(lambda: store().chat_delete_all(tc.tenant_id, user.username))
        dash.svc.state.audit(user.username, tc.tenant_id, "ask_delete", f"all n={n}")
        return {"ok": True, "deleted": n}

    @app.post("/api/tenants/{tid}/ask/messages/{mid}/feedback")
    def feedback(mid: str, body: ChatFeedback, g=Depends(tenant_guard)):
        user, tc = g
        if body.rating not in (1, -1):
            raise ServiceError(400, "rating must be 1 or -1")
        reasons = load_chat_config().get("feedback_reasons") or []
        reason = body.reason if body.reason in reasons else None
        if not db(lambda: store().chat_rate(tc.tenant_id, user.username, mid, body.rating, reason, body.note)):
            raise ServiceError(404, cmsg("not_found", "en"))
        dash.svc.state.audit(user.username, tc.tenant_id, "ask_feedback", f"msg={mid[:8]} rating={body.rating} reason={reason or '-'}")
        return {"ok": True}
