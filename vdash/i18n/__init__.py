"""Multi-language support: English (source), German, Vietnamese, Simplified Chinese.

Each ``<lang>.json`` has two maps:

* ``ui``   - interface strings by key (buttons, labels, messages) used by the web page;
* ``text`` - catalog and data strings keyed by their English text (KPI titles and
  descriptions, section names, column and series labels, known data values such
  as RFM segment names or weekday names).

Translation happens in the API layer *after* the result cache, so one cached
result serves every language. Anything without a translation falls back to
English. ``tests/test_dashboard.py`` checks that every catalog string is covered.
Adding a language = adding one JSON file and one entry in ``LANGS``.
"""
from __future__ import annotations

import copy
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
LANGS = {"en": "English", "de": "Deutsch", "vi": "Tiếng Việt", "zh": "中文"}
LOCALES = {"en": "en-US", "de": "de-DE", "vi": "vi-VN", "zh": "zh-CN"}
DEFAULT = "en"


def norm_lang(lang: str | None) -> str:
    lang = (lang or DEFAULT).lower()[:2]
    return lang if lang in LANGS else DEFAULT


@lru_cache(maxsize=None)
def bundle(lang: str) -> dict:
    lang = norm_lang(lang)
    base = json.loads((HERE / "en.json").read_text(encoding="utf-8"))
    if lang == "en":
        return {"ui": base["ui"], "text": base.get("text", {})}
    tr = json.loads((HERE / f"{lang}.json").read_text(encoding="utf-8"))
    # English 'text' entries prettify raw data values (e.g. early_morning -> Early morning); a language
    # inherits them unless it overrides the key.
    return {"ui": {**base["ui"], **tr.get("ui", {})}, "text": {**base.get("text", {}), **tr.get("text", {})}}


def t(lang: str, s: Any) -> Any:
    if not isinstance(s, str) or not s:
        return s
    return bundle(lang)["text"].get(s, s)


def ui(lang: str, key: str, **kw) -> str:
    s = bundle(lang)["ui"].get(key) or bundle("en")["ui"].get(key) or key
    try:
        return s.format(**kw) if kw else s
    except (KeyError, IndexError):
        return s


def translate_kpi_public(d: dict, lang: str) -> dict:
    if norm_lang(lang) == "en":
        return d
    d = dict(d)
    for k in ("title", "desc", "note"):
        d[k] = t(lang, d.get(k))
    d["series"] = [{**s, "name": t(lang, s.get("name"))} for s in d.get("series", [])]
    d["columns"] = [{**c, "label": t(lang, c.get("label"))} for c in d.get("columns", [])]
    return d


def translate_result(res: dict, lang: str, text_columns: set[str] | None = None) -> dict:
    """Return a translated copy of an engine result (the cached original is never mutated)."""
    lang = norm_lang(lang)
    r = copy.deepcopy(res)
    if r.get("caveat_key"):
        r["caveat"] = ui(lang, r["caveat_key"], **r.get("caveat_args", {}))
    typ = r.get("type")
    if typ == "timeseries":
        for s in r.get("series", []):
            s["name"] = t(lang, s.get("name"))
    elif typ == "breakdown":
        for i in r.get("items", []):
            if i.get("label_key"):
                i["label"] = ui(lang, i["label_key"], **i.get("label_args", {}))
            else:
                i["label"] = t(lang, i.get("label"))
    elif typ == "heatmap":
        r["y"] = [t(lang, y) for y in r.get("y", [])]
    elif typ == "table" and text_columns:
        for row in r.get("rows", []):
            for k in text_columns:
                row[k] = t(lang, row.get(k))
    return r
