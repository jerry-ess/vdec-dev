"""External-data tool: ``external_context`` (talk-to-data, external-data plan E2).

What outside things (events and holidays, weather, prices and the economy) clearly move this business's sales?

The tool does NOT calculate a verdict of its own. It reads the dashboard's own "What affects your sales" summary
(KPI ``market_verdict``, rows made by ``drivers.table_rows`` from the shared signal registry and the tenant's
Step 5 verdicts), so a chat answer and the dashboard page can never disagree. Only factors with a clear effect get
an effect size; everything else is one plain "no noticeable effect" fact and no chart. The wording is a pattern in
the data, never a proven cause. The tenant and its market profile come from the server session, never from arguments.
"""
from __future__ import annotations

from ... import drivers as D
from ...fmt import fmt_date
from ...i18n import t
from .common import ToolContext, ToolError, _num
from .money_style import ask_arg as fmt_arg

KPI_ID = "market_verdict"
FACTOR_GROUPS = ("any", "events", "weather", "economy")
_WEATHER = {D.CLEAR_LABEL["temperature"], D.CLEAR_LABEL["rain"], D.S_WEATHER, D.S_WEATHER_ECON}
_NONE_LINES = {D.S_WEATHER, D.S_ECON, D.S_WEATHER_ECON, D.S_OTHER}
_UPTO = {"en": "External data up to {d}", "vi": "Dữ liệu bên ngoài cập nhật đến {d}",
         "de": "Externe Daten bis {d}", "zh": "外部数据更新至 {d}"}
_NO_WEATHER = {"en": "No weather data is linked to your location, so weather cannot be checked.",
               "vi": "Chưa có dữ liệu thời tiết cho khu vực của bạn nên chưa kiểm tra được tác động của thời tiết.",
               "de": "Für Ihren Standort sind keine Wetterdaten verknüpft, daher kann das Wetter nicht geprüft werden.",
               "zh": "您所在地区未关联天气数据，因此无法检查天气影响。"}
_NOT_CLEAR = {"en": "No clear effect of outside factors on your sales was found in your history.",
              "vi": "Chưa thấy yếu tố bên ngoài nào tác động rõ rệt đến doanh số trong lịch sử của bạn.",
              "de": "In Ihrer Historie wurde kein klarer Einfluss äußerer Faktoren auf den Umsatz gefunden.",
              "zh": "在您的历史数据中未发现外部因素对销售有明确影响。"}


def _group(row: dict) -> str:
    """events | weather | economy for one summary row (English names, before translation)."""
    f = row.get("factor")
    if f in _WEATHER:
        return "weather"
    if row.get("when") or row.get("verdict") in (D.V_NOPAT, D.V_POS_UP, D.V_POS_DOWN, D.V_EARLY_UP, D.V_EARLY_DOWN, D.V_ONCE_SMALL, D.V_DEF_UP, D.V_DEF_DOWN, D.V_SHARED) or row.get("history"):
        return "events"
    return "economy"


def _data_through(ctx: ToolContext) -> str | None:
    """Latest day the shared external data holds for this tenant's market (best effort; None when unknown)."""
    try:
        ds = ctx.ds
        r = ds.query("SELECT MAX(v.obs_date) AS m FROM vw_tenant_external_signal v WHERE v.tenant_id = :tenant_id "
                     "AND v.status <> 'forecast'", {"tenant_id": ctx.tc.tenant_id})
        m = (r or [{}])[0].get("m")
        return str(m)[:10] if m else None
    except Exception:           # noqa: BLE001 - freshness is a nicety; the verdicts stand without it
        return None


