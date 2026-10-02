"""Fact sheet of a report (v36) - the only material the AI story writer will get (Steps 14-15).

    sheet = build_fact_sheet(doc, catalog)          # doc = a composed (or stored) report document
    prompt_json(sheet)                              # what is sent: no raw numbers, no real customer names

Every fact comes from the report document - nothing is queried or calculated anew - and carries:

* ``id`` (F1, F2 ...) for the writer to cite (charts: C1, C2 ... -> ``sheet["chart_parts"]``), ``chapter`` (happened / why / next / do = the report's four
  questions: how did we do, why, what's coming, what to do), ``topic``;
* the numbers **already written in the report's language** (``value``, ``previous``, ``change``: "586,7 Tr ₫",
  "-16,1%") - the writer copies them, it never formats or calculates (the number check of Step 15 compares
  the text against exactly these strings);
* ``judgement`` (good / bad / flat / none: is it good news for the business?) and ``importance`` (ranking);
* ``source`` = the report part and field it was taken from, plus the raw value (``raw``) - so every fact is
  traceable to a dashboard number. ``raw`` and ``source`` stay on the server.

Personal data: names from the data columns in ``config/report_story.json -> personal_data`` (customer and
salesperson names) are replaced by "Customer A", "Salesperson B" ... - in item lists, chart labels and every
text. ``sheet["aliases"]`` maps them back; it is never sent.
"""
from __future__ import annotations

import json
import re

from ..fmt import fmt_bucket, fmt_value
from ..i18n import ui
from .story_rules import judgement, load_story_config

CHAPTERS = ("happened", "why", "next", "do")
FACTS_VERSION = 1


def _num(v):
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _letters(n: int) -> str:
    s = ""
    n += 1
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


