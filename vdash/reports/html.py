"""Report document -> HTML (v29).

``render_html(doc, ui_texts, standalone=True)`` turns the composer's document into one HTML page:

* Text, tiles, tables, findings and actions are written here on the server (escaped, formatted with vdash.fmt).
* Charts are drawn in the browser by the dashboard's own ``web/chart_options.js`` + ECharts, from the chart data
  embedded in the page - so a chart in a report looks exactly like the same chart on the dashboard.
* ``standalone=True`` (download): ECharts, the chart code, CSS and data are all inside the file - it opens
  offline, can be e-mailed and printed. ``standalone=False`` (in-app preview): the two scripts are loaded from the
  server's /static folder; the page carries a nonce for its inline scripts (strict content security policy).
* Layout for screen and A4 print: cover, contents, one section per layer starting on a new page, blocks kept
  whole across pages, page numbers.
"""
from __future__ import annotations

import html
import json
from functools import lru_cache

from ..config import ROOT
from datetime import date
from ..fmt import fmt_arg, fmt_bucket, fmt_value
from .chart_marks import marks_for, norm as _norm

WEB = ROOT / "web"
NUMERIC = {"currency", "number", "int", "pct", "ratio", "days", "hours", "score"}
NOWRAP = NUMERIC | {"date", "month"}


@lru_cache(maxsize=4)
def _asset(name: str, mtime: float) -> str:          # mtime in the key: an updated file is read again
    return (WEB / name).read_text(encoding="utf-8")


def asset(name: str) -> str:
    return _asset(name, (WEB / name).stat().st_mtime)


def e(s) -> str:
    return html.escape("" if s is None else str(s), quote=True)


