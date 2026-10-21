"""The four catalog tools: kpi_value, compare_periods, kpi_breakdown, kpi_trend.

They run the dashboard's own KPI SQL through the same engine (same cache, same role rules), so a number from a tool
equals the number on the dashboard and in the report for the same period.
"""
from __future__ import annotations

from ...fmt import fmt_bucket
from ...reports.analysis import is_partial, non_additive
from ...reports.story_rules import load_story_config
from .common import MAX_SERIES, MAX_TOP_N, ToolContext, ToolError, _num, change_text
from .kpi_index import index_for, resolve_kpi


def _kpi(ctx: ToolContext, ref, viz_ok: tuple[str, ...], tool: str):
    k = resolve_kpi(ctx.catalog, ref, viz_ok)
    if k is None:
        known = ctx.catalog.by_id.get(ref) if isinstance(ref, str) else None
        if known is not None or (isinstance(ref, str) and ref in index_for(ctx.catalog).by_template):
            raise ToolError("shape_unavailable", f"'{ref}' is not available in the form {tool} needs.", kpi=ref,
                            needs=list(viz_ok))
        raise ToolError("unknown_kpi", f"Unknown KPI '{ref}'.", kpi=str(ref))
    ctx.check_kpi(k)
    return k


def _ok(ctx: ToolContext, tool: str, facts: list[dict], req, kpi, **extra) -> dict:
    out = {"ok": True, "tool": tool, "facts": facts, "tables": extra.pop("tables", []),
           "charts": extra.pop("charts", []), "meta": ctx.meta(req, kpi)}
    out.update(extra)
    return out


# ---------------------------------------------------------------------------------------------------- kpi_value
def kpi_value(ctx: ToolContext, kpi_id: str, period=None, compare: str = "none") -> dict:
    k = _kpi(ctx, kpi_id, ("metric",), "kpi_value")
    req = ctx.period(period, compare)
    res = ctx.run(k, req)
    if res.get("empty") or _num(res.get("value")) is None:
        raise ToolError("no_data", "There is no data for that period.", kpi=k.id)
    title, ptxt = ctx.title(k), ctx.period_text(req)
    text = f"{title}: {ctx.money(res['value'], k.fmt)} ({ptxt})"
    extra = {}
    if _num(res.get("compare_value")) is not None:
        ctext, craw = change_text(ctx, k, res["value"], res["compare_value"])
        ctext_period = f"{res.get('compare_start')} – {res.get('compare_end')}"
        text += f"; {ctx.tx('tl_compare_period', value=ctx.money(res['compare_value'], k.fmt))}" + (f", {ctx.tx('tl_change', value=ctext)}" if ctext else "")
        extra = {"previous": ctx.money(res["compare_value"], k.fmt), "raw_previous": _num(res["compare_value"]),
                 "change": ctext, "raw_change": craw, "compare_period": ctext_period}
    f = ctx.fact(title, res["value"], k.fmt, ptxt, text=text, **extra)
    return _ok(ctx, "kpi_value", [f], req, k, charts=[{"kpi": k.id, "viz": k.viz, "title": title, "def": ctx.kpi_def(k), "result": res}])


# ---------------------------------------------------------------------------------------------------- compare_periods
def compare_periods(ctx: ToolContext, kpi_id: str, period_a=None, period_b=None, against: str = "previous") -> dict:
    k = _kpi(ctx, kpi_id, ("metric",), "compare_periods")
    if against not in ("previous", "yoy"):
        raise ToolError("bad_args", "against must be 'previous' or 'yoy'")
    ra = ctx.period(period_a, "none")
    if period_b:
        rb = ctx.period(period_b, "none")
    else:
        from ...engine import compare_range
        cr = compare_range(ra.start, ra.end, against)
        rb = ctx.period({"start": cr[0].isoformat(), "end": cr[1].isoformat()}, "none")
    a, b = ctx.run(k, ra), ctx.run(k, rb)
    va, vb = _num(a.get("value")), _num(b.get("value"))
    if va is None and vb is None:
        raise ToolError("no_data", "There is no data for either period.", kpi=k.id)
    title = ctx.title(k)
    fa = ctx.fact(f"{title} ({ctx.period_text(ra)})", va, k.fmt, ctx.period_text(ra))
    fb = ctx.fact(f"{title} ({ctx.period_text(rb)})", vb, k.fmt, ctx.period_text(rb))
    facts = [fa, fb]
    if va is not None and vb is not None:
        ctext, craw = change_text(ctx, k, va, vb)
        facts.append(ctx.fact(f"{title}: {ctx.tx('tl_change', value='').strip()}", craw, None, f"{ctx.period_text(ra)} vs {ctx.period_text(rb)}",
                              text=f"{title}: {ctx.money(va, k.fmt)} {ctx.tx('tl_vs')} {ctx.money(vb, k.fmt)}" + (f", {ctx.tx('tl_change', value=ctext)}" if ctext else ""),
                              value_text=ctext, raw_a=va, raw_b=vb))
    return _ok(ctx, "compare_periods", facts, ra, k, meta_b=ctx.period_meta(rb),
               charts=[{"kpi": k.id, "viz": k.viz, "title": title, "def": ctx.kpi_def(k), "result": {"a": a, "b": b}}])


