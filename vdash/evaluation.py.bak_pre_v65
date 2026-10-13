"""Business evaluation (Executive Overview) and early warnings (AI Insights) - v9.

Both are produced by fixed rules, never by a language model: the same data always gives the same verdict,
every number can be traced to a query, and findings are translated like the rest of the UI (a message key
plus typed arguments; the page formats the numbers in the viewer's locale).

* **Evaluation** (``Rule.panel == "evaluation"``) - judged on the *selected period* against the page's
  comparison period. Each finding lands in ``positive`` ("what's going well") or ``negative`` ("what needs
  attention"). ``kpi_change`` rules reuse a catalog KPI through the KPI engine, so a finding's number is
  exactly the tile's number (and shares its cache entry).
* **Warnings** (``Rule.panel == "warning"``) - judged on the tenant's *latest data* (as-of = last data
  date), looking 7-30 days ahead; independent of the selected period. Each has a severity (``urgent`` /
  ``watch``) and a basis (``forecast`` / ``model`` / ``trend`` / ``data``). The refresher computes them
  once per data version and keeps a history in the state store, so a warning is flagged "new" only in the
  load where it first appeared and is never re-announced on every refresh.

Evaluators are generic (top movers, concentration, overdue regulars, ...); the catalog supplies each
rule's portable SQL, thresholds and texts. Date arithmetic happens here in Python and is bound as
parameters, so the SQL has no dialect date functions.
"""
from __future__ import annotations

import math
import statistics
from calendar import monthrange
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .catalog import Catalog, Kpi, Rule
from .datasources import DataSource
from .engine import KpiEngine, PeriodRequest, _num, add_months

ROLE_ORDER = ["viewer", "manager", "tenant_admin", "platform_admin"]
MAX_ITEMS = 500         # item rows kept per finding (the page gets the first 100; the download gets all kept rows)
TOP_PER_SIDE = 5        # findings shown per column / in the warning list


# ------------------------------------------------------------------ helpers
def _d(v: Any) -> date | None:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def _bind(ds: DataSource, **kw) -> dict:
    """Dates as ISO strings on SQLite (stored as text), native dates elsewhere."""
    out = {}
    for k, v in kw.items():
        out[k] = v.isoformat() if isinstance(v, date) and ds.dialect == "sqlite" else v
    return out


def arg(v: Any, fmt: str = "number") -> dict:
    """Typed message argument; the page formats it (currency, pct, signed_pct, signed_pts, int, date, month,
    text = data value shown as is, label = catalog text translated by the API)."""
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        v = None
    return {"v": v, "fmt": fmt}


def pct_change(cur: float | None, base: float | None) -> float | None:
    if cur is None or base in (None, 0):
        return None
    return 100.0 * (cur - base) / abs(base)


def percentile(vals: list[float], p: float) -> float | None:
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return None
    k = (len(vals) - 1) * p / 100.0
    lo, hi = math.floor(k), math.ceil(k)
    return vals[lo] + (vals[hi] - vals[lo]) * (k - lo)


@dataclass
class Finding:
    rule: str
    panel: str
    title: str
    msg: str
    args: dict[str, dict]
    side: str | None = None             # evaluation: positive | negative
    severity: str | None = None         # warning: urgent | watch
    basis: str | None = None
    score: float = 0.0                  # ranking inside a column (money impact when known)
    impact: float | None = None         # money impact in the tenant's currency, when meaningful
    link_kpi: str | None = None
    items: list[dict] = field(default_factory=list)
    items_cols: list[dict] = field(default_factory=list)
    items_total: int = 0

    def public(self) -> dict:
        return {k: getattr(self, k) for k in ("rule", "panel", "title", "msg", "args", "side", "severity", "basis",
                                               "score", "impact", "link_kpi", "items", "items_cols", "items_total")}


@dataclass
class Ctx:
    """What an evaluator gets: the data source, tenant, the rule (with tenant overrides merged into params),
    and either the period (evaluation) or the as-of date (warnings)."""
    ds: DataSource
    tenant_id: str
    rule: Rule
    params: dict
    engine: KpiEngine
    catalog: Catalog
    data_version: str
    req: PeriodRequest | None = None
    asof: date | None = None
    tz: str = "UTC"

    def q(self, name: str, **binds) -> list[dict]:
        sql = self.rule.sql[name]
        if ":tenant_id" not in sql:  # defence in depth; catalog validation already refuses this
            raise PermissionError("refusing to run SQL without a tenant filter")
        return self.ds.query(sql, {"tenant_id": self.tenant_id, **_bind(self.ds, **binds)})

    def finding(self, msg: str, args: dict, **kw) -> Finding:
        r = self.rule
        return Finding(rule=r.id, panel=r.panel, title=r.title, msg=msg, args=args, basis=r.basis,
                       link_kpi=kw.pop("link_kpi", r.link_kpi),
                       items_cols=[c.__dict__ for c in r.items_cols], **kw)

    def items(self, f: Finding, rows: list[dict]) -> Finding:
        f.items_total = len(rows)
        f.items = rows[:MAX_ITEMS]
        return f


