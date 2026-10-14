"""AI usage ledger and AI credits (v83) - config/ai_credits.json.

Two things, one place:

1. **Ledger** - ``ai_call_log`` has one row for every provider call (ok, cached, failed), whatever the mode: who, which
   feature, model, tokens, the price used at that moment, the real cost in micro-USD, and what was charged.
   No prompt or answer text is stored (content stays in the report / ask log and is linked by ref_type/ref_id).
2. **Credits** - a business has *lots* (a monthly plan grant that expires at the end of its calendar month, top-ups
   that never expire, admin adjustments). Credits are integers in millicredits (1 credit = 1000 mc), money in
   micro-USD: no floats in the books. Every movement is a row in ``ai_credit_txn`` (append-only).

Charging works per *action* (what the user asked for: one report, one question): ``ai_action(...)`` opens it, ``hold``
takes up to the feature's estimate from the lots (never more than there is), the calls inside write ledger rows through
``cached_complete_json``, and ``settle`` charges the accepted answer only (real cost x markup / credit value) and gives the
rest of the hold back. Failed calls, cache hits, answers our own checks rejected and the first attempt of a retry are
free. mode ``shadow`` records what *would* be charged without touching any balance (to calibrate the grants).
Admin / demo accounts are logged, never charged.
"""
from __future__ import annotations

import contextvars
import csv
import io
import json
from decimal import ROUND_HALF_UP, Decimal
import logging
import math
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import (BigInteger, Boolean, Column, Float, Index, Integer, String, Table, Text, UniqueConstraint, and_,
                        delete, func, insert, or_, select, update)

from .config import config_dir
from .report_store import md

log = logging.getLogger("vdash.credits")

MC = 1000                                   # millicredits per credit
MODES = ("off", "shadow", "enforce")
TOPUP_KINDS = ("top_up", "adjustment")      # spent after the monthly grant

# ---------------------------------------------------------------- tables (created by ReportStore.ensure_schema)
ai_call_log = Table(
    "ai_call_log", md,
    Column("id", String(36), primary_key=True),
    Column("request_id", String(36), nullable=False),            # one user action = one request id (several calls)
    Column("attempt", Integer, nullable=False, default=1),
    Column("tenant_id", String(128), nullable=False),
    Column("user_id", String(128)),
    Column("trigger", String(16), nullable=False, default="user"),     # user | schedule | system
    Column("feature", String(32), nullable=False),                      # report_story | ask_report | talk_* | platform_test | unknown
    Column("purpose", String(64)),
    Column("ref_type", String(32)),
    Column("ref_id", String(64)),
    Column("language", String(8)),
    Column("prompt_version", String(32)),
    Column("provider", String(32)),
    Column("model", String(64)),
    Column("thinking_level", String(16)),
    Column("temperature", Float),
    Column("max_output_tokens", Integer),
    Column("status", String(24), nullable=False),                       # ok | cached | failed
    Column("error_kind", String(32)),
    Column("latency_ms", Integer, nullable=False, default=0),
    Column("tokens_in", Integer, nullable=False, default=0),
    Column("tokens_in_cached", Integer, nullable=False, default=0),
    Column("tokens_out", Integer, nullable=False, default=0),
    Column("tokens_reasoning", Integer, nullable=False, default=0),
    Column("price_in_per_m", Float),                                    # snapshot of the price used (USD per 1M tokens)
    Column("price_out_per_m", Float),
    Column("cost_usd_micro", BigInteger, nullable=False, default=0),    # what the provider charged us
    Column("markup", Float),
    Column("credits_charged_mc", BigInteger, nullable=False, default=0),  # what the user was charged (shadow: would be)
    Column("charge_reason", String(32)),                                # charged | cached | failed | retry | rejected | exempt | unlimited | off | pending
    Column("exempt", Boolean, nullable=False, default=False),
    Column("mode", String(8)),
    Column("created_at", Float, nullable=False),
)
Index("ix_ai_call_log_tenant", ai_call_log.c.tenant_id, ai_call_log.c.created_at)
Index("ix_ai_call_log_request", ai_call_log.c.request_id)

ai_credit_lots = Table(
    "ai_credit_lots", md,
    Column("id", String(36), primary_key=True),
    Column("tenant_id", String(128), nullable=False),
    Column("kind", String(16), nullable=False),                  # plan_grant | top_up | promo | adjustment
    Column("amount_mc", BigInteger, nullable=False),
    Column("remaining_mc", BigInteger, nullable=False),
    Column("granted_at", Float, nullable=False),
    Column("expires_at", Float),                                 # null = never
    Column("source_ref", String(120)),                           # plan:<month>:<plan> | order id | ... (unique per business)
    Column("created_by", String(128)),
    Column("note", Text),
    UniqueConstraint("tenant_id", "source_ref", name="uq_ai_credit_lot_ref"),
)
Index("ix_ai_credit_lots_tenant", ai_credit_lots.c.tenant_id, ai_credit_lots.c.expires_at)

