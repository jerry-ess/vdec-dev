"""A report as a PDF without a browser (v46, AI plan Step 24): drawn directly with ReportLab.

Used where no Chrome / Edge / Chromium exists (a small web server such as Render's free tier). It reads the same
stored report document as the HTML page and shows the same things in the same order - cover, first page (headline,
key messages, "Since your last report", key figures, top actions), contents, the story chapters with their charts
and tables, the action plan, the appendix - but laid out for paper: A4, charts drawn natively as vector graphics
(trend lines and bars with the numbered unusual points, bar charts with the highlighted items, rings, heat maps).
Nothing is drawn by a script and nothing needs a network.

Fonts (``reports/fonts``): DejaVu Sans for Latin / Vietnamese / German, embedded in the PDF; Droid Sans Fallback for
Chinese characters (picked per character, so a mixed line works). Both are free to redistribute (licences included).
"""
from __future__ import annotations

import io
import math
import re
from datetime import date
from pathlib import Path

from ..fmt import fmt_arg, fmt_bucket, fmt_value
from .chart_marks import marks_for
from .html import NOWRAP, NUMERIC, _norm, _R

try:
    from reportlab.graphics.shapes import Circle, Drawing, Group, Line, PolyLine, Rect, Wedge
    from reportlab.graphics.shapes import String as _String
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT, TA_RIGHT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import (BaseDocTemplate, CondPageBreak, Frame, KeepTogether, PageBreak, PageTemplate, Paragraph, Spacer,
                                    Table, TableStyle)
    from reportlab.pdfbase.pdfmetrics import stringWidth as _sw
    AVAILABLE = True
except ImportError:           # pragma: no cover - reportlab is in requirements.txt
    AVAILABLE = False

FONTS = Path(__file__).parent / "fonts"
SANS, BOLD, CJK = "VX-Sans", "VX-Sans-Bold", "VX-CJK"
_REGISTERED = False

# colours = the page's tokens (html.py CSS)
INK, INK2, MUTED, AXIS = "#0b0b0b", "#52514e", "#6f6d68", "#898781"
GRID, BASE, SURF2 = "#e1e0d9", "#c3c2b7", "#f3f2ee"
ACCENT, ACCENT_SOFT = "#2a78d6", "#e6f0fc"
GOOD, BAD, WATCH = "#006300", "#b42f2f", "#8a5a00"
GOOD_SOFT, BAD_SOFT, WATCH_SOFT = "#e7f3e7", "#fbeaea", "#fff1d2"
PAL = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
SEQ = ["#cde2fb", "#86b6ef", "#3987e5", "#1c5cab", "#104281"]
TONE = {"good": GOOD, "bad": BAD, "warn": WATCH, "warning": WATCH, "info": ACCENT}
_CJK = re.compile("([⺀-鿿豈-﫿＀-￯　-〿]+)")


class PdfNativeError(RuntimeError):
    pass


def _register() -> None:
    global _REGISTERED
    if _REGISTERED:
        return
    for name, f in ((SANS, "DejaVuSans.ttf"), (BOLD, "DejaVuSans-Bold.ttf"), (CJK, "DroidSansFallbackFull.ttf")):
        pdfmetrics.registerFont(TTFont(name, str(FONTS / f)))
    pdfmetrics.registerFontFamily(SANS, normal=SANS, bold=BOLD, italic=SANS, boldItalic=BOLD)
    _REGISTERED = True


