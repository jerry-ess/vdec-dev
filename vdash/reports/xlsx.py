"""A created report as an Excel workbook (v35) - made from the stored report document, so it holds exactly the
numbers of the report the person created (no new query, not counted again).

Sheets, all labels in the report's language:

* **Summary** - the cover (business, period, comparison, data through, created), the report's name, a contents
  list with links to the data sheets, and **Key figures**: every figure of the report (scorecard tiles and figure
  parts) with value, comparison value, change % / change in points and the sentence.
* **Findings** - what's going well / needs attention / early warnings, each with its rows.
* **Actions** - quick wins: priority, when, action, why.
* **one sheet per chart or table** - the data behind it (time series, breakdowns, heat maps, lists).
* **Not included** - parts that could not be used, with the reason.

Numbers are real numbers (not text) with Excel number formats: amounts with the currency's decimals and the
currency code in the column header, percentages as Excel percentages (12.3% is stored as 0.123), dates as dates.
"""
from __future__ import annotations

import io
import re
from datetime import date

from ..fmt import fmt_bucket

INVALID = re.compile(r"[\[\]:*?/\\]")
HEAD_FILL = "E6F0FC"
WRAP_COL = 70


def _currency_digits(cur: str) -> int:
    try:
        from babel.numbers import get_currency_precision
        return get_currency_precision(cur or "USD")
    except Exception:   # noqa: BLE001
        return 2