# ------------------------------------------------------------------ evaluators: evaluation panel
def ev_kpi_change(c: Ctx) -> list[Finding]:
    """A catalog KPI moved clearly in the good or the bad direction vs the comparison period."""
    k: Kpi = c.catalog.by_id[c.rule.kpi]
    if c.req is None or c.req.compare == "none":
        return []
    res = c.engine.run(c.ds, c.tenant_id, k, c.req, c.data_version)
    if res.get("type") != "metric" or res.get("empty"):
        return []
    v, prev = _num(res.get("value")), _num(res.get("compare_value"))
    if v is None or prev is None:
        return []
    p = c.params
    if abs(prev) < float(p.get("min_base", 0)):
        return []                                   # too small a base: +50% of 4 customers is noise
    dp = pct_change(v, prev)
    if k.fmt == "pct":
        delta, dfmt, big_enough = v - prev, "pts", abs(v - prev) >= float(p.get("min_pts", 1.5))
    else:
        delta, dfmt, big_enough = dp, "pct", dp is not None and abs(dp) >= float(p.get("min_pct", 5.0))
    if not big_enough or delta is None or delta == 0:
        return []
    up = v > prev
    good = up == (k.good == "up")
    impact = abs(v - prev) if k.fmt == "currency" else None
    rel = abs(dp) if dp is not None else abs(delta)
    f = c.finding("ev_rose" if up else "ev_fell",
                  {"kpi": arg(k.title, "label"), "value": arg(v, k.fmt), "cmp": arg(prev, k.fmt),
                   "delta": arg(abs(delta), dfmt)},     # size of the move; the message says rose / fell
                  side="positive" if good else "negative", score=c.rule.weight * min(rel, 200.0), impact=impact,
                  link_kpi=c.rule.link_kpi or k.id)
    return [f]


def ev_movers(c: Ctx) -> list[Finding]:
    """Products (or any entity) with the largest money gain / loss vs the comparison period."""
    if c.req is None or c.req.compare == "none":
        return []
    from .engine import compare_range
    cs, ce = compare_range(c.req.start, c.req.end, c.req.compare)
    rows = c.q("rows", start=c.req.start, end=c.req.end, c_start=cs, c_end=ce)
    total = sum(_num(r.get("cur")) or 0.0 for r in rows)
    total_prev = sum(_num(r.get("prev")) or 0.0 for r in rows)
    if total <= 0 or total_prev <= 0:
        return []
    p = c.params
    floor = float(p.get("min_share_pct", 1.0)) / 100.0 * max(total, total_prev)
    top_n = int(p.get("top_n", 3))
    moves = []
    for r in rows:
        cur, prev = _num(r.get("cur")) or 0.0, _num(r.get("prev")) or 0.0
        moves.append({"label": r.get("label") or "(not set)", "cur": cur, "prev": prev, "chg": cur - prev,
                      "chg_pct": pct_change(cur, prev)})
    out = []
    for side, sign in (("positive", 1), ("negative", -1)):
        sel = sorted((m for m in moves if sign * m["chg"] >= floor), key=lambda m: (-sign * m["chg"], str(m["label"])))
        if not sel:
            continue
        names = [m["label"] for m in sel[:top_n]]
        amount = sum(m["chg"] for m in sel[:top_n])
        f = c.finding("ev_movers_up" if side == "positive" else "ev_movers_down",
                      {"names": arg(", ".join(names), "text"), "amount": arg(amount, "signed_currency")},
                      side=side, score=c.rule.weight * 100.0 * abs(amount) / total, impact=abs(amount))
        out.append(c.items(f, [{"label": m["label"], "cur": m["cur"], "prev": m["prev"], "chg": m["chg"],
                                "chg_pct": m["chg_pct"]} for m in sel]))
    return out


