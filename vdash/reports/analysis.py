"""Root-cause material for the analyst (v39): computed on the server, so the AI never has to calculate.

    doc["analysis"] = analyse(dash, user, tc, st, req, lg, results, states)

* **Bridges** - where the change of revenue comes from, in money, adding up exactly:
  revenue = orders x average order value  ->  "orders effect" + "basket effect";
  average order value = items per order x price per item -> the basket effect split into "items per order" and
  "price per item (incl. discounts and product mix)"; revenue = active customers x revenue per customer ->
  "customer count effect" + "spend per customer effect". (Log split: the parts add up to the change exactly.)
* **Drivers** - for every breakdown of revenue the user may see (customer type, daypart, weekday, payment method,
  salesperson, channel, location, category ...): this period vs the comparison period per item, the change in
  money and %, its share of the total change, and the shift of its share of revenue (points). The biggest
  drops and gains and the biggest share shifts are kept - this is where "hidden" causes show up (a weekday
  that collapsed, a payment method that disappeared, a salesperson missing from the data).
* **Unusual days** - days whose revenue is more than ``sigma`` standard deviations from the period's average,
  and the last 7 days against the period average.

Everything uses the dashboard's own KPI results for this user (role rules apply) for the period and for its
comparison period. Numbers are kept raw here; ``facts.py`` writes them in the report language.
"""
from __future__ import annotations

import math
import re
from datetime import date

from ..catalog import load_catalog
from ..engine import compare_range
from ..i18n import t
from .blocks import kpi_block_id
from .story_rules import load_story_config


def _num(v):
    try:
        x = None if v is None else float(v)
        return None if x is None or math.isnan(x) or math.isinf(x) else x
    except (TypeError, ValueError):
        return None


def _log_split(total_change: float, ratios: list[float | None], r_total: float) -> list[float] | None:
    """Split ``total_change`` in proportion to log(ratio_i) / log(r_total) (exact when prod(ratios) == r_total)."""
    if r_total <= 0 or abs(math.log(r_total)) < 1e-9 or any(r is None or r <= 0 for r in ratios):
        return None
    L = math.log(r_total)
    return [total_change * math.log(r) / L for r in ratios]


def bridges(core: dict, cfg: dict) -> list[dict]:
    """``core``: kpi id -> (value, compare_value). Returns bridge dicts with raw numbers."""
    b = cfg.get("bridge") or {}

    def pair(k):
        v = core.get(b.get(k) or "")
        return (v[0], v[1]) if v and v[0] is not None and v[1] not in (None, 0) and v[0] > 0 and v[1] > 0 else None

    out = []
    rev, orders, aov = pair("revenue"), pair("orders"), pair("aov")
    if rev and orders and aov:
        d = rev[0] - rev[1]
        parts = _log_split(d, [orders[0] / orders[1], (rev[0] / orders[0]) / (rev[1] / orders[1])], rev[0] / rev[1])
        if parts:
            br = {"id": "orders_basket", "kpi": b["revenue"], "now": rev[0], "before": rev[1], "change": d,
                  "parts": [{"key": "orders", "kpi": b["orders"], "now": orders[0], "before": orders[1], "effect": parts[0]},
                            {"key": "basket", "kpi": b["aov"], "now": aov[0], "before": aov[1], "effect": parts[1]}]}
            upt, price = pair("upt"), pair("price")
            if upt:      # basket = items per order x value per item; the rest of the basket effect is price / mix
                items_eff = _log_split(d, [upt[0] / upt[1]], rev[0] / rev[1])[0]
                br["basket_split"] = [{"key": "items_per_order", "kpi": b["upt"], "now": upt[0], "before": upt[1],
                                       "effect": items_eff},
                                      {"key": "value_per_item", "kpi": b.get("price"),
                                       "now": price[0] if price else None, "before": price[1] if price else None,
                                       "effect": parts[1] - items_eff}]
            out.append(br)
    cust, per = pair("customers"), pair("per_customer")
    if rev and cust:
        d = rev[0] - rev[1]
        parts = _log_split(d, [cust[0] / cust[1], (rev[0] / cust[0]) / (rev[1] / cust[1])], rev[0] / rev[1])
        if parts:
            out.append({"id": "customers_spend", "kpi": b["revenue"], "now": rev[0], "before": rev[1], "change": d,
                        "parts": [{"key": "customers", "kpi": b["customers"], "now": cust[0], "before": cust[1], "effect": parts[0]},
                                  {"key": "spend_per_customer", "kpi": b.get("per_customer"),
                                   "now": per[0] if per else rev[0] / cust[0], "before": per[1] if per else rev[1] / cust[1],
                                   "effect": parts[1]}]})
    disc = core.get(b.get("discount") or "")
    if out and disc and disc[0] is not None and disc[1] is not None:
        out[0]["discount_rate"] = {"kpi": b["discount"], "now": disc[0], "before": disc[1]}
    return out