CSS = r"""
:root{--page:#f5f5f2;--surface-1:#fcfcfb;--surface-2:#f3f2ee;--text-primary:#0b0b0b;--text-secondary:#52514e;--text-muted:#6f6d68;
--axis-muted:#898781;--grid:#e1e0d9;--baseline:#c3c2b7;--border:rgba(11,11,11,.10);--accent:#2a78d6;--accent-soft:#e6f0fc;
--good-text:#006300;--bad-text:#b42f2f;--good-soft:#e7f3e7;--bad-soft:#fbeaea;--watch-text:#8a5a00;--watch-soft:#fff1d2;
--good-line:#bfdfbf;--bad-line:#f0c4c4;--watch-line:#f0d9a6;
--s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a;--s4:#eda100;--s5:#e87ba4;--s6:#008300;--s7:#4a3aa7;--s8:#e34948;
--seq-0:#cde2fb;--seq-1:#86b6ef;--seq-2:#3987e5;--seq-3:#1c5cab;--seq-4:#104281;
--font:system-ui,-apple-system,"Segoe UI",Roboto,"Noto Sans","Helvetica Neue",Arial,"PingFang SC","Microsoft YaHei","Noto Sans CJK SC",sans-serif}
*{box-sizing:border-box}
html{background:var(--page)}
body{margin:0;color:var(--text-primary);font:14px/1.5 var(--font);-webkit-font-smoothing:antialiased}
.doc{max-width:760px;margin:0 auto;padding:24px 16px 48px}
.sheet{background:var(--surface-1);border:1px solid var(--border);border-radius:14px;padding:28px 32px;margin-bottom:20px}
h1,h2,h3,h4{margin:0;line-height:1.25}
.cover{padding:48px 32px}
.cover .kicker{color:var(--accent);font-weight:600;letter-spacing:.06em;text-transform:uppercase;font-size:12px}
.cover h1{font-size:32px;margin:10px 0 6px}
.cover .biz{font-size:20px;color:var(--text-secondary)}
.cover .meta{margin-top:28px;display:grid;gap:4px;color:var(--text-secondary)}
.cover .meta b{color:var(--text-primary);font-weight:600}
.note{background:var(--watch-soft);color:var(--watch-text);border-radius:10px;padding:8px 12px;margin-top:16px;font-size:13px}
.toc ol{margin:8px 0 0;padding-left:20px}.toc li{margin:2px 0}.toc .ch{color:var(--text-secondary);font-size:13px}
.layer>h2{font-size:22px;padding-bottom:10px;border-bottom:2px solid var(--text-primary);margin-bottom:6px}
.layer>h2 .n{color:var(--accent);margin-right:8px}
.chapter{margin-top:26px}
.chead{margin:0 0 12px}.chead h3{font-size:17px}.chead .desc{color:var(--text-muted);font-size:13px;margin-top:2px}
.block.first{border-top:0;padding-top:0}
.block{margin-top:16px;padding-top:14px;border-top:1px solid var(--grid);break-inside:avoid;page-break-inside:avoid}
.block>h4{font-size:15px}
.block>.bdesc{color:var(--text-muted);font-size:12.5px;margin-top:2px}
.say{margin-top:8px;padding-left:10px;border-left:3px solid var(--accent);color:var(--text-secondary)}
.bnote{color:var(--text-muted);font-size:12px;margin-top:4px}
.chart{width:100%;margin-top:8px}
.chip{display:inline-block;margin-top:6px;font-size:12px;color:var(--text-secondary);background:var(--surface-2);border-radius:999px;padding:2px 10px}
.tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:10px;margin-top:10px}
.tile{border:1px solid var(--border);border-radius:12px;padding:10px 12px;background:#fff;break-inside:avoid}
.tile .t{font-size:12px;color:var(--text-secondary)}
.tile .v{font-size:20px;font-weight:650;margin-top:2px;white-space:nowrap}
.tile .d{font-size:12px;margin-top:2px}
.tile svg{display:block;margin-top:6px}
.metric{display:flex;align-items:baseline;gap:14px;flex-wrap:wrap;margin-top:6px}
.metric .v{font-size:24px;font-weight:650}
.good{color:var(--good-text)}.bad{color:var(--bad-text)}.flat{color:var(--text-muted)}
.cmp{color:var(--text-muted);font-size:12px}
table.data{width:100%;border-collapse:collapse;margin-top:8px;font-size:12.5px}
table.data th{text-align:left;font-weight:600;color:var(--text-secondary);border-bottom:1px solid var(--baseline);padding:5px 6px}
table.data td{border-bottom:1px solid var(--grid);padding:5px 6px;vertical-align:top}
table.data .num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}table.data .nw{white-space:nowrap}
.more{color:var(--text-muted);font-size:12px;margin-top:4px}
.tscroll{overflow-x:auto;-webkit-overflow-scrolling:touch;max-width:100%}
.findings{list-style:none;margin:8px 0 0;padding:0}
.findings>li{border:1px solid var(--grid);border-left-width:4px;border-radius:10px;padding:8px 12px;margin-top:8px;break-inside:avoid}
.findings>li.positive{border-left-color:var(--good-text)}.findings>li.negative{border-left-color:var(--bad-text)}
.findings>li.urgent{border-left-color:var(--bad-text);background:#fffafa}.findings>li.watch{border-left-color:var(--s4)}
.findings .ft{font-weight:600}.findings .fx{margin-top:2px}
.badge{display:inline-block;font-size:11px;font-weight:600;border-radius:999px;padding:1px 8px;margin-right:6px;vertical-align:1px}
.b-urgent,.b-high{background:var(--bad-soft);color:var(--bad-text)}.b-watch,.b-medium{background:var(--watch-soft);color:var(--watch-text)}
.b-low,.b-new{background:var(--accent-soft);color:var(--accent)}
.actions{margin:8px 0 0;padding-left:22px}.actions>li{margin-top:10px;break-inside:avoid}
.actions .ax{font-weight:600}.actions .why{color:var(--text-muted);font-size:12.5px;margin-top:2px}
.skipped{font-size:12.5px;margin-top:8px;padding-left:18px}
.skipped li{break-inside:avoid;margin-bottom:6px}.skipped .r{font-weight:600;color:var(--text-secondary)}
.glance h2{font-size:20px;margin-bottom:12px}
.glance .head{font-size:19px;line-height:1.4;font-weight:600;padding:12px 16px;border-radius:12px;background:var(--surface-2);border-left:5px solid var(--text-muted)}
.glance .head.good{border-left-color:var(--good-text);background:var(--good-soft);color:inherit}
.glance .head.bad{border-left-color:var(--bad-text);background:var(--bad-soft);color:inherit}
.glance h3{font-size:14px;margin:20px 0 6px;color:var(--text-secondary);text-transform:uppercase;letter-spacing:.04em}
.msgs{list-style:none;margin:0;padding:0}.msgs li{position:relative;padding:5px 0 5px 20px;break-inside:avoid}
.msgs li::before{content:"";position:absolute;left:3px;top:12px;width:9px;height:9px;border-radius:50%;background:var(--text-muted)}
.msgs li.good::before{background:var(--good-text)}.msgs li.bad::before{background:var(--bad-text)}
.msgs li.warn::before{background:var(--s4)}.msgs li.info::before{background:var(--accent)}
.msgs li.good,.msgs li.bad{color:inherit}
.glance .tiles{grid-template-columns:repeat(3,minmax(0,1fr))}
@media (max-width:560px){.glance .tiles{grid-template-columns:repeat(2,minmax(0,1fr))}.glance .head{font-size:17px}}
.story{margin:12px 0 4px;font-size:15px;line-height:1.6}.story p{margin:0 0 10px}
.actions .steps{margin:4px 0 0;padding-left:18px;font-size:13px;color:var(--text-secondary)}
.diagnosis{margin:14px 0 6px;padding:14px 16px;border-radius:12px;background:var(--surface-2);break-inside:avoid}
.diagnosis h3{font-size:13px;margin:12px 0 6px;color:var(--text-secondary);text-transform:uppercase;letter-spacing:.04em}
.diagnosis h3:first-child{margin-top:0}
.causes{margin:0;padding-left:20px}.causes li{margin:6px 0}.causes .why{color:var(--text-secondary);font-size:13px;margin-top:2px}
.c-confirmed{background:var(--good-soft);color:var(--good-text)}.c-likely{background:var(--watch-soft);color:var(--watch-text)}
.c-possible{background:var(--accent-soft);color:var(--accent)}
.clues,.checks{margin:0;padding-left:18px}.clues li,.checks li{margin:5px 0}
.actions .effect{font-size:13px;margin-top:3px;color:var(--good-text)}
.ainote{margin-top:18px;font-size:11.5px;color:var(--text-muted)}
.foot{color:var(--text-muted);font-size:12px;text-align:center;margin-top:8px}
.foot.ai{margin-top:18px;font-size:11.5px;max-width:620px;margin-left:auto;margin-right:auto}
/* v42 (Step 20): charts made for reading - numbered unusual points, highlighted items, figure links */
.cmarks{list-style:none;margin:8px 0 0;padding:0;font-size:12.5px;color:var(--text-secondary)}
.cmarks li{display:flex;gap:8px;align-items:baseline;margin-top:4px}
.cmarks .mk{flex:none;display:inline-grid;place-items:center;width:18px;height:18px;border-radius:50%;background:var(--text-primary);color:#fff;font-size:11px;font-weight:700;-webkit-print-color-adjust:exact;print-color-adjust:exact}
.cmarks .hl{padding-left:26px}
.figref{white-space:nowrap;font-size:.85em;color:var(--text-muted)}
a.figref:hover{text-decoration:underline}
/* v43 (Step 21): since your last report */
.since{margin-top:16px;padding:12px 14px;border:1px solid var(--grid);border-radius:12px;background:#fff;break-inside:avoid;page-break-inside:avoid}
.since h4{margin:0 0 4px;font-size:13px;color:var(--text-secondary);text-transform:uppercase;letter-spacing:.04em}
.since .msgs{margin:6px 0 0}.since-sub{font-size:12.5px;color:var(--text-muted)}
/* v40 (Step 16): story layout */
.schapter .story{font-size:15.5px;line-height:1.65;margin-top:14px}
.fig{margin:18px 0 0;padding:14px 16px;border:1px solid var(--grid);border-radius:12px;background:#fff;break-inside:avoid;page-break-inside:avoid}
.fig>.block{margin:0;padding:0;border:0}
.fig figcaption{margin-top:10px;font-size:13.5px;color:var(--text-secondary);padding-left:10px;border-left:3px solid var(--accent)}
.fig figcaption b{color:var(--text-primary);font-weight:600;margin-right:4px}
.plan-wrap{margin-top:20px}.plan-wrap>h3{font-size:13px;margin:0 0 4px;color:var(--text-secondary);text-transform:uppercase;letter-spacing:.04em}
.plan{list-style:none;margin:0;padding:0}
.plan>li{border:1px solid var(--grid);border-radius:12px;padding:12px 14px;margin-top:10px;background:#fff;break-inside:avoid;page-break-inside:avoid}
.plan .pt{display:flex;gap:10px;align-items:baseline;font-size:15px}
.plan .pn{flex:none;display:inline-grid;place-items:center;width:24px;height:24px;border-radius:50%;background:var(--text-primary);color:#fff;font-size:12.5px;font-weight:650;-webkit-print-color-adjust:exact;print-color-adjust:exact}
.plan .pw{margin:6px 0 0 34px;color:var(--text-secondary);font-size:13.5px}
.plan .steps{margin:6px 0 0 34px;padding-left:18px;font-size:13.5px}
.plan .pm{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr) minmax(0,2fr);gap:8px 14px;margin:10px 0 0 34px;padding-top:8px;border-top:1px dashed var(--grid)}
.plan .pm dt{font-size:11px;color:var(--text-muted);text-transform:uppercase;letter-spacing:.04em}
.plan .pm dd{margin:1px 0 0;font-size:13px}
.plan .pm div:last-child dd{color:var(--good-text)}
.more-plan{margin-top:6px}.more-plan a{color:var(--accent);text-decoration:none}
.appendix-head h2{font-size:22px}.appendix-head .bdesc{margin-top:4px}
.appendix-head{margin-bottom:12px}
.appendix{font-size:13px}.appendix>h2{font-size:18px;border-bottom-width:1px}.appendix>h2 .n{color:var(--text-muted)}
.appendix .chapter{margin-top:18px}.appendix .chead h3{font-size:15px}.appendix .block>h4{font-size:14px}
.toc li.app{margin-top:8px}
@media (max-width:560px){.plan .pm{grid-template-columns:1fr 1fr;margin-left:0}.plan .pm div:last-child{grid-column:1/-1}
.plan .pw,.plan .steps{margin-left:0}.fig{padding:12px 10px}.schapter .story{font-size:15px}}
.empty{color:var(--text-muted);font-style:italic;margin-top:6px}
.toolbar{position:sticky;top:0;z-index:5;display:flex;gap:8px;justify-content:flex-end;max-width:760px;margin:0 auto;padding:10px 16px 0}
.toolbar a.tb{font-size:13px;border:1px solid var(--border);background:var(--surface-1);border-radius:999px;padding:6px 14px;color:inherit;text-decoration:none}
.toolbar button{font:inherit;font-size:13px;border:1px solid var(--border);background:var(--surface-1);border-radius:999px;padding:6px 14px;cursor:pointer}
@media (max-width:560px){.sheet{padding:20px 16px}.cover{padding:32px 16px}.cover h1{font-size:26px}}
@page{size:A4;margin:14mm 13mm 16mm}
@page{@bottom-center{content:counter(page) " / " counter(pages);font:10px var(--font);color:#6f6d68}}
@media print{html{background:#fff}.doc{max-width:none;padding:0}.toolbar{display:none}
.sheet{border:0;border-radius:0;padding:0;margin:0;background:#fff}
.cover{min-height:250mm;display:flex;flex-direction:column;justify-content:center;break-after:page}
.tscroll{overflow:visible}
.toc{break-after:page}.glance{break-after:page}.appendix-head{break-before:page;padding-bottom:10px}.appendix-head+.appendix{break-before:avoid}
.fig,.plan>li{border-color:#ddd}.diagnosis,.badge{-webkit-print-color-adjust:exact;print-color-adjust:exact}.layer{break-before:page}.tile{background:#fff}
.glance .head,.msgs li::before{-webkit-print-color-adjust:exact;print-color-adjust:exact}
a{color:inherit;text-decoration:none}}
"""

