"""Charts made for reading (v42, Step 20): what to draw on a chart so the reader sees the point at once.

Two kinds of marks, both worked out here in plain code (no AI) and stored with the report's chart data, so a stored
report always opens with the same marks:

* **unusual points** on a trend chart (an unusually good or bad day): robust statistics (median and median absolute
  deviation), so one big day does not hide itself by inflating the spread. At most three, each numbered 1, 2, 3 on
  the chart and explained in a line under it.
* **highlighted bars / slices**: the items of a breakdown chart (product, customer segment, weekday ...) that the
  chapter text or the chart's caption talks about. Everything else is drawn lighter.

Both are optional: a chart without marks is drawn exactly as before.
"""
from __future__ import annotations

import re
import statistics
import unicodedata
from typing import Any

MIN_POINTS = 14            # fewer points than this say nothing about "unusual"
MAX_MARKS = 3
MIN_SCORE = 3.5            # robust z-score (Iglewicz-Hoaglin): 3.5 is the textbook cut-off for an outlier
MIN_SHIFT = 0.35           # and the point must differ from the typical level by at least 35 %
MAX_FOCUS = 3
MIN_LABEL = 3              # shorter labels ("A", "1", "Wk") would match everywhere


def norm(s: Any) -> str:
    """Lower case, no accents, single spaces - so 'Dầu gội' matches 'dau goi' and 'Champions' matches 'CHAMPIONS'."""
    t = unicodedata.normalize("NFKD", str(s or "")).replace("đ", "d").replace("Đ", "D")
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", t.casefold()).strip()


def _mentions(text_n: str, label: str) -> bool:
    lab = norm(label)
    if len(lab) < MIN_LABEL or lab.replace(" ", "").replace(".", "").replace(",", "").isdigit():
        return False
    return re.search(r"(?<!\w)" + re.escape(lab) + r"(?!\w)", text_n) is not None


def anomalies(result: dict) -> list[dict]:
    """Unusual points of the chart's main series: [{i, x, dir, value, typical, pct}] (largest first, at most 3)."""
    if result.get("type") != "timeseries":
        return []
    xs, series = result.get("x") or [], result.get("series") or []
    cand = [(k, s) for k, s in enumerate(series) if not s.get("axis") and not s.get("stack")]
    if len(cand) > 1:
        cand = [c for c in cand if c[1].get("trend")][:1]
    if len(cand) != 1:
        return []
    k, s = cand[0]
    pts = [(i, float(v)) for i, v in enumerate(s.get("data") or []) if v is not None]
    if len(pts) < MIN_POINTS or len(xs) < len(s.get("data") or []):
        return []
    med = statistics.median(v for _, v in pts)
    mad = statistics.median(abs(v - med) for _, v in pts)
    if not mad or not med:
        return []
    out = []
    for i, v in pts:
        score = 0.6745 * (v - med) / mad
        if abs(score) >= MIN_SCORE and abs(v - med) >= MIN_SHIFT * abs(med):
            out.append({"i": i, "x": xs[i], "dir": "up" if v > med else "down", "series": k, "value": v,
                        "typical": med, "pct": 100.0 * (v - med) / abs(med), "score": abs(score)})
    out.sort(key=lambda a: -a["score"])
    out = sorted(out[:MAX_MARKS], key=lambda a: a["i"])          # numbered left to right
    for n, a in enumerate(out, 1):
        a.pop("score")
        a["n"] = n
    return out


def focus_items(result: dict, kpi: dict, texts: list[str]) -> list[str]:
    """Labels of the breakdown items that the given texts mention (at most 3; largest items first)."""
    if result.get("type") in ("timeseries", "heatmap") or not texts:
        return []
    text_n = norm(" ".join(t for t in texts if t))
    out = []
    for it in result.get("items") or []:
        if it.get("label_key") or it.get("key") == "__other":
            continue                       # labels that carry their own numbers, or the 'other' bucket
        if _mentions(text_n, it.get("label")):
            out.append(str(it["label"]))
    return out[:MAX_FOCUS] if len(out) < len(result["items"]) else []     # everything highlighted = nothing highlighted


def marks_for(block: dict, texts: list[str] | None = None) -> tuple[dict | None, list[dict], list[str]]:
    """(marks for the chart code, unusual points with their numbers, highlighted labels) of one chart block.
    The marks are None when there is nothing to draw."""
    r = block.get("result") or {}
    an = anomalies(r)
    fo = focus_items(r, block.get("kpi_public") or {}, texts or [])
    if not an and not fo:
        return None, [], []
    m: dict = {}
    if an:
        m["an"] = [{"i": a["i"], "dir": a["dir"], "k": a["series"], "n": a["n"]} for a in an]
    if fo:
        m["focus"] = fo
    return m, an, fo
