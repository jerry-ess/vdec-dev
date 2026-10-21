"""What moves a tenant's sales? (v90) - a rule-based verdict on external factors, no language model.

For each factor (temperature, rain, holidays and festivals, inflation) this module asks one question: is there a clear,
repeatable relationship between the factor and daily revenue in the tenant's own history? The answer is one of

    clear     a real relationship (statistically solid AND big enough to matter) - the chart for it is shown
    none      enough data, no clear relationship - the chart is hidden, the verdict row says so
    no_data   the factor is not linked, or the history is too short to judge

Method (plain, explainable, same data -> same verdict):
  * Daily revenue is first made comparable across the year: each day is divided by the average of the days around it
    (+-14 days), so trend and slow seasonality do not look like a weather effect.
  * Spearman rank correlation (robust to a few huge days) between that relative revenue and the factor.
  * "clear" needs |rho| >= 0.15 (0.50 for the monthly inflation test) AND p < 0.01 (normal approximation).
    Both are required: with hundreds of days a tiny effect can be "significant" without being useful.
  * Effect size shown to the user: revenue on the days with most of the factor vs the days with least of it.
This is an association in the tenant's own data, not proof of cause.

Pure Python (no numpy/scipy). The SQL is portable (SQLite, PostgreSQL, ClickHouse).
"""
from __future__ import annotations

import math
import re
from bisect import bisect_left, bisect_right
from datetime import date, timedelta

MIN_DAYS = 60            # daily tests need at least this many days with both values
MIN_MONTHS = 24          # monthly (inflation) test
MONTH_MIN_DAYS = 10      # a month counts when it has at least this many trading days
RHO_DAILY = 0.15
RHO_MONTHLY = 0.50
P_MAX = 0.01
WINDOW = 14              # days each side for the local average
MIN_FLAG_DAYS = 5        # holidays: at least this many holiday days and 30 normal days
RAIN_MM = 1.0            # a rainy day has at least this much rain

def weather_day_sql(signal: str) -> str:
    """v98: one value per day of a weather signal for THIS tenant (its own city only), from the shared registry."""
    return ("(SELECT v.obs_date AS wd, AVG(v.value) AS wv FROM vw_tenant_external_signal v "
            f"WHERE v.tenant_id = :tenant_id AND v.signal_key = '{signal}' AND v.role = 'weather' "
            "AND v.status <> 'forecast' GROUP BY v.obs_date)")


def weather_req(signal: str, min_distinct: int = 1):
    from .catalog import Req
    return Req("vw_tenant_external_signal", "value", min_distinct, False, f"signal_key = '{signal}' AND status <> 'forecast'", "weather history for your city")


_DAILY_HEAD = ("SELECT d.full_date AS day, SUM(m.daily_revenue) AS rev, %s AS temp, %s AS rain, "
               "MAX(CASE WHEN d.is_holiday OR d.holiday_name IS NOT NULL THEN 1 ELSE 0 END) AS hol, MAX(d.holiday_name) AS hname "
               "FROM mart_external_factor_daily m JOIN dim_date d ON d.dim_date_sk = m.date_sk AND d.tenant_id = m.tenant_id ")
# v98: temperature and rain come from the shared weather signals of the tenant's own city; DAILY_SQL (the tenant's own
# weather columns) stays as the fallback for a warehouse that has no shared layer.
DAILY_SQL_REG = (_DAILY_HEAD % ("AVG(wt.wv)", "AVG(wr.wv)")
                 + "LEFT JOIN " + weather_day_sql("TEMP_MAX") + " wt ON wt.wd = d.full_date "
                 + "LEFT JOIN " + weather_day_sql("RAIN_MM") + " wr ON wr.wd = d.full_date "
                 + "WHERE m.tenant_id = :tenant_id GROUP BY d.full_date ORDER BY d.full_date")
DAILY_SQL = (_DAILY_HEAD % ("AVG(m.temp_high)", "AVG(m.precipitation_mm)")
             + "WHERE m.tenant_id = :tenant_id GROUP BY d.full_date ORDER BY d.full_date")
# v93: the shared external event calendar (Events sheet of the manual data entry, stored by Step 4.x as
# <COUNTRY>.CAL.EVENT.<type>.<region> in ext_series / ext_observation, one row per event day, text_value = event name).
# Only national events (geo = country) of the tenant's own country (dim_location.country, set from the market profile).
EXT_EVENTS_SQL = (
    "SELECT o.text_value AS name, MIN(o.obs_date) AS s, MAX(o.obs_date) AS e FROM ext_observation o "
    "JOIN ext_series x ON x.series_id = o.series_id "
    "WHERE SUBSTR(x.metric, 1, 6) = 'EVENT_' AND x.geo = x.country AND o.text_value IS NOT NULL "
    "AND x.country = (SELECT MAX(l.country) FROM dim_location l WHERE l.tenant_id = :tenant_id) "
    "AND 'x' <> :tenant_id GROUP BY o.text_value ORDER BY MIN(o.obs_date)"
)
EVENTS_SQL = ("SELECT e.event_name AS name, e.start_date AS s, e.end_date AS e FROM dim_local_event_calendar e "
              "WHERE e.tenant_id = :tenant_id ORDER BY e.start_date")
