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
def _norm_opt(v) -> str:
    import re
    return re.sub(r"[^0-9a-zà-ỹ]+", " ", str(v).lower()).strip()


def _match_option(ctx: ToolContext, val: str, param: str, opts: dict):
    """-> (param, option value) or None. A model writes 'at_risk' where the list holds 'At risk', and may put a segment
    into the risk-band slot (or the reverse): tolerate case, underscores and the translated label, and look in the other
    list when this one has no such option."""
    for p in (param, "churn" if param == "segment" else "segment"):
        for o in opts.get(p, {}):
            if val == o:
                return p, o
    want = _norm_opt(val)
    for p in (param, "churn" if param == "segment" else "segment"):
        for o in opts.get(p, {}):
            if want and want in (_norm_opt(o), _norm_opt(t(ctx.lang, o))):
                return p, o
    if len(want) >= 4:                                   # "loyal" for "Loyal Customers", "vô địch" for "Nhà vô địch": one unambiguous hit
        hits = [(p, o) for p in (param, "churn" if param == "segment" else "segment") for o in opts.get(p, {})
                if want in _norm_opt(o) or want in _norm_opt(t(ctx.lang, o))]
        pre = [(p, o) for p, o in hits if _norm_opt(o).startswith(want) or _norm_opt(t(ctx.lang, o)).startswith(want)]
        for group in (pre, hits):
            if len(group) == 1:
                return group[0]
    return None


_RISK_WORD = __import__("re").compile(r"^(high|higher|highest|very high|critical|severe|cao|rat cao|hoch|hohes|hohe|sehr hoch)\b")
_BAND_NOTE = {"en": "No customer is in the high-risk band yet; showing the highest-risk band that has customers: {band}.",
              "vi": "Chưa có khách hàng nào ở mức nguy cơ cao; đây là mức nguy cơ cao nhất hiện có khách hàng: {band}.",
              "de": "Noch kein Kunde liegt im Band mit hohem Risiko; angezeigt wird das höchste Band mit Kunden: {band}."}


_NO_NAMES = {"en": "Customer names are not in this business's data, so only the number of customers per group can be shown.",
             "vi": "Dữ liệu của doanh nghiệp này không có tên khách hàng, nên chỉ có thể cho biết số khách hàng theo từng nhóm.",
             "de": "In den Daten dieses Unternehmens gibt es keine Kundennamen, daher kann nur die Zahl der Kunden je Gruppe gezeigt werden."}


_NO_GROUP = {"en": "No customer is in the '{group}' group; the numbers below are all groups.",
             "vi": "Hiện không có khách hàng nào thuộc nhóm '{group}'; các số dưới đây là của tất cả các nhóm.",
             "de": "Kein Kunde gehört zur Gruppe '{group}'; unten stehen alle Gruppen."}


_NO_HIGH_RISK = {"en": "No customer is at high risk of leaving; every customer is in the lower bands shown below.",
                 "vi": "Hiện không có khách hàng nào có nguy cơ rời bỏ cao; mọi khách hàng đều thuộc các nhóm thấp hơn dưới đây.",
                 "de": "Kein Kunde hat ein hohes Abwanderungsrisiko; alle Kunden liegen in den unten gezeigten niedrigeren Bändern."}


_EN_SEGMENTS = ("Champions", "Loyal customers", "Potential loyalists", "New customers", "Promising", "Need attention", "About to sleep",
                "At risk", "Can't lose them", "Hibernating", "Lost")