def esc(s) -> str:
    return str(s if s is not None else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def mk(s) -> str:
    """Text for a Paragraph: escaped, Chinese characters switched to the CJK font."""
    return _CJK.sub(lambda m: f'<font name="{CJK}">{m.group(1)}</font>', esc(s))


def _col(h: str):
    return colors.HexColor(h)


def _runs(text: str, bold: bool = False) -> list[tuple[str, str]]:
    """The text cut into runs, each with the font that has its characters (Chinese -> CJK, everything else DejaVu)."""
    out = []
    for part in _CJK.split(str(text)):
        if part:
            out.append((CJK if _CJK.fullmatch(part) else (BOLD if bold else SANS), part))
    return out


def stringWidth(text, font=SANS, size=9.0) -> float:
    return sum(_sw(chunk, f if f == CJK else (BOLD if font == BOLD else SANS), size) for f, chunk in _runs(text))


def String(x, y, text, fontName=SANS, fontSize=9, fillColor=None, textAnchor="start"):
    """A string for a Drawing that may mix Latin and Chinese: one piece per font run (placed one after the other)."""
    g = Group()
    runs = _runs(text, fontName == BOLD)
    total = sum(_sw(c, f, fontSize) for f, c in runs)
    cx = x - (total if textAnchor == "end" else total / 2 if textAnchor == "middle" else 0)
    for f, c in runs:
        g.add(_String(cx, y, c, fontName=f, fontSize=fontSize, fillColor=fillColor))
        cx += _sw(c, f, fontSize)
    return g


def _tokens(text: str) -> list[str]:
    return re.findall(r"[^\s\u2e80-\u9fff\uf900-\ufaff\uff00-\uffef\u3000-\u303f]+\s*|[\u2e80-\u9fff\uf900-\ufaff\uff00-\uffef\u3000-\u303f]\s*|\s+", str(text))


def simpleSplit(text, font, size, width) -> list[str]:
    lines, cur = [], ""
    for tk in _tokens(text):
        if cur and stringWidth(cur + tk.rstrip(), font, size) > width:
            lines.append(cur.rstrip())
            cur = tk
        else:
            cur += tk
    if cur.strip():
        lines.append(cur.rstrip())
    return lines


def _mix(h: str, amount: float) -> str:
    """The colour lightened towards white (amount 0 = same, 1 = white)."""
    c = _col(h)
    return "#%02x%02x%02x" % tuple(int(255 * (v + (1 - v) * amount)) for v in (c.red, c.green, c.blue))


def _ramp(t: float) -> str:
    t = max(0.0, min(1.0, t)) * (len(SEQ) - 1)
    i = min(int(t), len(SEQ) - 2)
    a, b = _col(SEQ[i]), _col(SEQ[i + 1])
    f = t - i
    return "#%02x%02x%02x" % tuple(int(255 * (x + (y - x) * f)) for x, y in ((a.red, b.red), (a.green, b.green), (a.blue, b.blue)))


def _font_for(text: str, bold=False) -> str:
    return CJK if _CJK.search(text or "") else (BOLD if bold else SANS)


def _fit(text: str, width: float, size: float, bold=False) -> str:
    """A string that fits ``width`` (cut with an ellipsis); works for Latin and Chinese text."""
    s = str(text)
    f = _font_for(s, bold)
    if stringWidth(s, f, size) <= width:
        return s
    while s and stringWidth(s + "…", f, size) > width:
        s = s[:-1]
    return s + "…"


def _wrap(text: str, width: float, size: float, lines: int = 2, bold=False) -> list[str]:
    f = _font_for(text, bold)
    out = simpleSplit(str(text), f, size, width)
    if len(out) > lines:
        out = out[:lines]
        out[-1] = _fit(out[-1] + "…", width, size, bold)
    return out or [""]


# ------------------------------------------------------------------ styles
def _styles() -> dict[str, ParagraphStyle]:
    def st(name, size=9.5, lead=None, color=INK, font=SANS, **kw):
        return ParagraphStyle(name, fontName=font, fontSize=size, leading=lead or size * 1.38, textColor=_col(color), **kw)
    return {
        "body": st("body"), "small": st("small", 8.5, color=MUTED), "muted": st("muted", 8.5, color=MUTED),
        "kicker": st("kicker", 9, color=ACCENT, font=BOLD, spaceAfter=6),
        "h1": st("h1", 28, 33, font=BOLD, spaceAfter=6), "biz": st("biz", 16, 20, color=INK2, spaceAfter=18),
        "meta": st("meta", 10.5, 15, color=INK2), "metab": st("metab", 11, 16, font=BOLD),
        "h2": st("h2", 17, 21, font=BOLD, spaceAfter=4), "h3": st("h3", 12.5, 16, font=BOLD, spaceBefore=10, spaceAfter=3, keepWithNext=1),
        "h3u": st("h3u", 8.5, 12, color=INK2, font=BOLD, spaceBefore=10, spaceAfter=3, keepWithNext=1),
        "h4": st("h4", 10.5, 13.5, font=BOLD, keepWithNext=1), "desc": st("desc", 8.5, 11.5, color=MUTED, keepWithNext=1),
        "head": st("head", 13.5, 18.5, font=BOLD), "msg": st("msg", 9.5, 13, leftIndent=12, bulletIndent=0),
        "say": st("say", 9.5, 13, color=INK2), "note": st("note", 9, 12, color=WATCH),
        "tile_t": st("tile_t", 8, 10.5, color=INK2), "tile_v": st("tile_v", 14, 17, font=BOLD),
        "tile_d": st("tile_d", 8, 10.5, color=MUTED),
        "cell": st("cell", 8.2, 10.5), "cellr": st("cellr", 8.2, 10.5, alignment=TA_RIGHT),
        "th": st("th", 8, 10.5, color=INK2, font=BOLD), "thr": st("thr", 8, 10.5, color=INK2, font=BOLD, alignment=TA_RIGHT),
        "story": st("story", 10, 14.5, spaceAfter=5), "foot": st("foot", 8, 11, color=MUTED),
        "ax": st("ax", 9.5, 13, font=BOLD), "why": st("why", 8.8, 12, color=MUTED), "fx": st("fx", 9, 12.5, color=INK2),
        "mark": st("mark", 8.5, 11.5, color=INK2, leftIndent=14), "toc": st("toc", 10.5, 15),
        "tocs": st("tocs", 8.5, 11, color=INK2, leftIndent=0),
    }


# ------------------------------------------------------------------ charts (ReportLab graphics, vector)
def _ticks(lo: float, hi: float, n: int = 4) -> list[float]:
    if hi <= lo:
        hi = lo + 1
    raw = (hi - lo) / n
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    start = math.floor(lo / step) * step
    out, v = [], start
    while v <= hi + step * 0.999 and len(out) < 12:
        out.append(round(v, 10))
        v += step
    return out


class Charts:
    """Draws chart blocks as ReportLab Drawings. ``W`` is the width in points."""

    def __init__(self, r: _R, W: float):
        self.r, self.W = r, W
        self.lg, self.cur = r.lg, r.cur

    def fmt(self, v, kind, compact=True) -> str:
        return fmt_value(v, kind, self.lg, self.cur, compact=compact)

    # ---------------------------------------------------------------- dispatch
    def draw(self, b: dict, marks: dict | None) -> Drawing:
        k, res = b["kpi_public"], b["result"]
        if res.get("type") == "timeseries":
            return self.timeseries(k, res, marks)
        if res.get("type") == "heatmap":
            return self.heatmap(k, res)
        return self.breakdown(k, res, marks)

    # ---------------------------------------------------------------- time series
    def timeseries(self, k, r, marks) -> Drawing:
        W, ser = self.W, r["series"]
        xs = [fmt_bucket(x, r.get("grain") or "day", self.lg) for x in r["x"]]
        n = len(xs)
        split = any(s.get("axis") == 1 for s in ser)
        panels = [[(i, s) for i, s in enumerate(ser) if (s.get("axis") == 1) == bool(p)] for p in ((0, 1) if split else (0,))]
        panels = [p for p in panels if p]
        legend_on = len(ser) > 1
        top = 26 if legend_on else 8
        if marks and marks.get("an"):
            top += 12
        heights = [150, 80] if len(panels) == 2 else [165]
        H = top + sum(heights) + (len(panels) - 1) * 26 + 34
        d = Drawing(W, H)
        pal = {i: PAL[i % 8] for i in range(len(ser))}
        # legend
        lx = 0
        if legend_on:
            for i, s in enumerate(ser):
                name = s.get("name") or k["title"]
                d.add(Rect(lx, H - 12, 10, 6, fillColor=_col(pal[i]), strokeColor=None))
                d.add(String(lx + 14, H - 11, name, fontName=_font_for(name), fontSize=8, fillColor=_col(INK2)))
                lx += 14 + stringWidth(name, _font_for(name), 8) + 14
        y_cursor = H - top
        left = 0
        axis_w = {}
        for pi, p in enumerate(panels):          # room for the value labels of each panel
            vals = [v for _, s in p for v in s["data"] if v is not None]
            lo, hi = (min(vals + [0]), max(vals + [0])) if vals else (0, 1)
            if any(s.get("type") == "bar" for _, s in p) or any(s.get("stack") for _, s in p):
                lo = min(lo, 0)
            if any(s.get("stack") for _, s in p):
                tot = [sum((s["data"][j] or 0) for _, s in p if s.get("stack")) for j in range(n)]
                hi = max(hi, max(tot) if tot else hi)
            ticks = _ticks(lo, hi, 4 if heights[pi] > 100 else 2)
            kind = p[0][1].get("fmt") or k.get("fmt")
            axis_w[pi] = (ticks, kind, max(stringWidth(self.fmt(t, kind), SANS, 7.5) for t in ticks))
            left = max(left, axis_w[pi][2] + 8)
        pw = W - left - 6
        slot = pw / max(n, 1)
        px = lambda j: left + slot * (j + 0.5)   # noqa: E731
        pts_by_series: dict[int, list] = {}
        for pi, p in enumerate(panels):
            ticks, kind, _ = axis_w[pi]
            h, ytop = heights[pi], y_cursor
            y0 = ytop - h
            tmin, tmax = ticks[0], ticks[-1]
            py = lambda v: y0 + (v - tmin) / ((tmax - tmin) or 1) * h   # noqa: E731
            for t in ticks:
                d.add(Line(left, py(t), left + pw, py(t), strokeColor=_col(GRID), strokeWidth=0.5))
                d.add(String(left - 4, py(t) - 2.5, self.fmt(t, kind), fontName=SANS, fontSize=7.5, fillColor=_col(AXIS), textAnchor="end"))
            if tmin < 0 < tmax:
                d.add(Line(left, py(0), left + pw, py(0), strokeColor=_col(BASE), strokeWidth=0.8))
            stack_base = [0.0] * n
            nbar = sum(1 for _, s in p if s.get("type") == "bar" and not s.get("stack"))
            bi = 0
            for i, s in p:
                data, c = s["data"], pal[i]
                if s.get("type") == "bar":
                    bw = min(14.0, slot * 0.72 / max(nbar, 1)) if not s.get("stack") else min(16.0, slot * 0.72)
                    for j, v in enumerate(data):
                        if v is None:
                            continue
                        if s.get("stack"):
                            b0, b1 = py(stack_base[j]), py(stack_base[j] + v)
                            stack_base[j] += v
                            x = px(j) - bw / 2
                        else:
                            b0, b1 = py(0 if tmin <= 0 else tmin), py(v)
                            x = px(j) - nbar * bw / 2 + bi * bw
                        d.add(Rect(x, min(b0, b1), bw, abs(b1 - b0) or 0.5, fillColor=_col(c), strokeColor=_col("#fcfcfb") if s.get("stack") else None, strokeWidth=0.4))
                        pts_by_series.setdefault(i, [None] * n)[j] = (px(j), max(b0, b1) if v >= 0 else min(b0, b1))
                    if not s.get("stack"):
                        bi += 1
                else:
                    seg: list[float] = []
                    pts = [None] * n
                    for j, v in enumerate(data):
                        if v is None:
                            if len(seg) >= 4:
                                d.add(PolyLine(seg, strokeColor=_col(c), strokeWidth=1.6, strokeLineJoin=1))
                            seg = []
                            continue
                        seg += [px(j), py(v)]
                        pts[j] = (px(j), py(v))
                        if n <= 2 or len(seg) == 2:
                            pass
                    if len(seg) >= 4:
                        d.add(PolyLine(seg, strokeColor=_col(c), strokeWidth=1.6, strokeLineJoin=1))
                    if n <= 31:
                        for pt in pts:
                            if pt:
                                d.add(Circle(pt[0], pt[1], 1.8 if n > 2 else 3.2, fillColor=_col(c), strokeColor=_col("#fcfcfb"), strokeWidth=0.6))
                    pts_by_series[i] = pts
                # the dashed straight trend (series that ask for it; same rule as the page: more than 5 points)
                if s.get("trend"):
                    vv = [(j, v) for j, v in enumerate(data) if v is not None]
                    if len(vv) > 5:
                        mx, my = sum(j for j, _ in vv) / len(vv), sum(v for _, v in vv) / len(vv)
                        den = sum((j - mx) ** 2 for j, _ in vv) or 1
                        sl = sum((j - mx) * (v - my) for j, v in vv) / den
                        a, bq = vv[0][0], vv[-1][0]
                        ya, yb = my + sl * (a - mx), my + sl * (bq - mx)
                        d.add(Line(px(a), py(max(min(ya, tmax), tmin)), px(bq), py(max(min(yb, tmax), tmin)), strokeColor=_col(INK2),
                                   strokeWidth=1.1, strokeDashArray=[3, 2]))
            if pi == 0 or True:
                if len(panels) == 2 and pi == 0:
                    pass
            # x labels under the last panel (or under each: only the last)
            if pi == len(panels) - 1:
                maxw = max((stringWidth(x, SANS, 7.5) for x in xs), default=20)
                every = max(1, math.ceil((maxw + 8) / max(slot, 1)))
                for j in range(0, n, every):
                    d.add(String(px(j), y0 - 11, xs[j], fontName=SANS, fontSize=7.5, fillColor=_col(AXIS), textAnchor="middle"))
            if len(panels) == 2 and pi == 1:
                names = " / ".join(s.get("name") or "" for _, s in p)
                d.add(String(left, ytop + 5, names, fontName=_font_for(names), fontSize=8, fillColor=_col(MUTED)))
            y_cursor = y0 - 26
        # numbered unusual points (dark disc, white number; above a high point, below a low one)
        for m in (marks or {}).get("an", []):
            pt = (pts_by_series.get(m["k"]) or [None] * n)[m["i"]] if m["i"] < n else None
            if not pt:
                continue
            dy = 11 if m["dir"] == "up" else -11
            d.add(Circle(pt[0], pt[1] + dy, 6.5, fillColor=_col(INK), strokeColor=_col("#fcfcfb"), strokeWidth=1))
            d.add(String(pt[0], pt[1] + dy - 2.8, str(m["n"]), fontName=BOLD, fontSize=8, fillColor=colors.white, textAnchor="middle"))
        if any(s.get("trend") and len([v for v in s["data"] if v is not None]) > 5 for s in ser):
            t = self.r.T("linear_label")
            d.add(Line(W - 20 - stringWidth(t, _font_for(t), 7.5) - 4, 5, W - 20 - stringWidth(t, _font_for(t), 7.5) + 8, 5,
                       strokeColor=_col(INK2), strokeWidth=1.1, strokeDashArray=[3, 2]))
            d.add(String(W - 20 - stringWidth(t, _font_for(t), 7.5) + 12, 2.5, t, fontName=_font_for(t), fontSize=7.5, fillColor=_col(MUTED)))
        return d

    # ---------------------------------------------------------------- breakdowns
    def breakdown(self, k, r, marks) -> Drawing:
        items = list(r["items"])
        viz = k.get("viz")
        fset = set((marks or {}).get("focus") or [])
        dim = lambda label: bool(fset) and label not in fset   # noqa: E731
        if viz == "donut" and (len(items) <= 2 or len(items) > 6):
            if len(items) > 12:
                rest = sum((i["value"] or 0) for i in items[5:])
                items = items[:5] + [{"key": "__other", "label": self.r.T("other"), "value": rest}]
                viz = "donut"
            else:
                viz = "hbar"
        kind = k.get("fmt")
        if viz == "donut":
            return self.donut(items, kind, dim, fset)
        if viz == "bar" and len(items) <= 12:
            return self.vbars(items, kind, dim, fset)
        return self.hbars(items, kind, dim, fset)

    def hbars(self, items, kind, dim, fset) -> Drawing:
        W = self.W
        lab_w = min(W * 0.36, 170.0)
        rowh = 26.0
        H = len(items) * rowh + 8
        d = Drawing(W, H)
        vmax = max([i["value"] or 0 for i in items] + [1e-9])
        bar_w = W - lab_w - 70
        for n, it in enumerate(items):
            y = H - 4 - (n + 1) * rowh
            lines = _wrap(str(it["label"]), lab_w - 8, 8.3)
            for li, t in enumerate(lines):
                yy = y + rowh / 2 + (len(lines) - 1) * 4.5 - li * 9 - 2.5
                d.add(String(lab_w - 6, yy, t, fontName=_font_for(t), fontSize=8.3, fillColor=_col(INK2), textAnchor="end"))
            v = it["value"] or 0
            w = max(0.5, v / vmax * bar_w) if vmax > 0 else 0.5
            dm = dim(it["label"])
            d.add(Rect(lab_w, y + 5, w, rowh - 10, fillColor=_col(_mix(PAL[0], 0.62) if dm else PAL[0]),
                       strokeColor=_col(INK) if fset and not dm else None, strokeWidth=1.2))
            d.add(String(lab_w + w + 4, y + rowh / 2 - 2.5, self.fmt(v, kind), fontName=SANS, fontSize=8, fillColor=_col(INK2)))
        return d

    def vbars(self, items, kind, dim, fset) -> Drawing:
        W, H = self.W, 190.0
        n = len(items)
        left, bottom, top = 8.0, 44.0, 16.0
        vmax = max([i["value"] or 0 for i in items] + [1e-9])
        d = Drawing(W, H)
        slot = (W - left - 6) / n
        bw = min(34.0, slot * 0.62)
        for j, it in enumerate(items):
            v = it["value"] or 0
            h = v / vmax * (H - bottom - top)
            x = left + slot * j + (slot - bw) / 2
            dm = dim(it["label"])
            d.add(Rect(x, bottom, bw, max(h, 0.5), fillColor=_col(_mix(PAL[0], 0.62) if dm else PAL[0]),
                       strokeColor=_col(INK) if fset and not dm else None, strokeWidth=1.2))
            d.add(String(x + bw / 2, bottom + h + 3, self.fmt(v, kind), fontName=SANS, fontSize=7.5, fillColor=_col(INK2), textAnchor="middle"))
            for li, t in enumerate(_wrap(str(it["label"]), slot - 2, 7.5, 3)):
                d.add(String(left + slot * j + slot / 2, bottom - 11 - li * 9, t, fontName=_font_for(t), fontSize=7.5,
                             fillColor=_col(INK2), textAnchor="middle"))
        d.add(Line(left, bottom, W - 6, bottom, strokeColor=_col(BASE), strokeWidth=0.8))
        return d

    def donut(self, items, kind, dim, fset) -> Drawing:
        W = self.W
        total = sum((i["value"] or 0) for i in items) or 1
        longest = max(len(str(i["label"])) for i in items)
        below = longest > 18
        R, r0 = 64.0, 36.0
        rows = len(items)
        if below:
            H = 2 * R + 14 + rows * 14 + 6
            cx, cy = W / 2, H - R - 6
            lx, ly = 8.0, cy - R - 14
        else:
            H = max(2 * R + 12, rows * 16 + 12)
            cx, cy = R + 12, H / 2
            lx, ly = cx + R + 40, H / 2 + (rows - 1) * 8
        d = Drawing(W, H)
        ang = 90.0
        for n, it in enumerate(items):
            v = it["value"] or 0
            sweep = 360.0 * v / total
            c = AXIS if it.get("key") == "__other" else PAL[n % 8]
            if dim(it["label"]):
                c = _mix(c, 0.6)
            if sweep > 0.05:
                w = Wedge(cx, cy, R, ang - sweep, ang, radius1=r0, fillColor=_col(c), strokeColor=_col("#fcfcfb"), strokeWidth=1.2)
                d.add(w)
            ang -= sweep
            y = ly - n * (14 if below else 16)
            d.add(Rect(lx, y - 1, 10, 7, fillColor=_col(c), strokeColor=None))
            pct = f"{round(100 * v / total)}%"
            label = str(it["label"]) if it.get("label_key") else f"{it['label']}  {pct}"
            label = _fit(label, W - lx - 20, 8.3)
            d.add(String(lx + 15, y, label, fontName=_font_for(label), fontSize=8.3, fillColor=_col(INK2)))
        return d

    # ---------------------------------------------------------------- heat map
    def heatmap(self, k, r) -> Drawing:
        W = self.W
        xl = [str(x) for x in r["x"]]
        if k["id"] == "hourly_heatmap":
            xl = [f"{int(float(x)):02d}h" for x in r["x"]]
        elif k["id"] == "cohort_retention":
            xl = [f"M{x}" for x in r["x"]]
        yl = [fmt_bucket(y, "month", self.lg) if re.fullmatch(r"\d{4}-\d{2}", y) else y for y in r["y"]]
        vals = [c[2] for c in r["cells"]]
        lo, hi = min(vals), max(vals)
        left = min(max(stringWidth(y, _font_for(y), 8) for y in yl) + 8, W * 0.3)
        cw = (W - left - 4) / max(len(xl), 1)
        ch = min(22.0, max(14.0, 230.0 / max(len(yl), 1)))
        H = len(yl) * ch + 54
        d = Drawing(W, H)
        top = H - 6
        cells = {(c[0], c[1]): c[2] for c in r["cells"]}
        for yi, y in enumerate(yl):
            yy = top - (yi + 1) * ch
            d.add(String(left - 5, yy + ch / 2 - 2.5, _fit(y, left - 8, 8), fontName=_font_for(y), fontSize=8, fillColor=_col(INK2), textAnchor="end"))
            for xi in range(len(xl)):
                v = cells.get((xi, yi))
                if v is None:
                    continue
                t = (v - lo) / ((hi - lo) or 1)
                d.add(Rect(left + xi * cw + 0.8, yy + 0.8, cw - 1.6, ch - 1.6, fillColor=_col(_ramp(t)), strokeColor=None))
                if cw >= 30 and ch >= 14:
                    txt = self.fmt(v, k.get("fmt"))
                    d.add(String(left + xi * cw + cw / 2, yy + ch / 2 - 2.5, txt, fontName=SANS, fontSize=6.8,
                                 fillColor=colors.white if t > 0.5 else _col(INK), textAnchor="middle"))
        every = max(1, math.ceil((max(stringWidth(x, SANS, 7.5) for x in xl) + 4) / cw))
        for xi in range(0, len(xl), every):
            d.add(String(left + xi * cw + cw / 2, top - len(yl) * ch - 11, xl[xi], fontName=SANS, fontSize=7.5, fillColor=_col(AXIS), textAnchor="middle"))
        gx, gy, gw = (W - 160) / 2, 8.0, 160.0           # the colour scale
        for i in range(40):
            d.add(Rect(gx + i * gw / 40, gy + 12, gw / 40 + 0.4, 6, fillColor=_col(_ramp(i / 39)), strokeColor=None))
        d.add(String(gx - 4, gy + 12, self.fmt(lo, k.get("fmt")), fontName=SANS, fontSize=7.5, fillColor=_col(AXIS), textAnchor="end"))
        d.add(String(gx + gw + 4, gy + 12, self.fmt(hi, k.get("fmt")), fontName=SANS, fontSize=7.5, fillColor=_col(AXIS)))
        return d


def spark(pts, w=110.0, h=22.0):
    vals = [float(p[1]) for p in (pts or []) if p and p[1] is not None]
    if len(vals) < 2:
        return None
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or 1
    xy = []
    for i, v in enumerate(vals):
        xy += [i * w / (len(vals) - 1), 2 + (v - lo) / rng * (h - 4)]
    d = Drawing(w, h)
    d.add(PolyLine(xy, strokeColor=_col(ACCENT), strokeWidth=1.3))
    return d


# ------------------------------------------------------------------ the document
class _Builder:
    def __init__(self, doc: dict, ui: dict, locale: str, width: float):
        self.doc, self.ui, self.width = doc, ui, width
        self.r = _R(doc, ui)
        self.S = _styles()
        self.charts = Charts(self.r, width)
        self.lg = self.r.lg

    # ---- small helpers
    def P(self, text, style="body", **kw) -> Paragraph:
        return Paragraph(mk(text), self.S[style])

    def PM(self, markup: str, style="body") -> Paragraph:        # markup already escaped / tagged
        return Paragraph(markup, self.S[style])

    def delta(self, m: dict) -> tuple[str, str]:
        fmt = m.get("fmt")
        d = m.get("delta_pts") if fmt == "pct" else m.get("delta_pct")
        if d is None:
            return "", MUTED
        txt = fmt_arg({"v": d, "fmt": "signed_pts" if fmt == "pct" else "signed_pct"}, self.lg, self.r.cur)
        if abs(d) < 0.05 or m.get("good") not in ("up", "down"):
            return txt, MUTED
        return txt, GOOD if (d > 0) == (m.get("good") != "down") else BAD

    def tile(self, title: str, m: dict, w: float):
        txt, color = self.delta(m)
        cmp = self.r.T("rpt_vs", value=m["compare_text"]) if m.get("compare_text") else ""
        parts = [self.P(title, "tile_t"), self.PM(mk(m.get("value_text")), "tile_v")]
        line = (f'<font color="{color}">{mk(txt)}</font> ' if txt else "") + mk(cmp)
        if line.strip():
            parts.append(self.PM(line, "tile_d"))
        sp = spark(m.get("spark"), min(110.0, w - 18))
        if sp:
            parts += [Spacer(1, 3), sp]
        return parts

    def tiles(self, tiles: list[dict], per_row: int = 3):
        w = (self.width - 6 * (per_row - 1)) / per_row
        rows = []
        for i in range(0, len(tiles), per_row):
            row = [self.tile(t["title"], t, w) for t in tiles[i:i + per_row]]
            row += [""] * (per_row - len(row))
            rows.append(row)
        t = Table(rows, colWidths=[w] * per_row, hAlign="LEFT")
        t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 8),
                               ("RIGHTPADDING", (0, 0), (-1, -1), 6), ("TOPPADDING", (0, 0), (-1, -1), 6),
                               ("BOTTOMPADDING", (0, 0), (-1, -1), 7)]
                              + [("BOX", (c, rw), (c, rw), 0.6, _col(BASE)) for rw, row in enumerate(rows) for c, cell in enumerate(row) if cell != ""]))
        # gaps between tiles: draw each tile in its own padded box by using a spacing column trick
        return t

    def table(self, cols: list[dict], rows: list[dict], width: float | None = None):
        width = width or self.width
        def num(c):
            return c.get("fmt") in NUMERIC
        head = [Paragraph(mk(c.get("label")), self.S["thr" if num(c) else "th"]) for c in cols]
        body = [[Paragraph(mk(self.r.v(r.get(c["key"]), c.get("fmt") or "text")), self.S["cellr" if num(c) else "cell"]) for c in cols] for r in rows]
        # column widths from the content: numbers and short labels stay narrow, long text gets the rest
        wts = []
        for c, i in zip(cols, range(len(cols))):
            longest = max([len(str(self.r.v(r.get(c["key"]), c.get("fmt") or "text"))) for r in rows] + [len(str(c.get("label") or ""))])
            wts.append(min(max(longest, 6), 46) if not num(c) else min(max(longest, 7), 16))
        tot = sum(wts)
        t = Table([head] + body, colWidths=[width * w / tot for w in wts], repeatRows=1, hAlign="LEFT")
        t.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, 0), 0.8, _col(BASE)), ("LINEBELOW", (0, 1), (-1, -1), 0.4, _col(GRID)),
                               ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 3), ("RIGHTPADDING", (0, 0), (-1, -1), 3),
                               ("TOPPADDING", (0, 0), (-1, -1), 2.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5)]))
        return t

    def badge(self, text: str, kind: str) -> str:
        fg, bg = {"urgent": (BAD, BAD_SOFT), "high": (BAD, BAD_SOFT), "watch": (WATCH, WATCH_SOFT), "medium": (WATCH, WATCH_SOFT),
                  "low": (ACCENT, ACCENT_SOFT), "new": (ACCENT, ACCENT_SOFT)}.get(kind, (INK2, SURF2))
        return f'<font name="{BOLD}" size="7.5" color="{fg}" backColor="{bg}">&nbsp;{mk(text)}&nbsp;</font>&nbsp;'

    def findings(self, b: dict) -> list:
        if not b["items"]:
            return [self.P(self.r.T("rpt_no_findings"), "muted")]
        out = []
        for f in b["items"]:
            kind = f.get("severity") or f.get("side") or ""
            bd = ""
            if f.get("severity"):
                bd += self.badge(self.r.T("sev_" + f["severity"]), f["severity"])
            if f.get("is_new"):
                bd += self.badge(self.r.T("rpt_new"), "new")
            inner = [self.PM(bd + f'<font name="{BOLD}">{mk(f["title"])}</font>', "body"), self.P(f["text"], "fx")]
            if f.get("rows") and f.get("columns"):
                inner.append(self.table(f["columns"], f["rows"], self.width - 28))
                if f.get("total_rows", 0) > len(f["rows"]):
                    inner.append(self.P(self.r.T("rpt_more_rows", n=f["total_rows"] - len(f["rows"])), "muted"))
            line = {"positive": GOOD, "negative": BAD, "urgent": BAD, "watch": PAL[3]}.get(kind, BASE)
            t = Table([[inner]], colWidths=[self.width - 6], hAlign="RIGHT")
            t.setStyle(TableStyle([("LINEBEFORE", (0, 0), (0, -1), 3, _col(line)), ("BOX", (0, 0), (-1, -1), 0.4, _col(GRID)),
                                   ("LEFTPADDING", (0, 0), (-1, -1), 8), ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))
            out += [KeepTogether([t]), Spacer(1, 4)]
        return out

    def actions_block(self, b: dict) -> list:
        if not b["items"]:
            return [self.P(self.r.T("rpt_no_actions"), "muted")]
        out = []
        for i, a in enumerate(b["items"], 1):
            head = f'{i}.&nbsp;' + self.badge(a["priority_text"], a["priority"]) + f' <font color="{MUTED}" size="8">{mk(a["timeline_text"])}</font>'
            out.append(KeepTogether([self.PM(head), self.P(a["text"], "ax"), self.P(self.r.T("rpt_because", why=a["because"]), "why"), Spacer(1, 5)]))
        return out

    def mark_notes(self, b: dict, an: list[dict], focus: list[str]) -> list:
        res, kpi = b["result"], b.get("kpi_public") or {}
        grain = res.get("grain") or "day"
        out = []
        for a in an:
            s = res["series"][a["series"]]
            kind = s.get("fmt") or kpi.get("fmt")
            txt = self.r.T("story_unusual_" + a["dir"], when=fmt_bucket(a["x"], grain, self.lg),
                           value=self.r.v(a["value"], kind, compact=True), typical=self.r.v(a["typical"], kind, compact=True),
                           pct=self.r.v(abs(a["pct"]), "pct"))
            out.append(self.PM(f'<font name="{BOLD}">{a["n"]}</font>&nbsp; {mk(txt)}', "mark"))
        if focus:
            out.append(self.P(self.r.T("story_highlighted", items=", ".join(focus)), "mark"))
        return out

    def block(self, b: dict, say: bool = True, texts: list[str] | None = None, first_head: list | None = None) -> list:
        S, typ = self.S, b.get("type")
        head = list(first_head or []) + [self.P(b["title"], "h4")] + ([self.P(b["desc"], "desc")] if b.get("desc") else [])
        body: list = []
        if typ == "tiles":
            body = [Spacer(1, 4), self.tiles(b["tiles"], 3)]
        elif typ == "metric":
            txt, color = self.delta(b)
            cmp = self.r.T("rpt_vs", value=b["compare_text"]) if b.get("compare_text") else ""
            line = (f'<font name="{BOLD}" size="17">{mk(b.get("value_text"))}</font>&nbsp;&nbsp;'
                    + (f'<font color="{color}">{mk(txt)}</font>&nbsp;' if txt else "") + f'<font color="{MUTED}" size="8">{mk(cmp)}</font>')
            body = [Spacer(1, 4), self.PM(line)]
            sp = spark(b.get("spark"), 140, 26)
            if sp:
                body += [Spacer(1, 3), sp]
            body.append(Spacer(1, 3))
        elif typ == "chart":
            marks, an, focus = marks_for(b, texts) if self.r.story_layout else (None, [], [])
            body = [Spacer(1, 4), self.charts.draw(b, marks)] + self.mark_notes(b, an, focus)
        elif typ == "table":
            body = [Spacer(1, 4), self.table(b["columns"], b["rows"])]
            if b.get("truncated"):
                body.append(self.P(self.r.T("rpt_more_rows", n=b["total_rows"] - len(b["rows"])), "muted"))
        elif typ == "findings":
            body = self.findings(b)
        elif typ == "actions":
            body = self.actions_block(b)
        else:
            body = [self.P(b.get("text"), "muted")]
        tail = []
        if say and b.get("text") and typ in ("chart", "metric"):
            t = Table([[self.P(b["text"], "say")]], colWidths=[self.width - 4], hAlign="RIGHT")
            t.setStyle(TableStyle([("LINEBEFORE", (0, 0), (0, -1), 2.5, _col(ACCENT)), ("LEFTPADDING", (0, 0), (-1, -1), 8),
                                   ("TOPPADDING", (0, 0), (-1, -1), 1), ("BOTTOMPADDING", (0, 0), (-1, -1), 1)]))
            tail = [Spacer(1, 4), t]
        if b.get("note") and typ == "metric":
            tail.append(self.P(b["note"], "muted"))
        small = typ in ("chart", "metric", "tiles")
        if small:
            return [KeepTogether(head + body + tail), Spacer(1, 10)]
        return head[:2] + body + tail + [Spacer(1, 10)] if len(head) > 1 else head + body + tail + [Spacer(1, 10)]

    # ---- page pieces
    def since_lines(self, chapter: str | None):
        ch = self.doc.get("changes")
        if not ch or not (ch.get("items") or ch.get("steady")):
            return []
        items = ch.get("items") or []
        if chapter is None:
            lines = [items[i] for i in ch.get("page1") or [] if 0 <= i < len(items)]
        else:
            lines = [i for i in items if i.get("chapter") == chapter]
        if not lines and ch.get("steady") and chapter in (None, "happened"):
            lines = [{"tone": "info", "text": ch.get("steady_text") or ""}]
        return [x for x in lines if x.get("text")]

    def bullets(self, lines: list[dict]) -> list:
        return [Paragraph(f'<bullet><font color="{TONE.get(x.get("tone") or "", MUTED)}">&bull;</font></bullet>{mk(x["text"])}', self.S["msg"]) for x in lines]

    def since_box(self, chapter: str | None = None) -> list:
        lines = self.since_lines(chapter)
        if not lines:
            return []
        title = self.r.T("chg_title")
        if chapter is not None:
            return [self.P(title, "h4"), *self.bullets(lines), Spacer(1, 6)]
        p = (self.doc.get("changes") or {}).get("prev") or {}
        try:
            day = fmt_bucket(date.fromtimestamp(float(p.get("generated_at"))).isoformat(), "day", self.lg)
        except (TypeError, ValueError):
            day = ""
        return [self.P(title, "h3u"), self.P(self.r.T("chg_sub", date=day, period=p.get("period_text") or ""), "muted"), *self.bullets(lines)]

    def cover(self) -> list:
        cv = self.doc["cover"]
        out = [Spacer(1, 90), self.P("Veridex BI", "kicker"), self.P(cv["title"], "h1"), self.P(cv["business"], "biz"),
               self.P(cv["period_text"], "metab")]
        for x in (cv["compare_text"], cv["data_text"], cv["generated_text"]):
            if x:
                out.append(self.P(x, "meta"))
        for key in ("past_note", "data_note", "story_note"):
            if cv.get(key):
                t = Table([[self.P(cv[key], "note")]], colWidths=[self.width], hAlign="LEFT")
                t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), _col(WATCH_SOFT)), ("LEFTPADDING", (0, 0), (-1, -1), 8),
                                       ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))
                out += [Spacer(1, 12), t]
        return out

    def glance(self) -> list:
        s = self.doc.get("summary")
        if not s or not (s.get("headline") or s.get("messages") or s.get("figures")):
            return []
        h = s.get("headline") or {}
        st = self.r.story()
        if st:
            s = dict(s)
            h = {**h, "text": st.get("headline") or h.get("text")}
            tone = {"warning": "warn"}
            if st.get("key_messages"):
                s["messages"] = [{"text": m["text"], "tone": tone.get(m.get("tone"), m.get("tone"))} for m in st["key_messages"]]
        out = [PageBreak(), self.P(s.get("title") or self.r.T("story_title"), "h2")]
        if h.get("text"):
            bar = {"good": (GOOD, GOOD_SOFT), "bad": (BAD, BAD_SOFT)}.get(h.get("tone") or "", (MUTED, SURF2))
            t = Table([[self.P(h["text"], "head")]], colWidths=[self.width], hAlign="LEFT")
            t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), _col(bar[1])), ("LINEBEFORE", (0, 0), (0, -1), 4, _col(bar[0])),
                                   ("LEFTPADDING", (0, 0), (-1, -1), 12), ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 9)]))
            out.append(t)
        if s.get("messages"):
            out += [self.P(self.r.T("story_messages"), "h3u"), *self.bullets(s["messages"])]
        out += self.since_box()
        if s.get("figures"):
            out += [self.P(self.r.T("story_figures"), "h3u"), self.tiles(s["figures"], 3)]
        if st and st.get("actions") and self.r.story_layout:
            out.append(self.P(self.r.T("story_actions"), "h3u"))
            for i, a in enumerate(st["actions"][:3], 1):
                who = " · ".join(x for x in (a.get("who"), a.get("when")) if x)
                out += [self.PM(f'{i}.&nbsp;<font name="{BOLD}">{mk(a["title"])}</font>'), *([self.P(who, "why")] if who else []), Spacer(1, 3)]
            out.append(self.P(self.r.T("story_plan_more"), "muted"))
        elif st and st.get("actions") and "actions" not in st["meta"].get("fallbacks", []):
            out.append(self.P(self.r.T("story_actions"), "h3u"))
            out += self.plan_items(st["actions"])
        elif s.get("actions"):
            out += [self.P(self.r.T("story_actions"), "h3u"), *self.actions_block({"items": s["actions"]})]
        if st:
            out += [Spacer(1, 6), self.P(self.r.T("story_ai_note"), "muted")]
        return out

    def plan_items(self, actions: list[dict]) -> list:
        out = []
        for i, a in enumerate(actions, 1):
            items = [self.PM(f'{i}.&nbsp;<font name="{BOLD}">{mk(a["title"])}</font>')]
            if a.get("why"):
                items.append(self.P(a["why"], "fx"))
            for x in a.get("steps") or []:
                items.append(Paragraph(f'<bullet>&bull;</bullet>{mk(x)}', self.S["msg"]))
            for key, v in (("story_who", a.get("who")), ("story_when", a.get("when")), ("story_effect", a.get("expected_effect"))):
                if v:
                    items.append(self.PM(f'<font color="{MUTED}">{mk(self.r.T(key))}:</font> {mk(v)}', "why"))
            out += [KeepTogether(items), Spacer(1, 6)]
        return out

    def diagnosis(self) -> list:
        st = self.r.story()
        if not st:
            return []
        out = []
        if st.get("root_causes"):
            out.append(self.P(self.r.T("story_root_causes"), "h3"))
            for i, rc in enumerate(st["root_causes"], 1):
                conf = rc.get("confidence") or "possible"
                out.append(KeepTogether([self.PM(f'{i}.&nbsp;{self.badge(self.r.T("story_conf_" + conf), "low")}<font name="{BOLD}">{mk(rc["cause"])}</font>'),
                                         self.P(rc.get("evidence") or "", "why"), Spacer(1, 4)]))
        for key, tkey in (("hidden_clues", "story_clues"), ("questions_to_check", "story_questions")):
            if st.get(key):
                out.append(self.P(self.r.T(tkey), "h3"))
                out += [Paragraph(f'<bullet>&bull;</bullet>{mk(c["text"] if isinstance(c, dict) else c)}', self.S["msg"]) for c in st[key]]
        return out

    def chapter(self, ch: dict) -> list:
        head = [self.P(ch["title"], "h3")] + ([self.P(ch["desc"], "desc")] if ch.get("desc") else [])
        blocks = ch["blocks"]
        out: list = []
        for i, b in enumerate(blocks):
            out += self.block(b, first_head=head if i == 0 else None)
        return out or head

    def figure(self, part, caption, texts, label) -> tuple[list, list[str]]:
        b = self.r.find_part(part)
        if not b or b.get("type") not in ("chart", "table", "metric", "tiles"):
            return [], []
        focus: list[str] = []
        if b.get("type") == "chart":
            focus = (marks_for(b, texts)[0] or {}).get("focus") or []
        items = self.block(b, say=False, texts=texts)
        if caption:
            lead = f"{label} · " if label else ""
            items = [*items[:-1], self.PM(f'<font name="{BOLD}">{mk(lead + self.r.T("story_what_to_see"))}</font> {mk(caption)}', "fx"), items[-1]]
        return items, focus

    def story_sections(self) -> tuple[list[tuple[str, str]], list]:
        st = self.r.story()
        chs = {c["id"]: c for c in st.get("chapters") or []}
        order = [x for x in ("happened", "why", "next", "do") if x in chs or (x == "do" and st.get("actions"))]
        toc, out = [], []
        for i, cid in enumerate(order, 1):
            ch = chs.get(cid) or {}
            title = self.r.T(f"rpt_layer_{cid}")
            plist = [p["text"] for p in ch.get("paragraphs") or []]
            figs, refs = [], {}
            for j, c in enumerate(ch.get("charts") or [], 1):
                label = self.r.T("story_fig", n=f"{i}.{j}")
                items, focus = self.figure(c.get("part"), c.get("caption"), plist + [c.get("caption") or ""], label)
                if not items:
                    continue
                figs += items
                hit = next((k for k, t in enumerate(plist) if any(f_ and _norm(f_) in _norm(t) for f_ in focus)), None)
                at = hit if hit is not None else min(j - 1, len(plist) - 1)
                if at >= 0:
                    refs.setdefault(at, []).append(label)
            paras = []
            for k, t in enumerate(plist):
                ref = "".join(f' <font color="{ACCENT}">({mk(self.r.T("story_see_fig", n=lb.split(" ")[-1]))})</font>' for lb in refs.get(k, []))
                paras.append(self.PM(mk(t) + ref, "story"))
            extra = self.diagnosis() if cid == "why" else (self.plan_items_wrap(st.get("actions") or []) if cid == "do" else [])
            toc.append((f"S{i}", title))
            out += [CondPageBreak(95 * mm), Spacer(1, 10), self.anchor_h2(f"S{i}", str(i), title), *paras, *self.since_box(cid), *figs, *extra]
        return toc, out

    def plan_items_wrap(self, actions) -> list:
        return [self.P(self.r.T("story_plan"), "h3"), *self.plan_items(actions)] if actions else []

    def anchor_h2(self, name: str, n: str, title: str) -> Paragraph:
        return self.PM(f'<a name="{name}"/><font color="{ACCENT}">{esc(n)}</font>&nbsp;&nbsp;{mk(title)}', "h2")

    def toc(self, entries: list[tuple[str, str, str]]) -> list:
        out = [Spacer(1, 14), self.P(self.r.T("rpt_contents"), "h3")]
        for i, (anchor, title, sub) in enumerate(entries, 1):
            out.append(self.PM(f'<a href="#{anchor}" color="{INK}">{mk(title)}</a>', "toc"))
            if sub:
                out.append(self.P(sub, "tocs"))
        return out

    def skipped(self) -> list:
        d = self.doc
        if not d.get("skipped"):
            return []
        groups: dict[str, list[str]] = {}
        for sk in d["skipped"]:
            groups.setdefault(sk["reason"], []).append(sk["title"])
        out = [PageBreak(), self.P(self.r.T("rpt_not_included"), "h3"), self.P(self.r.T("rpt_not_included_desc"), "desc"), Spacer(1, 4)]
        for reason, titles in sorted(groups.items(), key=lambda g: -len(g[1])):
            out.append(Paragraph(f'<bullet>&bull;</bullet><font name="{BOLD}">{mk(reason)}</font> — {mk(", ".join(titles))}', self.S["msg"]))
        return out

    def build(self) -> list:
        d, cv, r = self.doc, self.doc["cover"], self.r
        flow = self.cover() + self.glance()
        if r.story_layout:
            toc, sections = self.story_sections()
            entries = [(a, t, "") for a, t in toc]
            if d["layers"]:
                entries.append(("appendix", self.r.T("rpt_appendix"), " · ".join(ly["title"] for ly in d["layers"])))
            flow += self.toc(entries) + sections
            if d["layers"]:
                flow += [PageBreak(), self.anchor_h2("appendix", "", r.T("rpt_appendix")), self.P(r.T("rpt_appendix_desc"), "desc")]
                for i, ly in enumerate(d["layers"], 1):
                    flow += [CondPageBreak(60 * mm) if i == 1 else PageBreak(), Spacer(1, 8), self.anchor_h2(f"L{i}", f"A{i}", ly["title"])]
                    for ch in ly["chapters"]:
                        flow += self.chapter(ch)
        else:
            st = r.story()
            story_ids = [c["id"] for c in (st or {}).get("chapters") or [] if c.get("paragraphs")]
            by_id = {ly["id"]: ly for ly in d["layers"]}
            order = [x for x in ("happened", "why", "next", "do") if x in by_id or x in story_ids] + \
                [ly["id"] for ly in d["layers"] if ly["id"] not in ("happened", "why", "next", "do")]
            sections = [by_id.get(x) or {"id": x, "title": r.T(f"rpt_layer_{x}"), "chapters": []} for x in order]
            if d["layers"]:
                flow += self.toc([(f"L{i}", ly["title"], " · ".join(ch["title"] for ch in ly["chapters"])) for i, ly in enumerate(sections, 1)])
            for i, ly in enumerate(sections, 1):
                flow += [CondPageBreak(95 * mm) if i > 1 else Spacer(1, 4), Spacer(1, 8), self.anchor_h2(f"L{i}", str(i), ly["title"])]
                if st and f"chapter:{ly['id']}" not in st["meta"].get("fallbacks", []):
                    ch = next((c for c in st.get("chapters") or [] if c["id"] == ly["id"]), None)
                    flow += [self.P(p["text"], "story") for p in (ch or {}).get("paragraphs") or []]
                if ly["id"] == "why":
                    flow += self.diagnosis()
                for ch in ly["chapters"]:
                    flow += self.chapter(ch)
            if not d["layers"]:
                flow += [PageBreak(), self.P(r.T("rpt_nothing"), "muted")]
        flow += self.skipped()
        if r.story_layout or r.story():
            from ..fmt import fmt_bucket as _fb
            g = str(d["meta"].get("generated_at") or "")[:10]
            try:
                day = _fb(date.fromisoformat(g).isoformat(), "day", self.lg)
            except ValueError:
                day = g
            flow += [Spacer(1, 12), self.P(r.T("story_ai_footer", date=day), "foot")]
        flow += [Spacer(1, 6), self.P(r.T("rpt_footer", business=cv["business"]), "foot")]
        return flow