def list_limit(kpi) -> int | None:
    """The row limit of a top-N KPI (``LIMIT n`` in its SQL), else None."""
    m = re.search(r"\bLIMIT\s+(\d+)", getattr(kpi, "sql", None) or "", re.I)
    return int(m.group(1)) if m else None


def is_partial(kpi, n_items: int) -> bool:
    """v39.2: True when a breakdown is a cut-off top-N list (it hit its LIMIT), so its items are not the whole
    total: no share of the listed items, no "new"/"gone", no totals."""
    lim = list_limit(kpi)
    return lim is not None and n_items >= lim


def non_additive(kpi_id: str) -> bool:
    """v39.3: a breakdown whose items are averages (e.g. revenue per weekday), not parts of a total - it has no
    "share of the total" (report_story.json -> analysis.non_additive)."""
    return kpi_id in ((load_story_config().get("analysis") or {}).get("non_additive") or [])


def driver(kpi, cur: dict, prev: dict, n: int) -> dict | None:
    """One breakdown, this period vs comparison: per item change, share of the total change, share shift.
    For a cut-off top-N list (v39.2) only the items listed in both periods are compared, without totals,
    shares, "new" or "gone" - an item missing from a top-N list has not necessarily disappeared."""
    def items(r):
        return {str(i["label"]): _num(i.get("value")) or 0.0 for i in (r or {}).get("items") or []
                if not i.get("label_key")}
    a, b = items(cur), items(prev)
    if not a or not b:
        return None
    if is_partial(kpi, len(a)) or is_partial(kpi, len(b)):
        rows = [{"name": lab, "now": a[lab], "before": b[lab], "change": a[lab] - b[lab],
                 "change_pct": (100.0 * (a[lab] - b[lab]) / b[lab]) if b[lab] else None, "share_of_change": None}
                for lab in a if lab in b]
        if not rows:
            return None
        drops = sorted([r for r in rows if r["change"] < 0], key=lambda r: r["change"])[:n]
        gains = sorted([r for r in rows if r["change"] > 0], key=lambda r: -r["change"])[:n]
        return {"kpi": kpi.id, "fmt": kpi.fmt, "partial": True, "limit": list_limit(kpi), "additive": False,
                "now": None, "before": None, "change": sum(r["change"] for r in rows), "items": len(rows),
                "drops": drops, "gains": gains, "shifts": [], "concentration": 0.0, "new": [], "gone": []}
    labels = list(dict.fromkeys(list(a) + list(b)))
    ta, tb = sum(a.values()), sum(b.values())
    total = ta - tb
    rows = []
    for lab in labels:
        x, y = a.get(lab, 0.0), b.get(lab, 0.0)
        d = x - y
        rows.append({"name": lab, "now": x, "before": y, "change": d,
                     "change_pct": (100.0 * d / y) if y else None,
                     "share_of_change": (100.0 * d / abs(total)) if abs(total) > 1e-9 else None,
                     "share_now": 100.0 * x / ta if ta > 0 else None, "share_before": 100.0 * y / tb if tb > 0 else None,
                     "new": y == 0 and x > 0, "gone": x == 0 and y > 0})
    for r in rows:
        r["shift_pts"] = (r["share_now"] - r["share_before"]) if r["share_now"] is not None and r["share_before"] is not None else None
    drops = sorted([r for r in rows if r["change"] < 0], key=lambda r: r["change"])[:n]
    gains = sorted([r for r in rows if r["change"] > 0], key=lambda r: -r["change"])[:n]
    shifts = sorted([r for r in rows if r["shift_pts"] is not None and abs(r["shift_pts"]) >= 1.0],
                    key=lambda r: -abs(r["shift_pts"]))[:n]
    biggest = max((abs(r["share_of_change"]) for r in rows if r["share_of_change"] is not None), default=0.0)
    # v39.6: one item now carries (almost) everything while another that carried a large share went to zero -
    # usually a change in how sales are recorded (or a staff / terminal change), not a cause of the total change
    takeover = None
    top = max(rows, key=lambda r: r["share_now"] or 0.0)
    lost = [r for r in rows if r["gone"] and (r["share_before"] or 0) >= 20]
    if (top["share_now"] or 0) >= 95 and (top["share_before"] or 0) <= 80 and lost:
        takeover = {"now_all": top["name"], "gone": [r["name"] for r in lost]}
    return {"kpi": kpi.id, "fmt": kpi.fmt, "now": ta, "before": tb, "change": total, "items": len(labels),
            "drops": drops, "gains": gains, "shifts": shifts, "concentration": biggest, "takeover": takeover,
            "new": [r["name"] for r in rows if r["new"]], "gone": [r["name"] for r in rows if r["gone"]]}


