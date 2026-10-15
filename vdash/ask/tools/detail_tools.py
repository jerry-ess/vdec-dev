"""Detail tools: ``forecast``, ``drill`` and ``customer_segments``.

* ``forecast``          - the dashboard's own forecast KPIs (the forecasting run's output); nothing is re-forecast here.
* ``drill``             - the rows behind one chart element, built by the dashboard's drill-down code (same rows, same
                          totals, same role rules).
* ``customer_segments`` - the customers of a segment (manager role): the dashboard's "customer segment list", with
                          names replaced by Customer A, Customer B ... before they leave the server.
Template-level: the KPIs, drill definitions and list definitions come from the catalog; the tools name no tenant.
"""
from __future__ import annotations

import dataclasses

from ...drill import DrillError, build as build_drill, drop_empty_columns
from ...exports import build_count_sql, build_export_sql, filter_options
from ...fmt import fmt_date
from ...i18n import t
from ...reports.story_rules import load_story_config
from .catalog_tools import _kpi, _row_cells
from .common import ToolContext, ToolError, _num

MAX_ROWS = 20
FORECAST_TEMPLATE_KPI = "KPI-FCST-001"
CUSTOMER_FUTURE_KPI = "predicted_future_value"      # what current customers are expected to spend (customer-value prediction)


def _top_n(top_n, hi: int = MAX_ROWS) -> int:
    if not isinstance(top_n, int) or isinstance(top_n, bool) or not 1 <= top_n <= hi:
        raise ToolError("bad_args", f"top_n must be a whole number from 1 to {hi}")
    return top_n


# ---------------------------------------------------------------------------------------------------- forecast
def forecast(ctx: ToolContext, top_n: int = 5, focus: str = "demand") -> dict:
    """What the predictions expect: ``focus`` "demand" (default) = the next days' demand and the products expected to sell
    most; "customers" = what the current customers are expected to spend in the future (customer future value)."""
    top_n = _top_n(top_n, 15)
    if focus not in ("demand", "customers"):
        raise ToolError("bad_args", "focus must be 'demand' or 'customers'")
    if focus == "customers":
        kpis = [k for k in ctx.catalog.kpis if k.id == CUSTOMER_FUTURE_KPI]
    else:
        kpis = [k for k in ctx.catalog.kpis if FORECAST_TEMPLATE_KPI in k.template_kpis and k.viz in ("metric", "table")]
    facts, tables, charts, used = [], [], [], None
    req = ctx.period(None, "none")
    ptxt, last_err = ctx.period_text(req), None
    for k in kpis:
        try:
            ctx.check_kpi(k)
            res = ctx.run(k, req)
        except ToolError as e:
            last_err = e
            continue
        if res.get("empty"):
            continue
        used = used or k
        title = ctx.title(k)
        note = res.get("caveat")
        if res["type"] == "metric" and _num(res.get("value")) is not None:
            facts.append(ctx.fact(title, res["value"], k.fmt, "", text=f"{title}: {ctx.money(res['value'], k.fmt)}"
                                  + (f" ({note})" if note else ""), note=note))
        elif res["type"] == "table":
            cols = [type(c)(c.key, t(ctx.lang, c.label), c.fmt) for c in k.columns]
            rows = _row_cells(ctx, k, ctx.personal_kind(k), cols, res["rows"], top_n)
            for i, r in enumerate(rows, 1):
                facts.append(ctx.fact(f"{title} #{i}", None, None, "", text="; ".join(f"{a}: {b}" for a, b in r.items()),
                                      rank=i, cells=r))
            tables.append({"title": title, "columns": [c.label for c in cols], "rows": rows, "total_rows": len(res["rows"])})
        charts.append({"kpi": k.id, "viz": k.viz, "title": title, "result": res})
    if not facts:
        if last_err is not None:
            raise last_err
        raise ToolError("not_available", "This business has no customer-value prediction yet." if focus == "customers"
                        else "This business has no forecast yet.")
    return {"ok": True, "tool": "forecast", "facts": facts, "tables": tables, "charts": charts,
            "meta": ctx.meta(req, used)}