JS = r"""
(function () {
  "use strict";
  var D = JSON.parse(document.getElementById("vr-data").textContent);
  var PRINT_W = 680;                                   // A4 content width in CSS px (charts scale to fit exactly)
  var css = function (v) { return getComputedStyle(document.documentElement).getPropertyValue(v).trim(); };
  var T = function (k, a) { var s = D.ui[k] != null ? D.ui[k] : k; if (a) Object.keys(a).forEach(function (x) { s = s.split("{" + x + "}").join(a[x]); }); return s; };
  var CH = VdashCharts.create({ css: css, locale: function () { return D.locale; }, currency: function () { return D.currency; },
    T: T, trendMode: function () { return "ma"; }, scope: function () { return D.scope; } });
  var charts = [];
  function draw(forPrint) {
    charts.forEach(function (c) { c.dispose(); }); charts = [];
    document.querySelectorAll("[data-chart]").forEach(function (div) {
      var c = D.charts[div.getAttribute("data-chart")];
      var w = forPrint ? PRINT_W : Math.round(div.getBoundingClientRect().width || PRINT_W);
      var opt = CH.buildOption(c.kpi, c.result, w, c.marks);
      var h = opt.__height || (opt.__tall ? 440 : 300), chip = opt.__chip, note = opt.__note;
      ["__tall", "__height", "__chip", "__note", "__keys"].forEach(function (k) { delete opt[k]; });
      opt.animation = false;
      div.innerHTML = ""; div.style.height = "";
      var ch = echarts.init(div, null, { renderer: "svg", width: w, height: h }); ch.setOption(opt);
      ch.getZr().refreshImmediately();                  // paint now: before printing no animation frame runs
      charts.push(ch);
      var wrap = div.firstElementChild, svg = div.querySelector("svg");
      // scale with the page: the SVG flows in its box (ECharts places it absolutely in a fixed-size box)
      if (wrap) { wrap.style.width = "100%"; wrap.style.height = "auto"; wrap.style.overflow = "visible"; }
      if (svg) { svg.setAttribute("viewBox", "0 0 " + w + " " + h); svg.setAttribute("width", "100%"); svg.removeAttribute("height");
                 svg.setAttribute("preserveAspectRatio", "xMinYMin meet");
                 svg.style.position = "static"; svg.style.display = "block"; svg.style.width = "100%"; svg.style.height = "auto"; }
      var box = div.parentNode;
      box.querySelectorAll(".chip,.cnote").forEach(function (n) { n.remove(); });
      if (note) { var n = document.createElement("div"); n.className = "bnote cnote"; n.textContent = note; div.after(n); }
      if (chip) { var s = document.createElement("div"); s.className = "chip"; s.textContent = chip.text; div.after(s); }
    });
    document.documentElement.setAttribute("data-charts", String(charts.length));
  }
  draw(false);
  var vw = window.innerWidth, printing = false;
  window.addEventListener("resize", function () { if (!printing && Math.abs(window.innerWidth - vw) > 40) { vw = window.innerWidth; draw(false); } });
  window.addEventListener("beforeprint", function () { printing = true; draw(true); });
  window.addEventListener("afterprint", function () { printing = false; draw(false); });
  var p = document.getElementById("vr-print"); if (p) p.addEventListener("click", function () { window.print(); });
  // inside the dashboard's preview window the window has its own "Save as PDF" button: hide this one
  var tb = document.querySelector(".toolbar"); if (tb && window.self !== window.top) tb.style.display = "none";
})();
"""