def ev_concentration(c: Ctx) -> list[Finding]:
    """A few customers bring a large share of revenue in the period (dependence risk)."""
    if c.req is None:
        return []
    rows = c.q("rows", start=c.req.start, end=c.req.end)
    total = sum(_num(r.get("v")) or 0.0 for r in rows)
    named = sorted((r for r in rows if r.get("k") is not None and (_num(r.get("v")) or 0) > 0),
                   key=lambda r: (-(_num(r.get("v")) or 0), str(r.get("label"))))
    p = c.params
    n = int(p.get("top_n", 5))
    if total <= 0 or len(named) < max(n * 2, int(p.get("min_customers", 20))):
        return []
    share = 100.0 * sum(_num(r["v"]) or 0 for r in named[:n]) / total
    if share < float(p.get("share_pct", 35.0)):
        return []
    f = c.finding("ev_concentration", {"n": arg(n, "int"), "share": arg(share, "pct")}, side="negative",
                  score=c.rule.weight * share, impact=None)
    return [c.items(f, [{"label": r.get("label") or "(not set)", "v": _num(r["v"]),
                         "share": 100.0 * (_num(r["v"]) or 0) / total} for r in named[:max(n, 10)]])]


# ------------------------------------------------------------------ evaluators: warnings
def _sev(value: float, watch: float, urgent: float) -> str | None:
    """Severity for a 'bigger is worse' measure (e.g. a drop in %, a revenue share)."""
    if value >= urgent:
        return "urgent"
    if value >= watch:
        return "watch"
    return None


def w_forecast_drop(c: Ctx) -> list[Finding]:
    """Next-7-day demand forecast well below the same week last year (fallback: the last 4 weeks' average)."""
    fc = c.q("forecast")
    if not fc or _num(fc[0].get("fc")) is None or not fc[0].get("fc_from"):
        return []
    total, start = _num(fc[0]["fc"]), _d(fc[0]["fc_from"])
    ly_from, ly_to = add_months(start, -12), add_months(start, -12) + timedelta(days=6)
    ly = _num((c.q("actual", w_from=ly_from, w_to=ly_to) or [{}])[0].get("v"))
    msg = "w_forecast_drop"
    base = ly
    if not ly:
        rec = _num((c.q("actual", w_from=c.asof - timedelta(days=27), w_to=c.asof) or [{}])[0].get("v"))
        base, msg = (rec / 4.0 if rec else None), "w_forecast_drop_recent"
    if not base:
        return []
    drop = -(pct_change(total, base) or 0.0)
    p = c.params
    sev = _sev(drop, float(p.get("watch_drop_pct", 15)), float(p.get("urgent_drop_pct", 30)))
    if not sev:
        return []
    f = c.finding(msg, {"delta": arg(-drop, "signed_pct"), "fc": arg(total, "number"), "base": arg(base, "number"),
                        "from": arg(start.isoformat(), "date")}, severity=sev, score=drop)
    return [f]


def w_high_value_churn(c: Ctx) -> list[Finding]:
    """High-value customers (top 20% by lifetime spend) whom the churn model scores as likely to leave."""
    rows = c.q("customers")
    p = c.params
    cut = percentile([_num(r.get("spend")) for r in rows], float(p.get("value_percentile", 80)))
    if cut is None:
        return []
    risk_min = float(p.get("risk_min", 0.5))
    max_rec = float(p.get("max_recency_days", 180))   # gone for longer = already lost: history, not an early warning
    hit = [r for r in rows if (_num(r.get("risk")) or 0) >= risk_min and (_num(r.get("spend")) or 0) >= cut and cut > 0
           and (_num(r.get("recency")) is None or _num(r.get("recency")) <= max_rec)]
    if not hit:
        return []
    yearly = 12.0 * sum(_num(r.get("monthly")) or 0.0 for r in hit)
    rev12 = _num((c.q("rev12", y_from=c.asof - timedelta(days=364), asof=c.asof) or [{}])[0].get("v")) or 0.0
    share = 100.0 * yearly / rev12 if rev12 else 0.0
    sev = "urgent" if share >= float(p.get("urgent_share_pct", 5)) or len(hit) >= int(p.get("urgent_count", 10)) else "watch"
    hit.sort(key=lambda r: (-(_num(r.get("spend")) or 0), str(r.get("label"))))
    f = c.finding("w_churn_high_value", {"n": arg(len(hit), "int"), "amount": arg(yearly, "currency")},
                  severity=sev, score=yearly, impact=yearly)
    return [c.items(f, [{"label": r.get("label") or "(not set)", "risk": 100.0 * (_num(r.get("risk")) or 0),
                         "spend": _num(r.get("spend")), "recency": _num(r.get("recency")), "seg": r.get("seg")}
                        for r in hit])]