# ---------------------------------------------------------------------------------------------------- drill
def _pii_kind(expr: str) -> str:
    cols = (load_story_config().get("personal_data") or {}).get("columns") or {}
    for col, kind in cols.items():
        if col in expr:
            return kind
    return "customer"


def drill(ctx: ToolContext, kpi_id: str, period=None, key: str | None = None, bucket: str | None = None,
          grain: str = "day", top_n: int = 10) -> dict:
    """The rows behind one chart element (one category, or one time bucket) of a KPI."""
    top_n = _top_n(top_n)
    if grain not in ("day", "week", "month", "quarter", "year"):
        raise ToolError("bad_args", "grain must be day, week, month, quarter or year")
    if (key is None) == (bucket is None):
        raise ToolError("bad_args", "give exactly one of key (a category) or bucket (a period)")
    for name, v in (("key", key), ("bucket", bucket)):
        if v is not None and (not isinstance(v, str) or not v or len(v) > 200):
            raise ToolError("bad_args", f"{name} must be short text")
    k = _kpi(ctx, kpi_id, ("donut", "hbar", "bar", "timeseries", "table", "metric", "heatmap"), "drill")
    if k.drill is None:
        raise ToolError("shape_unavailable", "This KPI has no detail rows.", kpi=k.id)
    req = ctx.period(period, "none")
    src = ctx.catalog.details[k.drill.source]
    ds = ctx.dash.svc.registry.get(ctx.tc.datasource)
    clicked = {"key": key} if key is not None else {"bucket": bucket, "grain": grain}
    try:
        rows_sql, sum_sql, params, cols, rng = build_drill(
            k, src, clicked, req.start, req.end, ds.dialect, ctx.tc.tenant_id,
            show_pii=ctx.user.has_role("manager"), available=ds.columns())
    except DrillError as exc:
        raise ToolError("bad_args", str(exc), kpi=k.id)
    summary = ds.query(sum_sql, params)[0]
    total_rows = int(summary["n"] or 0)
    if total_rows == 0:
        raise ToolError("no_data", "There are no rows behind that value.", kpi=k.id)
    rows = ds.query(f"{rows_sql} LIMIT {top_n}", params)
    cols = drop_empty_columns(cols, rows)
    title, ptxt = ctx.title(k), ctx.period_text(req)
    shown, cells = [], []
    for r in rows:
        item, parts = {}, []
        for c in cols:
            v = r.get(c.key)
            if v is None:
                continue
            label = t(ctx.lang, c.label)
            if c.fmt == "date":
                s = fmt_date(v, ctx.lang)
            elif c.fmt in ("text", None):
                s = ctx.alias(_pii_kind(c.expr), v) if c.pii else t(ctx.lang, v)
            else:
                s = ctx.money(v, c.fmt)
            item[label] = s
            parts.append(f"{label}: {s}")
        shown.append(item)
        cells.append("; ".join(parts))
    what = key if key is not None else bucket
    total_label = t(ctx.lang, next((c.label for c in src.columns if c.expr == src.total_expr), "Total"))
    facts = [ctx.fact(f"{title}: {what}", summary.get("total_v"), next((c.fmt for c in src.columns if c.expr == src.total_expr), "number"),
                      ptxt, text=ctx.tx("tl_drill_total", n=total_rows, what=what, label=total_label,
                                        value=ctx.money(summary.get("total_v"), next((c.fmt for c in src.columns if c.expr == src.total_expr), "number")) or "-"),
                      rows=total_rows)]
    for i, text in enumerate(cells, 1):
        facts.append(ctx.fact(f"{title}: {what} #{i}", None, None, ptxt, text=text, rank=i, cells=shown[i - 1]))
    if total_rows > len(shown):
        facts.append(ctx.fact(f"{title}: rows not shown", total_rows - len(shown), "int", ptxt,
                              text=ctx.tx("tl_more_items", n=total_rows - len(shown))))
    return {"ok": True, "tool": "drill", "facts": facts,
            "tables": [{"title": title, "columns": [t(ctx.lang, c.label) for c in cols], "rows": shown, "total_rows": total_rows}],
            "charts": [], "meta": ctx.meta(req, k, clicked=clicked)}