MONTHLY_SQL = (
    "SELECT d.year_month AS ym, SUM(m.daily_revenue) * 1.0 / COUNT(DISTINCT d.full_date) AS rev, "
    "AVG(e.inflation_rate) AS infl, COUNT(DISTINCT d.full_date) AS days_n "
    "FROM mart_external_factor_daily m JOIN dim_date d ON d.dim_date_sk = m.date_sk AND d.tenant_id = m.tenant_id "
    "JOIN fact_economic_indicator e ON e.date_sk = m.date_sk AND e.tenant_id = m.tenant_id "
    "WHERE m.tenant_id = :tenant_id AND e.inflation_rate IS NOT NULL GROUP BY d.year_month ORDER BY d.year_month"
)

# fixed English vocabulary - translated by the page like every catalog text
FACTORS = [  # key, label, comparison wording
    ("temperature", "Temperature", "Hottest vs coolest fifth of days"),
    ("rain", "Rain", "Rainy days vs dry days"),
    ("holiday", "Holidays and festivals", "Holiday and festival days vs normal days"),
    ("economy", "Inflation", "Highest vs lowest third of months"),
]
V_CLEAR, V_NONE, V_NODATA = "Clear effect", "No noticeable effect on sales", "Not enough history yet"
V_UP, V_DOWN = "Sales reliably higher", "Sales reliably lower"   # the direction is part of the answer
V_NOPAT = "No repeating pattern"   # v95e: in-range events whose effect differs from year to year
V_POS_UP, V_POS_DOWN = "Sales probably higher", "Sales probably lower"   # v94: same direction every year, but size or strength not solid
R_YES, R_PROB, R_VARIES, R_ONCE = "Yes, every year", "Most years", "No, it varies", "Only seen once"   # v94: does the event repeat its effect?
# v96: market factors from the SHARED external tables (ext_series / ext_observation), same for every tenant of a country.
# (key, label, metric names to try - the one with the longest history is used; VN first, DE second)
MARKET_FACTORS = [
    ("fuel", "Fuel prices", ("RON95", "E10")),
    ("fx", "Exchange rate", ("USD_SELL", "EUR_USD")),
    ("food", "Food prices", ("CPI_FOOD_YOY", "HICP_FOOD_YOY")),
    ("retail", "Retail market trend", ("RETAIL_SALES_YOY", "RETAIL_VOLUME_IDX")),
    ("stock", "Stock market", ("VNINDEX_CLOSE", "DAX_CLOSE")),
]
FACTORS += [(k, lab, "Highest vs lowest third of months") for k, lab, _ in MARKET_FACTORS]
_METRICS_SQL = ", ".join("'%s'" % m for _, _, ms in MARKET_FACTORS for m in ms)
MONTHLY_REV_SQL = (
    "SELECT d.year_month AS ym, SUM(m.daily_revenue) * 1.0 / COUNT(DISTINCT d.full_date) AS rev, "
    "COUNT(DISTINCT d.full_date) AS days_n "
    "FROM mart_external_factor_daily m JOIN dim_date d ON d.dim_date_sk = m.date_sk AND d.tenant_id = m.tenant_id "
    "WHERE m.tenant_id = :tenant_id GROUP BY d.year_month ORDER BY d.year_month"
)
EXT_MONTHLY_SQL = (
    "SELECT x.metric AS metric, SUBSTR(CAST(o.obs_date AS TEXT), 1, 7) AS ym, AVG(o.value) AS v "
    "FROM ext_observation o JOIN ext_series x ON x.series_id = o.series_id "
    "WHERE x.country = (SELECT MAX(l.country) FROM dim_location l WHERE l.tenant_id = :tenant_id) "
    "AND x.metric IN (" + _METRICS_SQL + ") AND o.value IS NOT NULL AND o.status <> 'forecast' AND 'x' <> :tenant_id "
    "GROUP BY x.metric, SUBSTR(CAST(o.obs_date AS TEXT), 1, 7) ORDER BY 1, 2"
)
# v97: the SIGNAL REGISTRY (ext_signal_map, tenant views, Step 5 verdicts) replaces the fixed lists above when it is there.
# Every query is optional: when the registry objects are missing or empty the v96 lists keep working unchanged.
REG_SIGNALS_SQL = (   # demand-driver signals defined for the tenant's country (the tenant's market profile decides the country)
    "SELECT m.signal_key AS signal_key, MIN(m.label) AS label FROM ext_signal_map m "
    "JOIN tenant_market_profile p ON p.country = m.country "
    "WHERE p.tenant_id = :tenant_id AND m.enabled AND m.role = 'demand_driver' GROUP BY m.signal_key ORDER BY m.signal_key"
)
REG_MONTHLY_SQL = (   # monthly values per signal and series (the consumer picks the series with the best overlap)
    "SELECT v.signal_key AS metric, v.series_id AS sid, SUBSTR(CAST(v.obs_date AS TEXT), 1, 7) AS ym, AVG(v.value) AS v "
    "FROM vw_tenant_external_signal v "
    "WHERE v.tenant_id = :tenant_id AND v.role = 'demand_driver' AND v.status <> 'forecast' "
    "GROUP BY v.signal_key, v.series_id, SUBSTR(CAST(v.obs_date AS TEXT), 1, 7) ORDER BY 1, 2, 3"
)
REG_STATUS_SQL = (    # what Step 5 concluded per signal for this tenant (saved verdicts)
    "SELECT s.signal_key AS signal_key, s.status AS status, s.n_obs AS n_obs FROM tenant_external_factor_status s "
    "WHERE s.tenant_id = :tenant_id"
)
REG_EVENTS_SQL = (    # the tenant's calendar view: national days plus its own region or city (same shape as EXT_EVENTS_SQL)
    "SELECT c.name AS name, MIN(c.day) AS s, MAX(c.day) AS e FROM vw_tenant_calendar_day c "
    "WHERE c.tenant_id = :tenant_id AND c.kind = 'event' AND c.name <> '' GROUP BY c.name ORDER BY MIN(c.day)"
)
LEGACY_SIGNAL_KEY = {"FUEL_PRICE": "fuel", "FX_USD": "fx", "FOOD_PRICE_YOY": "food", "RETAIL_TREND": "retail",
                     "STOCK_INDEX": "stock"}
