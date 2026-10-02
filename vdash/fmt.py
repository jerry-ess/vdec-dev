"""Locale-aware number, money and date formatting on the server (v23).

Gives the same text as the web page (``fmt`` / ``fmtArg`` in ``web/app.js``, which use the browser's ``Intl``),
so a sentence in a download or a report reads exactly like the one on screen, in every language
(VND examples):

    en  ₫481.5M    +3.5%    Sep 26, 2026
    de  481,5 Mio. ₫    +3,5%    26. Sept. 2026
    vi  481,5 Tr ₫    +3,5%    26 thg 9, 2026
    zh  ₫4.81亿    +3.5%    2026年9月26日

Built on Babel (Unicode CLDR - the same locale data browsers use). ``tests/format_golden.json`` holds what the
browser prints for 1,768 value / kind / language / currency cases (``tools/make_format_golden.js``); the tests
require identical strings.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from functools import lru_cache
from typing import Any

from babel import Locale
from babel.dates import format_skeleton
from babel.numbers import (get_currency_precision, get_minus_sign_symbol, get_plus_sign_symbol, parse_pattern)

from .i18n import LOCALES, norm_lang, ui

DASH = "—"   # missing value, as on the page


@lru_cache(maxsize=None)
def _locale(lang: str) -> Locale:
    return Locale.parse(LOCALES[norm_lang(lang)].replace("-", "_"))


def _round(n: float, digits: int) -> Decimal:
    """Round half away from zero (the browser's rule), starting from the shortest decimal form of the float."""
    return Decimal(repr(float(n))).quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP)


def _num(n: float, lang: str, max_frac: int, min_frac: int = 0) -> str:
    loc = _locale(lang)
    p = parse_pattern(loc.decimal_formats[None].pattern)
    p.frac_prec = (min_frac, max_frac)
    return p.apply(_round(n, max_frac), loc, decimal_quantization=False)


def _money(n: float, lang: str, currency: str, max_frac: int) -> str:
    loc = _locale(lang)
    p = parse_pattern(loc.currency_formats["standard"].pattern)
    min_frac = min(get_currency_precision(currency), max_frac)
    p.frac_prec = (min_frac, max_frac)
    return p.apply(_round(n, max_frac), loc, currency=currency, currency_digits=False, decimal_quantization=False)