def w_overdue_regulars(c: Ctx) -> list[Finding]:
    """Regular, valuable customers whose time since the last order is well past their usual gap."""
    rows = c.q("customers", y_from=c.asof - timedelta(days=364), asof=c.asof)
    p = c.params
    min_orders = int(p.get("min_orders", 4))
    spend = [_num(r.get("spend12")) or 0.0 for r in rows]
    rev12 = sum(spend)
    cut = percentile([s for s in spend if s > 0], float(p.get("value_percentile", 80)))
    if not cut or rev12 <= 0:
        return []
    hit = []
    for r in rows:
        n, s = int(_num(r.get("n_orders")) or 0), _num(r.get("spend12")) or 0.0
        first, last = _d(r.get("first_d")), _d(r.get("last_d"))
        if n < min_orders or s < cut or not first or not last or last >= c.asof:
            continue
        gap = (last - first).days / (n - 1)
        since = (c.asof - last).days
        if gap <= 0:
            continue
        lo = max(float(p.get("gap_factor", 2.0)) * gap, float(p.get("min_days", 14)))
        hi = min(float(p.get("max_gap_factor", 4.0)) * gap, float(p.get("max_days", 180)))
        if lo < since <= hi:  # past the usual rhythm, but not long gone (that's history, not an early warning)
            hit.append({"label": r.get("label") or "(not set)", "orders": n, "last": last.isoformat(),
                        "gap": round(gap, 1), "since": since, "spend12": s})
    if not hit:
        return []
    at_stake = sum(h["spend12"] for h in hit)
    share = 100.0 * at_stake / rev12
    sev = "urgent" if share >= float(p.get("urgent_share_pct", 10)) else "watch"
    hit.sort(key=lambda h: (-h["spend12"], str(h["label"])))
    f = c.finding("w_overdue_regulars", {"n": arg(len(hit), "int"), "amount": arg(at_stake, "currency"),
                                          "share": arg(share, "pct")}, severity=sev, score=at_stake, impact=at_stake)
    return [c.items(f, hit)]


def w_bestseller_decline(c: Ctx) -> list[Finding]:
    """Best sellers (top 20% of products by the last 90 days' revenue) that the momentum mart marks declining."""
    rev = c.q("revenue", q_from=c.asof - timedelta(days=89), asof=c.asof)
    mom = {r["k"]: r for r in c.q("momentum")}
    rev = sorted((r for r in rev if (_num(r.get("rev")) or 0) > 0), key=lambda r: (-(_num(r.get("rev")) or 0), str(r.get("label"))))
    total = sum(_num(r["rev"]) or 0 for r in rev)
    if not rev or total <= 0:
        return []
    p = c.params
    top = rev[:max(1, math.ceil(len(rev) * float(p.get("top_pct", 20)) / 100.0))]
    hit = [r for r in top if r["k"] in mom]
    if not hit:
        return []
    share = 100.0 * sum(_num(r["rev"]) or 0 for r in hit) / total
    sev = "urgent" if share >= float(p.get("urgent_share_pct", 15)) else "watch"
    f = c.finding("w_bestseller_decline", {"n": arg(len(hit), "int"), "share": arg(share, "pct")},
                  severity=sev, score=share * total / 100.0, impact=None)
    return [c.items(f, [{"label": r.get("label") or "(not set)", "rev": _num(r["rev"]),
                         "share": 100.0 * (_num(r["rev"]) or 0) / total, "mom": _num(mom[r["k"]].get("mom")),
                         "status": mom[r["k"]].get("status")} for r in hit])]


def w_recent_drop(c: Ctx) -> list[Finding]:
    """Last 7 days' revenue clearly below a normal week (median of the 8 weeks before - robust to one-off
    big orders and holiday weeks)."""
    weeks = int(c.params.get("baseline_weeks", 8))
    rows = c.q("daily", w_from=c.asof - timedelta(days=7 * (weeks + 1) - 1), asof=c.asof)
    by_day = {_d(r.get("dt")): _num(r.get("v")) or 0.0 for r in rows}
    sums = []
    for i in range(weeks + 1):
        hi = c.asof - timedelta(days=7 * i)
        days = [hi - timedelta(days=j) for j in range(7)]
        sums.append((sum(by_day.get(d, 0.0) for d in days), any(d in by_day for d in days)))
    cur = sums[0][0]
    base_weeks = [s for s, has in sums[1:] if has]
    if len(base_weeks) < max(3, weeks - 2):
        return []
    base = statistics.median(base_weeks)
    drop = -(pct_change(cur, base) or 0.0)
    p = c.params
    sev = _sev(drop, float(p.get("watch_drop_pct", 20)), float(p.get("urgent_drop_pct", 35)))
    if not sev:
        return []
    f = c.finding("w_recent_drop", {"delta": arg(-drop, "signed_pct"), "value": arg(cur, "currency"),
                                     "base": arg(base, "currency")}, severity=sev, score=base - cur, impact=base - cur)
    return [f]