_PLAIN = {k: lab for k, lab, _ in MARKET_FACTORS}


def registry_factors(sig_rows):
    """[(key, plain label, (signal_key,))] from the registry. Known signals keep their old key (so charts and wording
    stay the same); a signal that is new gets its own key and appears in the summary without any code change."""
    out = []
    for r in sig_rows:
        sk = str(r.get("signal_key") or "")
        if not sk or sk in ("TEMP_MAX", "RAIN_MM"):      # weather is handled from the tenant's own weather table
            continue
        key = LEGACY_SIGNAL_KEY.get(sk, sk.lower())
        label = _PLAIN.get(key) or str(r.get("label") or sk).replace("_", " ").capitalize()
        out.append((key, label, (sk,)))
    return out


# wording of a CLEAR effect: the direction says what happens when the factor is HIGH
CLEAR_LABEL = {"temperature": "Hotter days", "rain": "Rainy days", "holiday": "Holidays and festivals",
               "economy": "Higher inflation", "fuel": "Higher fuel prices", "fx": "Higher exchange rate",
               "food": "Higher food prices", "retail": "Growing retail market", "stock": "Higher stock market"}
S_WEATHER, S_ECON, S_WEATHER_ECON = "Weather", "Prices and economy", "Weather, prices and economy"
S_ALL3 = "Temperature, rain and inflation"
S_ALL4 = "Temperature, rain, holidays and inflation"
S_OTHER = "Other factors tested"
LIKELY_MIN = 10.0        # v94: 'possible' effect = same direction every year and at least this many percent on average


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def _ranks(xs):
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    r = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1
        for k in range(i, j + 1):
            r[order[k]] = avg
        i = j + 1
    return r


