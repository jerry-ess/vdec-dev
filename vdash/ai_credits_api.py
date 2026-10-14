"""HTTP routes for AI credits (v83). Registered by ``api.create_app``; tenant routes go through ``tenant_guard``.

Business (any signed-in person of the business unless noted):
GET  /api/tenants/{tid}/ai-credits                  balance, plan grant, this month's use by feature and (owner) by person; anyone else sees only their own use,
                                                    the top-up range and the price per 1000 credits in every currency (default currency by language)
GET  /api/tenants/{tid}/ai-credits/usage            ?group=day|feature|user&since=&until=   (business administrator = owner; others see only themselves)
GET  /api/tenants/{tid}/ai-credits/calls            ?limit&offset&user&feature&format=csv   every AI action of the person / business
GET  /api/tenants/{tid}/ai-credits/transactions     (business administrator) the credit book
POST /api/tenants/{tid}/ai-credits/top-up           {credits, currency} -> a pending order with a payment reference (business administrator);
                                                    credits is chosen by the owner within top_up.min_credits..max_credits in steps of step_credits
Platform admin:
GET  /api/admin/ai-credits                          every business: plan, balance, this month's credits used, cost, margin
POST /api/admin/ai-credits/{tid}/adjust             {credits, kind, note, expires_days?}  add (positive) or remove (negative) credits
GET  /api/admin/ai-credits/{tid}/calls|transactions the full ledger incl. provider cost
GET  /api/admin/ai-credits/orders                   top-up orders;  POST .../orders/{id}/confirm | cancel
Credits are shown as credits (millicredits / 1000); real provider cost in USD only to platform admins.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException, Response
from pydantic import BaseModel, Field

from .ai_credits import MC, month_window, usage_csv
from .config import load_tenants
from .report_store import ReportStoreError
from .reports.plans import plan_for
from .services import ServiceError

log = logging.getLogger("vdash.credits")
MAX_ROWS = 500


class TopUpRequest(BaseModel):
    credits: int = Field(description="whole credits to add; within the configured min / max, in steps")
    currency: str = Field(min_length=3, max_length=8)


class AdjustRequest(BaseModel):
    credits: int = Field(description="whole credits; positive adds, negative removes")
    kind: str = "adjustment"                       # top_up | promo | adjustment
    note: str = Field(min_length=3, max_length=300)
    expires_days: int | None = Field(default=None, ge=1, le=3650)


def _cr(mc) -> int:
    """Credits are shown as whole numbers (new charges are whole credits already; this rounds older fractional ones)."""
    return int(round((mc or 0) / MC))


def level_of(bal: dict, cfg: dict) -> tuple[str, int]:
    """Traffic light for the remaining credits: green above the first alert ratio, yellow down to the second, red below.
    Returns (level, percent left of what the business had this month incl. top-ups). Always computed, whatever the mode."""
    if bal["unlimited"] or bal["exempt"]:
        return "none", 100
    base = bal["granted_this_month_mc"] + bal["topup_mc"]
    avail = max(0, bal["available_mc"])
    pct = 0 if base <= 0 else max(0, min(100, int(round(avail / base * 100))))
    lo = sorted(cfg.get("low_balance_alerts") or [0.2, 0.05], reverse=True)
    if avail <= 0 or (base > 0 and avail / base <= lo[-1]):
        return "red", pct
    if base > 0 and avail / base <= lo[0]:
        return "yellow", pct
    return "green", pct


def _call_out(r: dict, admin: bool) -> dict:
    out = {"id": r["id"], "request_id": r["request_id"], "at": r["created_at"], "user": r["user_id"], "feature": r["feature"],
           "purpose": r["purpose"], "ref_type": r["ref_type"], "ref_id": r["ref_id"], "status": r["status"],
           "tokens_in": r["tokens_in"], "tokens_out": r["tokens_out"], "credits": _cr(r["credits_charged_mc"]),
           "charge": r["charge_reason"], "exempt": bool(r["exempt"])}
    if admin:
        out.update(model=r["model"], provider=r["provider"], attempt=r["attempt"], error=r["error_kind"], latency_ms=r["latency_ms"],
                   tokens_reasoning=r["tokens_reasoning"], cost_usd=r["cost_usd_micro"] / 1e6, markup=r["markup"], mode=r["mode"],
                   thinking=r["thinking_level"], price_in_per_m=r["price_in_per_m"], price_out_per_m=r["price_out_per_m"])
    return out


def _rows_out(rows: list[dict], admin: bool) -> list[dict]:
    out = []
    for r in rows:
        o = {"key": r["key"], "calls": r["calls"], "tokens_in": r["tokens_in"], "tokens_out": r["tokens_out"], "credits": _cr(r["credits_mc"])}
        if admin:
            o["cost_usd"] = r["cost_usd_micro"] / 1e6
        if r.get("last_at"):
            o["last_at"] = r["last_at"]
        out.append(o)
    return out


def alert_level(bal: dict, cfg: dict) -> str:
    if bal["unlimited"] or bal["exempt"] or bal["mode"] != "enforce":
        return "ok"
    if bal["available_mc"] <= 0:
        return "empty"
    base = bal["granted_this_month_mc"] + bal["topup_mc"]
    if base <= 0:
        return "ok"
    ratio = bal["available_mc"] / base
    lo = sorted(cfg.get("low_balance_alerts") or [0.2, 0.05], reverse=True)
    if len(lo) > 1 and ratio <= lo[1]:
        return "critical"
    return "low" if ratio <= lo[0] else "ok"


def register(app: FastAPI, dash, tenant_guard, current_user) -> None:
    store = lambda: dash.svc.reports   # noqa: E731 - replaced in tests

    def svc():
        return store().credits

    def db(call):
        try:
            return call()
        except ReportStoreError as exc:
            log.warning("report store: %s", exc)
            raise ServiceError(503, "AI credits cannot be read right now - please try again in a few minutes")

    def window(tc, since: float | None, until: float | None) -> tuple[float, float]:
        _, s, e = month_window(tc)
        return (s if since is None else since), (e if until is None else until)

    def sees_staff(user, tc) -> bool:
        """Platform admins (and demo/test businesses, where everything is exempt) see every row; business users do not see
        the activity of the platform team that is logged in their business (admin / support / platform tests)."""
        return bool(user.is_platform_admin or getattr(tc, "ai_credit_exempt", False))

    def need(user, role: str):
        if not user.has_role(role):
            raise ServiceError(403, "You do not have access to this")

    # ------------------------------------------------------------ business
    @app.get("/api/tenants/{tid}/ai-credits")
    def credits_overview(lang: str = "en", g=Depends(tenant_guard)):
        user, tc = g
        s = db(svc)
        bal = db(lambda: s.balance(tc))
        _, since, until = month_window(tc)
        owner = user.has_role("tenant_admin")           # the owner sees every person's use; everyone else only their own
        inc = sees_staff(user, tc)
        if owner:
            feats = db(lambda: s.usage(tc.tenant_id, since, until, "feature", include_exempt=inc))
        else:
            feats = _own_usage(db(lambda: s.calls(tc.tenant_id, since, until, user=user.username, limit=100000, include_exempt=inc)), "feature", tc)
        level, pct = level_of(bal, s.cfg)
        out = {"mode": bal["mode"], "unlimited": bal["unlimited"], "exempt": bal["exempt"], "plan": plan_for(tc)["id"],
               "plan_credits_per_month": None if bal["unlimited"] else _cr(s.plan_credits_mc(bal["plan"])),
               "balance": {"available": _cr(bal["available_mc"]), "monthly": _cr(bal["monthly_mc"]), "topup": _cr(bal["topup_mc"]),
                           "held": _cr(bal["held_mc"]), "granted_this_month": _cr(bal["granted_this_month_mc"]),
                           "used_this_month": _cr(bal["used_this_month_mc"]), "month": bal["month"],
                           "resets_at": bal["resets_at"]},
               "alert": alert_level(bal, s.cfg), "level": level, "percent": pct, "scope": "business" if owner else "own",
               "by_feature": _rows_out(feats, False) if owner else feats,
               "can_top_up": user.has_role("tenant_admin")}
        if owner:
            out["by_user"] = _rows_out(db(lambda: s.usage(tc.tenant_id, since, until, "user", include_exempt=inc)), False)
        else:
            mine = [r for r in db(lambda: s.usage(tc.tenant_id, since, until, "user", include_exempt=inc)) if r["key"] == user.username]
            out["by_user"] = _rows_out(mine, False)
        cfg = s.cfg
        cur = cfg.get("currencies") or {}
        out["currencies"] = cur
        out["default_currency"] = (cfg.get("language_default_currency") or {}).get((lang or "en")[:2], "USD")
        tu = cfg.get("top_up") or {}
        out["top_up"] = {"min": tu.get("min_credits"), "step": tu.get("step_credits"), "max": tu.get("max_credits"),
                         "quick_picks": tu.get("quick_picks") or [], "price_per_1000": tu.get("price_per_1000") or {}}
        out["placeholder_prices"] = bool(cfg.get("placeholder_prices"))
        out["payment_note"] = (cfg.get("payment_note") or {}).get((lang or "en")[:2]) or (cfg.get("payment_note") or {}).get("en")
        out["orders"] = [{"id": o["id"], "ref": o["ref"], "pack_id": o["pack_id"], "credits": _cr(o["credits_mc"]), "currency": o["currency"],
                          "price": o["price"], "status": o["status"], "at": o["created_at"]}
                         for o in db(lambda: s.list_orders(tc.tenant_id, limit=10))] if user.has_role("tenant_admin") else []
        return out

    @app.get("/api/tenants/{tid}/ai-credits/summary")
    def credits_summary(g=Depends(tenant_guard)):
        """Small and cheap: what the sidebar bar needs (polled while the page is open)."""
        user, tc = g
        s = db(svc)
        bal = db(lambda: s.balance(tc))
        level, pct = level_of(bal, s.cfg)
        return {"mode": bal["mode"], "unlimited": bal["unlimited"], "exempt": bal["exempt"], "available": _cr(bal["available_mc"]),
                "level": level, "percent": pct}

    @app.get("/api/tenants/{tid}/ai-credits/usage")
    def credits_usage(group: str = "day", since: float | None = None, until: float | None = None, g=Depends(tenant_guard)):
        user, tc = g
        if group not in ("day", "feature", "user"):
            raise ServiceError(400, "group must be day, feature or user")
        a, b = window(tc, since, until)
        s = db(svc)
        if user.has_role("tenant_admin"):
            rows = db(lambda: s.usage(tc.tenant_id, a, b, group, tz=ZoneInfo(tc.timezone or "UTC"), include_exempt=sees_staff(user, tc)))
        else:                                              # everyone but the owner sees only their own use
            if group == "user":
                rows = [r for r in db(lambda: s.usage(tc.tenant_id, a, b, "user", include_exempt=sees_staff(user, tc))) if r["key"] == user.username]
            else:
                rows = _own_usage(db(lambda: s.calls(tc.tenant_id, a, b, user=user.username, limit=100000)), group, tc)
                return {"group": group, "rows": rows}
        return {"group": group, "rows": _rows_out(rows, False)}

    def _own_usage(calls, group, tc):
        agg: dict = {}
        tz = ZoneInfo(tc.timezone or "UTC")
        for r in calls:
            k = datetime.fromtimestamp(r["created_at"], tz).strftime("%Y-%m-%d") if group == "day" else r["feature"]
            a = agg.setdefault(k, {"key": k, "calls": 0, "tokens_in": 0, "tokens_out": 0, "credits": 0})
            a["calls"] += 1
            a["tokens_in"] += r["tokens_in"]
            a["tokens_out"] += r["tokens_out"]
            a["credits"] = a["credits"] + _cr(r["credits_charged_mc"])
        return sorted(agg.values(), key=lambda x: x["key"])

    @app.get("/api/tenants/{tid}/ai-credits/calls")
    def credits_calls(limit: int = 50, offset: int = 0, user: str | None = None, feature: str | None = None,
                      since: float | None = None, until: float | None = None, format: str = "json", g=Depends(tenant_guard)):
        u, tc = g
        limit = max(1, min(int(limit), MAX_ROWS if format != "csv" else 20000))
        a, b = window(tc, since, until)
        who = user if u.has_role("tenant_admin") else u.username  # only the owner sees other people's use
        rows = db(lambda: db(svc).calls(tc.tenant_id, a, b, user=who, feature=feature, limit=limit, offset=max(0, int(offset)),
                                        include_exempt=sees_staff(u, tc)))
        if format == "csv":
            return Response(usage_csv(rows), media_type="text/csv; charset=utf-8",
                            headers={"Content-Disposition": f'attachment; filename="ai-usage-{tc.tenant_id}.csv"'})
        return {"calls": [_call_out(r, False) for r in rows]}

    @app.get("/api/tenants/{tid}/ai-credits/transactions")
    def credits_txn(limit: int = 50, offset: int = 0, g=Depends(tenant_guard)):
        u, tc = g
        need(u, "tenant_admin")
        rows = db(lambda: db(svc).transactions(tc.tenant_id, max(1, min(int(limit), MAX_ROWS)), max(0, int(offset))))
        return {"transactions": [{"at": r["created_at"], "type": r["type"], "credits": _cr(r["amount_mc"]), "balance_after": _cr(r["balance_after_mc"]),
                                  "note": r["note"], "by": r["created_by"]} for r in rows]}

    @app.post("/api/tenants/{tid}/ai-credits/top-up")
    def credits_top_up(body: TopUpRequest, lang: str = "en", g=Depends(tenant_guard)):
        u, tc = g
        need(u, "tenant_admin")
        s = db(svc)
        try:
            o = db(lambda: s.request_top_up(tc, body.credits, body.currency.upper(), u.username))
        except ValueError as exc:
            raise ServiceError(400, str(exc))
        dash.svc.state.audit(u.username, tc.tenant_id, "ai_top_up_request", f"{o['ref']} {_cr(o['credits_mc'])} credits {o['currency']} {o['price']}")
        note = (s.cfg.get("payment_note") or {})
        return {"order": {"id": o["id"], "ref": o["ref"], "pack_id": o["pack_id"], "credits": _cr(o["credits_mc"]), "currency": o["currency"],
                          "price": o["price"], "status": o["status"]},
                "payment_note": note.get((lang or "en")[:2]) or note.get("en")}

    # ------------------------------------------------------------ platform admin
    def admin(user):
        if not user.is_platform_admin:
            raise HTTPException(403, "Platform admin only")

    @app.get("/api/admin/ai-credits")
    def admin_overview(user=Depends(current_user)):
        admin(user)
        s = db(svc)
        tenants = load_tenants()
        rows = []
        for tid, tc in tenants.items():
            bal = db(lambda: s.balance(tc))
            _, a, b = month_window(tc)
            tot = db(lambda: s.usage(tid, a, b, "tenant"))
            t = tot[0] if tot else {"calls": 0, "credits_mc": 0, "cost_usd_micro": 0}
            rows.append({"tenant_id": tid, "name": tc.display_name, "plan": bal["plan"], "unlimited": bal["unlimited"], "exempt": bal["exempt"],
                         "available": _cr(bal["available_mc"]), "monthly": _cr(bal["monthly_mc"]), "topup": _cr(bal["topup_mc"]),
                         "granted_this_month": _cr(bal["granted_this_month_mc"]), "used_this_month": _cr(bal["used_this_month_mc"]),
                         "calls_this_month": t["calls"], "cost_usd_this_month": t["cost_usd_micro"] / 1e6,
                         "alert": alert_level(bal, s.cfg), "level": level_of(bal, s.cfg)[0], "percent": level_of(bal, s.cfg)[1]})
        cfg = s.cfg
        return {"mode": s.mode, "markup": cfg.get("markup"), "credit_value_usd": cfg.get("credit_value_usd"),
                "placeholder_plans": [p for p, v in (cfg.get("plans") or {}).items() if v.get("placeholder")],
                "tenants": rows, "pending_orders": db(lambda: s.list_orders(status="pending", limit=50))}

    @app.post("/api/admin/ai-credits/{tid}/adjust")
    def admin_adjust(tid: str, body: AdjustRequest, user=Depends(current_user)):
        admin(user)
        tc = load_tenants().get(tid)
        if tc is None:
            raise HTTPException(404, "Unknown business")
        s = db(svc)
        mc = int(body.credits) * MC
        if mc == 0:
            raise ServiceError(400, "credits must not be 0")
        if body.kind not in ("top_up", "promo", "adjustment"):
            raise ServiceError(400, "kind must be top_up, promo or adjustment")
        if mc > 0:
            exp = time.time() + body.expires_days * 86400 if body.expires_days else None
            lot = db(lambda: s.add_lot(tid, body.kind, mc, user.username, body.note, expires_at=exp))
            done = mc if lot else 0
        else:
            done = -db(lambda: s.remove_credits(tid, -mc, user.username, body.note))
        dash.svc.state.audit(user.username, tid, "ai_credit_adjust", f"{body.kind} {_cr(done)} {body.note}")
        return {"changed": _cr(done), "balance": _cr(db(lambda: s.balance(tc))["available_mc"])}

    @app.get("/api/admin/ai-credits/{tid}/calls")
    def admin_calls(tid: str, limit: int = 100, offset: int = 0, user_id: str | None = None, feature: str | None = None,
                    since: float | None = None, until: float | None = None, user=Depends(current_user)):
        admin(user)
        tc = load_tenants().get(tid)
        if tc is None:
            raise HTTPException(404, "Unknown business")
        a, b = window(tc, since, until)
        rows = db(lambda: db(svc).calls(tid, a, b, user=user_id, feature=feature, limit=max(1, min(int(limit), MAX_ROWS)), offset=max(0, int(offset))))
        return {"calls": [_call_out(r, True) for r in rows]}

    @app.get("/api/admin/ai-credits/{tid}/transactions")
    def admin_txn(tid: str, limit: int = 100, offset: int = 0, user=Depends(current_user)):
        admin(user)
        rows = db(lambda: db(svc).transactions(tid, max(1, min(int(limit), MAX_ROWS)), max(0, int(offset))))
        return {"transactions": [{"at": r["created_at"], "type": r["type"], "credits": _cr(r["amount_mc"]), "balance_after": _cr(r["balance_after_mc"]),
                                  "lot": r["lot_id"], "request_id": r["request_id"], "note": r["note"], "by": r["created_by"]} for r in rows]}

    @app.get("/api/admin/ai-credits/orders")
    def admin_orders(status: str | None = None, user=Depends(current_user)):
        admin(user)
        return {"orders": [{**o, "credits": _cr(o["credits_mc"])} for o in db(lambda: db(svc).list_orders(status=status, limit=200))]}

    @app.post("/api/admin/ai-credits/orders/{oid}/confirm")
    def admin_confirm(oid: str, user=Depends(current_user)):
        admin(user)
        try:
            o = db(lambda: db(svc).confirm_order(oid, user.username))
        except ValueError as exc:
            raise ServiceError(400, str(exc))
        if o is None:
            raise HTTPException(404, "Order not found")
        dash.svc.state.audit(user.username, o["tenant_id"], "ai_top_up_confirm", f"{o['ref']} {_cr(o['credits_mc'])}")
        return {"order": {**o, "credits": _cr(o["credits_mc"])}}

    @app.post("/api/admin/ai-credits/orders/{oid}/cancel")
    def admin_cancel(oid: str, user=Depends(current_user)):
        admin(user)
        if not db(lambda: db(svc).cancel_order(oid, user.username)):
            raise HTTPException(404, "No pending order with this id")
        return {"ok": True}