# ---------------------------------------------------------------------------------------------------- kpi_breakdown
def _row_cells(ctx: ToolContext, kpi, kind, cols, rows, n):
    keys = set((load_story_config().get("personal_data") or {}).get("row_keys") or [])
    out = []
    for r in rows[:n]:
        item = {}
        for c in cols:
            v = r.get(c.key)
            if v is None:
                continue
            if c.fmt in (None, "text"):
                item[c.label] = ctx.alias(kind, v) if kind and c.key in keys else v
            else:
                item[c.label] = ctx.money(v, c.fmt)
        out.append(item)
    return out


def _cell_name(y, x, hours: bool = True) -> str:
    """'Sat 19:00' for a weekday x hour cell; '2026-09 / M1' (cohort month, months since) or 'Sat / 3' for any other pair.
    A bare '19' would read as a figure."""
    xs = str(x).strip()
    if hours and xs.isdigit() and 0 <= int(xs) <= 23:
        return f"{y} {int(xs):02d}:00"
    return f"{y} / {x}"


def kpi_breakdown(ctx: ToolContext, kpi_id: str, period=None, top_n: int = 10, compare: str = "none") -> dict:
    k = _kpi(ctx, kpi_id, ("donut", "hbar", "bar", "table", "heatmap"), "kpi_breakdown")
    if not isinstance(top_n, int) or isinstance(top_n, bool) or not 1 <= top_n <= MAX_TOP_N:
        raise ToolError("bad_args", f"top_n must be a whole number from 1 to {MAX_TOP_N}")
    req = ctx.period(period, "none")
    res = ctx.run(k, req)
    if res.get("empty"):
        raise ToolError("no_data", "There is no data for that period.", kpi=k.id)
    title, ptxt, kind = ctx.title(k), ctx.period_text(req), ctx.personal_kind(k)
    facts: list[dict] = []
    tables: list[dict] = []
    if res["type"] == "heatmap":
        cells = sorted((c for c in res.get("cells") or [] if _num(c[2]) is not None), key=lambda c: -_num(c[2]))
        xs, ys = res.get("x") or [], res.get("y") or []
        rows = []
        hours = "hour" in k.id                          # only an hour-of-day axis reads as 19:00; 'months since first purchase' does not
        if "cohort" in k.id:                            # month 0 of every cohort is 100% by definition: not an answer
            cells = [c for c in cells if str(xs[c[0]]).strip() != "0"] or cells
        for rank, (xi, yi, v) in enumerate(cells[:top_n], 1):
            yl = fmt_bucket(ys[yi], "month", ctx.lang) if "cohort" in k.id else ys[yi]          # '2026-09' reads as 'Sep 2026'
            when = _cell_name(yl, xs[xi], hours) if hours else _cell_name(yl, f"M{xs[xi]}" if "cohort" in k.id else xs[xi], False)
            facts.append(ctx.fact(f"{title}: {when}", v, k.fmt, ptxt, text=f"{when}: {ctx.money(v, k.fmt)}", rank=rank, name=when))
            rows.append({"rank": rank, "name": when, "value": ctx.money(v, k.fmt)})
        tables.append({"title": title, "columns": ["#", "name", "value"], "rows": rows, "total_rows": len(cells)})
    elif res["type"] == "table":
        from ...i18n import t
        cols = [type(c)(c.key, t(ctx.lang, c.label), c.fmt) for c in k.columns]
        rows = _row_cells(ctx, k, kind, cols, res["rows"], top_n)
        for i, r in enumerate(rows, 1):
            facts.append(ctx.fact(f"{title} #{i}", None, None, ptxt, text="; ".join(f"{a}: {b}" for a, b in r.items()), cells=r))
        tables.append({"title": title, "columns": [c.label for c in cols], "rows": rows,
                       "total_rows": len(res["rows"])})
    else:
        items = sorted((i for i in res["items"] if _num(i.get("value")) is not None), key=lambda i: -_num(i["value"]))
        total = sum(_num(i["value"]) for i in items)
        partial = is_partial(k, len(items))
        share_ok = (not partial and not non_additive(k.id) and k.fmt in ("currency", "int", "number") and total > 0
                    and all(_num(i["value"]) >= 0 for i in items) and len(items) > 1)
        rows = []
        for rank, i in enumerate(items[:top_n], 1):
            name = ctx.alias(kind, i["label"]) if kind else ctx.daypart_name(k, i["label"])
            share = 100.0 * _num(i["value"]) / total if share_ok else None
            stext = ctx.money(share, "pct") if share is not None else None
            text = f"{name}: {ctx.money(i['value'], k.fmt)}" + (f" ({ctx.tx('tl_of_total', value=stext)})" if stext else "")
            facts.append(ctx.fact(f"{title}: {name}", i["value"], k.fmt, ptxt, text=text, rank=rank, name=name,
                                  share=stext, raw_share=share))
            rows.append({"rank": rank, "name": name, "value": ctx.money(i["value"], k.fmt), "share": stext})
        if share_ok:
            facts.append(ctx.fact(f"{title}: total", total, k.fmt, ptxt,
                                  text=f"{title}: {ctx.tx('tl_total', value=ctx.money(total, k.fmt), n=len(items))}"))
        if len(items) > top_n:
            facts.append(ctx.fact(f"{title}: items not shown", len(items) - top_n, "int", ptxt,
                                  text=ctx.tx("tl_more_items", n=len(items) - top_n)))
        tables.append({"title": title, "columns": ["#", "name", "value", "share"], "rows": rows, "total_rows": len(items)})
    return _ok(ctx, "kpi_breakdown", facts, req, k, tables=tables,
               charts=[{"kpi": k.id, "viz": k.viz, "title": title, "def": ctx.kpi_def(k), "result": res}])