class _Sheet:
    def __init__(self, doc: dict, catalog, cfg: dict):
        self.doc, self.catalog, self.cfg = doc, catalog, cfg
        self.lg = doc["meta"]["lang"]
        self.cur = doc["meta"].get("currency") or ""
        self.fc = cfg.get("facts") or {}
        pd = cfg.get("personal_data") or {}
        self.personal_cols = pd.get("columns") or {}
        self.row_keys = set(pd.get("row_keys") or [])
        self.facts: list[dict] = []
        self.aliases: dict[str, str] = {}          # alias -> real name
        self._alias_of: dict[tuple[str, str], str] = {}
        self._count: dict[str, int] = {}
        self._fig_kpis: set[str] = set()

    # ------------------------------------------------------------ helpers
    def f(self, v, fmt, compact=True) -> str | None:
        if v is None:
            return None
        return fmt_value(v, fmt or "number", self.lg, self.cur, compact=compact)

    def change_text(self, m: dict) -> str | None:
        if m.get("fmt") == "pct":
            d = m.get("delta_pts")
            if d is None and _num(m.get("value")) is not None and _num(m.get("compare_value")) is not None:
                d = _num(m["value"]) - _num(m["compare_value"])
            return None if d is None else self._signed(d, ui(self.lg, "pts"))
        d = m.get("delta_pct")
        return None if d is None else self._signed(d, "%")

    def _signed(self, d: float, unit: str) -> str:
        from ..fmt import fmt_arg
        return fmt_arg({"v": d, "fmt": "signed_pts" if unit != "%" else "signed_pct"}, self.lg, self.cur)

    def alias(self, kind: str, real) -> str:
        real = str(real)
        key = (kind, real)
        if key not in self._alias_of:
            n = self._count.get(kind, 0)
            self._count[kind] = n + 1
            a = ui(self.lg, f"story_alias_{kind}").replace("{n}", _letters(n))
            self._alias_of[key] = a
            self.aliases[a] = real
        return self._alias_of[key]

    def personal_kind_kpi(self, kpi_id: str | None) -> str | None:
        k = self.catalog.by_id.get(kpi_id) if kpi_id else None
        for r in (k.requires if k else []):
            if r.column in self.personal_cols:
                return self.personal_cols[r.column]
        return None

    def personal_kind_rule(self, rule_id: str | None) -> str | None:
        r = self.catalog.rules_by_id.get(rule_id) if rule_id else None
        if r is None:
            return None
        for q in list(r.requires) + list(r.requires_any):
            if q.column in self.personal_cols:
                return self.personal_cols[q.column]
        return "customer" if r.items_min_role != "viewer" else None

    def weight(self, kpi_id: str | None) -> float:
        return float((self.fc.get("kpi_weight") or {}).get(kpi_id, 1.0))

    def add(self, **fact) -> dict:
        fact = {k: v for k, v in fact.items() if v not in (None, "", [], {})}
        self.facts.append(fact)
        return fact

    def rows(self, cols: list[dict], rows: list[dict], kind: str | None) -> list[dict]:
        """Up to max_items rows as {column label: text}; personal names replaced."""
        out = []
        for r in rows[: int(self.fc.get("max_items", 5))]:
            item = {}
            for c in cols:
                v = r.get(c["key"])
                if v is None:
                    continue
                if kind and c["key"] in self.row_keys and (c.get("fmt") in (None, "text")):
                    v = self.alias(kind, v)
                item[c.get("label") or c["key"]] = v if c.get("fmt") in (None, "text") else self.f(v, c.get("fmt"))
            out.append(item)
        return out

    # ------------------------------------------------------------ facts from the document
    def metric(self, chapter: str, title: str, m: dict, source: str, kpi_id: str | None):
        if _num(m.get("value")) is None or (kpi_id and kpi_id in self._fig_kpis):
            return                                  # one figure per KPI (scorecard tile and figure part repeat it)
        if kpi_id:
            self._fig_kpis.add(kpi_id)
        fmt = m.get("fmt")
        j = judgement(m.get("good"), m.get("value"), m.get("compare_value"), fmt)
        ch = m.get("delta_pts") if fmt == "pct" else m.get("delta_pct")
        size = abs(_num(ch) or 0) * (2 if fmt == "pct" else 1)
        imp = self.weight(kpi_id) * (min(size, 100) / 10 if ch is not None else 0.5)
        self.add(kind="figure", chapter=chapter, topic=title, value=m.get("value_text") or self.f(m["value"], fmt),
                 previous=m.get("compare_text"), change=self.change_text(m), judgement=j, text=m.get("text"),
                 note=m.get("note"), importance=round(imp, 2),
                 source={"part": source, "field": "value", "kpi": kpi_id},
                 raw={"value": m.get("value"), "previous": m.get("compare_value")})

    def finding_items(self, chapter: str, block: dict):
        sev = self.fc.get("severity") or {}
        for rank, it in enumerate(block.get("items") or []):
            kind = self.personal_kind_rule(it.get("rule"))
            if block["id"] == "early_warnings":
                tone, imp = "warning", float(sev.get(it.get("severity"), 3))
            else:
                tone = "good" if block["id"] == "eval_positive" else "bad"
                imp = max(1.0, 6.0 - rank * 0.75)
            self.add(kind="finding" if block["id"] != "early_warnings" else "warning", chapter=chapter,
                     topic=it.get("title"), text=it.get("text"), judgement=tone, severity=it.get("severity"),
                     basis=it.get("basis"), new=bool(it.get("is_new")) or None,
                     items=self.rows(it.get("columns") or [], it.get("rows") or [], kind),
                     more=(it.get("total_rows") or 0) - len(it.get("rows") or []) if (it.get("total_rows") or 0) > len(it.get("rows") or []) else None,
                     importance=round(imp, 2), source={"part": block["id"], "rule": it.get("rule")})

    def chart(self, chapter: str, b: dict):
        res, kp = b.get("result") or {}, b.get("kpi_public") or {}
        fmt = kp.get("fmt")
        kind = self.personal_kind_kpi(b.get("kpi"))
        facts: dict = {}
        typ = res.get("type")
        if typ == "timeseries":
            series = []
            grain = res.get("grain", "day")
            for s in (res.get("series") or [])[:3]:
                pts = [(x, _num(v)) for x, v in zip(res.get("x") or [], s.get("data") or []) if _num(v) is not None]
                if len(pts) < 2:
                    continue
                sf = s.get("fmt") or fmt
                hi, lo = max(pts, key=lambda p: p[1]), min(pts, key=lambda p: p[1])
                half = len(pts) // 2
                a1 = sum(p[1] for p in pts[:half]) / max(half, 1)
                a2 = sum(p[1] for p in pts[half:]) / max(len(pts) - half, 1)
                name = s.get("name") or b["title"]
                if kind:
                    name = self.alias(kind, name)
                entry = {"series": name, "highest": f"{fmt_bucket(hi[0], grain, self.lg)}: {self.f(hi[1], sf)}",
                         "lowest": f"{fmt_bucket(lo[0], grain, self.lg)}: {self.f(lo[1], sf)}",
                         "average": self.f(sum(p[1] for p in pts) / len(pts), sf),
                         "points": len(pts)}
                if a1:
                    entry["second_half_vs_first_half"] = self._signed(100.0 * (a2 - a1) / abs(a1), "%")
                series.append(entry)
            facts = {"series": series} if series else {}
        elif typ == "breakdown":
            items = [i for i in res.get("items") or [] if _num(i.get("value")) is not None]
            total = sum(_num(i["value"]) for i in items)
            share_ok = fmt in ("currency", "int", "number") and total > 0 and all(_num(i["value"]) >= 0 for i in items)
            ranked = sorted(items, key=lambda i: -_num(i["value"]))
            top = []
            for i in ranked[: int(self.fc.get("max_items", 5))]:
                label = self.alias(kind, i["label"]) if kind else i["label"]
                e = {"name": label, "value": self.f(i["value"], fmt)}
                if share_ok and len(items) > 1:
                    e["share"] = self.f(100.0 * _num(i["value"]) / total, "pct")
                top.append(e)
            facts = {"top": top, "count": len(items)}
            if len(ranked) > len(top):
                low = ranked[-1]
                facts["lowest"] = {"name": self.alias(kind, low["label"]) if kind else low["label"], "value": self.f(low["value"], fmt)}
        elif typ == "heatmap":
            cells = sorted([c for c in res.get("cells") or [] if _num(c[2]) is not None], key=lambda c: -_num(c[2]))
            xs, ys = res.get("x") or [], res.get("y") or []
            hour = b.get("kpi") == "hourly_heatmap"
            facts = {"top": [{"when": f"{ys[yi]} {str(xs[xi]).zfill(2) + 'h' if hour else xs[xi]}", "value": self.f(v, fmt)}
                             for xi, yi, v in cells[: int(self.fc.get("max_items", 5))]]}
        if not facts:
            return
        self.add(kind="chart", chapter=chapter, topic=b["title"], chart=b["id"], text=b.get("text"), **facts,
                 note=b.get("note"), importance=round(2.0 * self.weight(b.get("kpi")), 2), source={"part": b["id"]})

    def table(self, chapter: str, b: dict):
        kind = self.personal_kind_kpi(b.get("kpi"))
        rows = self.rows(b.get("columns") or [], b.get("rows") or [], kind)
        if not rows:
            return
        self.add(kind="list", chapter=chapter, topic=b["title"], chart=b["id"], items=rows,
                 more=(b.get("total_rows") or 0) - len(rows) if (b.get("total_rows") or 0) > len(rows) else None,
                 importance=round(1.5 * self.weight(b.get("kpi")), 2), source={"part": b["id"]})

    def actions(self, items: list[dict], source: str):
        pr = self.fc.get("priority") or {}
        for a in items:
            self.add(kind="action", chapter="do", topic=a.get("text"), priority=a.get("priority_text"),
                     when=a.get("timeline_text"), because=a.get("because"),
                     importance=float(pr.get(a.get("priority"), 3)), source={"part": source, "action": a.get("id")})

    # ------------------------------------------------------------ build
    def build(self) -> dict:
        doc = self.doc
        seen_parts = set()
        for ly in doc["layers"]:
            for ch in ly["chapters"]:
                for b in ch["blocks"]:
                    seen_parts.add(b["id"])
                    typ = b.get("type")
                    if typ == "tiles":
                        for tl in b.get("tiles") or []:
                            self.metric(ly["id"], tl["title"], tl, f"{b['id']}:{tl['kpi']}", tl["kpi"])
                    elif typ == "metric":
                        self.metric(ly["id"], b["title"], b, b["id"], b.get("kpi"))
                    elif typ == "findings":
                        self.finding_items(ly["id"], b)
                    elif typ == "actions":
                        self.actions(b.get("items") or [], b["id"])
                    elif typ == "chart":
                        self.chart(ly["id"], b)
                    elif typ == "table":
                        self.table(ly["id"], b)
        # evidence: parts not in the printed report that explain "why" (composer, report_story.json evidence_parts)
        for b in doc.get("evidence") or []:
            if b["id"] in seen_parts:
                continue
            seen_parts.add(b["id"])
            n = len(self.facts)
            {"metric": lambda: self.metric(b["layer"], b["title"], b, b["id"], b.get("kpi")),
             "chart": lambda: self.chart(b["layer"], b), "table": lambda: self.table(b["layer"], b)}[b["type"]]()
            for f in self.facts[n:]:
                f["source"]["evidence"] = True
        # the first page's material when those parts were not ticked (same numbers, see composer.at_a_glance)
        s = doc.get("summary") or {}
        have = {(f.get("kind"), f.get("topic")) for f in self.facts}
        for fg in s.get("figures") or []:
            if ("figure", fg["title"]) not in have:
                self.metric("happened", fg["title"], fg, f"summary:{fg['kpi']}", fg["kpi"])
        for m in s.get("messages") or []:
            ref = m.get("ref") or ""
            if ref.split(":")[0] not in seen_parts and not any(f.get("text") == m["text"] for f in self.facts):
                chapter = "next" if m["tone"] in ("warn", "info") else "happened"
                self.add(kind={"warn": "warning", "info": "figure"}.get(m["tone"], "finding"), chapter=chapter,
                         text=m["text"], judgement={"warn": "warning", "info": "none"}.get(m["tone"], m["tone"]),
                         importance=5.0, source={"part": "summary", "ref": ref})
        if "quick_wins" not in seen_parts:
            self.actions(s.get("actions") or [], "summary")

        # an evaluation finding that only restates a figure ("Orders fell 10.5% ...") becomes a mark on the figure
        figs = {f["source"].get("kpi"): f for f in self.facts if f["kind"] == "figure" and f.get("source", {}).get("kpi")}
        keep = []
        for f in self.facts:
            rule = self.catalog.rules_by_id.get((f.get("source") or {}).get("rule") or "")
            if f["kind"] == "finding" and not f.get("items") and rule is not None and rule.kpi in figs:
                figs[rule.kpi]["evaluation"] = f.get("judgement")
                figs[rule.kpi]["importance"] = round(figs[rule.kpi].get("importance", 0) + 2, 2)
                continue
            keep.append(f)
        self.facts = keep

        # names in free text (finding sentences, chart sentences) are replaced too
        if self.aliases:
            names = sorted(((real, a) for a, real in self.aliases.items()), key=lambda x: -len(x[0]))
            pat = re.compile("|".join(re.escape(r) for r, _ in names))
            lookup = dict(names)
            for f in self.facts:
                for key in ("text", "topic", "because"):
                    if isinstance(f.get(key), str):
                        f[key] = pat.sub(lambda m_: lookup[m_.group(0)], f[key])

        # order: by chapter, most important first; ids in that order; cap the size
        order = {c: i for i, c in enumerate(CHAPTERS)}
        self.facts.sort(key=lambda f: (order.get(f["chapter"], 9), -f.get("importance", 0)))
        cap = int(self.fc.get("max_facts", 90))
        dropped = max(0, len(self.facts) - cap)
        if dropped:
            keep = sorted(self.facts, key=lambda f: -f.get("importance", 0))[:cap]
            ids = {id(f) for f in keep}
            self.facts = [f for f in self.facts if id(f) in ids]
        for i, f in enumerate(self.facts, 1):
            f["id"] = f"F{i}"

        cv, meta = doc["cover"], doc["meta"]
        chapter_titles = {ly["id"]: ly["title"] for ly in doc["layers"]}
        # charts get neutral ids (C1, C2 ...) - the report part names are internal; ``chart_parts`` maps them back
        chart_parts, charts = {}, []
        for f in self.facts:
            if f.get("chart"):
                cid = f"C{len(chart_parts) + 1}"
                chart_parts[cid] = f["chart"]
                f["chart"] = cid
                charts.append({"id": cid, "title": f["topic"], "chapter": f["chapter"]})
        return {
            "facts_version": FACTS_VERSION,
            "language": self.lg,
            "business": meta.get("business"),
            "industry": meta.get("industry"),
            "currency": self.cur,
            "period": cv.get("period_text"),
            "comparison": cv.get("compare_text"),
            "data_through": cv.get("data_text"),
            "past_period_note": cv.get("past_note"),
            "chapters": [{"id": c, "title": chapter_titles.get(c) or ui(self.lg, f"rpt_layer_{c}")} for c in CHAPTERS],
            "first_page_draft": {"headline": (s.get("headline") or {}).get("text"),
                                 "messages": [m["text"] for m in s.get("messages") or []]},
            "facts": self.facts,
            "charts": charts,
            "dropped_facts": dropped,
            "aliases": self.aliases,
            "chart_parts": chart_parts,
        }


