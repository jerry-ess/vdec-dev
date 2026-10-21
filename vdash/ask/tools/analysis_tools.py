"""Why-tools: ``explain_change`` and ``top_movers``.

They wrap the report's own root-cause code (``reports/analysis.py``: bridges and drivers), so the numbers and the
split of a change are the same as in the report's analysis section. All arithmetic is done here, never by the AI.
"""
from __future__ import annotations

from ...engine import compare_range
from .money_style import ask_arg as fmt_arg
from ...reports.analysis import bridges, driver
from ...reports.story_rules import load_story_config
from .common import ToolContext, ToolError, _num, change_text
from .kpi_index import resolve_kpi

BRIDGE_KEY_LABEL = {"orders": "tl_part_orders", "basket": "tl_part_basket", "items_per_order": "tl_part_items",
                    "value_per_item": "tl_part_price", "customers": "tl_part_customers",
                    "spend_per_customer": "tl_part_spend"}


def _usable(ctx: ToolContext, kid: str):
    k = ctx.catalog.by_id.get(kid)
    if k is None or not ctx.user.has_role(k.min_role):
        return None
    return k if (ctx.st.get("availability") or {}).get(kid, {}).get("available") else None


def _prev_req(ctx: ToolContext, req, against: str):
    cr = compare_range(req.start, req.end, against)
    if not cr:
        raise ToolError("bad_args", "There is no comparison period for that range.")
    return ctx.period({"start": cr[0].isoformat(), "end": cr[1].isoformat()}, "none")


def _money(ctx, v, fmt):
    return fmt_arg({"v": v, "fmt": "signed_currency"}, ctx.lang, ctx.currency) if fmt == "currency" else ctx.money(v, fmt)


def _signed_pct(ctx, v):
    return fmt_arg({"v": v, "fmt": "signed_pct"}, ctx.lang, ctx.currency) if v is not None else None


def _rows(ctx: ToolContext, d: dict, kind, n: int, fmt: str):
    out = []
    for r in (d["drops"] + d["gains"]):
        name = ctx.alias(kind, r["name"]) if kind else r["name"]
        out.append((name, r))
    return out


def explain_change(ctx: ToolContext, period=None, against: str = "previous", period_b=None) -> dict:
    """Where did the change in revenue between two periods come from?"""
    if against not in ("previous", "yoy"):
        raise ToolError("bad_args", "against must be 'previous' or 'yoy'")
    cfg = load_story_config().get("analysis") or {}
    bcfg = cfg.get("bridge") or {}
    rev = _usable(ctx, bcfg.get("revenue", ""))
    if rev is None:
        raise ToolError("not_available", "This business has no data for that yet.", kpi=bcfg.get("revenue"))
    req = ctx.period(period, against)
    preq = ctx.period(period_b, "none") if period_b else _prev_req(ctx, req, against)     # period_b: e.g. the whole month before
    core, ktitle = {}, {}
    for kid in dict.fromkeys(bcfg.values()):
        k = _usable(ctx, kid)
        if k is None or k.viz != "metric":
            continue
        r = ctx.run(k, req)
        ktitle[kid] = ctx.title(k)
        before = _num(ctx.run(k, preq).get("value")) if period_b else _num(r.get("compare_value"))
        core[kid] = (_num(r.get("value")), before)
    now, before = core.get(rev.id, (None, None))
    if now is None or before is None:
        raise ToolError("no_data", "There is no data for one of the two periods.", kpi=rev.id)
    ptxt, ctxt = ctx.period_text(req), ctx.period_text(preq)
    title = ctx.title(rev)
    ctext, craw = change_text(ctx, rev, now, before)
    facts = [ctx.fact(f"{title}: change", now - before, "currency", f"{ptxt} vs {ctxt}",
                      text=f"{title}: {ctx.money(now, rev.fmt)} ({ptxt}) {ctx.tx('tl_vs')} {ctx.money(before, rev.fmt)} ({ctxt}), "
                           f"{_money(ctx, now - before, 'currency')}" + (f" ({ctext})" if ctext else ""),
                      raw_now=now, raw_before=before, raw_change_pct=craw, now_period=ptxt, before_period=ctxt)]
    br = bridges(core, bcfg and cfg)
    unusual = []
    for b in br:
        sign = lambda e: _money(ctx, e, "currency")
        for p in b["parts"] + (b.get("basket_split") or []):
            label = ctx.tx(BRIDGE_KEY_LABEL.get(p["key"], p["key"]))
            pc = None
            if p.get("now") is not None and p.get("before"):
                pc = _signed_pct(ctx, 100.0 * (p["now"] - p["before"]) / abs(p["before"]))
            facts.append(ctx.fact(f"{title}: {label}", p["effect"], "currency", f"{ptxt} vs {ctxt}",
                                  text=f"{label}: {sign(p['effect'])}" + (f" ({pc})" if pc else ""),
                                  raw_effect=p["effect"], bridge=b["id"], part=p["key"]))
    # breakdowns: biggest drops and gains per dimension
    dims = []
    for kid in cfg.get("drivers") or []:
        k = _usable(ctx, kid)
        if k is not None and k.viz in ("donut", "hbar", "bar") and not k.post:
            dims.append(k)
    drivers = []
    for k in dims:
        try:
            a, b = ctx.run(k, req), ctx.run(k, preq)
        except ToolError:
            continue
        if a.get("type") != "breakdown" or b.get("type") != "breakdown" or a.get("empty") or b.get("empty"):
            continue
        d = driver(k, a, b, int(cfg.get("max_items", 3)))
        if not d:
            continue
        if k.id in (cfg.get("non_additive") or []):
            d["additive"] = False
            for r in d["drops"] + d["gains"]:
                r["share_of_change"] = None
        d["title"] = ctx.title(k)
        d["kind"] = ctx.personal_kind(k)
        drivers.append(d)
    drivers.sort(key=lambda d: -abs(d["change"]) * (1 + d["concentration"] / 100))
    for d in drivers[:4]:
        if d.get("takeover"):
            facts.append(ctx.fact(f"{d['title']}: recording change", None, None, f"{ptxt} vs {ctxt}",
                                  text=ctx.tx("tl_takeover", title=d["title"])))
            continue
        for name, r in _rows(ctx, d, d["kind"], 3, d["fmt"]):
            pct = _signed_pct(ctx, r["change_pct"])
            share = r.get("share_of_change")
            if share is not None and abs(share) > 100:      # offsetting moves: "9,000% of the change" would mislead
                share = None
            text = f"{d['title']} / {name}: {ctx.money(r['now'], d['fmt'])} {ctx.tx('tl_vs')} {ctx.money(r['before'], d['fmt'])}, " \
                   f"{_money(ctx, r['change'], d['fmt'])}" + (f" ({pct})" if pct else "") + \
                   (f", {ctx.tx('tl_of_change', value=ctx.money(abs(share), 'pct'))}" if share is not None else "")
            facts.append(ctx.fact(f"{d['title']}: {name}", r["change"], d["fmt"], f"{ptxt} vs {ctxt}", text=text,
                                  name=name, raw_now=r["now"], raw_before=r["before"], raw_share_of_change=share,
                                  breakdown=d["kpi"]))
    return {"ok": True, "tool": "explain_change", "facts": facts, "tables": [], "charts": [],
            "meta": ctx.meta(req, rev, compare_period=ctx.period_meta(preq)),
            "bridges": br}