ai_credit_txn = Table(
    "ai_credit_txn", md,
    Column("id", String(36), primary_key=True),
    Column("tenant_id", String(128), nullable=False),
    Column("type", String(16), nullable=False),                  # grant | top_up | hold | release | settle | expire | adjust | refund
    Column("amount_mc", BigInteger, nullable=False),             # signed; the sum over a lot's rows = its remaining credits
    Column("lot_id", String(36)),
    Column("request_id", String(36)),
    Column("balance_after_mc", BigInteger),
    Column("note", Text),
    Column("created_by", String(128)),
    Column("created_at", Float, nullable=False),
    UniqueConstraint("request_id", "type", "lot_id", name="uq_ai_credit_txn_once"),
)
Index("ix_ai_credit_txn_tenant", ai_credit_txn.c.tenant_id, ai_credit_txn.c.created_at)

ai_credit_holds = Table(
    "ai_credit_holds", md,
    Column("id", String(36), primary_key=True),                  # = the action's request id
    Column("tenant_id", String(128), nullable=False),
    Column("amount_mc", BigInteger, nullable=False),
    Column("split_json", Text, nullable=False),                  # [[lot_id, mc], ...] in the order taken
    Column("created_at", Float, nullable=False),
    Column("expires_at", Float, nullable=False),
)

ai_credit_orders = Table(
    "ai_credit_orders", md,
    Column("id", String(36), primary_key=True),
    Column("ref", String(16), nullable=False),                   # short code the customer quotes when paying
    Column("tenant_id", String(128), nullable=False),
    Column("pack_id", String(32), nullable=False),
    Column("credits_mc", BigInteger, nullable=False),
    Column("currency", String(8), nullable=False),
    Column("price", Float, nullable=False),
    Column("status", String(12), nullable=False),                # pending | paid | cancelled
    Column("requested_by", String(128), nullable=False),
    Column("created_at", Float, nullable=False),
    Column("confirmed_by", String(128)),
    Column("confirmed_at", Float),
    Column("lot_id", String(36)),
    Column("note", Text),
)
Index("ix_ai_credit_orders_tenant", ai_credit_orders.c.tenant_id, ai_credit_orders.c.created_at)

# ---------------------------------------------------------------- config
_CFG_LOCK = threading.Lock()
_CFG_CACHE: dict[str, tuple[float, dict]] = {}


def config_path() -> Path:
    return config_dir() / "ai_credits.json"


def load_config(path: Path | None = None) -> dict:
    path = path or config_path()
    if not path.is_file():
        return {"mode": "off", "credit_value_usd": 0.001, "markup": 5.0, "min_markup": 1.0, "features": {}, "plans": {},
                "top_up": {}, "currencies": {}, "language_default_currency": {}, "hold_stale_seconds": 600}
    mtime = path.stat().st_mtime
    with _CFG_LOCK:
        hit = _CFG_CACHE.get(str(path))
        if hit and hit[0] == mtime:
            return hit[1]
        raw = json.loads(path.read_text(encoding="utf-8"))
        _CFG_CACHE[str(path)] = (mtime, raw)
        return raw


def validate_config(raw: dict) -> list[str]:
    errs: list[str] = []
    if raw.get("mode") not in MODES:
        errs.append(f"mode must be one of {MODES}")
    cv = raw.get("credit_value_usd")
    if not isinstance(cv, (int, float)) or isinstance(cv, bool) or cv <= 0:
        errs.append("credit_value_usd must be a number > 0")
    mk, lo = raw.get("markup"), raw.get("min_markup", 1)
    if not isinstance(mk, (int, float)) or isinstance(mk, bool) or mk < lo:
        errs.append(f"markup must be a number >= min_markup ({lo})")
    for f, v in (raw.get("markup_by_feature") or {}).items():
        if not isinstance(v, (int, float)) or v < lo:
            errs.append(f"markup_by_feature.{f} must be >= min_markup ({lo})")
    feats = raw.get("features") or {}
    if "other" not in feats:
        errs.append("features.other is required (the default for a feature without its own entry)")
    for f, v in feats.items():
        for k in ("hold_credits", "min_credits"):
            if not isinstance(v.get(k), (int, float)) or v[k] < 0:
                errs.append(f"features.{f}.{k} must be a number >= 0")
        if v.get("min_credits", 0) > v.get("hold_credits", 0):
            errs.append(f"features.{f}: min_credits must not exceed hold_credits")
    for p, v in (raw.get("plans") or {}).items():
        c = v.get("credits_per_month")
        if c is not None and (not isinstance(c, (int, float)) or isinstance(c, bool) or c < 0):
            errs.append(f"plans.{p}.credits_per_month must be a number >= 0 or null")
    cur = raw.get("currencies") or {}
    for lg, c in (raw.get("language_default_currency") or {}).items():
        if c not in cur:
            errs.append(f"language_default_currency.{lg}: {c} is not in currencies")
    tu = raw.get("top_up") or {}
    mn, st, mx = tu.get("min_credits"), tu.get("step_credits"), tu.get("max_credits")
    if not all(isinstance(x, int) and not isinstance(x, bool) and x > 0 for x in (mn, st, mx)):
        errs.append("top_up: min_credits, step_credits and max_credits must be whole numbers > 0")
    else:
        if mx < mn:
            errs.append("top_up: max_credits must be >= min_credits")
        if mn % st or mx % st:
            errs.append("top_up: min_credits and max_credits must be multiples of step_credits")
    for q in tu.get("quick_picks") or []:
        if isinstance(mn, int) and isinstance(mx, int) and isinstance(st, int) and not (mn <= q <= mx and q % st == 0):
            errs.append(f"top_up.quick_picks: {q} is outside the range or not a multiple of the step")
    for c in cur:
        if not isinstance((tu.get("price_per_1000") or {}).get(c), (int, float)) or tu["price_per_1000"][c] <= 0:
            errs.append(f"top_up.price_per_1000: no price in {c}")
    return errs