def w_mtd_pace(c: Ctx) -> list[Finding]:
    """Month-to-date revenue, projected to the full month, well below the same month last year (or last month)."""
    a = c.asof
    p = c.params
    days_in = monthrange(a.year, a.month)[1]
    if a.day < int(p.get("min_day", 7)):
        return []
    ym = a.strftime("%Y-%m")
    ym_prev = add_months(date(a.year, a.month, 1), -1).strftime("%Y-%m")
    ym_ly = add_months(date(a.year, a.month, 1), -12).strftime("%Y-%m")
    rows = {r["ym"]: _num(r.get("v")) for r in c.q("months", ym=ym, ym_prev=ym_prev, ym_ly=ym_ly, asof=a)}
    mtd = rows.get(ym)
    if not mtd:
        return []
    proj = mtd * days_in / a.day
    base, which = rows.get(ym_ly), "w_mtd_pace_ly"
    if not base:
        base, which = rows.get(ym_prev), "w_mtd_pace_prev"
    if not base:
        return []
    drop = -(pct_change(proj, base) or 0.0)
    sev = _sev(drop, float(p.get("watch_drop_pct", 15)), float(p.get("urgent_drop_pct", 30)))
    if not sev:
        return []
    f = c.finding(which, {"month": arg(ym, "month"), "proj": arg(proj, "currency"), "delta": arg(-drop, "signed_pct"),
                          "base": arg(base, "currency"), "mtd": arg(mtd, "currency")},
                  severity=sev, score=base - proj, impact=base - proj)
    return [f]


def w_new_customers_drop(c: Ctx) -> list[Finding]:
    """First-time customers in the last 4 weeks clearly below the 4-week average of the 12 weeks before."""
    rows = c.q("firsts", asof=c.asof)
    firsts = [d for d in (_d(r.get("first_d")) for r in rows) if d]
    if not firsts:
        return []
    win = 28
    count = lambda lo, hi: sum(1 for d in firsts if lo <= d <= hi)  # noqa: E731
    cur = count(c.asof - timedelta(days=win - 1), c.asof)
    prior = [count(c.asof - timedelta(days=win * (i + 1) - 1), c.asof - timedelta(days=win * i)) for i in (1, 2, 3)]
    base = sum(prior) / 3.0
    p = c.params
    if base < float(p.get("min_base", 8)):
        return []
    drop = -(pct_change(cur, base) or 0.0)
    sev = _sev(drop, float(p.get("watch_drop_pct", 30)), float(p.get("urgent_drop_pct", 50)))
    if not sev:
        return []
    return [c.finding("w_new_customers_drop", {"delta": arg(-drop, "signed_pct"), "value": arg(cur, "int"),
                                               "base": arg(base, "number")}, severity=sev, score=base - cur)]


def w_stale_data(c: Ctx) -> list[Finding]:
    """No new data for N days (wall clock in the tenant's time zone): the data feed may have stopped.
    Evaluated live on each request, not per data version (by definition the version does not change)."""
    today = datetime.now(ZoneInfo(c.tz)).date() if c.tz else date.today()
    days = (today - c.asof).days
    p = c.params
    if days < int(p.get("watch_days", 3)):
        return []
    sev = "urgent" if days >= int(p.get("urgent_days", 7)) else "watch"
    return [c.finding("w_stale_data", {"n": arg(days, "int"), "date": arg(c.asof.isoformat(), "date")},
                      severity=sev, score=days)]


EVALUATORS: dict[str, Callable[[Ctx], list[Finding]]] = {
    "kpi_change": ev_kpi_change, "movers": ev_movers, "concentration": ev_concentration,
    "forecast_drop": w_forecast_drop, "high_value_churn": w_high_value_churn, "overdue_regulars": w_overdue_regulars,
    "bestseller_decline": w_bestseller_decline, "recent_drop": w_recent_drop, "mtd_pace": w_mtd_pace,
    "new_customers_drop": w_new_customers_drop, "stale_data": w_stale_data,
}
LIVE = {"stale_data"}   # evaluated on every request (depends on today's date, not on the data)


# ------------------------------------------------------------------ check types and their parameters (v12)
# The rule file (config/rules/<catalog>.json) chooses checks and sets these parameters; the loader validates
# every value against this table (type, range) and fills in the defaults. Adding a *type* of check = code here.
@dataclass(frozen=True)
class Param:
    kind: str            # "int" | "num"
    lo: float
    hi: float
    default: float
    help: str