# ---------------------------------------------------------------------------------------------------- kpi_trend
def _bucket_span(key, grain: str):
    """(first day, last day) of a week / month / quarter bucket, or None."""
    import re
    from datetime import date, timedelta
    k = str(key)
    try:
        if grain == "month" and re.fullmatch(r"\d{4}-\d{2}.*", k):
            y, m = int(k[:4]), int(k[5:7])
            return date(y, m, 1), date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)
        if grain == "week" and re.fullmatch(r"\d{4}-\d{2}-\d{2}.*", k):
            s = date.fromisoformat(k[:10])
            return s, s + timedelta(days=6)
        m = re.fullmatch(r"(\d{4})-Q([1-4])", k)
        if grain == "quarter" and m:
            y, q = int(m.group(1)), int(m.group(2))
            first = date(y, 3 * q - 2, 1)
            nxt = date(y + (q == 4), 1 if q == 4 else 3 * q + 1, 1)
            return first, nxt - timedelta(days=1)
    except ValueError:
        return None
    return None


def _complete_points(pts, grain: str, start, end):
    """Points whose bucket lies fully inside the period: a month still in progress is neither the 'lowest' nor 'latest'.
    -> (points, dropped_partial_last). Falls back to all points when too few would remain."""
    if grain not in ("week", "month", "quarter") or len(pts) < 3:
        return pts, False
    keep = list(pts)
    dropped = False
    sp = _bucket_span(keep[-1][0], grain)
    if sp and sp[1] > end:
        keep, dropped = keep[:-1], True
    sp = _bucket_span(keep[0][0], grain) if keep else None
    if sp and sp[0] < start:
        keep = keep[1:]
    return (keep, dropped) if len(keep) >= 2 else (pts, False)