def spearman(xs, ys):
    """(rho, p) - p from the normal approximation, adequate for n >= 24; (0, 1) when undefined."""
    n = len(xs)
    if n < 3:
        return 0.0, 1.0
    rx, ry = _ranks(xs), _ranks(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    sxx = sum((a - mx) ** 2 for a in rx)
    syy = sum((b - my) ** 2 for b in ry)
    if sxx == 0 or syy == 0:
        return 0.0, 1.0
    rho = sum((a - mx) * (b - my) for a, b in zip(rx, ry)) / math.sqrt(sxx * syy)
    z = abs(rho) * math.sqrt(n - 1)
    return rho, math.erfc(z / math.sqrt(2))


def relative_revenue(rev, window=None):
    """Each value / the mean of the values within +-window positions (None when that mean is not positive or the value
    is missing). Positions, not calendar days: a tenant's calendar only holds days with sales."""
    window = WINDOW if window is None else window
    n = len(rev)
    out = [None] * n
    for i in range(n):
        if rev[i] is None:
            continue
        w = [v for v in rev[max(0, i - window): i + window + 1] if v is not None]
        m = sum(w) / len(w) if w else 0
        if m > 0:
            out[i] = rev[i] / m
    return out


def _mean(v):
    return sum(v) / len(v) if v else None


def _diff_pct(hi, lo):
    a, b = _mean(hi), _mean(lo)
    if a is None or b is None or b == 0:
        return None
    return (a / b - 1.0) * 100.0


def _verdict(rho, p, n, min_rho):
    return "clear" if (abs(rho) >= min_rho and p < P_MAX) else "none"


def analyze_daily(rows):
    """rows: dicts with rev, temp, rain, hol in date order. Returns {factor: result dict}."""
    rev = [_num(r.get("rev")) for r in rows]
    rel = relative_revenue(rev)
    res = {}

    def pairs(vals):
        return [(v, r) for v, r in zip(vals, rel) if v is not None and r is not None]

    # temperature: top vs bottom fifth
    pr = pairs([_num(r.get("temp")) for r in rows])
    if len(pr) >= MIN_DAYS and len({v for v, _ in pr}) > 3:
        rho, p = spearman([v for v, _ in pr], [r for _, r in pr])
        srt = sorted(pr, key=lambda t: t[0])
        k = max(5, len(srt) // 5)
        res["temperature"] = {"status": _verdict(rho, p, len(pr), RHO_DAILY), "rho": rho, "p": p, "n": len(pr),
                              "effect": _diff_pct([r for _, r in srt[-k:]], [r for _, r in srt[:k]])}
    # rain: rainy vs dry days
    pr = pairs([_num(r.get("rain")) for r in rows])
    rainy = [r for v, r in pr if v >= RAIN_MM]
    dry = [r for v, r in pr if v < RAIN_MM]
    if len(pr) >= MIN_DAYS and len(rainy) >= 10 and len(dry) >= 10:
        rho, p = spearman([v for v, _ in pr], [r for _, r in pr])
        res["rain"] = {"status": _verdict(rho, p, len(pr), RHO_DAILY), "rho": rho, "p": p, "n": len(pr),
                       "effect": _diff_pct(rainy, dry)}
    # holidays and festivals
    pr = pairs([_num(r.get("hol")) for r in rows])
    hol = [r for v, r in pr if v >= 1]
    norm = [r for v, r in pr if v < 1]
    if len(pr) >= MIN_DAYS and len(hol) >= MIN_FLAG_DAYS and len(norm) >= 30:
        rho, p = spearman([v for v, _ in pr], [r for _, r in pr])
        res["holiday"] = {"status": _verdict(rho, p, len(pr), RHO_DAILY), "rho": rho, "p": p, "n": len(pr),
                          "effect": _diff_pct(hol, norm), "flag_days": len(hol)}
    return res


FEST_MIN_YEARS = 2       # a festival must have been seen in at least this many different years
FEST_MIN_EFFECT = 25.0   # and move revenue by at least this many percent, the same way every time
FEST_LEADS = (0, 7, 14, 21)   # shopping starts this many days before the festival: each lead-up is tried
FEST_AFTER = 2           # days after the festival that still count
FEST_BASE = 45           # the weeks around the period (before and after) that form the comparison
FEST_CMP = "Festival period (with lead-up) vs the weeks around it"


def _d(x):
    return x if isinstance(x, date) else date.fromisoformat(str(x)[:10])


def analyze_festivals(rows):
    """Each named holiday / festival as a PERIOD, not a single day: people shop in the days before it. For every festival
    the lead-up (0, 7, 14 or 21 days before) that shows the strongest effect is chosen, then the festival period's
    average daily revenue is compared with the 45 days before and after it. A festival is 'clear' when it was seen in
    >= 2 different years, the effect had the same direction (and > 10%) EVERY year, and the average is >= 25%.
    One lucky day, or a festival that sold well one year and badly the next, is not a pattern.
    Returns [{name, effect, lead, years, windows: [(start, end, days_n, effect), ...]}] strongest first."""
    by_date = {}
    names = {}
    for r in rows:
        v = _num(r.get("rev"))
        if v is None:
            continue
        d = _d(r.get("day"))
        by_date[d] = v
        if r.get("hname"):
            names.setdefault(str(r["hname"]), {}).setdefault(d.year, []).append(d)
    ds = sorted(by_date)
    vals = [by_date[d] for d in ds]

    def in_range(a, b):
        return vals[bisect_left(ds, a): bisect_right(ds, b)]

    found = []
    for name, years in names.items():
        if len(years) < FEST_MIN_YEARS:
            continue
        best = None
        for lead in FEST_LEADS:
            effs, wins = [], []
            for _y, dl in sorted(years.items()):
                ws, we = min(dl) - timedelta(days=lead), max(dl) + timedelta(days=FEST_AFTER)
                win = in_range(ws, we)
                base = in_range(ws - timedelta(days=FEST_BASE), ws - timedelta(days=1)) + \
                    in_range(we + timedelta(days=1), we + timedelta(days=FEST_BASE))
                if len(win) < (3 if lead else 1) or len(base) < 10 or sum(base) <= 0:
                    break
                e = (sum(win) / len(win)) / (sum(base) / len(base)) - 1.0
                effs.append(e)
                wins.append((ws, we, len(win), e * 100.0))
            else:
                if len(effs) >= FEST_MIN_YEARS:
                    mean = sum(effs) / len(effs) * 100.0
                    same = all(e > 0.10 for e in effs) or all(e < -0.10 for e in effs)
                    if same and abs(mean) >= FEST_MIN_EFFECT and (best is None or abs(mean) > abs(best["effect"])):
                        best = {"name": name, "effect": mean, "lead": lead, "years": len(effs), "windows": wins}
        if best:
            found.append(best)
    found.sort(key=lambda f: -abs(f["effect"]))
    return found


EVENT_BASE = 60          # normal days (outside every event) this far before and after an event form its comparison
EVENT_MIN_BASE = 10      # at least this many normal trading days are needed
EVENT_CMP = "Event period vs normal days around it"
EVENT_MIN_DAYS = 3       # one-day events are too noisy to call a pattern
EVENT_PARTIAL_DAYS = 14  # an event still running at the end of the data counts once it has this many trading days


def _merge(intervals):
    out = []
    for a, b in sorted(intervals):
        if out and a <= out[-1][1] + timedelta(days=1):
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


_YEAR = re.compile(r"\s*\b(19|20)\d{2}\b")
_PHASE = re.compile(r"\s*\bPhase\s*\d+\b", re.I)
_TET = re.compile(r"^Tet\b.*Shopping Peak", re.I)
SINGLE_DAY_LEAD = 7      # a one-day event is judged from a week before it (shopping starts before the day itself)


def event_display(name):
    """Event name without the year ('Mid-Autumn Festival (Tet Trung Thu) 2025' -> without '2025')."""
    return _YEAR.sub("", str(name)).strip()


def event_key(name):
    """Groups the same event across years: no year, no 'Phase n', and the yearly Tet names (zodiac) become one."""
    n = _PHASE.sub("", event_display(name)).strip()
    return "Tet Shopping Peak" if _TET.match(n) else n


def event_window(a, b):
    """One-day events start a week earlier; longer events keep the calendar's own dates."""
    return (a - timedelta(days=SINGLE_DAY_LEAD), b) if a == b else (a, b)


def event_occurrences(daily, events):
    """The tenant's own event calendar (the SAME periods the event table shows, lead-up included). Per occurrence:
    average daily revenue during the event vs the average on normal days (outside EVERY event) within 60 days before
    and after. Comparing with nearby days, not the whole history, keeps growth and the season out of the result."""
    by_date = {}
    for r in daily:
        v = _num(r.get("rev"))
        if v is not None:
            by_date[_d(r.get("day"))] = v
    if not by_date:
        return []
    ds = sorted(by_date)
    vals = [by_date[d] for d in ds]
    last = ds[-1]
    evs = []
    for e in events:
        try:
            a0, b0 = _d(e.get("s")), _d(e.get("e"))
        except (TypeError, ValueError):
            continue
        a0, b0 = event_window(a0, b0)
        evs.append((str(e.get("name")), a0, b0))
    merged = _merge([(a, b) for _, a, b in evs])
    starts = [m[0] for m in merged]

    def in_event(d):
        i = bisect_right(starts, d) - 1
        return i >= 0 and merged[i][0] <= d <= merged[i][1]

    def normal(a, b):
        i, j = bisect_left(ds, a), bisect_right(ds, b)
        return [vals[k] for k in range(i, j) if not in_event(ds[k])]

    out = []
    for name, a, b in evs:
        i, j = bisect_left(ds, a), bisect_right(ds, b)
        win = vals[i:j]
        if not win or a > last:
            continue
        base = normal(a - timedelta(days=EVENT_BASE), a - timedelta(days=1)) + \
            normal(b + timedelta(days=1), b + timedelta(days=EVENT_BASE))
        eff = None
        if len(base) >= EVENT_MIN_BASE and sum(base) > 0:
            eff = ((sum(win) / len(win)) / (sum(base) / len(base)) - 1.0) * 100.0
        out.append({"name": event_display(name), "key": event_key(name), "s": a, "e": b, "days_n": len(win), "rev": sum(win), "effect": eff,
                    "usable": eff is not None and len(win) >= EVENT_MIN_DAYS and (b <= last or len(win) >= EVENT_PARTIAL_DAYS)})
    return out


def clear_events(occ):
    """Events whose period moved revenue the same way (> 10%) in EVERY year they were observed, by >= 25% on average.
    Same shape as analyze_festivals: [{name, effect, years, windows: [(start, end, days_n, effect%)]}]."""
    byname = {}
    for o in occ:
        if o["usable"]:
            byname.setdefault(o.get("key") or o["name"], []).append(o)
    found = []
    for name, lst in byname.items():
        if len(lst) < FEST_MIN_YEARS or len({o["s"].year for o in lst}) < FEST_MIN_YEARS:
            continue
        effs = [o["effect"] for o in lst]
        same = all(e > 10 for e in effs) or all(e < -10 for e in effs)
        mean = sum(effs) / len(effs)
        if same and abs(mean) >= FEST_MIN_EFFECT:
            found.append({"name": lst[-1]["name"], "effect": mean, "lead": None, "years": len(lst),
                          "windows": [(o["s"], o["e"], o["days_n"], o["effect"]) for o in lst]})
    found.sort(key=lambda f: -abs(f["effect"]))
    return found


def event_stats(occ):
    """v94: per event (all years together): does it repeat its effect?
    clear   same direction > 10% every year, mean >= 25%        (reliable: plan around it)
    likely  same direction every year, mean >= 10%               (a tendency, size varies)
    varies  the years disagree                                   (no pattern)
    once    seen in fewer than 2 different years                 (can't tell)"""
    by = {}
    for o in occ:
        if o["usable"]:
            by.setdefault(o.get("key") or o["name"], []).append(o)
    out = {}
    for key, lst in by.items():
        lst.sort(key=lambda o: o["s"])
        effs = [o["effect"] for o in lst]
        mean = sum(effs) / len(effs)
        if len(lst) < FEST_MIN_YEARS or len({o["s"].year for o in lst}) < FEST_MIN_YEARS:
            cls = "once"
        elif (all(e > 10 for e in effs) or all(e < -10 for e in effs)) and abs(mean) >= FEST_MIN_EFFECT:
            cls = "clear"
        elif (all(e > 0 for e in effs) or all(e < 0 for e in effs)) and abs(mean) >= LIKELY_MIN:
            cls = "likely"
        else:
            cls = "varies"
        out[key] = {"cls": cls, "name": lst[-1]["name"], "mean": mean, "occ": lst,
                    "hist": ", ".join(f"{o['s'].year}: {o['effect']:+.0f}%" for o in lst)}
    return out


_REPEAT = {"clear": R_YES, "likely": R_PROB, "varies": R_VARIES, "once": R_ONCE}


def _overlaps(o, a, b):
    return (a is None or o["e"] >= a) and (b is None or o["s"] <= b)


def event_table_rows(res, start=None, end=None):
    """Rows of the event table - every calendar event that overlaps the selected period, with how it did in each year
    and whether it repeats its effect (v94)."""
    a = _d(start) if start else None
    b = _d(end) if end else None
    stats = res.get("event_stats") or {}
    rows = []
    for o in res.get("event_occ") or []:
        if not _overlaps(o, a, b) or o["effect"] is None:
            continue
        st = stats.get(o.get("key") or o["name"])
        rows.append({"event": o["name"], "start_d": o["s"].isoformat(), "end_d": o["e"].isoformat(),
                     "days_n": o["days_n"], "rev": o["rev"], "uplift": round(o["effect"], 1),
                     "history": st["hist"] if st else "", "repeats": _REPEAT[st["cls"]] if st else V_NODATA})
    rows.sort(key=lambda r: (-r["uplift"], r["event"]))
    return rows


def analyze_monthly(rows):
    """Inflation vs revenue per month. Revenue per trading day is made relative to the +-3 month average first, because
    a growing business and a rising price level would otherwise correlate for no real reason."""
    rows = [r for r in rows if (_num(r.get("days_n")) or 0) >= MONTH_MIN_DAYS]
    rel = relative_revenue([_num(r.get("rev")) for r in rows], window=3)
    pr = [(_num(r.get("infl")), x) for r, x in zip(rows, rel)]
    pr = [(a, b) for a, b in pr if a is not None and b is not None]
    if len(pr) < MIN_MONTHS or len({a for a, _ in pr}) < 4:
        return {}
    rho, p = spearman([a for a, _ in pr], [b for _, b in pr])
    srt = sorted(pr, key=lambda t: t[0])
    k = max(3, len(srt) // 3)
    return {"economy": {"status": _verdict(rho, p, len(pr), RHO_MONTHLY), "rho": rho, "p": p, "n": len(pr),
                        "effect": _diff_pct([b for _, b in srt[-k:]], [b for _, b in srt[:k]])}}


def _ym(v):
    d = re.sub(r"\D", "", str(v))[:6]
    return d if len(d) == 6 else None


def analyze_market(rev_rows, ext_rows, factors=None):
    """v96: fuel, exchange rate, food prices, retail trend, stock index vs monthly revenue - same strict rule as the
    inflation test (>= 24 months, |rho| >= 0.50, p < 0.01). Revenue per trading day is made relative to the +-3 month
    average, and the factor is taken as its deviation from its own +-3 month average, so two trending lines do not look
    related. Series that are shorter than 24 months of overlap come out as 'not enough data' (no result)."""
    rows = [r for r in rev_rows if (_num(r.get("days_n")) or 0) >= MONTH_MIN_DAYS and _ym(r.get("ym"))]
    if len(rows) < MIN_MONTHS:
        return {}
    months = [_ym(r["ym"]) for r in rows]
    rel = relative_revenue([_num(r.get("rev")) for r in rows], window=3)
    by_metric = {}
    for r in ext_rows:
        v, m = _num(r.get("v")), _ym(r.get("ym"))
        if v is not None and m:     # v97: registry rows carry the series id, so one signal can have several series
            sid = r.get("sid")
            by_metric.setdefault((r.get("metric"), "" if sid is None else str(sid)), {})[m] = v
    out = {}
    for key, _label, metrics in (factors or MARKET_FACTORS):
        cand = [d for (name, _sid), d in by_metric.items() if name in metrics]
        if not cand:
            continue
        series = max(cand, key=lambda d: sum(1 for m in months if m in d))
        vals = [series.get(m) for m in months]
        dev = [None] * len(vals)
        for i, v in enumerate(vals):
            if v is None:
                continue
            w = [x for x in vals[max(0, i - 3): i + 4] if x is not None]
            dev[i] = v - sum(w) / len(w)
        pr = [(a, b) for a, b in zip(dev, rel) if a is not None and b is not None]
        if len(pr) < MIN_MONTHS or len({round(a, 6) for a, _ in pr}) < 4:
            continue
        rho, p = spearman([a for a, _ in pr], [b for _, b in pr])
        srt = sorted(pr, key=lambda t: t[0])
        k = max(3, len(srt) // 3)
        out[key] = {"status": _verdict(rho, p, len(pr), RHO_MONTHLY), "rho": rho, "p": p, "n": len(pr),
                    "effect": _diff_pct([b for _, b in srt[-k:]], [b for _, b in srt[:k]])}
    return out


def compute(query) -> dict:
    """query(sql) -> list of dict rows (tenant already bound). Returns {factor: result}; missing = no_data.
    A failed query only removes that part (the verdict says 'not enough data')."""
    out = {}
    try:
        daily = None
        try:      # v98: weather from the registry; when it has none for this tenant, the tenant's own weather columns
            daily = query(DAILY_SQL_REG)
            if not any(_num(r.get("temp")) is not None or _num(r.get("rain")) is not None for r in daily):
                daily = None
        except Exception:
            daily = None
        if daily is None:
            daily = query(DAILY_SQL)
        out.update(analyze_daily(daily))
        events = []
        for sql in (REG_EVENTS_SQL, EXT_EVENTS_SQL, EVENTS_SQL):   # v97: tenant calendar view, then the v93 shared series, then the tenant's own calendar
            try:
                events = query(sql)
            except Exception:
                events = []
            if events:
                break
        if events:    # ONE source of event periods for the summary AND the event table
            occ = event_occurrences(daily, events)
            out["event_occ"] = occ
            out["festivals"] = clear_events(occ)
            out["event_stats"] = event_stats(occ)
        else:         # no calendar: fall back to the named holidays / festivals in the calendar table
            out["festivals"] = analyze_festivals(daily)
    except Exception:
        pass
    try:
        out.update(analyze_monthly(query(MONTHLY_SQL)))
    except Exception:
        pass
    try:      # v96: shared external market series (no result when the tables or the series are missing)
        factors, ext_rows, reg = None, None, None
        try:      # v97: registry first - which signals exist for this tenant's country, and the monthly values
            sigs = query(REG_SIGNALS_SQL)
            if sigs:
                factors = registry_factors(sigs)
                ext_rows = query(REG_MONTHLY_SQL)
                reg = {"factors": [(k, lab) for k, lab, _ in factors], "status": {},
                       "clear_label": {LEGACY_SIGNAL_KEY.get(str(r.get("signal_key")), str(r.get("signal_key")).lower()): r.get("label")
                                       for r in sigs}}
                try:
                    reg["status"] = {r["signal_key"]: r for r in query(REG_STATUS_SQL)}
                except Exception:
                    pass
        except Exception:
            factors, ext_rows, reg = None, None, None
        if not ext_rows:      # registry missing or empty: the v96 fixed lists
            factors, reg, ext_rows = None, None, query(EXT_MONTHLY_SQL)
        res = analyze_market(query(MONTHLY_REV_SQL), ext_rows, factors)
        out.update(res)
        if reg:
            reg["no_result"] = [lab for k, lab in reg["factors"] if k not in res
                                and _short_history(reg["status"].get({v: k2 for k2, v in LEGACY_SIGNAL_KEY.items()}.get(k, k.upper())))]
            out["_registry"] = reg
    except Exception:
        pass
    return out


def _short_history(st):
    """True when Step 5 looked at this signal for the tenant, found some values, and could not use it yet."""
    return bool(st) and st.get("status") in ("dropped", "candidate") and (_num(st.get("n_obs")) or 0) > 0


def _verdict_text(effect):
    return V_UP if (effect or 0) >= 0 else V_DOWN


def table_rows(results: dict, start=None, end=None) -> list[dict]:
    """The summary (v94) - only what a business owner can use:
      1. events that clearly repeat their effect every year (those inside the selected dates first),
      2. events that tend to (same direction every year, size varies),
      3. any weather / economy factor with a clear effect,
      4. ONE line for everything tested that has no clear effect.
    The numbers are the event table's own (same periods, same comparison)."""
    a = _d(start) if start else None
    b = _d(end) if end else None
    rows = []

    def add(name, verdict, eff, hist, when):
        rows.append({"factor": name, "verdict": verdict, "effect": None if eff is None else round(eff, 1),
                     "history": hist, "when": when})

    def span(o):
        return f"{o['s'].isoformat()} - {o['e'].isoformat()}"

    stats = results.get("event_stats")
    if stats is not None:
        for cls, cap in (("clear", 8), ("likely", 5)):
            items = []
            for st in stats.values():
                if st["cls"] != cls:
                    continue
                hit = [o for o in st["occ"] if _overlaps(o, a, b)]
                if not hit:      # v95c: ONLY events inside the selected dates (same events as the event table)
                    continue
                items.append((-abs(hit[-1]["effect"]), st["name"], st, hit))
            items.sort(key=lambda t: t[:2])
            for _, _, st, hit in items[:cap]:
                up = st["mean"] >= 0     # reliability / direction from the history of every year
                verdict = (V_UP if up else V_DOWN) if cls == "clear" else (V_POS_UP if up else V_POS_DOWN)
                # the number is the selected period's own figure - the same one the event table shows
                add(st["name"], verdict, hit[-1]["effect"], st["hist"], span(hit[-1]))
        # v95e: every other event inside the selected dates (the event table lists them) is accounted for in ONE line
        others = sorted({st["name"] for st in stats.values() if st["cls"] in ("varies", "once")
                         and any(_overlaps(x, a, b) for x in st["occ"])})
        if others:
            add(", ".join(others[:6]) + (f" (+{len(others) - 6})" if len(others) > 6 else ""), V_NOPAT, None, None, None)
    else:   # no event calendar: the festivals found from the holiday names in the calendar table
        for f in results.get("festivals") or []:
            hit = [w for w in f["windows"] if (a is None or w[1] >= a) and (b is None or w[0] <= b)]
            if not hit:
                continue
            add(f["name"], _verdict_text(f["effect"]), hit[-1][3],
                ", ".join(f"{w[0].year}: {w[3]:+.0f}%" for w in f["windows"]),
                f"{hit[-1][0].isoformat()} - {hit[-1][1].isoformat()}")
    none_keys = []
    reg = results.get("_registry") or {}
    known = {k for k, _l, _c in FACTORS}
    extra = [(k, lab, "Highest vs lowest third of months") for k, lab in reg.get("factors", []) if k not in known]
    for key, label, _ in FACTORS + extra:
        if key == "holiday" and stats is not None:    # the events above already answer the holiday question
            continue
        r = results.get(key)
        if r and r["status"] == "clear":
            eff = None if r.get("effect") is None else r["effect"]
            add(CLEAR_LABEL.get(key) or (reg.get("clear_label") or {}).get(key) or label, _verdict_text(eff), eff, None, None)
        elif r:
            none_keys.append(key)
    if none_keys:
        ks = set(none_keys) - {"holiday"}
        w = bool(ks & {"temperature", "rain"})
        e = bool(ks - {"temperature", "rain"})
        name = S_WEATHER_ECON if (w and e) else S_WEATHER if w else S_ECON if e else S_OTHER
        add(name, V_NONE, None, None, None)
    if reg.get("no_result"):     # v97: Step 5 saw values but too few months to judge - say so in ONE plain line
        names = reg["no_result"]
        add(", ".join(names[:6]) + (f" (+{len(names) - 6})" if len(names) > 6 else ""), V_NODATA, None, None, None)
    if not rows:
        add(V_NODATA, V_NODATA, None, None, None)
    return rows


def is_clear(results: dict, key: str) -> tuple[bool, str]:
    if key == "events":    # the event table is shown whenever the tenant has any events with sales around them
        return (True, "") if results.get("event_occ") else (False, "no event calendar for this tenant's country")
    r = results.get(key)
    if not r:
        return False, "not enough data to judge a relationship with sales"
    if r["status"] != "clear":
        return False, "no clear relationship with sales in this tenant's history"
    return True, ""