def charge_mc(cost_usd_micro: int, markup: float, credit_value_usd: float) -> int:
    """Credits for a real cost: cost x markup / credit value, in WHOLE credits (rounded up, at least 1), as millicredits."""
    if cost_usd_micro <= 0:
        return 0
    return max(1, math.ceil(cost_usd_micro / 1e6 * markup / credit_value_usd - 1e-9)) * MC


def _tz(tc):
    try:
        return ZoneInfo(getattr(tc, "timezone", "UTC") or "UTC")
    except Exception:   # noqa: BLE001
        return ZoneInfo("UTC")


def month_window(tc, now: float | None = None) -> tuple[str, float, float]:
    """(month key, start, end) of the business's calendar month, as epoch seconds."""
    tz = _tz(tc)
    d = datetime.fromtimestamp(time.time() if now is None else now, tz)
    start = datetime(d.year, d.month, 1, tzinfo=tz)
    end = datetime(d.year + (d.month == 12), 1 if d.month == 12 else d.month + 1, 1, tzinfo=tz)
    return d.strftime("%Y-%m"), start.timestamp(), end.timestamp()


# ---------------------------------------------------------------- service
class NoCredits(RuntimeError):
    """Raised by an action that was asked to hold credits the business does not have."""


class CreditService:
    """Everything about the ledger and the credits, on top of the report store's database."""

    def __init__(self, store):
        self.store = store

    # ------------------------------------------------------------ config helpers
    @property
    def cfg(self) -> dict:
        return load_config()

    @property
    def mode(self) -> str:
        m = self.cfg.get("mode")
        return m if m in MODES else "off"

    def feature_cfg(self, feature: str) -> dict:
        f = self.cfg.get("features") or {}
        base = f.get("talk_to_data") if feature.startswith("talk_") and "talk_to_data" in f else None
        return f.get(feature) or base or f.get("other") or {"hold_credits": 30, "min_credits": 10}

    def markup_for(self, feature: str) -> float:
        c = self.cfg
        return float((c.get("markup_by_feature") or {}).get(feature, c.get("markup", 5.0)))

    def plan_credits_mc(self, plan_id: str) -> int | None:
        """The plan's monthly grant in millicredits; None = unlimited."""
        p = (self.cfg.get("plans") or {}).get(plan_id)
        if p is None:
            return 0
        c = p.get("credits_per_month")
        return None if c is None else int(round(float(c) * MC))

    def _begin(self):
        return self.store._begin()

    # ------------------------------------------------------------ lots, grants, expiry
    @staticmethod
    def _spend_order(row) -> tuple:
        return (row["kind"] in TOPUP_KINDS, row["expires_at"] is None, row["expires_at"] or 0.0, row["granted_at"])

    def _live_lots(self, con, tenant_id: str, now: float) -> list[dict]:
        t = ai_credit_lots.c
        rows = con.execute(select(ai_credit_lots).where(and_(
            t.tenant_id == tenant_id, t.remaining_mc > 0, or_(t.expires_at.is_(None), t.expires_at > now)))).fetchall()
        return sorted((dict(r._mapping) for r in rows), key=self._spend_order)

    def _txn(self, con, tenant_id: str, type_: str, amount_mc: int, lot_id: str | None = None, request_id: str | None = None,
             note: str | None = None, by: str | None = None, now: float | None = None) -> None:
        con.execute(insert(ai_credit_txn).values(
            id=str(uuid.uuid4()), tenant_id=tenant_id, type=type_, amount_mc=int(amount_mc), lot_id=lot_id,
            request_id=request_id, balance_after_mc=self._balance_in(con, tenant_id, now or time.time()), note=note,
            created_by=by, created_at=now or time.time()))

    def _balance_in(self, con, tenant_id: str, now: float) -> int:
        t = ai_credit_lots.c
        return int(con.execute(select(func.coalesce(func.sum(t.remaining_mc), 0)).where(and_(
            t.tenant_id == tenant_id, or_(t.expires_at.is_(None), t.expires_at > now)))).scalar() or 0)

    def expire_due(self, tenant_id: str, now: float | None = None) -> int:
        """Write an 'expire' row for every lot that has run out of time and still holds credits."""
        now = time.time() if now is None else now
        t = ai_credit_lots.c
        n = 0
        with self._begin() as con:
            for r in con.execute(select(ai_credit_lots).where(and_(
                    t.tenant_id == tenant_id, t.remaining_mc > 0, t.expires_at.is_not(None), t.expires_at <= now))).fetchall():
                row = dict(r._mapping)
                took = con.execute(update(ai_credit_lots).where(and_(t.id == row["id"], t.remaining_mc == row["remaining_mc"]))
                                   .values(remaining_mc=0)).rowcount
                if took:
                    self._txn(con, tenant_id, "expire", -row["remaining_mc"], row["id"], note=f"{row['kind']} expired", now=now)
                    n += 1
        return n

    def ensure_grant(self, tc, now: float | None = None) -> int:
        """This month's plan grant for the business (idempotent). A plan change in the month adds only the difference
        (an upgrade); a downgrade changes nothing until next month. Returns the millicredits added now."""
        now = time.time() if now is None else now
        target = self.plan_credits_mc(getattr(tc, "plan", "") or self._default_plan())
        if not target:                                           # unlimited (None) or a plan with no credits
            return 0
        month, start, end = month_window(tc, now)
        pid = getattr(tc, "plan", "") or self._default_plan()
        t = ai_credit_lots.c
        with self._begin() as con:
            given = int(con.execute(select(func.coalesce(func.sum(t.amount_mc), 0)).where(and_(
                t.tenant_id == tc.tenant_id, t.kind == "plan_grant", t.source_ref.like(f"plan:{month}:%")))).scalar() or 0)
            if target <= given:
                return 0
            add = target - given
            lot_id = str(uuid.uuid4())
            res = con.execute(self.store._insert_ignore(
                ai_credit_lots, id=lot_id, tenant_id=tc.tenant_id, kind="plan_grant", amount_mc=add, remaining_mc=add,
                granted_at=now, expires_at=end, source_ref=f"plan:{month}:{pid}", created_by="system",
                note=f"{pid} plan, {month}" + (" (upgrade difference)" if given else "")))
            if res.rowcount != 1:
                return 0
            self._txn(con, tc.tenant_id, "grant", add, lot_id, note=f"{pid} plan {month}", by="system", now=now)
            return add

    def _default_plan(self) -> str:
        from .reports.plans import load_plans
        return load_plans().get("default_plan") or ""

    def unlimited(self, tc) -> bool:
        return self.plan_credits_mc(getattr(tc, "plan", "") or self._default_plan()) is None

    # ------------------------------------------------------------ balance
    def balance(self, tc, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        self.sweep(tc.tenant_id, now)
        self.expire_due(tc.tenant_id, now)
        self.ensure_grant(tc, now)
        month, start, end = month_window(tc, now)
        with self._begin() as con:
            lots = self._live_lots(con, tc.tenant_id, now)
            held = int(con.execute(select(func.coalesce(func.sum(ai_credit_holds.c.amount_mc), 0)).where(
                ai_credit_holds.c.tenant_id == tc.tenant_id)).scalar() or 0)
            c = ai_call_log.c
            used = int(con.execute(select(func.coalesce(func.sum(c.credits_charged_mc), 0)).where(and_(
                c.tenant_id == tc.tenant_id, c.created_at >= start, c.created_at < end, c.exempt.is_(False),
                c.status == "ok"))).scalar() or 0)
            granted = int(con.execute(select(func.coalesce(func.sum(ai_credit_lots.c.amount_mc), 0)).where(and_(
                ai_credit_lots.c.tenant_id == tc.tenant_id, ai_credit_lots.c.kind == "plan_grant",
                ai_credit_lots.c.granted_at >= start, ai_credit_lots.c.granted_at < end))).scalar() or 0)
        monthly = sum(r["remaining_mc"] for r in lots if r["kind"] not in TOPUP_KINDS)
        topup = sum(r["remaining_mc"] for r in lots if r["kind"] in TOPUP_KINDS)
        total = monthly + topup
        nxt = min((r["expires_at"] for r in lots if r["expires_at"]), default=None)
        return {"mode": self.mode, "unlimited": self.unlimited(tc), "available_mc": total, "monthly_mc": monthly,
                "topup_mc": topup, "held_mc": held, "granted_this_month_mc": granted, "used_this_month_mc": used,
                "month": month, "resets_at": end, "next_expiry": nxt,
                "exempt": bool(getattr(tc, "ai_credit_exempt", False)), "plan": getattr(tc, "plan", "") or self._default_plan()}

    # ------------------------------------------------------------ holds
    def sweep(self, tenant_id: str, now: float | None = None) -> int:
        """Give back holds nobody settled (a crashed worker): the credits return in full."""
        now = time.time() if now is None else now
        h = ai_credit_holds.c
        with self._begin() as con:
            ids = [r[0] for r in con.execute(select(h.id).where(and_(h.tenant_id == tenant_id, h.expires_at < now))).fetchall()]
        for rid in ids:
            self.settle(rid, 0, note="hold expired")
        return len(ids)

    def hold(self, tc, request_id: str, est_mc: int, min_mc: int, now: float | None = None) -> int | None:
        """Take up to ``est_mc`` from the lots (spend order), never more than there is. None when less than ``min_mc``
        is available: the caller must not call the model."""
        now = time.time() if now is None else now
        self.sweep(tc.tenant_id, now)
        self.expire_due(tc.tenant_id, now)
        self.ensure_grant(tc, now)
        t = ai_credit_lots.c
        for _try in range(3):
            try:
                with self._begin() as con:
                    lots = self._live_lots(con, tc.tenant_id, now)
                    avail = sum(r["remaining_mc"] for r in lots)
                    if avail < max(min_mc, 1):
                        return None
                    want, split = min(est_mc, avail), []
                    for r in lots:
                        if want <= 0:
                            break
                        take = min(r["remaining_mc"], want)
                        if con.execute(update(ai_credit_lots).where(and_(t.id == r["id"], t.remaining_mc >= take))
                                       .values(remaining_mc=t.remaining_mc - take)).rowcount != 1:
                            raise _Retry()
                        split.append([r["id"], take])
                        want -= take
                    got = sum(x[1] for x in split)
                    if got < max(min_mc, 1):
                        raise _Retry()
                    con.execute(insert(ai_credit_holds).values(
                        id=request_id, tenant_id=tc.tenant_id, amount_mc=got, split_json=json.dumps(split),
                        created_at=now, expires_at=now + float(self.cfg.get("hold_stale_seconds") or 600)))
                    for lot_id, mc in split:
                        self._txn(con, tc.tenant_id, "hold", -mc, lot_id, request_id, now=now)
                    return got
            except _Retry:
                continue
        return None

    def settle(self, request_id: str, charge_mc_: int, note: str | None = None, now: float | None = None) -> int:
        """Charge ``charge_mc_`` against the hold and return the rest. Charges beyond the hold are taken from what is
        left, never below zero. Returns what was actually charged. Safe to call twice (the second call charges 0)."""
        now = time.time() if now is None else now
        t = ai_credit_lots.c
        with self._begin() as con:
            h = con.execute(select(ai_credit_holds).where(ai_credit_holds.c.id == request_id)).fetchone()
            if h is None:
                return 0
            tenant_id, held = h.tenant_id, int(h.amount_mc)
            split = [list(x) for x in json.loads(h.split_json)]
            con.execute(delete(ai_credit_holds).where(ai_credit_holds.c.id == request_id))
            charge = max(int(charge_mc_), 0)
            if charge <= held:
                back = held - charge
                for lot_id, mc in reversed(split):               # give back to the lots taken last (top-ups) first
                    r = min(mc, back)
                    if r > 0:
                        con.execute(update(ai_credit_lots).where(t.id == lot_id).values(remaining_mc=t.remaining_mc + r))
                        self._txn(con, tenant_id, "release", r, lot_id, request_id, note=note, now=now)
                        back -= r
                return charge
            extra, taken = charge - held, 0
            for r in self._live_lots(con, tenant_id, now):       # an over-run is taken from what is left
                if extra <= 0:
                    break
                take = min(r["remaining_mc"], extra)
                if con.execute(update(ai_credit_lots).where(and_(t.id == r["id"], t.remaining_mc >= take))
                               .values(remaining_mc=t.remaining_mc - take)).rowcount == 1:
                    self._txn(con, tenant_id, "settle", -take, r["id"], request_id, note="over the hold", now=now)
                    extra -= take
                    taken += take
            return held + taken

    # ------------------------------------------------------------ admin: adjust, top-up
    def add_lot(self, tenant_id: str, kind: str, amount_mc: int, by: str, note: str | None = None,
                expires_at: float | None = None, source_ref: str | None = None, now: float | None = None) -> str | None:
        """New credits (top-up, promo or adjustment). ``source_ref`` makes it idempotent (one lot per order)."""
        now = time.time() if now is None else now
        lot_id = str(uuid.uuid4())
        with self._begin() as con:
            res = con.execute(self.store._insert_ignore(
                ai_credit_lots, id=lot_id, tenant_id=tenant_id, kind=kind, amount_mc=int(amount_mc),
                remaining_mc=int(amount_mc), granted_at=now, expires_at=expires_at, source_ref=source_ref,
                created_by=by, note=note))
            if res.rowcount != 1:
                return None
            self._txn(con, tenant_id, {"top_up": "top_up"}.get(kind, "adjust"), int(amount_mc), lot_id, note=note, by=by, now=now)
        return lot_id

    def remove_credits(self, tenant_id: str, amount_mc: int, by: str, note: str | None = None,
                       now: float | None = None) -> int:
        """Take credits away (a correction or refund), in spend order, never below zero. Returns what was removed."""
        now = time.time() if now is None else now
        t = ai_credit_lots.c
        left, removed = int(amount_mc), 0
        with self._begin() as con:
            for r in self._live_lots(con, tenant_id, now):
                if left <= 0:
                    break
                take = min(r["remaining_mc"], left)
                if con.execute(update(ai_credit_lots).where(and_(t.id == r["id"], t.remaining_mc >= take))
                               .values(remaining_mc=t.remaining_mc - take)).rowcount == 1:
                    self._txn(con, tenant_id, "adjust", -take, r["id"], note=note, by=by, now=now)
                    left -= take
                    removed += take
        return removed

    def top_up_price(self, credits: int, currency: str) -> float:
        """Price of ``credits`` in ``currency`` (credits / 1000 x the configured price per 1000, half-up to the currency's decimals)."""
        tu = self.cfg.get("top_up") or {}
        rate = (tu.get("price_per_1000") or {}).get(currency)
        if rate is None:
            raise ValueError("unknown currency")
        dec = int(((self.cfg.get("currencies") or {}).get(currency) or {}).get("decimals", 2))
        q = (Decimal(int(credits)) * Decimal(str(rate)) / Decimal(1000)).quantize(Decimal(1).scaleb(-dec), rounding=ROUND_HALF_UP)
        return float(q)

    def request_top_up(self, tc, credits: int, currency: str, by: str, now: float | None = None) -> dict:
        tu = self.cfg.get("top_up") or {}
        mn, st, mx = int(tu.get("min_credits") or 0), int(tu.get("step_credits") or 1), int(tu.get("max_credits") or 0)
        if isinstance(credits, bool) or int(credits) != credits:
            raise ValueError("credits must be a whole number")
        credits = int(credits)
        if credits < mn or credits > mx:
            raise ValueError(f"credits must be between {mn} and {mx}")
        if credits % st:
            raise ValueError(f"credits must be a multiple of {st}")
        price = self.top_up_price(credits, currency)
        now = time.time() if now is None else now
        oid = str(uuid.uuid4())
        row = {"id": oid, "ref": "TU" + oid.replace("-", "")[:6].upper(), "tenant_id": tc.tenant_id, "pack_id": "custom",
               "credits_mc": credits * MC, "currency": currency, "price": price, "status": "pending", "requested_by": by, "created_at": now}
        with self._begin() as con:
            con.execute(insert(ai_credit_orders).values(**row))
        return row

    def confirm_order(self, order_id: str, by: str, now: float | None = None) -> dict | None:
        """Payment received: add the credits (one lot per order, even if confirmed twice)."""
        now = time.time() if now is None else now
        o = ai_credit_orders.c
        with self._begin() as con:
            r = con.execute(select(ai_credit_orders).where(o.id == order_id)).fetchone()
            if r is None:
                return None
            row = dict(r._mapping)
            if row["status"] == "cancelled":
                raise ValueError("the order was cancelled")
        exp_days = self.cfg.get("top_up_expiry_days")
        lot = self.add_lot(row["tenant_id"], "top_up", row["credits_mc"], by, f"order {row['ref']}",
                           expires_at=(now + exp_days * 86400) if exp_days else None, source_ref=f"order:{order_id}", now=now)
        with self._begin() as con:
            con.execute(update(ai_credit_orders).where(o.id == order_id).values(
                status="paid", confirmed_by=by, confirmed_at=row["confirmed_at"] or now, lot_id=lot or row["lot_id"]))
            r = con.execute(select(ai_credit_orders).where(o.id == order_id)).fetchone()
        return dict(r._mapping)

    def cancel_order(self, order_id: str, by: str) -> bool:
        o = ai_credit_orders.c
        with self._begin() as con:
            return con.execute(update(ai_credit_orders).where(and_(o.id == order_id, o.status == "pending")).values(
                status="cancelled", confirmed_by=by, confirmed_at=time.time())).rowcount == 1

    def list_orders(self, tenant_id: str | None = None, status: str | None = None, limit: int = 100) -> list[dict]:
        o = ai_credit_orders.c
        q = select(ai_credit_orders)
        if tenant_id:
            q = q.where(o.tenant_id == tenant_id)
        if status:
            q = q.where(o.status == status)
        with self._begin() as con:
            return [dict(r._mapping) for r in con.execute(q.order_by(o.created_at.desc()).limit(limit))]

    # ------------------------------------------------------------ ledger
    def log_call(self, **v) -> str:
        v.setdefault("id", str(uuid.uuid4()))
        v.setdefault("created_at", time.time())
        v.setdefault("attempt", 1)
        with self._begin() as con:
            con.execute(insert(ai_call_log).values(**v))
        return v["id"]

    def finalize_call(self, call_id: str, credits_mc: int, reason: str) -> None:
        c = ai_call_log.c
        with self._begin() as con:
            con.execute(update(ai_call_log).where(c.id == call_id).values(credits_charged_mc=int(credits_mc), charge_reason=reason))

    # ------------------------------------------------------------ reports on the ledger
    def usage(self, tenant_id: str | None, since: float, until: float, group: str = "day", tz=None,
              include_exempt: bool = True) -> list[dict]:
        """Calls, tokens, credits (and cost) by day | feature | user | model | status in a window."""
        c = ai_call_log.c
        if group == "day":
            tzinfo = tz or ZoneInfo("UTC")
            rows = self.calls(tenant_id, since, until, limit=100000, include_exempt=include_exempt)
            agg: dict[str, dict] = {}
            for r in rows:
                k = datetime.fromtimestamp(r["created_at"], tzinfo).strftime("%Y-%m-%d")
                a = agg.setdefault(k, {"key": k, "calls": 0, "tokens_in": 0, "tokens_out": 0, "credits_mc": 0, "cost_usd_micro": 0})
                a["calls"] += 1
                a["tokens_in"] += r["tokens_in"]
                a["tokens_out"] += r["tokens_out"]
                a["credits_mc"] += r["credits_charged_mc"]
                a["cost_usd_micro"] += r["cost_usd_micro"]
            return sorted(agg.values(), key=lambda x: x["key"])
        col = {"feature": c.feature, "user": c.user_id, "model": c.model, "status": c.status, "tenant": c.tenant_id}[group]
        q = select(col, func.count(), func.coalesce(func.sum(c.tokens_in), 0), func.coalesce(func.sum(c.tokens_out), 0),
                   func.coalesce(func.sum(c.credits_charged_mc), 0), func.coalesce(func.sum(c.cost_usd_micro), 0),
                   func.max(c.created_at)).where(and_(c.created_at >= since, c.created_at < until))
        if tenant_id:
            q = q.where(c.tenant_id == tenant_id)
        if not include_exempt:
            q = q.where(c.exempt.is_(False))
        with self._begin() as con:
            rows = con.execute(q.group_by(col).order_by(func.sum(c.credits_charged_mc).desc())).fetchall()
        return [{"key": r[0], "calls": int(r[1]), "tokens_in": int(r[2]), "tokens_out": int(r[3]), "credits_mc": int(r[4]),
                 "cost_usd_micro": int(r[5]), "last_at": r[6]} for r in rows]

    def calls(self, tenant_id: str | None, since: float, until: float, user: str | None = None, feature: str | None = None,
              limit: int = 100, offset: int = 0, include_exempt: bool = True) -> list[dict]:
        c = ai_call_log.c
        q = select(ai_call_log).where(and_(c.created_at >= since, c.created_at < until))
        if not include_exempt:
            q = q.where(c.exempt.is_(False))
        if tenant_id:
            q = q.where(c.tenant_id == tenant_id)
        if user:
            q = q.where(c.user_id == user)
        if feature:
            q = q.where(c.feature == feature)
        with self._begin() as con:
            return [dict(r._mapping) for r in con.execute(q.order_by(c.created_at.desc()).limit(limit).offset(offset))]

    def transactions(self, tenant_id: str, limit: int = 100, offset: int = 0) -> list[dict]:
        t = ai_credit_txn.c
        with self._begin() as con:
            return [dict(r._mapping) for r in con.execute(select(ai_credit_txn).where(t.tenant_id == tenant_id)
                                                          .order_by(t.created_at.desc()).limit(limit).offset(offset))]

    def reconcile(self, tenant_id: str) -> dict:
        """Books check: for every lot, the sum of its transactions must equal what it still holds (plus open holds)."""
        with self._begin() as con:
            lots = {r.id: int(r.remaining_mc) for r in con.execute(select(ai_credit_lots).where(ai_credit_lots.c.tenant_id == tenant_id))}
            sums = {r[0]: int(r[1]) for r in con.execute(select(ai_credit_txn.c.lot_id, func.sum(ai_credit_txn.c.amount_mc)).where(and_(
                ai_credit_txn.c.tenant_id == tenant_id, ai_credit_txn.c.lot_id.is_not(None))).group_by(ai_credit_txn.c.lot_id))}
        bad = {k: (lots.get(k), sums.get(k, 0)) for k in lots if lots[k] != sums.get(k, 0)}
        return {"ok": not bad, "mismatches": bad}


class _Retry(Exception):
    pass


# ---------------------------------------------------------------- the action (one user request)
_CURRENT: contextvars.ContextVar["AiAction | None"] = contextvars.ContextVar("vdash_ai_action", default=None)


def current_action() -> "AiAction | None":
    return _CURRENT.get()


class AiAction:
    """One thing a user asked for (a report, a question). Calls made while it is current are written to the ledger;
    ``settle`` charges the accepted answer and closes it."""

    def __init__(self, svc: CreditService | None, tc, user, feature: str, *, trigger: str = "user", purpose: str | None = None,
                 ref_type: str | None = None, ref_id: str | None = None, language: str | None = None,
                 prompt_version: str | None = None, request_id: str | None = None, exempt: bool | None = None):
        self.svc, self.tc, self.feature, self.trigger = svc, tc, feature, trigger
        self.username = getattr(user, "username", user if isinstance(user, str) else None)
        self.purpose, self.ref_type, self.ref_id = purpose, ref_type, ref_id
        self.language, self.prompt_version = language, prompt_version
        self.request_id = request_id or str(uuid.uuid4())
        self.calls: list[dict] = []                 # {id, status, cost_micro}
        self.held_mc = 0
        self.holding = False
        self.hold_failed = False                    # asked for credits and there were not enough
        self.settled = False
        self.charged_mc = 0
        self.exempt = bool(getattr(tc, "ai_credit_exempt", False)) or bool(getattr(user, "is_platform_admin", False)) or bool(exempt)
        self.unlimited = bool(svc and svc.unlimited(tc)) if svc else False

    # -- mode helpers
    @property
    def mode(self) -> str:
        return self.svc.mode if self.svc else "off"

    @property
    def enforcing(self) -> bool:
        return self.mode == "enforce" and not self.exempt and not self.unlimited

    def hold(self) -> bool:
        """Reserve credits before calling the model. False = not enough: do not call. Always True when nothing is
        enforced (off / shadow / exempt / unlimited plan)."""
        if not self.enforcing:
            return True
        fc = self.svc.feature_cfg(self.feature)
        got = self.svc.hold(self.tc, self.request_id, int(fc["hold_credits"] * MC), int(fc["min_credits"] * MC))
        if got is None:
            self.hold_failed = True
            return False
        self.held_mc, self.holding = got, True
        return True

    def record(self, *, status: str, tokens_in: int = 0, tokens_out: int = 0, tokens_reasoning: int = 0, cost_usd: float = 0.0,
               latency_ms: int = 0, error_kind: str | None = None, llm=None, temperature=None, max_output_tokens=None,
               thinking=None, attempt: int | None = None, provider: str | None = None, model: str | None = None) -> str | None:
        """One ledger row for one provider call. Never raises (the ledger must not break a user's answer)."""
        if self.svc is None:
            return None
        try:
            p = (getattr(llm, "prices", None) or {}).get(model or getattr(llm, "model", None)) or [None, None]
            row_id = self.svc.log_call(
                request_id=self.request_id, attempt=attempt or len(self.calls) + 1, tenant_id=self.tc.tenant_id,
                user_id=self.username, trigger=self.trigger, feature=self.feature, purpose=self.purpose, ref_type=self.ref_type,
                ref_id=self.ref_id, language=self.language, prompt_version=self.prompt_version,
                provider=provider or getattr(llm, "provider", None), model=model or getattr(llm, "model", None),
                thinking_level=thinking if thinking is not None else getattr(llm, "thinking", None),
                temperature=temperature, max_output_tokens=max_output_tokens, status=status, error_kind=error_kind,
                latency_ms=int(latency_ms or 0), tokens_in=int(tokens_in or 0), tokens_out=int(tokens_out or 0),
                tokens_reasoning=int(tokens_reasoning or 0), price_in_per_m=p[0], price_out_per_m=p[1],
                cost_usd_micro=int(round(float(cost_usd or 0) * 1e6)), markup=self.svc.markup_for(self.feature),
                credits_charged_mc=0, charge_reason="pending", exempt=self.exempt, mode=self.mode)
        except Exception as exc:   # noqa: BLE001
            log.warning("ai ledger row not written: %s", exc)
            return None
        self.calls.append({"id": row_id, "status": status, "cost_micro": int(round(float(cost_usd or 0) * 1e6))})
        return row_id

    def settle(self, accept: "str | bool" = "last", note: str | None = None) -> int:
        """Charge what was accepted and close. accept: "last" (the final ok answer; earlier attempts are free),
        "all" (every ok call), or "none" / False (the answer was not used). Returns millicredits charged to the user
        (what WOULD be charged in shadow mode)."""
        if self.settled or self.svc is None:
            return self.charged_mc
        self.settled = True
        if accept is True:
            accept = "last"
        if accept is False:
            accept = "none"
        oks = [c for c in self.calls if c["status"] == "ok"]
        accepted = [] if accept == "none" else (oks if accept == "all" else oks[-1:])
        acc_ids = {c["id"] for c in accepted}
        total = 0
        for c in self.calls:
            if c["id"] in acc_ids:
                mc, why = charge_mc(c["cost_micro"], self.svc.markup_for(self.feature), float(self.svc.cfg["credit_value_usd"])), "charged"
            else:
                mc = 0
                why = ("cached" if c["status"] == "cached" else "failed" if c["status"] == "failed"
                       else "retry" if acc_ids else "rejected")
            if self.exempt:
                mc, why = 0, "exempt"
            elif self.unlimited and mc:
                mc, why = 0, "unlimited"
            elif self.mode == "off" and mc:
                mc, why = 0, "off"
            total += mc
            c["mc"], c["why"] = mc, why
        charged = total
        try:
            if self.holding:
                charged = self.svc.settle(self.request_id, total, note=note or "action settled")
                self.holding = False
            for c in self.calls:
                self.svc.finalize_call(c["id"], c["mc"], c["why"])
        except Exception as exc:   # noqa: BLE001
            log.error("credit settle failed for %s: %s", self.request_id, exc)
        self.charged_mc = charged
        return charged

    def close(self) -> None:
        if not self.settled:
            self.settle("none", note="action closed without a result")


@contextmanager
def ai_action(store, tc, user, feature: str, **kw):
    """``with ai_action(store, tc, user, "ask_report", ref_type="report", ref_id=rid) as act:`` ... ``act.settle(...)``.
    A store of None (tests, scripts) gives an action that records nothing."""
    svc = None
    if store is not None:
        try:
            svc = store.credits
        except Exception:   # noqa: BLE001
            svc = None
    act = AiAction(svc, tc, user, feature, **kw)
    token = _CURRENT.set(act)
    try:
        yield act
    finally:
        _CURRENT.reset(token)
        try:
            act.close()
        except Exception:   # noqa: BLE001
            log.exception("ai action close failed")


def usage_csv(rows: list[dict]) -> str:
    out = io.StringIO()
    cols = ["created_at", "tenant_id", "user_id", "feature", "purpose", "status", "model", "tokens_in", "tokens_out",
            "credits_charged_mc", "charge_reason", "request_id"]
    w = csv.writer(out)
    w.writerow(["time_utc", "business", "user", "feature", "purpose", "status", "model", "tokens_in", "tokens_out",
                "credits", "charge", "request_id"])
    for r in rows:
        w.writerow([datetime.utcfromtimestamp(r["created_at"]).strftime("%Y-%m-%d %H:%M:%S") if k == "created_at"
                    else (r[k] / MC if k == "credits_charged_mc" else ("" if r.get(k) is None else r[k])) for k in cols])
    return out.getvalue()