def _segment_counts(ctx: ToolContext, segment, churn_band) -> dict:
    """The customer list needs customer names. When the data has none (anonymous customers) the dashboard still shows the
    customers per buying-behaviour segment and per risk band: answer from those two charts, and say names are missing."""
    from .catalog_tools import kpi_breakdown
    avail = ctx.st.get("availability") or {}
    wanted = [(kid, val) for kid, val in (("rfm_segments", segment), ("churn_risk_distribution", churn_band))
              if avail.get(kid, {}).get("available")]
    if len(wanted) == 2 and (segment is None) != (churn_band is None):       # asked about one of them: that chart only
        wanted = [w for w in wanted if w[1] is not None]
    facts, tables, charts, got, notes_missing = [], [], [], 0, []
    for kid, val in wanted:
        try:
            r = kpi_breakdown(ctx, kid, top_n=12)
        except ToolError:
            continue
        want = _norm_opt(val) if isinstance(val, str) else ""
        keep = [f for f in r["facts"] if f.get("rank") is not None]
        en_by_rank = {}
        if want and ctx.lang != "en":          # the group as English names it: the same chart, same ranks, read in English
            try:
                en_by_rank = {f["rank"]: _norm_opt(f.get("name") or "")
                              for f in kpi_breakdown(dataclasses.replace(ctx, lang="en", aliases={}, _alias_of={}, _counts={}), kid, top_n=12)["facts"]
                              if f.get("rank") is not None}
            except Exception:                  # noqa: BLE001 - the localized names alone are the fallback
                en_by_rank = {}
        hit = [f for f in keep if want and (want in _norm_opt(f.get("name") or "") or want in en_by_rank.get(f.get("rank"), ""))]
        if want and not hit:                                   # the whole chart is shown; "nobody is in it" is said only when that is certain
            known = kid == "rfm_segments" and any(want in _norm_opt(e) for e in _EN_SEGMENTS)       # a real segment the chart does not list
            if (kid == "churn_risk_distribution" and _RISK_WORD.search(want)) or known:
                notes_missing.append(val)
        facts += hit or r["facts"]
        tables += r["tables"]
        charts += r["charts"]
        got += 1
    if not got:
        raise ToolError("not_available", "This business has no data for that yet.")
    for val in notes_missing[::-1]:                  # first: "nobody is in that group" is the answer, the bands below are context
        risk = _RISK_WORD.search(_norm_opt(val))
        tpl = (_NO_HIGH_RISK if risk else _NO_GROUP).get(ctx.lang) or (_NO_HIGH_RISK if risk else _NO_GROUP)["en"]
        facts.insert(0, ctx.fact("no_such_group", None, None, ctx.st.get("max_date") or "", text=tpl.format(group=val)))
    note = _NO_NAMES.get(ctx.lang, _NO_NAMES["en"])
    last = next((f for f in reversed(facts) if f.get("rank") is not None or f.get("raw") is not None), None)
    if last is not None:                  # a caveat on a real figure: a separate text-only fact got quoted by the writer as if it were a share
        last["note"] = note
    else:
        facts.append(ctx.fact("names_note", None, None, ctx.st.get("max_date") or "", text=note, note=note))
    return {"ok": True, "tool": "customer_segments", "facts": facts, "tables": tables, "charts": charts, "meta": ctx.meta(counts_only=True)}


def customer_segments(ctx: ToolContext, segment: str | None = None, churn_band: str | None = None,
                      top_n: int = 10, top: bool = False) -> dict:
    """Customers of a segment (and/or a risk-of-leaving band), biggest spenders first. Manager role. ``top``: the most
    valuable customers overall (lifetime spend), each with segment, risk of leaving and expected future spend."""
    top_n = _top_n(top_n)
    exp = next((e for e in ctx.catalog.exports if e.id == "customer_segments"), None)
    if exp is not None and not ctx.user.has_role(exp.min_role):
        raise ToolError("forbidden", ctx.tx("ask_tool_forbidden"))
    if exp is None or not (ctx.st.get("availability") or {}).get(f"export:{exp.id}", {}).get("available"):
        return _segment_counts(ctx, segment, churn_band)     # no customer names in the data: the counts per segment still exist
    ds = ctx.dash.svc.registry.get(ctx.tc.datasource)
    opts = {f["param"]: {o["value"]: o["count"] for o in f["options"]} for f in filter_options(exp, ds, ctx.tc.tenant_id)}
    chosen, missing_band = {}, None
    for param, val, label in (("segment", segment, "segment"), ("churn", churn_band, "churn_band")):
        if val is None:
            continue
        if not isinstance(val, str):
            raise ToolError("bad_args", f"unknown {label} '{val}'", allowed=sorted(opts.get(param, {})))
        hit = _match_option(ctx, val, param, opts)
        if hit is None and param == "churn" and _RISK_WORD.search(_norm_opt(val)) and opts.get("churn"):
            # a risk level that no customer is in yet ("high" when everybody is below 30%): say so and show the highest band that has customers
            ranked = sorted(opts["churn"], key=lambda o: next((i for i, w in enumerate(("high", "medium", "low")) if _norm_opt(o).startswith(w)), 9))
            chosen["churn"] = ranked[0]
            missing_band = val
            continue
        if hit is None and param == "churn":             # a band the data does not have: the most valuable customers, each with their own risk
            top = True
            continue
        if hit is None:
            raise ToolError("bad_args", f"unknown {label} '{val}'", allowed=sorted(opts.get(param, {})))
        if hit[0] in chosen:                             # the model put a segment into the band slot as well: the explicit segment wins
            continue
        chosen[hit[0]] = hit[1]
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
                                      text=f"{t(ctx.lang, title)} - {t(ctx.lang, val)}: {n}", segment=t(ctx.lang, val), group=param))
    else:
        what = ", ".join(t(ctx.lang, v) for v in chosen.values())
        facts.append(ctx.fact(what, total, "int", ptxt, text=ctx.tx("tl_segment_count", n=total, what=what)))
        if missing_band:
            facts.append(ctx.fact("risk_band_note", None, None, ptxt, text=_BAND_NOTE.get(ctx.lang, _BAND_NOTE["en"]).format(band=what)))
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