def _compact(n: float, lang: str, max_frac: int, currency: str | None = None) -> str:
    """Short compact form (12.3K, 481,5 Mio. ₫, ₫4.81亿) the way the browser does it: the scaled number is rounded
    half away from zero, and if rounding reaches the next unit the next unit is used (999,999 -> 1M, not 1000K).
    Money keeps the currency's minimum decimals ($10.00K), like Intl."""
    loc = _locale(lang)
    cf = loc.compact_currency_formats["short"] if currency else loc.compact_decimal_formats["short"]
    mags = sorted((int(m) for m in cf["other"]), reverse=True)
    a = _round(abs(n), max_frac)
    pattern, scaled = None, a
    for _ in range(3):                          # at most one step up after rounding; 3 for safety
        mag = next((m for m in mags if a >= m), None)
        if mag is None:
            pattern, scaled = None, a
            break
        pat = str(cf["other"][str(mag)].pattern if hasattr(cf["other"][str(mag)], "pattern") else cf["other"][str(mag)])
        if parse_pattern(pat).pattern == "0":   # this unit is not shortened
            pattern, scaled = None, a
            break
        div = Decimal(mag // (10 ** (parse_pattern(pat).pattern.count("0") - 1)))
        scaled = (a / div).quantize(Decimal(1).scaleb(-max_frac), rounding=ROUND_HALF_UP)
        bumped = scaled * div
        if any(m > mag and bumped >= m for m in mags):
            a = bumped                          # rounding reached the next unit: pick again
            continue
        form = loc.plural_form(scaled)
        if form not in cf or str(mag) not in cf[form]:
            form = "other"
        pattern = cf[form][str(mag)]
        break
    min_frac = min(get_currency_precision(currency), max_frac) if currency else 0
    value = -scaled if n < 0 else scaled
    if pattern is None:
        return _money(float(value), lang, currency, max_frac) if currency else _num(float(value), lang, max_frac, min_frac)
    p = parse_pattern(pattern.pattern if hasattr(pattern, "pattern") else pattern)
    p.frac_prec = (min_frac, max_frac)
    return p.apply(value, loc, currency=currency, currency_digits=False, decimal_quantization=False)


def _signed(s_abs: str, negative: bool, lang: str) -> str:
    loc = _locale(lang)
    return (get_minus_sign_symbol(loc) if negative else get_plus_sign_symbol(loc)) + s_abs


def fmt_value(v: Any, kind: str | None, lang: str, currency: str, compact: bool = True) -> str:
    """One value as the page's ``fmt(v, kind, compact)`` shows it."""
    if v is None or v == "" or (isinstance(v, float) and (v != v or v in (float("inf"), float("-inf")))):
        return DASH
    if kind in (None, "text", "label"):
        return str(v)
    if kind == "date":
        return fmt_date(v, lang)
    if kind == "month":
        return fmt_month(v, lang)
    try:
        n = float(v)
    except (TypeError, ValueError):
        return str(v)
    big = abs(n) >= 10000
    if kind == "currency":
        if compact and big:
            return _compact(n, lang, 2, currency)
        return _money(n, lang, currency, 2 if abs(n) < 100 else 0)
    if kind == "pct":
        return _num(n, lang, 1, 1 if abs(n) < 10 else 0) + "%"
    if kind == "int":
        return _compact(n, lang, 1) if compact and big else _num(n, lang, 0)
    if kind == "ratio":
        return _num(n, lang, 2, 2)
    if kind == "days":
        return _num(n, lang, 1) + " d"
    if kind == "hours":
        return _num(n, lang, 1) + " h"
    if kind == "score":
        return _num(n, lang, 1)
    return _compact(n, lang, 1) if compact and big else _num(n, lang, 2)


def fmt_arg(a: dict, lang: str, currency: str) -> str:
    """A typed finding argument (``evaluation.arg``) as the page's ``fmtArg`` shows it."""
    v, f = (a or {}).get("v"), (a or {}).get("fmt")
    if v is None:
        return DASH
    if f in ("label", "text"):
        return str(v)
    if f in ("signed_pct", "signed_pts", "pts", "signed_currency"):
        n = float(v)
        if f == "pts":
            return f"{_num(n, lang, 1, 1)} {ui(lang, 'pts')}"
        if f == "signed_currency":
            neg = _round(n, 1) < 0
            body = _compact(abs(n), lang, 1, currency) if abs(n) >= 10000 else _money(abs(n), lang, currency, 1)
            return _signed(body, neg, lang)
        neg = _round(n, 1) < 0
        s = _signed(_num(abs(n), lang, 1, 1), neg, lang)
        return f"{s}%" if f == "signed_pct" else f"{s} {ui(lang, 'pts')}"
    return fmt_value(v, f, lang, currency)


def _as_date(v: Any) -> date | None:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def fmt_date(v: Any, lang: str) -> str:
    d = _as_date(v)
    return format_skeleton("yMMMd", d, locale=_locale(lang)) if d else str(v)


def fmt_month(v: Any, lang: str) -> str:
    d = _as_date(str(v)[:7] + "-01")
    return format_skeleton("yMMM", d, locale=_locale(lang)) if d else str(v)


def fmt_bucket(key: Any, grain: str, lang: str) -> str:
    """A chart time bucket as the page labels it (``fmtBucket``): "26 Sep", "Week of 21 Sep", "Sep 2026", "2026-Q3"."""
    import re
    k = str(key)
    if re.fullmatch(r"\d{4}-Q\d", k) or re.fullmatch(r"\d{4}", k):
        return k
    if re.fullmatch(r"\d{4}-\d{2}", k):
        return fmt_month(k, lang)
    d = _as_date(k) if re.fullmatch(r"\d{4}-\d{2}-\d{2}.*", k) else None
    if d is None:
        return k
    s = format_skeleton("MMMd", d, locale=_locale(lang))
    return ui(lang, "wk", d=s) if grain == "week" else s