def top_movers(ctx: ToolContext, kpi_id: str, period=None, against: str = "previous", top_n: int = 5, period_b=None) -> dict:
    """Which items of a breakdown rose or fell most between two periods."""
    if against not in ("previous", "yoy"):
        raise ToolError("bad_args", "against must be 'previous' or 'yoy'")
    if not isinstance(top_n, int) or isinstance(top_n, bool) or not 1 <= top_n <= 10:
        raise ToolError("bad_args", "top_n must be a whole number from 1 to 10")
    from .catalog_tools import _kpi
    k = _kpi(ctx, kpi_id, ("donut", "hbar", "bar"), "top_movers")
    req = ctx.period(period, against)
    preq = ctx.period(period_b, "none") if period_b else _prev_req(ctx, req, against)
    a, b = ctx.run(k, req), ctx.run(k, preq)
    if a.get("empty") or b.get("empty"):
        raise ToolError("no_data", "There is no data for one of the two periods.", kpi=k.id)
    d = driver(k, a, b, top_n)
    if not d:
        raise ToolError("no_data", "There is nothing to compare between the two periods.", kpi=k.id)
    if k.id in (load_story_config().get("analysis") or {}).get("non_additive", []):
        for r in d["drops"] + d["gains"]:
            r["share_of_change"] = None
    title, kind = ctx.title(k), ctx.personal_kind(k)
    ptxt, ctxt = ctx.period_text(req), ctx.period_text(preq)
    facts, rows = [], []
    for label, group in (("fell", d["drops"]), ("rose", d["gains"])):
        for rank, r in enumerate(group, 1):
            name = ctx.alias(kind, r["name"]) if kind else ctx.daypart_name(k, r["name"])
            pct = _signed_pct(ctx, r["change_pct"])
            text = f"{title} / {name}: {ctx.money(r['now'], k.fmt)} {ctx.tx('tl_vs')} {ctx.money(r['before'], k.fmt)}, " \
                   f"{_money(ctx, r['change'], k.fmt)}" + (f" ({pct})" if pct else "")
            facts.append(ctx.fact(f"{title}: {name}", r["change"], k.fmt, f"{ptxt} vs {ctxt}", text=text, name=name,
                                  direction=label, rank=rank, raw_now=r["now"], raw_before=r["before"],
                                  now_text=ctx.money(r["now"], k.fmt), before_text=ctx.money(r["before"], k.fmt), pct_text=pct or None))
            rows.append({"direction": label, "name": name, "now": ctx.money(r["now"], k.fmt),
                         "before": ctx.money(r["before"], k.fmt), "change": _money(ctx, r["change"], k.fmt), "change_pct": pct})
    if not facts:
        raise ToolError("no_data", "Nothing changed between the two periods.", kpi=k.id)
    for key, label in (("new", "new"), ("gone", "gone")):
        names = [ctx.alias(kind, n) if kind else n for n in d.get(key) or []][:5]
        if names:
            facts.append(ctx.fact(f"{title}: {label}", len(d[key]), "int", f"{ptxt} vs {ctxt}",
                                  text=ctx.tx("tl_new_items" if key == "new" else "tl_gone_items", names=", ".join(names))))
    return {"ok": True, "tool": "top_movers", "facts": facts,
            "tables": [{"title": title, "columns": ["direction", "name", "now", "before", "change", "change_pct"], "rows": rows,
                        "total_rows": len(rows)}],
            "charts": [], "meta": ctx.meta(req, k, compare_period=ctx.period_meta(preq)), "partial": bool(d.get("partial"))}