# ---------------------------------------------------------------------------------------------------- customer_segments
def customer_segments(ctx: ToolContext, segment: str | None = None, churn_band: str | None = None,
                      top_n: int = 10, top: bool = False) -> dict:
    """Customers of a segment (and/or a risk-of-leaving band), biggest spenders first. Manager role. ``top``: the most
    valuable customers overall (lifetime spend), each with segment, risk of leaving and expected future spend."""
    top_n = _top_n(top_n)
    exp = next((e for e in ctx.catalog.exports if e.id == "customer_segments"), None)
    if exp is None:
        raise ToolError("not_available", "This business has no data for that yet.")
    if not ctx.user.has_role(exp.min_role):
        raise ToolError("forbidden", ctx.tx("ask_tool_forbidden"))
    if not (ctx.st.get("availability") or {}).get(f"export:{exp.id}", {}).get("available"):
        raise ToolError("not_available", "This business has no data for that yet.")
    ds = ctx.dash.svc.registry.get(ctx.tc.datasource)
    opts = {f["param"]: {o["value"]: o["count"] for o in f["options"]} for f in filter_options(exp, ds, ctx.tc.tenant_id)}
    chosen = {}
    for param, val, label in (("segment", segment, "segment"), ("churn", churn_band, "churn_band")):
        if val is None:
            continue
        if not isinstance(val, str) or val not in opts.get(param, {}):
            raise ToolError("bad_args", f"unknown {label} '{val}'", allowed=sorted(opts.get(param, {})))
        chosen[param] = val
    cnt_sql, cnt_params = build_count_sql(exp, ds, chosen)
    cnt_params["tenant_id"] = ctx.tc.tenant_id
    total = int(ds.query(cnt_sql, cnt_params)[0]["n"])
    ptxt = ctx.st.get("max_date") or ""
    facts = []
    if top and not chosen:                                   # the most valuable customers overall
        facts.append(ctx.fact(t(ctx.lang, "Customers"), total, "int", ptxt))
    elif not chosen:                                         # overview: how many customers per segment / risk band
        for param, title in (("segment", "Customer segment"), ("churn", "Risk of leaving (band)")):
            for val, n in (opts.get(param) or {}).items():
                facts.append(ctx.fact(f"{t(ctx.lang, title)}: {t(ctx.lang, val)}", n, "int", ptxt,
                                      text=f"{t(ctx.lang, title)} - {t(ctx.lang, val)}: {n}", segment=t(ctx.lang, val)))
    else:
        what = ", ".join(t(ctx.lang, v) for v in chosen.values())
        facts.append(ctx.fact(what, total, "int", ptxt, text=ctx.tx("tl_segment_count", n=total, what=what)))
    sql, params, cols = build_export_sql(dataclasses.replace(exp, max_rows=top_n), ds, chosen)
    params["tenant_id"] = ctx.tc.tenant_id
    rows = ds.query(sql, params) if (chosen or top) else []
    shown = []
    for i, r in enumerate(rows, 1):
        item, parts = {}, []
        for c in cols:
            v = r.get(c.key)
            if v is None:
                continue
            label = t(ctx.lang, c.label)
            if c.key == "customer":
                s = ctx.alias("customer", v)
            elif c.fmt == "date":
                s = fmt_date(v, ctx.lang)
            elif c.fmt == "text":
                s = t(ctx.lang, v)
            else:
                s = ctx.money(v, c.fmt)
            item[label] = s
            parts.append(f"{label}: {s}")
        shown.append(item)
        facts.append(ctx.fact(f"#{i}", None, None, ptxt, text="; ".join(parts), rank=i, cells=item))
    if not facts:
        raise ToolError("no_data", "No customers match.")
    return {"ok": True, "tool": "customer_segments", "facts": facts,
            "tables": [{"title": t(ctx.lang, exp.title), "columns": [t(ctx.lang, c.label) for c in cols], "rows": shown,
                        "total_rows": total}] if shown else [],
            "charts": [], "meta": ctx.meta(chosen=chosen)}