def external_context(ctx: ToolContext, factor: str = "any", period=None) -> dict:
    """Which outside factors clearly change sales for this business? ``factor``: any | events | weather | economy."""
    if factor not in FACTOR_GROUPS:
        raise ToolError("bad_args", f"factor must be one of {', '.join(FACTOR_GROUPS)}")
    k = ctx.catalog.by_id.get(KPI_ID)
    if k is None:
        raise ToolError("not_available", "This business has no data for that yet.", kpi=KPI_ID)
    ctx.check_kpi(k)                                   # role + the business has daily sales linked to external data
    req = ctx.period(period, "none")
    raw = ctx.dash.run_many(ctx.user, ctx.tc, [k], req, ctx.st)[KPI_ID]      # untranslated: rows keep their English keys
    if raw.get("type") == "error":
        raise ToolError("failed", "The data could not be read right now.", kpi=KPI_ID)
    rows = [r for r in raw.get("rows") or [] if r.get("factor")]
    ptxt, lang = ctx.period_text(req), ctx.lang
    nodata = {D.V_NODATA}
    usable = [r for r in rows if r.get("verdict") not in nodata or r.get("factor") != D.V_NODATA]
    if factor == "weather" and not any(_group(r) == "weather" for r in rows):
        raise ToolError("not_available", _NO_WEATHER.get(lang, _NO_WEATHER["en"]), kpi=KPI_ID, scope="weather")
    facts = []
    sel = [r for r in usable if factor == "any" or _group(r) == factor]
    def rank(r):
        v = r["verdict"]
        return 0 if v in (D.V_UP, D.V_DOWN) else 1 if v in (D.V_POS_UP, D.V_POS_DOWN) else 2
    sel.sort(key=rank)
    for r in sel:
        name, verdict = t(lang, r["factor"]), t(lang, r["verdict"])
        eff = _num(r.get("effect"))
        text = f"{name}: {verdict}"
        extra = {"group": _group(r), "verdict_key": r["verdict"], "clear": r["verdict"] in (D.V_UP, D.V_DOWN)}
        if eff is not None:
            text += f" ({fmt_arg({'v': eff, 'fmt': 'signed_pct'}, lang, ctx.currency)})"
        if r.get("when"):
            text += f", {r['when']}"
        if r.get("history"):
            extra["by_year"] = r["history"]
        facts.append(ctx.fact(name, eff, "pct" if eff is not None else None, ptxt, text=text, **extra))
    if not facts or not any(f.get("clear") or f.get("verdict_key") in (D.V_POS_UP, D.V_POS_DOWN) for f in facts):
        facts.insert(0, ctx.fact(_NOT_CLEAR.get(lang, _NOT_CLEAR["en"]), None, None, ptxt,
                                 text=_NOT_CLEAR.get(lang, _NOT_CLEAR["en"]), group="summary", clear=False))
    through = _data_through(ctx)
    if through:
        facts.append(ctx.fact(_UPTO.get(lang, _UPTO["en"]).format(d=fmt_date(through, lang)), None, None, "",
                              text=_UPTO.get(lang, _UPTO["en"]).format(d=fmt_date(through, lang)), group="freshness",
                              external_through=through))
    return {"ok": True, "tool": "external_context", "facts": facts, "tables": [], "charts": [],
            "meta": ctx.meta(req, k, factor=factor, external_through=through,
                             scope_note="Events listed are those inside the period; weather and economy verdicts use the whole history.")}


_CTX_LINE = {"en": "May be linked to outside factors that clearly move your sales: {items}",
             "vi": "Có thể liên quan đến các yếu tố bên ngoài tác động rõ rệt đến doanh số của bạn: {items}",
             "de": "Möglicherweise verbunden mit äußeren Faktoren, die Ihren Umsatz klar beeinflussen: {items}",
             "zh": "可能与明显影响您销售的外部因素有关：{items}"}


def clear_context_fact(ctx: ToolContext, start, end, max_items: int = 3) -> dict | None:
    """ONE fact for 'why did revenue change': only outside factors with a CLEAR effect (same rows as the dashboard
    summary), worded as a possibility. None when there is nothing clear, or when anything at all goes wrong - the
    explanation never fails because of external data."""
    try:
        k = ctx.catalog.by_id.get(KPI_ID)
        if k is None or not ctx.user.has_role(k.min_role):
            return None
        if not (ctx.st.get("availability") or {}).get(KPI_ID, {}).get("available"):
            return None
        req = ctx.period({"start": str(start), "end": str(end)}, "none")
        raw = ctx.dash.run_many(ctx.user, ctx.tc, [k], req, ctx.st)[KPI_ID]
        if raw.get("type") == "error":
            return None
        lang = ctx.lang
        items = []
        for r in raw.get("rows") or []:
            eff = _num(r.get("effect"))
            if r.get("verdict") in (D.V_UP, D.V_DOWN) and eff is not None:
                items.append(f"{t(lang, r['factor'])} ({fmt_arg({'v': eff, 'fmt': 'signed_pct'}, lang, ctx.currency)})")
        if not items:
            return None
        text = _CTX_LINE.get(lang, _CTX_LINE["en"]).format(items="; ".join(items[:max_items]))
        return ctx.fact("external_context", None, None, ctx.period_text(req), text=text, group="external_context")
    except Exception:           # noqa: BLE001
        return None