def render_pdf(doc: dict, ui: dict, locale: str = "") -> bytes:
    """The report document as PDF bytes (A4). Raises PdfNativeError if ReportLab is missing."""
    if not AVAILABLE:
        raise PdfNativeError("ReportLab is not installed (pip install reportlab)")
    _register()
    buf = io.BytesIO()
    lm = 18 * mm
    page_w, page_h = A4
    width = page_w - 2 * lm
    b = _Builder(doc, ui, locale, width)
    cv = doc["cover"]
    foot = f'{cv["business"]} · {cv["period_text"]}'

    def on_page(canv, dc):
        canv.saveState()
        canv.setFillColor(_col(AXIS))
        x = lm
        for f, c in _runs(foot):
            canv.setFont(f, 7.5)
            canv.drawString(x, 10 * mm, c)
            x += _sw(c, f, 7.5)
        canv.setFont(SANS, 7.5)
        canv.drawRightString(page_w - lm, 10 * mm, f"Veridex BI · {dc.page}")
        canv.restoreState()
    p = doc["meta"]["period"]
    tpl = BaseDocTemplate(buf, pagesize=A4, leftMargin=lm, rightMargin=lm, topMargin=16 * mm, bottomMargin=18 * mm,
                          title=f'{cv["title"]} · {cv["business"]} · {p["start"]} – {p["end"]}', author="Veridex BI",
                          creator="Veridex BI report")
    tpl.addPageTemplates([PageTemplate(id="p", frames=[Frame(lm, 18 * mm, width, page_h - 34 * mm, id="f", leftPadding=0,
                                                              rightPadding=0, topPadding=0, bottomPadding=0)], onPage=on_page)])
    try:
        tpl.build(b.build())
    except Exception as exc:   # noqa: BLE001
        raise PdfNativeError(f"{type(exc).__name__}: {str(exc)[:200]}") from None
    return buf.getvalue()