def _p(kind, lo, hi, default, help_):
    return Param(kind, lo, hi, default, help_)


DROP_WATCH = "Warn (Watch) when the drop is at least this many %"
DROP_URGENT = "Mark as Urgent when the drop is at least this many %"
CHECKS: dict[str, dict] = {
    "kpi_change": {
        "panel": "evaluation", "basis": None, "needs_kpi": True,
        "help": "A dashboard KPI moved clearly up or down vs the comparison period. Good or bad follows the KPI's direction.",
        "params": {"min_pct": _p("num", 0, 1000, 5.0, "Minimum change in % (for KPIs that are not percentages)"),
                   "min_pts": _p("num", 0, 100, 1.5, "Minimum change in percentage points (for KPIs that are percentages)"),
                   "min_base": _p("num", 0, 1e12, 0, "Ignore the KPI when the comparison value is below this (too small to judge)")}},
    "movers": {
        "panel": "evaluation", "basis": None,
        "help": "Products with the largest revenue gain and loss vs the comparison period.",
        "params": {"top_n": _p("int", 1, 20, 3, "Products named in the sentence"),
                   "min_share_pct": _p("num", 0, 100, 1.0, "Minimum change, as % of period revenue, for a product to count")}},
    "concentration": {
        "panel": "evaluation", "basis": None,
        "help": "A few customers bring a large share of revenue (dependence risk). Needs no comparison period.",
        "params": {"top_n": _p("int", 1, 50, 5, "Number of top customers"),
                   "share_pct": _p("num", 0, 100, 35.0, "Flag when the top customers bring at least this % of revenue"),
                   "min_customers": _p("int", 0, 100000, 20, "Only judge when at least this many customers bought")}},
    "forecast_drop": {
        "panel": "warning", "basis": "forecast",
        "help": "Next 7 days' forecast vs the same week last year (or the last 4 weeks' average).",
        "params": {"watch_drop_pct": _p("num", -1e9, 100, 15, DROP_WATCH),
                   "urgent_drop_pct": _p("num", 0, 1e9, 30, DROP_URGENT)}},
    "high_value_churn": {
        "panel": "warning", "basis": "model",
        "help": "Valuable customers whom the prediction marks as likely to leave.",
        "params": {"risk_min": _p("num", 0, 1, 0.5, "Minimum risk of leaving (0-1)"),
                   "value_percentile": _p("num", 0, 100, 80, "Valuable = lifetime spend at or above this percentile"),
                   "max_recency_days": _p("int", 1, 3650, 180, "Ignore customers gone longer than this (already lost)"),
                   "urgent_share_pct": _p("num", 0, 100, 5, "Urgent when their yearly spend is at least this % of revenue"),
                   "urgent_count": _p("int", 1, 100000, 10, "... or when at least this many customers are affected")}},
    "overdue_regulars": {
        "panel": "warning", "basis": "trend",
        "help": "Regular, valuable customers silent for longer than their usual gap between orders.",
        "params": {"min_orders": _p("int", 2, 1000, 4, "Regular = at least this many orders"),
                   "value_percentile": _p("num", 0, 100, 80, "Valuable = 12-month spend at or above this percentile"),
                   "gap_factor": _p("num", 1, 20, 2.0, "Overdue after this many times the usual gap"),
                   "max_gap_factor": _p("num", 1, 50, 4.0, "Stop warning after this many times the usual gap"),
                   "min_days": _p("int", 1, 365, 14, "Never overdue before this many days"),
                   "max_days": _p("int", 1, 3650, 180, "Stop warning after this many days"),
                   "urgent_share_pct": _p("num", 0, 100, 10, "Urgent when their spend is at least this % of 12-month revenue")}},
    "bestseller_decline": {
        "panel": "warning", "basis": "model",
        "help": "Best sellers (by the last 90 days' revenue) that are losing sales momentum.",
        "params": {"top_pct": _p("num", 1, 100, 20, "Best sellers = top this % of products"),
                   "urgent_share_pct": _p("num", 0, 100, 15, "Urgent when they bring at least this % of revenue")}},
    "recent_drop": {
        "panel": "warning", "basis": "trend",
        "help": "Last 7 days' revenue vs a normal week (median of the weeks before).",
        "params": {"baseline_weeks": _p("int", 3, 52, 8, "Weeks used for the normal-week baseline"),
                   "watch_drop_pct": _p("num", -1e9, 100, 20, DROP_WATCH),
                   "urgent_drop_pct": _p("num", 0, 1e9, 35, DROP_URGENT)}},
    "mtd_pace": {
        "panel": "warning", "basis": "trend",
        "help": "Month-to-date revenue projected to the full month vs the same month last year (or last month).",
        "params": {"min_day": _p("int", 1, 28, 7, "Only judge from this day of the month"),
                   "watch_drop_pct": _p("num", -1e9, 100, 15, DROP_WATCH),
                   "urgent_drop_pct": _p("num", 0, 1e9, 30, DROP_URGENT)}},
    "new_customers_drop": {
        "panel": "warning", "basis": "trend",
        "help": "First-time customers in the last 4 weeks vs the 4-week average before.",
        "params": {"min_base": _p("num", 0, 1e6, 8, "Only judge when the usual 4-week count is at least this"),
                   "watch_drop_pct": _p("num", -1e9, 100, 30, DROP_WATCH),
                   "urgent_drop_pct": _p("num", 0, 1e9, 50, DROP_URGENT)}},
    "stale_data": {
        "panel": "warning", "basis": "data",
        "help": "No new data for some days (checked against today's date).",
        "params": {"watch_days": _p("int", 1, 365, 3, "Warn after this many days without new data"),
                   "urgent_days": _p("int", 1, 365, 7, "Urgent after this many days")}},
}
assert set(CHECKS) == set(EVALUATORS)