def kpi_trend(ctx: ToolContext, kpi_id: str, period=None, grain: str = "auto") -> dict:
    k = _kpi(ctx, kpi_id, ("timeseries",), "kpi_trend")
    if grain not in ("auto", "day", "week", "month", "quarter"):
        raise ToolError("bad_args", "grain must be auto, day, week, month or quarter")
    req = ctx.period(period, "none", grain)
    res = ctx.run(k, req)
    if res.get("empty"):
        raise ToolError("no_data", "There is no data for that period.", kpi=k.id)
    title, ptxt, kind = ctx.title(k), ctx.period_text(req), ctx.personal_kind(k)
    g = res.get("grain", "day")
    facts: list[dict] = []
    for s in (res.get("series") or [])[:MAX_SERIES]:
        pts = [(x, _num(v)) for x, v in zip(res.get("x") or [], s.get("data") or []) if _num(v) is not None]
        if len(pts) < 2:
            continue
        pts, partial_last = _complete_points(pts, g, req.start, req.end)
        fm = s.get("fmt") or k.fmt
        name = s.get("name") or title
        if kind and s.get("name"):
            name = ctx.alias(kind, name)
        label = f"{title}" if name == title else f"{title}: {name}"
        hi, lo = max(pts, key=lambda p: p[1]), min(pts, key=lambda p: p[1])
        last = pts[-1]
        avg = sum(p[1] for p in pts) / len(pts)
        half = len(pts) // 2
        a1 = sum(p[1] for p in pts[:half]) / max(half, 1)
        a2 = sum(p[1] for p in pts[half:]) / max(len(pts) - half, 1)
        if hi[1] == lo[1]:                       # a series that never moved: say so, never "highest 0.0%, lowest 0.0%"
            facts.append(ctx.fact(label, last[1], fm, ptxt, text=ctx.tx("tl_flat", label=label, value=ctx.money(last[1], fm), n=len(pts)),
                                  latest=ctx.money(last[1], fm), latest_at=fmt_bucket(last[0], g, ctx.lang), points=len(pts), grain=g,
                                  flat=True, note=ctx.tx("tl_partial_last") if partial_last else None))
            continue
        extra = {}
        txt = (f"{label}: {ctx.tx('tl_latest', value=ctx.money(last[1], fm))} ({fmt_bucket(last[0], g, ctx.lang)}), "
               f"{ctx.tx('tl_highest', value=ctx.money(hi[1], fm))} ({fmt_bucket(hi[0], g, ctx.lang)}), "
               f"{ctx.tx('tl_lowest', value=ctx.money(lo[1], fm))} ({fmt_bucket(lo[0], g, ctx.lang)}), "
               f"{ctx.tx('tl_average', value=ctx.money(avg, fm), n=len(pts))}")
        if a1:
            ctext, craw = change_text(ctx, type("K", (), {"fmt": "number"})(), a2, a1)
            txt += f"; {ctx.tx('tl_half_change', value=ctext)}"
            extra = {"second_half_vs_first_half": ctext, "raw_half_change": craw}
        facts.append(ctx.fact(label, last[1], fm, ptxt, text=txt, latest=ctx.money(last[1], fm),
                              highest=ctx.money(hi[1], fm), lowest=ctx.money(lo[1], fm), average=ctx.money(avg, fm),
                              latest_at=fmt_bucket(last[0], g, ctx.lang), highest_at=fmt_bucket(hi[0], g, ctx.lang),
                              lowest_at=fmt_bucket(lo[0], g, ctx.lang),
                              points=len(pts), grain=g, raw_highest=hi[1], raw_lowest=lo[1], raw_average=avg,
                              note=ctx.tx("tl_partial_last") if partial_last else None, **extra))
    if not facts:
        raise ToolError("no_data", "There are too few points to show a trend.", kpi=k.id)
    if g == "day":                                    # days far from the period's usual level (same rule as the report)
        from ...reports.analysis import unusual_days
        from ...reports.story_rules import load_story_config as _cfg
        u = unusual_days(res, float(((_cfg().get("analysis") or {}).get("sigma")) or 2.0), ctx.lang)
        if u:
            def items(rows):
                return ", ".join(f"{fmt_bucket(r['date'], 'day', ctx.lang)} ({ctx.money(r['value'], u.get('fmt') or k.fmt)})"
                                 for r in sorted(rows, key=lambda r: -abs(r['value'] - u['average']))[:3])
            for side, key in (("high", "tl_unusual_high"), ("low", "tl_unusual_low")):
                if u[side]:
                    facts.append(ctx.fact(f"{title}: {side}", len(u[side]), "int", ptxt, text=ctx.tx(key, items=items(u[side])),
                                          unusual=side, days=[r["date"] for r in u[side]],
                                          times_average=[round(r["times_average"], 1) for r in u[side] if r.get("times_average")]))
        elif len(facts) and len(res.get("x") or []) >= 10:
            facts.append(ctx.fact(f"{title}: unusual days", 0, "int", ptxt, text=ctx.tx("tl_unusual_none"), unusual="none"))
    return _ok(ctx, "kpi_trend", facts, req, k, charts=[{"kpi": k.id, "viz": k.viz, "title": title, "def": ctx.kpi_def(k), "result": res}])