class _R:
    def __init__(self, doc: dict, ui: dict):
        self.doc, self.ui = doc, ui
        self.lg = doc["meta"]["lang"]
        self.cur = doc["meta"]["currency"]
        self.charts: dict[str, dict] = {}
        st = doc.get("story")
        # v40 (Step 16): an AI-written story gets the story layout; reports without one render as before
        self.story_layout = bool(st and st.get("source") in ("ai", "mixed"))

    def T(self, key: str, **kw) -> str:
        s = self.ui.get(key, key)
        for k, v in kw.items():
            s = s.replace("{" + k + "}", str(v))
        return s

    def v(self, value, fmt, compact=False) -> str:
        return fmt_value(value, fmt, self.lg, self.cur, compact=compact)

    # ------------------------------------------------------------ pieces
    def spark(self, pts) -> str:
        vals = [float(p[1]) for p in (pts or []) if p and p[1] is not None]
        if len(vals) < 2:
            return ""
        w, h = 140, 28
        lo, hi = min(vals), max(vals)
        rng = (hi - lo) or 1
        xy = " ".join(f"{i * w / (len(vals) - 1):.1f},{h - 2 - (v - lo) / rng * (h - 4):.1f}" for i, v in enumerate(vals))
        return (f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" aria-hidden="true">'
                f'<polyline fill="none" stroke="#2a78d6" stroke-width="1.6" points="{xy}"/></svg>')

    def delta(self, m: dict) -> str:
        fmt = m.get("fmt")
        d = m.get("delta_pts") if fmt == "pct" else m.get("delta_pct")
        if d is None:
            return ""
        txt = fmt_arg({"v": d, "fmt": "signed_pts" if fmt == "pct" else "signed_pct"}, self.lg, self.cur)
        if abs(d) < 0.05:
            cls = "flat"
        else:
            good = (d > 0) == (m.get("good") != "down")
            cls = "good" if good else "bad"
            if m.get("good") not in ("up", "down"):
                cls = "flat"
        return f'<span class="{cls}">{e(txt)}</span>'

    def tile(self, title: str, m: dict) -> str:
        cmp = (f' <span class="cmp">{e(self.T("rpt_vs", value=m["compare_text"]))}</span>'
               if m.get("compare_text") else "")
        return (f'<div class="tile"><div class="t">{e(title)}</div><div class="v">{e(m.get("value_text"))}</div>'
                f'<div class="d">{self.delta(m)}{cmp}</div>{self.spark(m.get("spark"))}</div>')

    def table(self, cols: list[dict], rows: list[dict]) -> str:
        def cls(c):
            f = c.get("fmt")
            return "num" if f in NUMERIC else ("nw" if f in NOWRAP else "")
        head = "".join(f'<th class="{cls(c)}">{e(c.get("label"))}</th>' for c in cols)
        body = "".join("<tr>" + "".join(
            f'<td class="{cls(c)}">{e(self.v(r.get(c["key"]), c.get("fmt") or "text"))}</td>'
            for c in cols) + "</tr>" for r in rows)
        # v40: a wide table scrolls inside its box on a phone instead of widening the whole page
        return f'<div class="tscroll"><table class="data"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'

    def findings(self, b: dict) -> str:
        if not b["items"]:
            return f'<div class="empty">{e(self.T("rpt_no_findings"))}</div>'
        out = []
        for f in b["items"]:
            cls = f.get("severity") or f.get("side") or ""
            badges = ""
            if f.get("severity"):
                badges += f'<span class="badge b-{e(f["severity"])}">{e(self.T("sev_" + f["severity"]))}</span>'
            if f.get("is_new"):
                badges += f'<span class="badge b-new">{e(self.T("rpt_new"))}</span>'
            rows = ""
            if f.get("rows") and f.get("columns"):
                rows = self.table(f["columns"], f["rows"])
                if f.get("total_rows", 0) > len(f["rows"]):
                    rows += f'<div class="more">{e(self.T("rpt_more_rows", n=f["total_rows"] - len(f["rows"])))}</div>'
            out.append(f'<li class="{e(cls)}"><div class="ft">{badges}{e(f["title"])}</div>'
                       f'<div class="fx">{e(f["text"])}</div>{rows}</li>')
        return f'<ul class="findings">{"".join(out)}</ul>'

    def actions(self, b: dict) -> str:
        if not b["items"]:
            return f'<div class="empty">{e(self.T("rpt_no_actions"))}</div>'
        li = "".join(
            f'<li><div><span class="badge b-{e(a["priority"])}">{e(a["priority_text"])}</span>'
            f'<span class="cmp">{e(a["timeline_text"])}</span></div><div class="ax">{e(a["text"])}</div>'
            f'<div class="why">{e(self.T("rpt_because", why=a["because"]))}</div></li>' for a in b["items"])
        return f'<ol class="actions">{li}</ol>'

    def block(self, b: dict, say: bool = True, id_prefix: str = "", texts: list[str] | None = None) -> str:
        head = f'<h4>{e(b["title"])}</h4>' + (f'<div class="bdesc">{e(b["desc"])}</div>' if b.get("desc") else "")
        typ, body = b.get("type"), ""
        if typ == "tiles":
            body = '<div class="tiles">' + "".join(self.tile(t["title"], t) for t in b["tiles"]) + "</div>"
        elif typ == "metric":
            cmp = (f'<span class="cmp">{e(self.T("rpt_vs", value=b["compare_text"]))}</span>' if b.get("compare_text") else "")
            body = (f'<div class="metric"><span class="v">{e(b.get("value_text"))}</span>{self.delta(b)}{cmp}'
                    f'{self.spark(b.get("spark"))}</div>')
        elif typ == "chart":
            key = f"c{len(self.charts)}"
            # v42: only a report with a story gets marks; a report without one is drawn exactly as before
            marks, an, focus = marks_for(b, texts) if self.story_layout else (None, [], [])
            self.charts[key] = {"kpi": b["kpi_public"], "result": b["result"], **({"marks": marks} if marks else {})}
            body = (f'<div class="chart" data-chart="{key}" role="img" aria-label="{e(b["title"])}"></div>'
                    + self.mark_notes(b, an, focus))
        elif typ == "table":
            body = self.table(b["columns"], b["rows"])
            if b.get("truncated"):
                body += f'<div class="more">{e(self.T("rpt_more_rows", n=b["total_rows"] - len(b["rows"])))}</div>'
        elif typ == "findings":
            body = self.findings(b)
        elif typ == "actions":
            body = self.actions(b)
        else:
            body = f'<div class="empty">{e(b.get("text"))}</div>'
        say = f'<div class="say">{e(b["text"])}</div>' if say and b.get("text") and typ in ("chart", "metric") else ""
        note = f'<div class="bnote">{e(b["note"])}</div>' if b.get("note") and typ == "metric" else ""
        return f'<section class="block" id="{e(id_prefix + b["id"])}">{head}{body}{say}{note}</section>'

    def mark_notes(self, b: dict, an: list[dict], focus: list[str]) -> str:
        """v42: one line per numbered unusual point, and one line naming what is highlighted."""
        if not an and not focus:
            return ""
        res, kpi = b["result"], b.get("kpi_public") or {}
        grain = res.get("grain") or "day"
        lis = []
        for a in an:
            s = res["series"][a["series"]]
            kind = s.get("fmt") or kpi.get("fmt")
            txt = self.T("story_unusual_" + a["dir"], when=fmt_bucket(a["x"], grain, self.lg),
                         value=self.v(a["value"], kind, compact=True), typical=self.v(a["typical"], kind, compact=True),
                         pct=self.v(abs(a["pct"]), "pct"))
            lis.append(f'<li><span class="mk">{a["n"]}</span><span>{e(txt)}</span></li>')
        if focus:
            lis.append(f'<li class="hl">{e(self.T("story_highlighted", items=", ".join(focus)))}</li>')
        return f'<ul class="cmarks">{"".join(lis)}</ul>'

    def since_box(self, chapter: str | None = None) -> str:
        """v43: 'Since your last report' - three lines on the first page (chapter=None), the details in the chapter they
        belong to. Everything comes from ``doc["changes"]`` (worked out and stored when the report was made)."""
        ch = self.doc.get("changes")
        if not ch or not (ch.get("items") or ch.get("steady")):
            return ""
        items = ch.get("items") or []
        if chapter is None:
            lines = [items[i] for i in ch.get("page1") or [] if 0 <= i < len(items)]
        else:
            lines = [i for i in items if i.get("chapter") == chapter]
        if not lines and ch.get("steady") and chapter in (None, "happened"):
            lines = [{"tone": "info", "text": ch.get("steady_text") or ""}]
        lines = [x for x in lines if x.get("text")]
        if not lines:
            return ""
        ul = '<ul class="msgs">' + "".join(f'<li class="{e(x.get("tone") or "")}">{e(x["text"])}</li>' for x in lines) + "</ul>"
        title = self.T("chg_title")
        if chapter is not None:
            return f'<div class="since"><h4>{e(title)}</h4>{ul}</div>'
        p = ch.get("prev") or {}
        try:
            day = fmt_bucket(date.fromtimestamp(float(p.get("generated_at"))).isoformat(), "day", self.lg)
        except (TypeError, ValueError):
            day = ""
        sub = self.T("chg_sub", date=day, period=p.get("period_text") or "")
        return f'<h3>{e(title)}</h3><div class="since-sub">{e(sub)}</div>{ul}'

    def glance(self) -> str:
        """v36: the first page - headline, key messages, key figures, top priorities."""
        s = self.doc.get("summary")
        if not s or not (s.get("headline") or s.get("messages") or s.get("figures")):
            return ""
        h = s.get("headline") or {}
        st = self.story()
        if st:                                   # v38: AI-written headline, messages and actions where they passed
            s = dict(s)
            h = {**h, "text": st.get("headline") or h.get("text")}
            tone = {"warning": "warn"}
            if st.get("key_messages"):
                s["messages"] = [{"text": m["text"], "tone": tone.get(m.get("tone"), m.get("tone"))} for m in st["key_messages"]]
        out = [f'<h2>{e(s.get("title") or self.T("story_title"))}</h2>']
        if h.get("text"):
            out.append(f'<div class="head {e(h.get("tone") or "")}">{e(h["text"])}</div>')
        if s.get("messages"):
            out.append(f'<h3>{e(self.T("story_messages"))}</h3><ul class="msgs">'
                       + "".join(f'<li class="{e(m.get("tone") or "")}">{e(m["text"])}</li>' for m in s["messages"]) + "</ul>")
        out.append(self.since_box())
        if s.get("figures"):
            out.append(f'<h3>{e(self.T("story_figures"))}</h3><div class="tiles">'
                       + "".join(self.tile(f["title"], f) for f in s["figures"]) + "</div>")
        if st and st.get("actions") and self.story_layout:      # v40: top 3, the full plan is in "What to do"
            li = "".join(f'<li><div class="ax">{e(a["title"])}</div>'
                         + (f'<div class="cmp">{e(" · ".join(x for x in (a.get("who"), a.get("when")) if x))}</div>'
                            if a.get("who") or a.get("when") else "") + "</li>" for a in st["actions"][:3])
            more = (f'<div class="cmp more-plan"><a href="#plan">{e(self.T("story_plan_more"))}</a></div>'
                    if len(st["actions"]) > 0 else "")
            out.append(f'<h3>{e(self.T("story_actions"))}</h3><ol class="actions">{li}</ol>{more}')
        elif st and st.get("actions") and "actions" not in st["meta"].get("fallbacks", []):
            li = "".join(
                f'<li><div class="ax">{e(a["title"])}</div><div class="why">{e(a.get("why") or "")}</div>'
                + (f'<ul class="steps">{"".join(f"<li>{e(x)}</li>" for x in a["steps"])}</ul>' if a.get("steps") else "")
                + (f'<div class="effect">{e(self.T("story_effect"))}: {e(a["expected_effect"])}</div>' if a.get("expected_effect") else "")
                + (f'<div class="cmp">{e(" · ".join(x for x in (a.get("who"), a.get("when")) if x))}</div>'
                   if a.get("who") or a.get("when") else "") + "</li>" for a in st["actions"])
            out.append(f'<h3>{e(self.T("story_actions"))}</h3><ol class="actions">{li}</ol>')
        elif s.get("actions"):
            out.append(f'<h3>{e(self.T("story_actions"))}</h3>' + self.actions({"items": s["actions"]}))
        if st:
            out.append(f'<div class="ainote">{e(self.T("story_ai_note"))}</div>')
        return f'<section class="sheet glance" id="glance">{"".join(out)}</section>'

    def story(self) -> dict | None:
        """v38: the AI-written story, when there is one (rule-written stories only repeat the first page)."""
        st = self.doc.get("story")
        return st if st and st.get("source") in ("ai", "mixed") else None

    def layer_intro(self, layer_id: str) -> str:
        st = self.story()
        if not st or f"chapter:{layer_id}" in st["meta"].get("fallbacks", []):
            return ""
        ch = next((c for c in st.get("chapters") or [] if c["id"] == layer_id), None)
        if not ch or not ch.get("paragraphs"):
            return ""
        return '<div class="story">' + "".join(f'<p>{e(p["text"])}</p>' for p in ch["paragraphs"]) + "</div>"

    def diagnosis(self) -> str:
        """v39: root causes (with how sure they are), hidden connections, what to check."""
        st = self.story()
        if not st:
            return ""
        out = []
        if st.get("root_causes"):
            out.append(f'<h3>{e(self.T("story_root_causes"))}</h3><ol class="causes">' + "".join(
                f'<li><span class="badge c-{e(r.get("confidence") or "")}">{e(self.T("story_conf_" + (r.get("confidence") or "possible")))}</span>'
                f'<b>{e(r["cause"])}</b><div class="why">{e(r.get("evidence") or "")}</div></li>' for r in st["root_causes"]) + "</ol>")
        if st.get("hidden_clues"):
            out.append(f'<h3>{e(self.T("story_clues"))}</h3><ul class="clues">'
                       + "".join(f'<li>{e(c["text"])}</li>' for c in st["hidden_clues"]) + "</ul>")
        if st.get("questions_to_check"):
            out.append(f'<h3>{e(self.T("story_questions"))}</h3><ul class="checks">'
                       + "".join(f'<li>{e(q)}</li>' for q in st["questions_to_check"]) + "</ul>")
        return f'<div class="diagnosis">{"".join(out)}</div>' if out else ""

    def chapter(self, ch: dict) -> str:
        """The chapter heading is put inside its first block, which never breaks across pages - so a heading is
        never left alone at the bottom of a printed page."""
        head = (f'<div class="chead"><h3>{e(ch["title"])}</h3>'
                + (f'<div class="desc">{e(ch["desc"])}</div>' if ch.get("desc") else "") + "</div>")
        blocks = [self.block(b) for b in ch["blocks"]]
        if blocks:
            blocks[0] = blocks[0].replace('<section class="block"', '<section class="block first"', 1).replace(">", ">" + head, 1)
        return f'<div class="chapter">{"".join(blocks) or head}</div>'

    # ------------------------------------------------------------ v40: story layout (Step 16)
    def find_part(self, pid: str | None) -> dict | None:
        for ly in self.doc["layers"]:
            for ch in ly["chapters"]:
                for b in ch["blocks"]:
                    if b["id"] == pid:
                        return b
        return next((b for b in self.doc.get("evidence") or [] if b["id"] == pid), None)

    def figure(self, part: str | None, caption: str | None, texts: list[str] | None = None,
               label: str = "", fid: str = "") -> tuple[str, list[str]]:
        """A chart (or table) the writer chose for a chapter, with what to see in it.
        v42: numbered ('Figure 2.1'), with an anchor the text links to; ``texts`` (the chapter's paragraphs and this
        caption) decide which items of a breakdown chart are highlighted. Returns (html, highlighted labels)."""
        b = self.find_part(part)
        if not b or b.get("type") not in ("chart", "table", "metric", "tiles"):
            return "", []
        before = len(self.charts)
        html_ = self.block(b, say=False, id_prefix="s-", texts=texts)
        focus = (((self.charts.get(f"c{before}") or {}).get("marks") or {}).get("focus") or []) if len(self.charts) > before else []
        lead = f"{label} · " if label else ""
        cap = (f'<figcaption><b>{e(lead + self.T("story_what_to_see"))}</b> {e(caption)}</figcaption>' if caption else "")
        anchor = f' id="{e(fid)}"' if fid else ""
        return f'<figure class="fig"{anchor}>{html_}{cap}</figure>', focus

    def plan(self, actions: list[dict]) -> str:
        """The action plan: every action with why, steps, who, when and the expected effect."""
        items = []
        for i, a in enumerate(actions, 1):
            meta = "".join(f'<div><dt>{e(self.T(k))}</dt><dd>{e(v)}</dd></div>' for k, v in
                           (("story_who", a.get("who")), ("story_when", a.get("when")), ("story_effect", a.get("expected_effect"))) if v)
            steps = (f'<ul class="steps">{"".join(f"<li>{e(x)}</li>" for x in a["steps"])}</ul>' if a.get("steps") else "")
            items.append(f'<li><div class="pt"><span class="pn">{i}</span><b>{e(a["title"])}</b></div>'
                         + (f'<div class="pw">{e(a["why"])}</div>' if a.get("why") else "")
                         + steps + (f'<dl class="pm">{meta}</dl>' if meta else "") + "</li>")
        return (f'<div class="plan-wrap" id="plan"><h3>{e(self.T("story_plan"))}</h3>'
                f'<ol class="plan">{"".join(items)}</ol></div>') if items else ""

    def story_sections(self) -> tuple[list[tuple[str, str]], list[str]]:
        """(contents entries, sections) of the story: the four questions with the writer's text and charts."""
        st = self.story()
        chs = {c["id"]: c for c in st.get("chapters") or []}
        order = [x for x in ("happened", "why", "next", "do") if x in chs or (x == "do" and st.get("actions"))]
        toc, out = [], []
        for i, cid in enumerate(order, 1):
            ch = chs.get(cid) or {}
            title = self.T(f"rpt_layer_{cid}")
            plist = [p["text"] for p in ch.get("paragraphs") or []]
            figs_l, refs = [], {}
            for j, c in enumerate(ch.get("charts") or [], 1):
                label = self.T("story_fig", n=f"{i}.{j}")
                h, focus = self.figure(c.get("part"), c.get("caption"), plist + [c.get("caption") or ""], label, f"fig-{i}-{j}")
                if not h:
                    continue
                figs_l.append(h)
                # the paragraph that talks about the highlighted item points at the figure; otherwise paragraph j does
                hit = next((k for k, t in enumerate(plist) if any(f_ and _norm(f_) in _norm(t) for f_ in focus)), None)
                at = hit if hit is not None else min(j - 1, len(plist) - 1)
                if at >= 0:
                    refs.setdefault(at, []).append((label, f"fig-{i}-{j}"))
            paras = "".join(
                "<p>" + e(t) + "".join(f' <a class="figref" href="#{e(fid)}">({e(self.T("story_see_fig", n=lb.split(" ")[-1]))})</a>'
                                       for lb, fid in refs.get(k, [])) + "</p>" for k, t in enumerate(plist))
            figs = self.since_box(cid) + "".join(figs_l)
            extra = self.diagnosis() if cid == "why" else (self.plan(st.get("actions") or []) if cid == "do" else "")
            toc.append((f"S{i}", title))
            out.append(f'<section class="sheet layer schapter" id="S{i}"><h2><span class="n">{i}</span>{e(title)}</h2>'
                       f'<div class="story">{paras}</div>{figs}{extra}</section>')
        return toc, out

    def ai_footer(self) -> str:
        from datetime import date as _d
        from ..fmt import fmt_bucket
        g = str(self.doc["meta"].get("generated_at") or "")[:10]
        try:
            day = fmt_bucket(_d.fromisoformat(g).isoformat(), "day", self.lg)
        except ValueError:
            day = g
        return f'<div class="foot ai">{e(self.T("story_ai_footer", date=day))}</div>'

    def story_body(self) -> str:
        d, cv = self.doc, self.doc["cover"]
        parts = [self.cover()]
        g = self.glance()
        if g:
            parts.append(g)
        toc, sections = self.story_sections()
        app_title = self.T("rpt_appendix")
        lis = "".join(f'<li><a href="#{a}">{e(t)}</a></li>' for a, t in toc)
        if d["layers"]:
            lis += (f'<li class="app"><a href="#appendix">{e(app_title)}</a><div class="ch">'
                    f'{e(" · ".join(ly["title"] for ly in d["layers"]))}</div></li>')
        parts.append(f'<section class="sheet toc"><h3>{e(self.T("rpt_contents"))}</h3><ol>{lis}</ol></section>')
        parts += sections
        if d["layers"]:
            parts.append(f'<section class="sheet appendix-head" id="appendix"><h2>{e(app_title)}</h2>'
                         f'<div class="bdesc">{e(self.T("rpt_appendix_desc"))}</div></section>')
            for i, ly in enumerate(d["layers"], 1):
                chs = "".join(self.chapter(ch) for ch in ly["chapters"])
                parts.append(f'<section class="sheet layer appendix" id="L{i}"><h2><span class="n">A{i}</span>'
                             f'{e(ly["title"])}</h2>{chs}</section>')
        parts += self.skipped_section()
        parts.append(self.ai_footer())
        parts.append(f'<div class="foot">{e(self.T("rpt_footer", business=cv["business"]))}</div>')
        return "".join(parts)

    def cover(self) -> str:
        cv = self.doc["cover"]
        meta = "".join(f"<div>{e(x)}</div>" for x in (cv["compare_text"], cv["data_text"], cv["generated_text"]) if x)
        past = f'<div class="note">{e(cv["past_note"])}</div>' if cv.get("past_note") else ""
        if cv.get("data_note"):
            past += f'<div class="note">{e(cv["data_note"])}</div>'
        if cv.get("story_note"):                             # v41: why the AI story is missing (quota, failure)
            past += f'<div class="note">{e(cv["story_note"])}</div>'
        return (f'<section class="sheet cover"><div class="kicker">Veridex BI</div><h1>{e(cv["title"])}</h1>'
                f'<div class="biz">{e(cv["business"])}</div><div class="meta"><b>{e(cv["period_text"])}</b>{meta}</div>{past}</section>')

    def skipped_section(self) -> list[str]:
        d = self.doc
        if not d.get("skipped"):
            return []
        groups: dict[str, list[str]] = {}
        for sk in d["skipped"]:
            groups.setdefault(sk["reason"], []).append(sk["title"])
        li = "".join(f'<li><span class="r">{e(reason)}</span> — {e(", ".join(titles))}</li>'
                     for reason, titles in sorted(groups.items(), key=lambda g: -len(g[1])))
        return [f'<section class="sheet"><h3>{e(self.T("rpt_not_included"))}</h3>'
                f'<div class="bdesc">{e(self.T("rpt_not_included_desc"))}</div><ul class="skipped">{li}</ul></section>']

    # ------------------------------------------------------------ page
    def body(self) -> str:
        if self.story_layout:
            return self.story_body()
        d, cv = self.doc, self.doc["cover"]
        meta = "".join(f"<div>{e(x)}</div>" for x in (cv["compare_text"], cv["data_text"], cv["generated_text"]) if x)
        past = f'<div class="note">{e(cv["past_note"])}</div>' if cv.get("past_note") else ""
        if cv.get("data_note"):
            past += f'<div class="note">{e(cv["data_note"])}</div>'
        if cv.get("story_note"):                             # v41: why the AI story is missing (quota, failure)
            past += f'<div class="note">{e(cv["story_note"])}</div>'
        cover = (f'<section class="sheet cover"><div class="kicker">Veridex BI</div><h1>{e(cv["title"])}</h1>'
                 f'<div class="biz">{e(cv["business"])}</div><div class="meta"><b>{e(cv["period_text"])}</b>{meta}</div>{past}</section>')
        # v39: the report's sections in question order; a question the AI answered without printed parts (e.g. "why"
        # in an executive report) still gets its section with the analysis
        st = self.story()
        story_ids = [c["id"] for c in (st or {}).get("chapters") or [] if c.get("paragraphs")]
        by_id = {ly["id"]: ly for ly in d["layers"]}
        order = [x for x in ("happened", "why", "next", "do") if x in by_id or x in story_ids] + \
            [ly["id"] for ly in d["layers"] if ly["id"] not in ("happened", "why", "next", "do")]
        sections = [by_id.get(x) or {"id": x, "title": self.T(f"rpt_layer_{x}"), "chapters": []} for x in order]
        toc = "".join(f'<li><a href="#L{i}">{e(ly["title"])}</a><div class="ch">'
                      f'{e(" · ".join(ch["title"] for ch in ly["chapters"]))}</div></li>'
                      for i, ly in enumerate(sections, 1))
        parts = [cover]
        g = self.glance()
        if g:
            parts.append(g)
        if d["layers"]:
            parts.append(f'<section class="sheet toc"><h3>{e(self.T("rpt_contents"))}</h3><ol>{toc}</ol></section>')
        for i, ly in enumerate(sections, 1):
            chs = "".join(self.chapter(ch) for ch in ly["chapters"])
            diag = self.diagnosis() if ly["id"] == "why" else ""
            parts.append(f'<section class="sheet layer" id="L{i}"><h2><span class="n">{i}</span>{e(ly["title"])}</h2>'
                         f'{self.layer_intro(ly["id"])}{diag}{chs}</section>')
        if not d["layers"]:
            parts.append(f'<section class="sheet"><div class="empty">{e(self.T("rpt_nothing"))}</div></section>')
        if d.get("skipped"):
            groups: dict[str, list[str]] = {}
            for sk in d["skipped"]:
                groups.setdefault(sk["reason"], []).append(sk["title"])
            li = "".join(f'<li><span class="r">{e(reason)}</span> — {e(", ".join(titles))}</li>'
                         for reason, titles in sorted(groups.items(), key=lambda g: -len(g[1])))
            parts.append(f'<section class="sheet"><h3>{e(self.T("rpt_not_included"))}</h3>'
                         f'<div class="bdesc">{e(self.T("rpt_not_included_desc"))}</div><ul class="skipped">{li}</ul></section>')
        parts.append(f'<div class="foot">{e(self.T("rpt_footer", business=cv["business"]))}</div>')
        return "".join(parts)