def unusual_days(trend: dict | None, sigma: float, lg: str) -> dict | None:
    if not trend or trend.get("type") != "timeseries" or trend.get("grain") != "day" or not trend.get("series"):
        return None
    s = trend["series"][0]
    pts = [(x, _num(v)) for x, v in zip(trend.get("x") or [], s.get("data") or []) if _num(v) is not None]
    if len(pts) < 10:
        return None
    vals = [v for _, v in pts]
    mean = sum(vals) / len(vals)
    sd = math.sqrt(sum((v - mean) ** 2 for v in vals) / len(vals))
    out = {"series": s.get("name"), "fmt": s.get("fmt"), "average": mean, "days": len(pts), "high": [], "low": []}
    if sd > 0:
        for x, v in pts:
            z = (v - mean) / sd
            if abs(z) >= sigma:
                d = date.fromisoformat(str(x)[:10])
                (out["high"] if z > 0 else out["low"]).append({"date": str(x)[:10], "weekday": d.weekday(), "value": v,
                                                               "times_average": v / mean if mean else None})
    last = pts[-7:]
    if len(pts) >= 14 and mean:
        avg7 = sum(v for _, v in last) / len(last)
        out["last7_average"] = avg7
        out["last7_vs_average_pct"] = 100.0 * (avg7 - mean) / mean
    return out if (out["high"] or out["low"] or "last7_average" in out) else None


def analyse(dash, user, tc, st, req, lg, results: dict, states: dict) -> dict:
    """Root-cause material (raw numbers). Empty when there is no comparison period."""
    cfg = (load_story_config().get("analysis") or {})
    out = {"bridges": [], "drivers": [], "unusual_days": None}
    catalog = load_catalog(tc.catalog)
    state_of = {b["id"]: b["state"] for ly in states["layers"] for ch in ly["chapters"] for b in ch["blocks"]}

    def usable(kid):
        return kid in catalog.by_id and state_of.get(kpi_block_id(kid)) == "available"

    # unusual days (current period only)
    tk = cfg.get("trend_kpi", "revenue_trend")
    if usable(tk):
        if tk not in results:
            results = {**results, **dash.run_many(user, tc, [catalog.by_id[tk]], req, st)}
        out["unusual_days"] = unusual_days(dash.translate(catalog.by_id[tk], results[tk], lg, user),
                                           float(cfg.get("sigma", 2.0)), lg)
    cmp = compare_range(req.start, req.end, req.compare)
    if not cmp:
        return out
    # bridges: the metric KPIs carry their own comparison value
    bkpis = [k for k in dict.fromkeys((cfg.get("bridge") or {}).values()) if usable(k)]
    need = [catalog.by_id[k] for k in bkpis if k not in results]
    if need:
        results = {**results, **dash.run_many(user, tc, need, req, st)}
    core = {}
    for k in bkpis:
        r = dash.translate(catalog.by_id[k], results[k], lg, user)
        if r.get("type") == "metric" and not r.get("empty"):
            core[k] = (_num(r.get("value")), _num(r.get("compare_value")))
    out["bridges"] = bridges(core, cfg)
    # drivers: each breakdown for this period and for the comparison period
    dims = [catalog.by_id[k] for k in cfg.get("drivers") or [] if usable(k)]
    if dims:
        preq = dash.period(cmp[0].isoformat(), cmp[1].isoformat(), "auto", "none", st)
        now = dash.run_many(user, tc, [k for k in dims if k.id not in results], req, st)
        now = {**{k.id: results[k.id] for k in dims if k.id in results}, **now}
        before = dash.run_many(user, tc, dims, preq, st)
        for k in dims:
            a = dash.translate(k, now[k.id], lg, user)
            b = dash.translate(k, before[k.id], lg, user)
            if a.get("type") != "breakdown" or b.get("type") != "breakdown" or a.get("empty") or b.get("empty"):
                continue
            d = driver(k, a, b, int(cfg.get("max_items", 3)))
            if d and k.id in (cfg.get("non_additive") or []):
                d["additive"] = False
                for r in d["drops"] + d["gains"]:
                    r["share_of_change"] = None
                d["shifts"] = []
            if d:
                d["title"] = t(lg, k.title)
                out["drivers"].append(d)
        out["drivers"].sort(key=lambda d: -abs(d["change"]) * (1 + d["concentration"] / 100))
    return out
