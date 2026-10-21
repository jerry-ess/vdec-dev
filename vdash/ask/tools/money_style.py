"""How talk-to-data writes Vietnamese dong (customer feedback of 19 Oct 2026). Applies to chat answers only; the dashboard pages and the
reports keep ``vdash.fmt`` as it is.

    vi   1,5k   717,96tr   9,26ty    (no spaces, no currency sign)
    en   9.26B VND    -222.09M VND    de   9,26 Mrd. VND
Other currencies and every other kind of value are formatted by ``vdash.fmt`` unchanged."""
from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal

from ... import fmt as _fmt

_VI_UNITS = ((Decimal(10) ** 9, "ty"), (Decimal(10) ** 6, "tr"), (Decimal(10) ** 3, "k"))


def _vi_amount(n: float, max_frac: int, compact: bool) -> str:
    a = abs(Decimal(repr(float(n))))
    if compact:
        for i, (mag, unit) in enumerate(_VI_UNITS):
            if a >= mag:
                scaled = (a / mag).quantize(Decimal(1).scaleb(-max_frac), rounding=ROUND_HALF_UP)
                if scaled >= 1000 and i > 0:                  # 999,999k -> 1tr, not 1000k
                    mag, unit = _VI_UNITS[i - 1]
                    scaled = (a / mag).quantize(Decimal(1).scaleb(-max_frac), rounding=ROUND_HALF_UP)
                return _fmt._num(float(scaled), "vi", max_frac) + unit
    return _fmt._num(float(a), "vi", 0 if a >= 100 else max_frac)


def _vnd_other(s: str, lang: str) -> str:
    """en / de (and the rest): the dong sign becomes the code VND, after the number."""
    if "₫" not in s:
        return s
    if lang == "de":
        return s.replace("₫", "VND")
    body = s.replace("₫", "").replace(" ", " ").strip()
    sign = body[0] if body[:1] in "+-−–" else ""
    return f"{sign}{body[len(sign):].strip()} VND"


def ask_value(v, kind, lang: str, currency: str, compact: bool = True) -> str:
    if currency != "VND" or kind != "currency" or lang not in ("vi", "en", "de"):
        return _fmt.fmt_value(v, kind, lang, currency, compact=compact)
    try:
        n = float(v)
    except (TypeError, ValueError):
        return _fmt.fmt_value(v, kind, lang, currency, compact=compact)
    if n != n or n in (float("inf"), float("-inf")):
        return _fmt.fmt_value(v, kind, lang, currency, compact=compact)
    if lang == "vi":
        return ("-" if n < 0 and round(abs(n)) else "") + _vi_amount(n, 2, compact)
    return _vnd_other(_fmt.fmt_value(v, kind, lang, currency, compact=compact), lang)


def ask_arg(a: dict, lang: str, currency: str) -> str:
    if currency != "VND" or (a or {}).get("fmt") != "signed_currency" or (a or {}).get("v") is None or lang not in ("vi", "en", "de"):
        return _fmt.fmt_arg(a, lang, currency)
    n = float(a["v"])
    if lang == "vi":
        neg = _fmt._round(n, 1) < 0
        return _fmt._signed(_vi_amount(abs(n), 1, True), neg, "vi")
    return _vnd_other(_fmt.fmt_arg(a, lang, currency), lang)


_WS = "[\\s\u00a0\u202f]*"
_EN_RX = re.compile(r"([+\-\u2212]?)\u20ab(\d[\d,]*(?:\.\d+)?)([KMBT]?)")
_VI_RX = re.compile(r"(?<![\w.,])(\d[\d.]*(?:,\d+)?)" + _WS + r"(Tr|N|T)?" + _WS + "\u20ab")
_VI_UNIT = {"N": "k", "Tr": "tr", "T": "ty"}


def restyle_text(s: str, lang: str, currency: str) -> str:
    """Amounts already written into a sentence by the report engine (findings, warnings, actions) get the same style:
    '\u20ab86.64M' -> '86.64M VND', '86,64 Mio. \u20ab' -> '86,64 Mio. VND', '86,64 Tr \u20ab' -> '86,64tr'."""
    if currency != "VND" or not isinstance(s, str) or "\u20ab" not in s:
        return s
    if lang == "vi":
        def vi(m):
            num, unit = m.group(1), m.group(2)
            if unit:
                return num + _VI_UNIT[unit]
            try:
                return _vi_amount(float(num.replace(".", "").replace(",", ".")), 2, True)
            except ValueError:
                return m.group(0)
        return _VI_RX.sub(vi, s)
    if lang == "de":
        return s.replace("\u20ab", "VND")
    if lang == "en":
        return _EN_RX.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)} VND", s)
    return s


def restyle_result(obj, lang: str, currency: str):
    """Every string inside a tool result (facts, table cells, point texts), in place."""
    if currency != "VND" or lang not in ("vi", "en", "de"):
        return obj
    if isinstance(obj, dict):
        for k, v in obj.items():
            obj[k] = restyle_result(v, lang, currency)
        return obj
    if isinstance(obj, list):
        for i, v in enumerate(obj):
            obj[i] = restyle_result(v, lang, currency)
        return obj
    return restyle_text(obj, lang, currency) if isinstance(obj, str) else obj