class _Book:
    def __init__(self, doc: dict, ui: dict):
        from openpyxl import Workbook
        self.doc, self.ui = doc, ui
        self.lg = doc["meta"]["lang"]
        self.cur = doc["meta"].get("currency") or ""
        d = _currency_digits(self.cur)
        self.money = "#,##0" + ("." + "0" * d if d else "")
        self.wb = Workbook()
        self.names: set[str] = set()
        self.data_sheets: list[tuple[str, str, str]] = []      # (sheet, part title, chapter)
        self.part_sheet: dict[str, str] = {}                     # v40: part id -> its data sheet
        st = doc.get("story")
        self.story = st if st and st.get("source") in ("ai", "mixed") else None

    # ------------------------------------------------------------ helpers
    def T(self, key: str, **kw) -> str:
        s = self.ui.get(key, key)
        for k, v in kw.items():
            s = s.replace("{" + k + "}", str(v))
        return s

    def sheet(self, title: str, first: bool = False):
        base = re.sub(r"\s+", " ", INVALID.sub(" ", title)).strip().strip("'") or "Sheet"
        if len(base) > 31:                      # Excel's limit: cut at a word, mark the cut
            cut = base[:30].rsplit(" ", 1)[0] if " " in base[:30] else base[:30]
            base = cut.rstrip(" &·-,") + "…"
        name, i = base, 2
        while name.casefold() in self.names:
            suffix = f" ({i})"
            name, i = base[:31 - len(suffix)] + suffix, i + 1
        self.names.add(name.casefold())
        if first:
            ws = self.wb.active
            ws.title = name
        else:
            ws = self.wb.create_sheet(name)
        return ws

    def header(self, fmt: str | None, label: str, unit: bool = True) -> str:
        if fmt == "currency" and self.cur and unit:
            return f"{label} ({self.cur})"
        return label

    def put(self, ws, row: int, col: int, value, fmt: str | None = None):
        """Write one value as Excel wants it: numbers as numbers, % as a fraction, ISO dates as dates."""
        cell = ws.cell(row=row, column=col)
        if value is None:
            return cell
        if fmt in ("date",) and isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}.*", value):
            try:
                cell.value = date.fromisoformat(value[:10])
                cell.number_format = "yyyy-mm-dd"
                return cell
            except ValueError:
                pass
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            cell.value = value
            return cell
        v = float(value)
        if fmt == "pct":
            cell.value, cell.number_format = round(v / 100, 6), "0.0%"
        elif fmt == "signed_pct":
            cell.value, cell.number_format = round(v / 100, 6), "+0.0%;-0.0%;0.0%"
        elif fmt == "pts":
            cell.value, cell.number_format = round(v, 4), "+0.0;-0.0;0.0"     # the header says "points"
        elif fmt == "currency":
            cell.value, cell.number_format = round(v, 4), self.money
        elif fmt == "int":
            cell.value, cell.number_format = int(round(v)), "#,##0"
        elif fmt == "ratio":
            cell.value, cell.number_format = round(v, 4), "0.00"
        elif fmt in ("days", "hours", "score"):
            cell.value, cell.number_format = round(v, 4), "#,##0.0"
        else:
            cell.value = int(v) if v.is_integer() else round(v, 6)
            cell.number_format = "#,##0" if v.is_integer() else "#,##0.##"
        return cell

    def title_rows(self, ws, title: str, lines: list[str]) -> int:
        from openpyxl.styles import Alignment, Font
        ws.cell(row=1, column=1, value=title).font = Font(bold=True, size=14)
        r = 2
        for line in lines:
            if line:
                c = ws.cell(row=r, column=1, value=line)
                c.alignment = Alignment(wrap_text=False, vertical="top")
                r += 1
        return r + 1

    def table(self, ws, top: int, cols: list[dict], rows: list[dict], first_col: int = 1, filter_=True) -> int:
        """cols = [{key, label, fmt}]. Returns the next free row."""
        from openpyxl.styles import Alignment, Font, PatternFill
        from openpyxl.utils import get_column_letter
        fill = PatternFill("solid", fgColor=HEAD_FILL)
        for ci, c in enumerate(cols):
            cell = ws.cell(row=top, column=first_col + ci, value=self.header(c.get("fmt"), c.get("label") or "", not c.get("nounit")))
            cell.font, cell.fill = Font(bold=True), fill
            cell.alignment = Alignment(vertical="top", wrap_text=True)
        for ri, r in enumerate(rows, top + 1):
            for ci, c in enumerate(cols):
                self.put(ws, ri, first_col + ci, r.get(c["key"]), c.get("fmt"))
        for ci, c in enumerate(cols):
            letter = get_column_letter(first_col + ci)
            width = max([len(str(c.get("label") or "")) + (6 if c.get("fmt") == "currency" else 0)]
                        + [len(str(r.get(c["key"]) if r.get(c["key"]) is not None else "")) for r in rows[:300]])
            ws.column_dimensions[letter].width = max(ws.column_dimensions[letter].width or 0, min(max(10, width + 2), 60))
        if filter_ and rows:
            ws.auto_filter.ref = (f"{get_column_letter(first_col)}{top}:"
                                  f"{get_column_letter(first_col + len(cols) - 1)}{top + len(rows)}")
        return top + len(rows) + 1

    # ------------------------------------------------------------ chart data -> rows
    def chart_table(self, b: dict) -> tuple[list[dict], list[dict]]:
        res, kp = b.get("result") or {}, b.get("kpi_public") or {}
        typ = res.get("type")
        if typ == "timeseries":
            grain = res.get("grain", "day")
            xs = res.get("x") or []
            iso = all(isinstance(x, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", x) for x in xs)
            label = self.T("xl_week_of") if grain == "week" and iso else self.T("xl_period")
            cols = [{"key": "x", "label": label, "fmt": "date" if iso else "text"}]
            series = res.get("series") or []
            for i, s in enumerate(series):
                cols.append({"key": f"s{i}", "label": s.get("name") or b["title"], "fmt": s.get("fmt") or kp.get("fmt")})
            rows = []
            for j, x in enumerate(xs):
                r = {"x": x if iso else fmt_bucket(x, grain, self.lg)}
                for i, s in enumerate(series):
                    data = s.get("data") or []
                    r[f"s{i}"] = data[j] if j < len(data) else None
                rows.append(r)
            return cols, rows
        if typ == "breakdown":
            cols = [{"key": "label", "label": self.T("xl_category"), "fmt": "text"},
                    {"key": "value", "label": b["title"], "fmt": kp.get("fmt")}]
            return cols, [{"label": it.get("label"), "value": it.get("value")} for it in res.get("items") or []]
        if typ == "heatmap":
            xs = res.get("x") or []
            cols = [{"key": "y", "label": "", "fmt": "text"}] + [{"key": f"c{i}", "label": str(x), "fmt": kp.get("fmt"),
                                                                 "nounit": True} for i, x in enumerate(xs)]
            rows = [{"y": y} for y in res.get("y") or []]
            for xi, yi, v in res.get("cells") or []:
                if 0 <= yi < len(rows):
                    rows[yi][f"c{xi}"] = v
            return cols, rows
        if typ == "table":
            return list(res.get("columns") or kp.get("columns") or []), list(res.get("rows") or [])
        return [], []

    # ------------------------------------------------------------ sheets
    def blocks(self):
        for ly in self.doc["layers"]:
            for ch in ly["chapters"]:
                for b in ch["blocks"]:
                    yield ly, ch, b

    def summary(self, ws, report_name: str):
        cv = self.doc["cover"]
        lines = [report_name, cv.get("business"), cv.get("period_text"), cv.get("compare_text"), cv.get("data_text"),
                 cv.get("generated_text"), cv.get("past_note"), cv.get("data_note")]
        r = self.title_rows(ws, cv.get("title") or "Report", lines)
        ws.column_dimensions["A"].width = 46
        self._contents_row = r          # the contents list is written once the other sheets exist
        return ws

    @staticmethod
    def link(cell, sheet_name: str):
        """A link to another sheet of this workbook (works in Excel, LibreOffice and Numbers)."""
        from openpyxl.worksheet.hyperlink import Hyperlink
        cell.hyperlink = Hyperlink(ref=cell.coordinate, location="'" + sheet_name.replace("'", "''") + "'!A1")
        cell.style = "Hyperlink"

    def contents(self, ws, fixed: list[str]):
        from openpyxl.styles import Font
        r = self._contents_row
        ws.cell(row=r, column=1, value=self.T("rpt_contents")).font = Font(bold=True, size=12)
        r += 1
        for name in fixed:
            c = ws.cell(row=r, column=1, value=name)
            self.link(c, name)
            r += 1
        for name, title, chapter in self.data_sheets:
            c = ws.cell(row=r, column=1, value=title)
            self.link(c, name)
            ws.cell(row=r, column=2, value=chapter)
            r += 1
        return r + 1

    def key_figures(self, ws, top: int):
        from openpyxl.styles import Font
        ws.cell(row=top, column=1, value=self.T("xl_key_figures")).font = Font(bold=True, size=12)
        figs = []
        for _ly, ch, b in self.blocks():
            if b["type"] == "tiles":
                for t in b["tiles"]:
                    figs.append((t["title"], b["title"], t))
            elif b["type"] == "metric":
                figs.append((b["title"], ch["title"], b))
        cols = [{"key": "part", "label": self.T("xl_part"), "fmt": "text"},
                {"key": "chapter", "label": self.T("xl_chapter"), "fmt": "text"},
                {"key": "value", "label": self.T("xl_value"), "fmt": "text"},
                {"key": "compare", "label": self.T("xl_compare_value"), "fmt": "text"},
                {"key": "chg", "label": self.T("xl_change_pct"), "fmt": "pct"},
                {"key": "pts", "label": self.T("xl_change_pts"), "fmt": "pts"},
                {"key": "text", "label": self.T("xl_in_words"), "fmt": "text"}]
        from openpyxl.styles import Alignment, PatternFill
        fill = PatternFill("solid", fgColor=HEAD_FILL)
        hr = top + 1
        for ci, c in enumerate(cols, 1):
            cell = ws.cell(row=hr, column=ci, value=c["label"])
            cell.font, cell.fill = Font(bold=True), fill
            cell.alignment = Alignment(vertical="top", wrap_text=True)
        for ri, (title, chapter, m) in enumerate(figs, hr + 1):
            fmt = m.get("fmt")
            self.put(ws, ri, 1, title)
            self.put(ws, ri, 2, chapter)
            # each figure keeps its own unit: amounts with the currency, % as %, counts as counts
            unit = f" ({self.cur})" if fmt == "currency" and self.cur else ""
            if unit:
                ws.cell(row=ri, column=1, value=f"{title}{unit}")
            self.put(ws, ri, 3, m.get("value"), fmt)
            self.put(ws, ri, 4, m.get("compare_value"), fmt)
            if fmt == "pct":
                self.put(ws, ri, 6, m.get("delta_pts"), "pts")
            else:
                self.put(ws, ri, 5, m.get("delta_pct"), "signed_pct")
            self.put(ws, ri, 7, m.get("text"))
        for letter, w in zip("ABCDEFG", (40, 26, 16, 18, 12, 12, 70)):
            ws.column_dimensions[letter].width = max(ws.column_dimensions[letter].width or 0, w)
        return hr + len(figs) + 1

    def findings_sheet(self) -> str | None:
        from openpyxl.styles import Alignment, Font
        items = [(ch, b) for _ly, ch, b in self.blocks() if b["type"] == "findings"]
        if not items:
            return None
        ws = self.sheet(self.T("xl_findings"))
        r = self.title_rows(ws, self.T("xl_findings"), [])
        for _ch, b in items:
            ws.cell(row=r, column=1, value=b["title"]).font = Font(bold=True, size=12)
            r += 1
            if not b["items"]:
                ws.cell(row=r, column=1, value=self.T("rpt_no_findings"))
                r += 2
                continue
            for f in b["items"]:
                badge = []
                if f.get("severity"):
                    badge.append(self.T("sev_" + f["severity"]))
                if f.get("is_new"):
                    badge.append(self.T("rpt_new"))
                ws.cell(row=r, column=1, value=f["title"] + (f"  [{', '.join(badge)}]" if badge else "")).font = Font(bold=True)
                r += 1
                c = ws.cell(row=r, column=1, value=f.get("text"))
                c.alignment = Alignment(wrap_text=False)
                r += 1
                if f.get("rows") and f.get("columns"):
                    r = self.table(ws, r, f["columns"], f["rows"], first_col=2, filter_=False)
                    if f.get("total_rows", 0) > len(f["rows"]):
                        ws.cell(row=r - 1, column=2, value=self.T("rpt_more_rows", n=f["total_rows"] - len(f["rows"])))
                        r += 1
                r += 1
            r += 1
        ws.column_dimensions["A"].width = 4          # finding texts run across; their row tables start in column B
        return ws.title

    def actions_sheet(self) -> str | None:
        from openpyxl.styles import Alignment
        acts = [b for _ly, _ch, b in self.blocks() if b["type"] == "actions"]
        if not acts:
            return None
        ws = self.sheet(self.T("xl_actions"))
        r = self.title_rows(ws, acts[0]["title"], [acts[0].get("desc")])
        cols = [{"key": "priority", "label": self.T("xl_priority"), "fmt": "text"},
                {"key": "when", "label": self.T("xl_when"), "fmt": "text"},
                {"key": "text", "label": self.T("xl_action"), "fmt": "text"},
                {"key": "because", "label": self.T("xl_because"), "fmt": "text"}]
        rows = [{"priority": a.get("priority_text"), "when": a.get("timeline_text"), "text": a.get("text"),
                 "because": a.get("because")} for b in acts for a in b["items"]]
        if not rows:
            ws.cell(row=r, column=1, value=self.T("rpt_no_actions"))
            return ws.title
        end = self.table(ws, r, cols, rows)
        for letter, w in zip("ABCD", (16, 16, WRAP_COL, WRAP_COL)):
            ws.column_dimensions[letter].width = w
        for row in ws.iter_rows(min_row=r + 1, max_row=end - 1):
            for c in row:
                c.alignment = Alignment(wrap_text=True, vertical="top")
        ws.freeze_panes = ws.cell(row=r + 1, column=1)
        return ws.title

    def data_sheet(self, ch: dict, b: dict):
        if b["type"] == "chart":
            cols, rows = self.chart_table(b)
        else:
            cols, rows = b.get("columns") or [], b.get("rows") or []
        if not cols:
            return
        ws = self.sheet(b["title"])
        # the chart's own sentence; not the description (it explains the interactive chart: "click a period ...")
        lines = [ch["title"], b.get("text") if b["type"] == "chart" else b.get("desc")]
        if any(c.get("nounit") and c.get("fmt") == "currency" for c in cols) and self.cur:
            lines.append(self.T("xl_values_in", cur=self.cur))
        if b.get("truncated") and b.get("total_rows"):
            lines.append(self.T("rpt_more_rows", n=b["total_rows"] - len(rows)))
        r = self.title_rows(ws, b["title"], lines)
        self.table(ws, r, cols, rows)
        ws.freeze_panes = ws.cell(row=r + 1, column=2 if b.get("result", {}).get("type") == "heatmap" else 1)
        self.data_sheets.append((ws.title, b["title"], ch["title"]))
        self.part_sheet[b["id"]] = ws.title

    def story_sheet(self, ws):
        """v40 (Step 16): the AI-written story as the first sheet - headline, key messages, the four chapters
        with the charts they point to (linked to their data sheets), the diagnosis and the action plan."""
        from openpyxl.styles import Alignment, Font, PatternFill
        st, cv = self.story, self.doc["cover"]
        wrap = Alignment(wrap_text=True, vertical="top")
        ws.column_dimensions["A"].width = 5
        ws.column_dimensions["B"].width = 100
        ws.cell(row=1, column=1, value=self.T("xl_story")).font = Font(bold=True, size=14)
        ws.cell(row=2, column=1, value=" · ".join(x for x in (cv.get("business"), cv.get("period_text")) if x))
        r = 4

        def head(text, size=12):
            nonlocal r
            ws.cell(row=r, column=1, value=text).font = Font(bold=True, size=size)
            r += 1

        def para(text, col=2, bold=False, color=None):
            nonlocal r
            c = ws.cell(row=r, column=col, value=text)
            c.alignment, c.font = wrap, Font(bold=bold, color=color)
            r += 1
            return c
        if st.get("headline"):
            c = ws.cell(row=r, column=1, value=st["headline"])
            c.font, c.alignment = Font(bold=True, size=13), wrap
            ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=2)
            ws.row_dimensions[r].height = 36
            r += 2
        if st.get("key_messages"):
            head(self.T("story_messages"))
            mark = {"good": "▲", "bad": "▼", "warning": "!", "info": "•"}
            for m in st["key_messages"]:
                ws.cell(row=r, column=1, value=mark.get(m.get("tone"), "•")).alignment = Alignment(horizontal="center", vertical="top")
                para(m["text"])
            r += 1
        for i, ch in enumerate([c for c in st.get("chapters") or [] if c.get("paragraphs")], 1):
            head(f'{i}. {self.T("rpt_layer_" + ch["id"])}')
            for p in ch["paragraphs"]:
                para(p["text"])
            for fig in ch.get("charts") or []:
                b = self.find_part(fig.get("part"))
                if not b:
                    continue
                c = para(f'{self.T("xl_chart")}: {b["title"]} — {self.T("story_what_to_see")} {fig.get("caption") or ""}',
                         color="52514E")
                if fig.get("part") in self.part_sheet:      # the link sits in column A: a wrapped link cell
                    a = ws.cell(row=c.row, column=1, value="↗")   # does not grow its row in every spreadsheet app
                    self.link(a, self.part_sheet[fig["part"]])
                    a.alignment = Alignment(horizontal="center", vertical="top")
            if ch["id"] == "why":
                for key, items in (("story_root_causes", [f'[{self.T("story_conf_" + (x.get("confidence") or "possible"))}] '
                                                          f'{x["cause"]} — {x.get("evidence") or ""}' for x in st.get("root_causes") or []]),
                                   ("story_clues", [x["text"] for x in st.get("hidden_clues") or []]),
                                   ("story_questions", list(st.get("questions_to_check") or []))):
                    if items:
                        ws.cell(row=r, column=2, value=self.T(key)).font = Font(bold=True)
                        r += 1
                        for x in items:
                            ws.cell(row=r, column=1, value="•").alignment = Alignment(horizontal="center", vertical="top")
                            para(x)
            r += 1
        acts = st.get("actions") or []
        if acts:
            head(self.T("story_plan"))
            for n, a in enumerate(acts, 1):
                ws.cell(row=r, column=1, value=n).alignment = Alignment(horizontal="center", vertical="top")
                para(a.get("title"), bold=True)
                if a.get("why"):
                    para(a["why"], color="52514E")
                for x in a.get("steps") or []:
                    para(f"• {x}")
                meta = [f'{self.T(k)}: {a[f]}' for k, f in (("story_who", "who"), ("story_when", "when")) if a.get(f)]
                if meta:
                    para(" · ".join(meta), color="52514E")
                if a.get("expected_effect"):
                    para(f'{self.T("story_effect")}: {a["expected_effect"]}', color="006300")
                r += 1
        c = ws.cell(row=r, column=1, value=self.T("story_ai_footer", date=self.generated_day()))
        c.font, c.alignment = Font(italic=True, color="6F6D68", size=9), wrap
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=2)
        ws.row_dimensions[r].height = 30

    def generated_day(self) -> str:
        from datetime import date as _d
        from ..fmt import fmt_bucket
        g = str(self.doc["meta"].get("generated_at") or "")[:10]
        try:
            return fmt_bucket(_d.fromisoformat(g).isoformat(), "day", self.lg)
        except ValueError:
            return g

    def find_part(self, pid):
        for _ly, _ch, b in self.blocks():
            if b["id"] == pid:
                return b
        return next((b for b in self.doc.get("evidence") or [] if b["id"] == pid), None)

    def not_included(self) -> str | None:
        rows = [{"title": sk.get("title"), "reason": sk.get("reason")} for sk in self.doc.get("skipped") or []]
        rows += [{"title": b["title"], "reason": b.get("text")} for _ly, _ch, b in self.blocks() if b["type"] == "empty"]
        if not rows:
            return None
        ws = self.sheet(self.T("rpt_not_included"))
        r = self.title_rows(ws, self.T("rpt_not_included"), [self.T("rpt_not_included_desc")])
        self.table(ws, r, [{"key": "title", "label": self.T("xl_part"), "fmt": "text"},
                           {"key": "reason", "label": self.T("xl_reason"), "fmt": "text"}], rows)
        return ws.title

    def build(self, report_name: str) -> bytes:
        story_ws = self.sheet(self.T("xl_story"), first=True) if self.story else None    # v40: the story first
        ws = self.sheet(self.T("xl_summary"), first=not self.story)
        self.summary(ws, report_name)
        fixed = [n for n in (self.findings_sheet(), self.actions_sheet()) if n]
        for _ly, ch, b in self.blocks():
            if b["type"] in ("chart", "table"):
                self.data_sheet(ch, b)
        ni = self.not_included()
        self._contents_row = self.key_figures(ws, self._contents_row) + 1   # the figures first, then the contents
        if story_ws is not None:
            self.story_sheet(story_ws)
            fixed = [story_ws.title] + fixed
        self.contents(ws, fixed + ([ni] if ni else []))
        for sh in self.wb.worksheets:            # prints on A4 landscape, all columns on one page width
            sh.page_setup.orientation, sh.page_setup.paperSize = "landscape", sh.PAPERSIZE_A4
            sh.sheet_properties.pageSetUpPr.fitToPage = True
            sh.page_setup.fitToWidth, sh.page_setup.fitToHeight = 1, 0
        self.wb.properties.title = f'{self.doc["cover"].get("title")} · {self.doc["cover"].get("business")}'
        self.wb.properties.creator = "Veridex BI"
        out = io.BytesIO()
        self.wb.save(out)
        return out.getvalue()


def report_xlsx(doc: dict, ui: dict, report_name: str = "") -> bytes:
    """The workbook for a report document; ``ui`` = the report language's UI texts (English as fallback)."""
    return _Book(doc, ui).build(report_name)
