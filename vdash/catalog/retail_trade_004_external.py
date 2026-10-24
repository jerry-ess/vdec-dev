"""Ask-only KPIs for the OUTSIDE factors (talk-to-data external-data plan, step E3).

One definition list (``SPEC``) makes, for every outside signal of the shared signal registry, a number for the period
(``ext_<x>``, with the change against an earlier period) and a trend (``ext_<x>_trend``). They read the tenant's own
view of the registry (``vw_tenant_external_signal``: the signals of the tenant's country and its own city only), so a
tenant never sees another market's values. When the view does not exist, or holds nothing for the tenant, the normal
availability probe marks them 'not available' and the chat says so. Which factor really moves a tenant's sales is NOT
decided here: that is the ``external_context`` tool (same verdicts as the dashboard's Market page).

To cover a new registry signal, add one line to ``SPEC`` (and its wording to i18n and config/ask_synonyms).
"""
from __future__ import annotations

from . import Kpi, Req, Series

VIEW = "vw_tenant_external_signal"

# id, English title, English description, registry signal_key, role filter, aggregation, format
SPEC = [
    ("ext_temperature", "Average highest daily temperature (°C)",
     "The average of the highest temperature of each day in your city over the dates you chose.",
     "TEMP_MAX", "weather", "AVG", "number"),
    ("ext_rainfall", "Rainfall (mm)",
     "The total rain that fell in your city over the dates you chose, in millimetres.",
     "RAIN_MM", "weather", "SUM", "number"),
    ("ext_fuel_price", "Fuel price",
     "The average fuel price in your country over the dates you chose.",
     "FUEL_PRICE", "", "AVG", "number"),
    ("ext_exchange_rate", "Exchange rate against the US dollar",
     "The average exchange rate against the US dollar over the dates you chose.",
     "FX_USD", "", "AVG", "number"),
    ("ext_food_prices", "Food price inflation, year on year",
     "How much food prices in your country are above the same time last year, on average over the dates you chose.",
     "FOOD_PRICE_YOY", "", "AVG", "pct"),
    ("ext_retail_market", "Retail market trend",
     "The average retail market trend of your country over the dates you chose.",
     "RETAIL_TREND", "", "AVG", "number"),
    ("ext_stock_market", "Stock market index",
     "The average level of your country's main stock market index over the dates you chose.",
     "STOCK_INDEX", "", "AVG", "number"),
]


def _where(signal: str, role: str) -> str:
    """The tenant's values of one signal: only its own rows, no forecasts, and ONE series (the one with the most
    observations), because a signal can have several series (e.g. two fuel types) that must not be averaged together."""
    r = f" AND x.role = '{role}'" if role else ""
    rv = f" AND v.role = '{role}'" if role else ""
    return (f"v.tenant_id = :tenant_id AND v.signal_key = '{signal}'{rv} AND v.status <> 'forecast' "
            f"AND v.obs_date BETWEEN :start AND :end AND v.series_id = (SELECT x.series_id FROM {VIEW} x "
            f"WHERE x.tenant_id = :tenant_id AND x.signal_key = '{signal}'{r} AND x.status <> 'forecast' "
            f"GROUP BY x.series_id ORDER BY COUNT(*) DESC, x.series_id LIMIT 1)")


def build() -> list[Kpi]:
    out = []
    for kid, title, desc, signal, role, agg, fmt in SPEC:
        req = [Req(VIEW, "value", 1, False, f"signal_key = '{signal}' AND status <> 'forecast'", "outside data for your market")]
        w = _where(signal, role)
        out.append(Kpi(kid, title, [], "metric", f"SELECT {agg}(v.value) AS v FROM {VIEW} v WHERE {w}", fmt=fmt, good=None,
                       desc=desc, requires=list(req), compare=True))
        out.append(Kpi(kid + "_trend", title + " over time", [], "timeseries",
                       f"SELECT MIN(d.full_date) AS k, {agg}(v.value) AS v FROM {VIEW} v "
                       f"JOIN dim_date d ON d.full_date = v.obs_date AND d.tenant_id = v.tenant_id WHERE {w} "
                       f"GROUP BY @BUCKET@ ORDER BY k",
                       fmt=fmt, good=None, desc=desc, series=[Series("v", title, "line", fmt)], requires=list(req)))
    return out