# pairs that must stay in order: (smaller, larger)
PARAM_ORDER = [("watch_drop_pct", "urgent_drop_pct"), ("gap_factor", "max_gap_factor"), ("min_days", "max_days"),
               ("watch_days", "urgent_days")]


def check_params(check: str, params: dict, where: str) -> tuple[dict, list[str]]:
    """Validate ``params`` for a check type; returns (complete params with defaults, error messages)."""
    spec = CHECKS[check]["params"]
    errors, out = [], {k: p.default for k, p in spec.items()}
    for k, v in (params or {}).items():
        if k.startswith("_"):
            continue                      # comments
        if k not in spec:
            errors.append(f"{where}: unknown parameter '{k}' for check '{check}' (allowed: {', '.join(spec)})")
            continue
        p = spec[k]
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            errors.append(f"{where}: parameter '{k}' must be a number, got {v!r}")
            continue
        if p.kind == "int" and float(v) != int(v):
            errors.append(f"{where}: parameter '{k}' must be a whole number, got {v!r}")
            continue
        if not (p.lo <= v <= p.hi):
            errors.append(f"{where}: parameter '{k}' = {v} is outside {p.lo:g}..{p.hi:g}")
            continue
        out[k] = int(v) if p.kind == "int" else float(v)
    for a, b in PARAM_ORDER:
        if a in out and b in out and out[a] > out[b]:
            errors.append(f"{where}: '{a}' ({out[a]:g}) must not be larger than '{b}' ({out[b]:g})")
    return out, errors


# ------------------------------------------------------------------ running rules
def rule_role(rule: Rule, catalog: Catalog) -> str:
    """Effective minimum role: the rule's own, raised to its KPI's (finance / PII KPIs stay restricted)."""
    roles = [rule.min_role] + ([catalog.by_id[rule.kpi].min_role] if rule.kpi else [])
    return max(roles, key=ROLE_ORDER.index)


_WARNED: set[str] = set()


def rule_params(rule: Rule, alerts_cfg: dict) -> dict:
    """Rule-file parameters with the tenant's overrides (tenants.json alerts.params) on top. An invalid override
    is ignored (logged once) instead of breaking the panel; ``tools/check_rules.py`` reports it before deploy."""
    override = (alerts_cfg.get("params", {}) or {}).get(rule.id) or {}
    if not override:
        return rule.params
    merged, errors = check_params(rule.fn, {**rule.params, **override}, f"tenant override for rule '{rule.id}'")
    if errors:
        import logging
        for e in errors:
            if e not in _WARNED:
                _WARNED.add(e)
                logging.getLogger("vdash.evaluation").warning("%s (ignored)", e)
        return rule.params
    return merged


def rule_available(rule: Rule, avail: dict) -> bool:
    """A KPI rule follows its KPI's availability; other rules follow their data set's probe (``ruledata:<name>``),
    so a rule added in the rule file needs no new probe."""
    if rule.kpi:
        return bool(avail.get(rule.kpi, {}).get("available"))
    return bool(avail.get(f"ruledata:{rule.data}", {}).get("available"))