def build_fact_sheet(doc: dict, catalog, cfg: dict | None = None) -> dict:
    return _Sheet(doc, catalog, cfg or load_story_config()).build()


def prompt_json(sheet: dict) -> str:
    """The fact sheet as sent to a writer: without raw values, sources and the name map."""
    slim = {k: v for k, v in sheet.items() if k not in ("aliases", "dropped_facts", "chart_parts")}
    slim["facts"] = [{k: v for k, v in f.items() if k not in ("raw", "source", "importance")} for f in sheet["facts"]]
    if sheet.get("aliases"):   # the first-page draft can carry a real name inside a finding sentence
        names = sorted(((real, a) for a, real in sheet["aliases"].items()), key=lambda x: -len(x[0]))
        pat = re.compile("|".join(re.escape(r) for r, _ in names))
        lookup = dict(names)
        d = slim["first_page_draft"]
        d["headline"] = pat.sub(lambda m: lookup[m.group(0)], d["headline"] or "") or None
        d["messages"] = [pat.sub(lambda m: lookup[m.group(0)], x) for x in d["messages"]]
    return json.dumps(slim, ensure_ascii=False, separators=(",", ":"))


def estimate_tokens(text: str) -> int:
    """Rough size for budgeting: ~3.5 characters per token for Latin script, ~1.5 for Chinese."""
    cjk = sum(1 for ch in text if "一" <= ch <= "鿿")
    return int((len(text) - cjk) / 3.5 + cjk / 1.5)