def render_html(doc: dict, ui: dict, locale: str, standalone: bool = True, nonce: str | None = None,
                static_prefix: str = "/static", excel_url: str | None = None) -> str:
    """Complete HTML page for a report document (see module doc)."""
    r = _R(doc, ui)
    body = r.body()
    keys = ("pts", "wk", "other", "heat_phone_note", "cohort_phone_note", "ma_label", "linear_label", "trend_chip", "trend_tip")
    data = {"charts": r.charts, "locale": locale, "currency": doc["meta"]["currency"], "scope": doc["meta"]["tenant_id"],
            "ui": {k: v for k, v in ui.items() if k in keys or k.startswith(("unit_", "per_"))}}
    # data inside <script type="application/json">: no '<', '>' or '&' at all, so no name in the data can end the
    # script element or start markup (JSON.parse turns the escapes back into the original characters)
    blob = (json.dumps(data, ensure_ascii=False, separators=(",", ":"))
            .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"))
    na = f' nonce="{e(nonce)}"' if nonce else ""
    if standalone:
        libs = (f'<script{na}>{asset("vendor/echarts.min.js")}</script>'
                f'<script{na}>{asset("chart_options.js")}</script>')
    else:
        libs = (f'<script{na} src="{static_prefix}/vendor/echarts.min.js"></script>'
                f'<script{na} src="{static_prefix}/chart_options.js"></script>')
    p = doc["meta"]["period"]
    title = f'{doc["cover"]["title"]} · {doc["cover"]["business"]} · {p["start"]} – {p["end"]}'
    xl = f'<a class="tb" href="{e(excel_url)}" download>{e(r.T("rpt_excel"))}</a>' if excel_url else ""   # v35
    toolbar = (f'<div class="toolbar">{xl}<button id="vr-print" type="button">{e(r.T("rpt_print"))}</button></div>')
    return (f'<!doctype html><html lang="{e(r.lg)}"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>{e(title)}</title>'
            f'<meta name="generator" content="Veridex BI report v{doc.get("doc_version", 1)}">'
            f'<style>{CSS}</style></head><body>{toolbar}<main class="doc">{body}</main>'
            f'<script type="application/json" id="vr-data">{blob}</script>{libs}<script{na}>{JS}</script></body></html>')