def active_rules(catalog: Catalog, panel: str, avail: dict, alerts_cfg: dict, live: bool | None = None) -> list[Rule]:
    disabled = set(alerts_cfg.get("disabled", []) or [])
    enabled = set(alerts_cfg.get("enabled", []) or [])      # tenant switches on a rule that is off in the rule file
    out = []
    for r in catalog.rules:
        on = (r.enabled or r.id in enabled) and r.id not in disabled
        if r.panel != panel or not on or not rule_available(r, avail):
            continue
        if live is not None and (r.fn in LIVE) != live:
            continue
        out.append(r)
    return out


def run_rules(rules: list[Rule], ds: DataSource, tenant_id: str, engine: KpiEngine, catalog: Catalog, version: str,
              alerts_cfg: dict, req: PeriodRequest | None = None, asof: date | None = None, tz: str = "UTC",
              pool=None) -> tuple[list[Finding], list[dict]]:
    """Run rules (in parallel when a pool is given). One failing rule never breaks the panel: it is reported in
    ``errors`` and skipped."""
    def one(r: Rule):
        ctx = Ctx(ds, tenant_id, r, rule_params(r, alerts_cfg), engine, catalog, version, req, asof, tz)
        return EVALUATORS[r.fn](ctx)

    findings: list[Finding] = []
    errors: list[dict] = []
    futs = [(r, pool.submit(one, r) if pool else None) for r in rules]
    for r, fut in futs:
        try:
            findings += fut.result() if fut else one(r)
        except Exception as exc:  # pragma: no cover - reported to admins, never shown as a finding
            errors.append({"rule": r.id, "error": f"{type(exc).__name__}: {str(exc).splitlines()[0][:200]}"})
    return findings, errors


def rank(findings: list[Finding], key: Callable[[Finding], Any]) -> list[Finding]:
    return sorted(findings, key=key)


def eval_cache_key(tenant_id: str, version: str, req, catalog, alerts_cfg: dict) -> str:
    """v16: one cache key for the period evaluation, used by the API and by the refresher's prewarm."""
    import json as _json
    return (f"{tenant_id}|{version}|__eval__|{req.key()}|{catalog.rules_version}|"
            f"{_json.dumps(alerts_cfg, sort_keys=True)}")


def evaluate_period(catalog: Catalog, ds: DataSource, tenant_id: str, engine: KpiEngine, version: str, avail: dict,
                    alerts_cfg: dict, req: PeriodRequest, pool=None) -> dict:
    rules = active_rules(catalog, "evaluation", avail, alerts_cfg)
    found, errors = run_rules(rules, ds, tenant_id, engine, catalog, version, alerts_cfg, req=req, pool=pool)
    pos = rank([f for f in found if f.side == "positive"], lambda f: -f.score)
    neg = rank([f for f in found if f.side == "negative"], lambda f: -f.score)
    return {"positive": [f.public() for f in pos], "negative": [f.public() for f in neg],
            "checked": len(rules), "errors": errors}


SEV_ORDER = {"urgent": 0, "watch": 1}


def evaluate_warnings(catalog: Catalog, ds: DataSource, tenant_id: str, engine: KpiEngine, version: str, avail: dict,
                      alerts_cfg: dict, asof: date, tz: str, live: bool, pool=None) -> dict:
    rules = active_rules(catalog, "warning", avail, alerts_cfg, live=live)
    found, errors = run_rules(rules, ds, tenant_id, engine, catalog, version, alerts_cfg, asof=asof, tz=tz, pool=pool)
    return {"warnings": [f.public() for f in found], "checked": len(rules), "errors": errors}


def sort_warnings(ws: list[dict]) -> list[dict]:
    return sorted(ws, key=lambda w: (SEV_ORDER.get(w.get("severity"), 9), -(w.get("score") or 0)))


def for_role(findings: list[dict], role_of: Callable[[str], str], has_role: Callable[[str], bool],
             items_role: Callable[[str], str]) -> list[dict]:
    """Drop findings the user may not see; strip item lists (e.g. customer names) below the item role."""
    out = []
    for f in findings:
        if not has_role(role_of(f["rule"])):
            continue
        if not has_role(items_role(f["rule"])):
            f = {**f, "items": [], "items_hidden": bool(f.get("items_total"))}
        out.append(f)
    return out


# ------------------------------------------------------------------ plain-text rendering (exports)
def render_text(template: str, args: dict, currency: str, lang: str = "en") -> str:
    """Server-side sentence (downloads, reports). v23: numbers, money and dates are written in ``lang`` exactly as
    the page writes them (``vdash.fmt``, same rules as ``fmtArg`` in web/app.js) - e.g. de "481,5 Mio. ₫"."""
    from .fmt import fmt_arg
    s = template
    for k, a in args.items():
        s = s.replace("{" + k + "}", fmt_arg(a, lang, currency))
    return s
