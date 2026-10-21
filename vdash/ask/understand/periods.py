"""Period resolver (talk-to-data Step 3): turns the time words of a question into dates. Pure rules, no AI.

Why rules: "last month" must mean the same calendar month every time, relative to the tenant's latest loaded day
(the *anchor*, never the wall clock), in English, Vietnamese (with or without accents) and German. The planner is
given these dates and may not change them; a plan that disagrees is overridden (see planner.apply_periods).

    parse_periods("Số đơn hàng tháng 9 so với tháng 8", anchor=date(2026, 10, 3))
    -> PeriodParse(periods=[Sep 2026, Aug 2026], compare=None)

Template-level: nothing here knows a tenant or an industry.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, timedelta


def fold(s: str) -> str:
    """lower-case, accents removed (Vietnamese đ, German ß/umlauts too) so one pattern serves 'tháng' and 'thang'."""
    s = (s or "").replace("đ", "d").replace("Đ", "D").replace("ß", "ss")
    s = unicodedata.normalize("NFD", s)
    return "".join(c for c in s if unicodedata.category(c) != "Mn").lower()


MONTHS = {
    1: ["january", "jan", "januar"], 2: ["february", "feb", "februar"], 3: ["march", "mar", "marz", "maerz"],
    4: ["april", "apr"], 5: ["may", "mai"], 6: ["june", "jun", "juni"], 7: ["july", "jul", "juli"],
    8: ["august", "aug"], 9: ["september", "sep", "sept"], 10: ["october", "oct", "oktober", "okt"],
    11: ["november", "nov"], 12: ["december", "dec", "dezember", "dez"],
}
_MONTH_BY_NAME = {n: m for m, names in MONTHS.items() for n in names}
_MONTH_WORDS = "|".join(sorted(_MONTH_BY_NAME, key=len, reverse=True))

UNIT = {"day": "day", "days": "day", "ngay": "day", "tag": "day", "tage": "day", "tagen": "day",
        "week": "week", "weeks": "week", "tuan": "week", "woche": "week", "wochen": "week",
        "month": "month", "months": "month", "thang": "month", "monat": "month", "monate": "month", "monaten": "month",
        "quarter": "quarter", "quarters": "quarter", "quy": "quarter", "quartal": "quarter", "quartale": "quarter",
        "year": "year", "years": "year", "nam": "year", "jahr": "year", "jahre": "year", "jahren": "year"}
_UNIT_WORDS = "|".join(sorted(UNIT, key=len, reverse=True))


# ----------------------------------------------------------------------------------------------- date helpers
def month_range(y: int, m: int) -> tuple[date, date]:
    start = date(y, m, 1)
    nxt = date(y + (m == 12), m % 12 + 1, 1)
    return start, nxt - timedelta(days=1)


def add_months(y: int, m: int, n: int) -> tuple[int, int]:
    i = y * 12 + (m - 1) + n
    return i // 12, i % 12 + 1


def quarter_range(y: int, q: int) -> tuple[date, date]:
    s, _ = month_range(y, 3 * (q - 1) + 1)
    _, e = month_range(y, 3 * (q - 1) + 3)
    return s, e


def shift_range(start: date, end: date, unit: str, n: int = 1) -> tuple[date, date]:
    """The same kind of period ``n`` units earlier. Calendar units keep calendar alignment (a month stays a month)."""
    if unit == "day":
        return start - timedelta(days=n), end - timedelta(days=n)
    if unit == "week":
        return start - timedelta(days=7 * n), end - timedelta(days=7 * n)
    step = {"month": 1, "quarter": 3, "year": 12}[unit] * n
    if start.day == 1 and (end + timedelta(days=1)).day == 1:          # whole calendar months
        y, m = add_months(start.year, start.month, -step)
        ye, me = add_months(end.year, end.month, -step)
        return date(y, m, 1), month_range(ye, me)[1]
    y, m = add_months(start.year, start.month, -step)
    ye, me = add_months(end.year, end.month, -step)
    return date(y, m, min(start.day, month_range(y, m)[1].day)), date(ye, me, min(end.day, month_range(ye, me)[1].day))


@dataclass
class Period:
    start: date
    end: date
    label: str          # the words that produced it (folded)
    kind: str           # rel | month | quarter | year | last_n | before
    unit: str = ""      # day | week | month | quarter | year: what one step "before" means for this period

    def spec(self) -> dict:
        return {"start": self.start.isoformat(), "end": self.end.isoformat()}


@dataclass
class PeriodParse:
    periods: list[Period] = field(default_factory=list)
    compare: str | None = None            # previous | yoy
    shift: dict | None = None             # {"unit","n"}: "and the month before?" with no period of its own (needs state)
    notes: list[str] = field(default_factory=list)
    residual: str = ""                    # the folded question with every time / compare phrase blanked out

    def specs(self) -> list[dict]:
        return [p.spec() for p in self.periods]


# ----------------------------------------------------------------------------------------------- patterns (folded text)
_YOY = re.compile(
    r"(same (period|month|quarter|week|days?) (last|a) year|year[- ]over[- ]year|\byoy\b|year ago|"
    r"(compared? (with|to)|versus|vs\.?|against) (the same \w+ )?(last|previous) year|"
    r"cung ky nam ngoai|cung ky nam truoc|so voi nam ngoai|so voi nam truoc|"
    r"vorjahr|im vergleich zum vorjahr|gleichen? (zeitraum|monat|quartal) (im|des) vorjahr|vorjahreszeitraum|zum vorjahr)")
_PREV = re.compile(
    r"(previous period|prior period|period before|earlier period|ky truoc|vorperiode|vorherigen? (zeitraum|periode)|"
    r"letzten? zeitraum|vorzeitraum|(vergleich zum|zum|gegenuber dem|gegenuber|versus) vormonat)")
_BEFORE = re.compile(
    rf"(?:the )?(?:(\d+) )?({_UNIT_WORDS})s? (?:before|earlier|prior)|"
    rf"(?:the )?(?:(\d+) )?({_UNIT_WORDS})s? (?:davor|zuvor|vorher)|"
    rf"(?:(\d+) )?({_UNIT_WORDS}) (?:lien )?(?:truoc do nua|truoc nua|truoc do|truoc nua do)\b|"
    r"(?:the )?(month|quarter|week|year) before|(?:den |die |das )?(monat|quartal|woche|jahr) davor|"
    r"thang (?:lien )?(?:truoc nua|truoc do)\b|quy (?:lien )?truoc nua\b|quy (?:lien )?truoc do\b")

_CMP_CUE = re.compile(r"(so voi|compar\w*|\bvs\.?|versus|than|gegenuber|vergleich\w*|zum|im vergleich zu|\bhon|\bhay|\bals)\s+(the |dem |der |den )?$")
_LAST_N = re.compile(
    rf"(?:last|past|previous|recent|letzten?|letzte|vergangenen?|vorigen?|letzte|vergangene)\s+(\d+)\s+({_UNIT_WORDS})\b|"
    rf"\b(\d+)\s+({_UNIT_WORDS})\s+(?:qua|gan day|gan nhat|vua qua|gan nhat qua)\b|"
    rf"\b(\d+)\s+({_UNIT_WORDS})\s+(?:zuruck|zuvor)\b|"
    rf"(?:trong\s+)?(\d+)\s+({_UNIT_WORDS})\s+(?:qua|gan day)")
_REL = [
    # (regex, kind, which)
    (re.compile(r"\b(last|previous|prior|past) month\b|\bthang (truoc|vua roi|rooi)\b|\b(letzte[nms]?|vorige[nms]?|vergangene[nms]?) monat(s)?\b|\bvormonat\b"), "last_month"),
    (re.compile(r"\b(this|current) month\b|\bthang nay\b|\b(diese[nms]?|aktuelle[nms]?|laufende[nms]?) monat(s)?\b"), "this_month"),
    (re.compile(r"\b(last|previous|prior|past) quarter\b|\bquy (truoc|vua roi)\b|\b(letzte[nms]?|vorige[nms]?|vergangene[nms]?) quartal(s)?\b|\bvorquartal\b"), "last_quarter"),
    (re.compile(r"\b(this|current) quarter\b|\bquy nay\b|\b(diese[nms]?|aktuelle[nms]?) quartal(s)?\b"), "this_quarter"),
    (re.compile(r"\b(last|previous|prior) year\b|\bnam (ngoai|truoc)\b|\b(letzte[nms]?|vorige[nms]?|vergangene[nms]?) jahr(es)?\b|\bvorjahr\b"), "last_year"),
    (re.compile(r"\b(this|current) year\b|\bso far this year\b|\bsince the (start|beginning) of (the|this) year\b|\bsince (the )?(start|beginning) of (20\d\d)\b|\byear so far\b|\byear[- ]to[- ]date\b|\bytd\b|\bnam nay\b|\btu dau nam\b|"
                r"\b(dieses|diesem|aktuelles|laufendes) jahr(es)?\b|\bseit jahresbeginn\b|\bjahr bis heute\b|\bbisher dieses jahr\b"), "ytd"),
    (re.compile(r"\b(last|previous) week\b|\btuan (truoc|vua roi|qua)\b|\b(letzte[nms]?|vorige[nms]?|vergangene[nms]?) woche\b"), "last_week"),
    (re.compile(r"\b(this|current) week\b|\btuan nay\b|\bdiese woche\b"), "this_week"),
    (re.compile(r"\b(all time|since the beginning|ever|from the start)\b|\btoan bo thoi gian\b|\bgesamten zeitraum\b|\ballzeit\b"), "all"),
]
_QUARTER = re.compile(r"\bq([1-4])(?:\s*[/ -]?\s*(20\d\d))?\b|\bquy\s*([1-4])(?:\s*[/ -]\s*(20\d\d))?\b|\b([1-4])\.?\s*quartal(?:\s*(20\d\d))?\b|\bquartal\s*([1-4])\b")
_NAMED_MONTH = re.compile(
    rf"\bthang\s*(0?[1-9]|1[0-2])\b(?:\s*[/ ,-]?\s*(?:nam\s*)?(20\d\d))?|\b({_MONTH_WORDS})\b(?:\s*,?\s*(20\d\d))?")
_YEAR_ONLY = re.compile(r"\b(?:in|during|for|trong nam|nam|im jahr|in jahr)\s+(20\d\d)\b")


def _anchor_period(kind: str, anchor: date, min_date: date | None) -> Period | None:
    y, m = anchor.year, anchor.month
    if kind == "last_month":
        yy, mm = add_months(y, m, -1)
        s, e = month_range(yy, mm)
        return Period(s, e, kind, "rel", "month")
    if kind == "this_month":
        return Period(date(y, m, 1), anchor, kind, "rel", "month")
    if kind == "last_quarter":
        q = (m - 1) // 3 + 1
        yy, qq = (y, q - 1) if q > 1 else (y - 1, 4)
        s, e = quarter_range(yy, qq)
        return Period(s, e, kind, "rel", "quarter")
    if kind == "this_quarter":
        q = (m - 1) // 3 + 1
        return Period(quarter_range(y, q)[0], anchor, kind, "rel", "quarter")
    if kind == "last_year":
        return Period(date(y - 1, 1, 1), date(y - 1, 12, 31), kind, "rel", "year")
    if kind == "ytd":
        return Period(date(y, 1, 1), anchor, kind, "rel", "year")
    if kind == "last_week":
        s = anchor - timedelta(days=anchor.weekday() + 7)
        return Period(s, s + timedelta(days=6), kind, "rel", "week")
    if kind == "this_week":
        return Period(anchor - timedelta(days=anchor.weekday()), anchor, kind, "rel", "week")
    if kind == "all":
        return Period(min_date or date(anchor.year - 5, 1, 1), anchor, kind, "rel", "year")
    return None


def _last_n(n: int, unit: str, anchor: date) -> Period:
    if unit == "day":
        return Period(anchor - timedelta(days=n - 1), anchor, f"last_{n}_days", "last_n", "day")
    if unit == "week":
        return Period(anchor - timedelta(days=7 * n - 1), anchor, f"last_{n}_weeks", "last_n", "week")
    step = {"month": 1, "quarter": 3, "year": 12}[unit]
    y, m = add_months(anchor.year, anchor.month, -(n * step) + 1)
    s = date(y, m, 1) if anchor == month_range(anchor.year, anchor.month)[1] else anchor - timedelta(days=round(30.4 * n * step) - 1)
    return Period(s, anchor, f"last_{n}_{unit}s", "last_n", unit)


_GAP_STOP = {"vs", "versus", "and", "to", "with", "than", "compared", "compare", "und", "so", "voi", "mit", "gegen", "zu", "or", "oder", "hay", "against"}


def _year_gap_ok(gap: str) -> bool:
    """Nothing, or one or two plain words ("September revenue this year", "doanh thu tháng 9 của năm nay"), sit between
    a month / quarter and the year word that belongs to it; a comparison word means two separate periods."""
    words = gap.replace(",", " ").split()
    return len(words) <= 2 and not any(w in _GAP_STOP for w in words)


def parse_periods(text: str, anchor: date, min_date: date | None = None) -> PeriodParse:
    """All periods named in the question, in the order they are mentioned, plus a compare mode when the question says
    "versus the previous period / the same period last year". Dates are inclusive and never later than the anchor."""
    q = fold(text)
    out = PeriodParse()
    spans: list[tuple[int, int, Period | str]] = []

    def taken(a: int, b: int) -> bool:
        return any(a < e and s < b for s, e, _ in spans)

    # 1. year-over-year / previous-period wording is a compare mode, not a period of its own
    for rx, mode in ((_YOY, "yoy"), (_PREV, "previous")):
        for m in rx.finditer(q):
            spans.append((m.start(), m.end(), mode))
            if out.compare is None:
                out.compare = mode

    # 2. "the month before", "the 7 days before"
    for m in _BEFORE.finditer(q):
        if taken(m.start(), m.end()):
            continue
        g = m.groups()
        n, unit = 1, None
        pairs = [(g[0], g[1]), (g[2], g[3]), (g[4], g[5])]
        for num, u in pairs:
            if u:
                n, unit = int(num or 1), UNIT.get(u)
                break
        if unit is None:
            word = g[6] or g[7]
            unit = UNIT.get(word) if word else None
        if unit is None:
            unit = "quarter" if "quy" in m.group(0) else "month"
        spans.append((m.start(), m.end(), Period(anchor, anchor, "before", "before", unit)))
        spans[-1][2].label = f"{n}:{unit}"

    # 3. explicit "last N days/weeks/months"
    for m in _LAST_N.finditer(q):
        if taken(m.start(), m.end()):
            continue
        nums = [x for x in m.groups() if x is not None]
        n, u = int(nums[0]), UNIT.get(nums[1])
        if u and 1 <= n <= 400:
            spans.append((m.start(), m.end(), _last_n(n, u, anchor)))

    # 4. quarters (Q3, quy 3, 3. Quartal)
    for m in _QUARTER.finditer(q):
        if taken(m.start(), m.end()):
            continue
        g = m.groups()
        qn = next((int(x) for x in (g[0], g[2], g[4], g[6]) if x), None)
        yr = next((int(x) for x in (g[1], g[3], g[5]) if x), None)
        if qn is None:
            continue
        cur_q = (anchor.month - 1) // 3 + 1
        y = yr or (anchor.year if qn <= cur_q else anchor.year - 1)
        s, e = quarter_range(y, qn)
        spans.append((m.start(), m.end(), Period(s, min(e, anchor), f"q{qn}", "quarter", "quarter")))

    # 5. named months (August, thang 8, im August)
    for m in _NAMED_MONTH.finditer(q):
        if taken(m.start(), m.end()):
            continue
        g = m.groups()
        if g[0]:
            mm, yr = int(g[0]), int(g[1]) if g[1] else None
        else:
            name = g[2]
            ctx = q[max(0, m.start() - 14):m.start()]
            prep = re.search(r"\b(in|im|of|since|for|until|during|from|to|von|bis|seit|ab|fur)\s*$", ctx) is not None
            if name == "may" and not (prep or m.group(0) != "may"):
                continue                                    # the verb "may"
            if len(name) == 3 and name != "may" and not (prep or g[3]):
                continue                                    # abbreviations need a preposition or a year
            mm, yr = _MONTH_BY_NAME[name], int(g[3]) if g[3] else None
        if yr is None:
            yr = anchor.year if mm <= anchor.month else anchor.year - 1
        s, e = month_range(yr, mm)
        spans.append((m.start(), m.end(), Period(s, min(e, anchor), f"m{yr}-{mm:02d}", "month", "month")))

    # 6. relative words
    for rx, kind in _REL:
        for m in rx.finditer(q):
            if taken(m.start(), m.end()):
                continue
            p = _anchor_period(kind, anchor, min_date)
            if p:
                spans.append((m.start(), m.end(), p))

    # 7. a bare year
    for m in _YEAR_ONLY.finditer(q):
        if taken(m.start(), m.end()):
            continue
        y = int(m.group(1))
        spans.append((m.start(), m.end(), Period(date(y, 1, 1), min(date(y, 12, 31), anchor), f"y{y}", "year", "year")))

    blanked = list(q)
    for a0, b0, _ in spans:
        for j in range(a0, b0):
            blanked[j] = " "
    out.residual = re.sub(r"\s+", " ", "".join(blanked)).strip()
    spans.sort(key=lambda x: x[0])
    i = 0
    while i < len(spans) - 1:                       # "tháng 9 năm nay" / "September last year": the year belongs to the month
        a, b = spans[i], spans[i + 1]
        if (isinstance(a[2], Period) and a[2].kind in ("month", "quarter") and isinstance(b[2], Period)
                and b[2].label in ("ytd", "last_year") and _year_gap_ok(q[a[1]:b[0]])):
            if b[2].label == "last_year":
                s0, e0 = shift_range(a[2].start, a[2].end, "year", 1)
                a[2].start, a[2].end = s0, e0
            else:
                a[2].end = min(a[2].end, anchor)
            del spans[i + 1]
            continue
        i += 1
    for st0, _, item in spans:
        if isinstance(item, str):
            continue
        if item.kind == "before":
            n_s, unit = item.label.split(":")
            n = int(n_s)
            if out.periods:                                  # relative to the period named just before it
                base = out.periods[-1]
                s, e = shift_range(base.start, base.end, unit, n)
                out.periods.append(Period(s, e, f"before:{unit}", "before", base.unit or unit))
            elif n == 1 and unit in ("month", "quarter", "week", "year") and _CMP_CUE.search(q[max(0, st0 - 24):st0]):
                p0 = _anchor_period("last_" + unit if unit != "year" else "last_year", anchor, min_date)
                if p0:                                       # "compared with the quarter before" = "...the previous quarter"
                    out.periods.append(p0)
                else:
                    out.shift = {"unit": unit, "n": n}
            else:
                out.shift = {"unit": unit, "n": n}           # "and the month before?": the planner shifts the last turn
            continue
        out.periods.append(item)
    # an interval can never reach past the anchor
    for p in out.periods:
        if p.end > anchor:
            p.end = anchor
            out.notes.append("clipped_to_loaded_data")
    return out


def default_compare_period(p: Period) -> tuple[date, date]:
    """The period right before ``p`` of the same length (what the dashboard calls 'previous')."""
    days = (p.end - p.start).days + 1
    return p.start - timedelta(days=days), p.start - timedelta(days=1)


def previous_calendar_span(p: Period) -> tuple[date, date] | None:
    """For a period that is whole calendar months (a month, quarter or year) the same number of whole months right
    before it, so "September" is compared with August, not with Aug 2-31; None for any other period."""
    if p.start.day != 1 or (p.end + timedelta(days=1)).day != 1:
        return None
    n = (p.end.year - p.start.year) * 12 + p.end.month - p.start.month + 1
    m = p.start.year * 12 + p.start.month - 1 - n
    start = date(m // 12, m % 12 + 1, 1)
    return start, p.start - timedelta(days=1)
