"""The structure of an answer, decided by CODE (talk-to-data): which facts matter, the verdict, and the detail lines.

Why code and not the AI: the writer used to see a dozen facts and pick freely, so the same question got different causes
in English, Vietnamese and German, a "best day" answer never named the day, and a plain figure came with no verdict.
Now, for every tool:

* ``keep``      the few facts the opening sentence is written from (the AI only phrases them; it never chooses the story);
* ``verdict``   good / bad / mixed / flat, from the change against the earlier period and the KPI's own "which direction
                is favourable" (``Kpi.good``) or a reading rule in ``config/ask_readings.json``;
* ``points``    the detail lines below the opening sentence, built from the tools' own formatted lines: the same lines
                in every language, with no figure typed by anyone;
* ``headline``  for tools whose answer is a list (warnings, actions) the opening sentence is fixed wording, no AI call.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache

from ...config import config_dir
from ...i18n import ui
from ...reports.story_rules import load_story_config
from . import compose as C
from .messages import br

FLAT_PCT, FLAT_PTS, FLAT_TREND = 1.0, 0.5, 3.0
MAX_POINTS = 14
AOV_Q = re.compile(r"average order value|\baov\b|order value|basket value|gia tri don|don trung binh|bestellwert|warenkorbwert", re.I)
UNUSUAL_Q = re.compile(r"unusual|outlier|spike|abnormal|exceptional|bat thuong|dot bien|auffallend|ausreißer|ausreisser|ungewöhnlich|ungewohnlich", re.I)
HEADLINE_BY_CODE = {"warnings", "recommended_actions", "compare_periods", "custom_query", "customer_segments"}


@dataclass
class Brief:
    verdict: dict | None = None            # {"tone", "label", "say"?}
    points: list[dict] = field(default_factory=list)       # {"text", "tone"?, "heading"?, "indent"?, "why"?, "meta"?, "rank"?}
    keep: set | None = None                # fact ids the opening sentence is written from (None = the first facts)
    headline: str | None = None            # fixed opening sentence (no AI)
    reading: str | None = None             # a sentence that reads a figure ("demand barely reacts to price"), appended to the opening
    notes: list[str] = field(default_factory=list)
    period_of: dict = field(default_factory=dict)          # fact id -> period wording (snapshot figures: "as of ...")
    period_text: str | None = None
    writer_hint: dict | None = None        # what the AI is told about the verdict
    rules: str | None = None               # the opening sentence when no AI writes it (code-built from the key facts)

    def to_public(self) -> dict:
        return {"verdict": self.verdict, "points": self.points}


DROP_COLS = re.compile(r"score|điểm|punkte|first purchase|lần mua đầu|erster kauf|^rank|^#|\bfrom\b|\bto\b|^từ$|^đến$|^von$|^bis$", re.I)
PREFER_COLS = [re.compile(x, re.I) for x in (r"days since|số ngày|tage seit", r"total spend|tổng chi|gesamtausgaben|revenue|doanh thu|umsatz|expected|dự kiến|erwartet",
                                              r"orders|đơn|bestellungen", r"units|số lượng|stück|menge")]


def _row_texts(rows: list[dict], max_figs: int = 3) -> list[str]:
    """One readable line per ranked row: the name, then at most three figures (columns that are the same in every row,
    scores and first-purchase dates are left out; the table has everything)."""
    cells = [r.get("cells") or {} for r in rows]
    same = {c for c in (cells[0] if cells else {}) if len({str(x.get(c)) for x in cells}) == 1 and len(cells) > 1}
    out = []
    for r, cs in zip(rows, cells):
        names, _, _ = C.split_cells(cs)
        name = names[0] if names else str(r.get("label", ""))
        extra = [(c, v) for c, v in cs.items() if v not in (None, "") and str(v) != name and c not in same
                 and not DROP_COLS.search(str(c).strip()) and str(c).strip().lower() not in C.NAME_COLUMNS and str(c).strip().lower() not in C.RANK_COLUMNS]
        def prio(item):
            for i, rx in enumerate(PREFER_COLS):
                if rx.search(str(item[0])):
                    return i
            return len(PREFER_COLS)
        extra = sorted(extra[:], key=prio)[:max_figs]
        extra.sort(key=lambda it: list(cs).index(it[0]))
        out.append(name + (" — " + " · ".join(f"{c} {v}" for c, v in extra) if extra else ""))
    return out


# ---------------------------------------------------------------------------------------------------- small helpers
def _ok(parts):
    return [p for p in parts if p.kind == "ok"]


def _facts(p):
    return p.result.get("facts") or []


def _mk(p) -> dict:
    return (p.result.get("meta") or {}).get("kpi") or {}


def _pt(text, tone=None, **kw) -> dict:
    d = {"text": str(text)}
    if tone:
        d["tone"] = tone
    d.update({k: v for k, v in kw.items() if v not in (None, "", False)})
    return d


def _strip_title(text: str, label: str = "") -> str:
    """'Receipts by payment method / E-wallet: ₫49M ...' -> 'E-wallet: ₫49M ...' (the title may itself hold ': ')."""
    text = str(text)
    title = _label_title(label) if label else ""
    if title and text.startswith(title + " / "):
        return text[len(title) + 3:]
    i = text.find(" / ")
    j = text.find(": ")
    if i >= 0 and (j < 0 or i < j or j < i and text[:i].count(": ") >= 1):
        return text[i + 3:]
    return text


def _tone(delta, good, flat):
    if delta is None:
        return None
    if abs(delta) < flat:
        return "flat"
    if good not in ("up", "down"):
        return None
    return "good" if (delta > 0) == (good == "up") else "bad"


@lru_cache(maxsize=1)
def _readings() -> dict:
    try:
        return json.loads((config_dir() / "ask_readings.json").read_text(encoding="utf-8")).get("kpis") or {}
    except (OSError, ValueError):
        return {}


def reading_for(kpi_id: str, raw, lang: str) -> tuple[str, str, str | None] | None:
    """(tone, sentence, extra note) for a figure that needs reading, or None."""
    spec = _readings().get(kpi_id)
    if not spec or not isinstance(raw, (int, float)):
        return None
    v = abs(raw) if spec.get("abs") else raw
    for band in spec.get("bands") or []:
        if "lt" not in band or v < band["lt"]:
            say = (band.get("say") or {})
            note = None
            if spec.get("positive_note") and raw > 0 and spec.get("abs"):
                note = (spec["positive_note"] or {}).get(lang) or (spec["positive_note"] or {}).get("en")
            return band.get("tone") or "flat", say.get(lang) or say.get("en") or "", note
    return None


def asked_direction(question: str, lang: str) -> str | None:
    q = (question or "").lower()
    up = len(re.findall(rf"\b(?:{C.UP_WORDS.get(lang, C.UP_WORDS['en'])})\b", q, re.I))
    dn = len(re.findall(rf"\b(?:{C.DOWN_WORDS.get(lang, C.DOWN_WORDS['en'])})\b", q, re.I))
    return "down" if dn > up else "up" if up > dn else None


def _label_title(label: str) -> str:
    return str(label).rsplit(": ", 1)[0]


# ---------------------------------------------------------------------------------------------------- verdict
def _verdict_of(parts, lang) -> tuple[str, str | None] | None:
    """(tone, reading sentence) from the first tool that can judge."""
    headline_kpi = (load_story_config().get("headline") or {}).get("kpi") or "net_revenue"
    for p in _ok(parts):
        fs, mk = _facts(p), _mk(p)
        if p.tool == "kpi_value" and fs:
            rd = reading_for(mk.get("id", ""), fs[0].get("raw"), lang)
            if rd:
                return rd[0], rd[1]
            rc = fs[0].get("raw_change")
            if rc is not None:
                t = _tone(rc, mk.get("good"), FLAT_PTS if mk.get("fmt") == "pct" else FLAT_PCT)
                if t:
                    return t, None
        elif p.tool == "compare_periods":
            f = next((x for x in fs if x.get("value_text") is not None or x.get("raw_a") is not None), None)
            if f and f.get("raw") is not None:
                t = _tone(f["raw"], mk.get("good"), FLAT_PTS if mk.get("fmt") == "pct" else FLAT_PCT)
                if t:
                    return t, None
        elif p.tool == "findings":
            sides = [f.get("side") for f in fs]
            main = next((f for f in fs if f.get("rule") == f"chg_{headline_kpi}"), None)
            if main is not None:
                return ("good" if main.get("side") == "positive" else "bad"), None
            if "positive" in sides and "negative" in sides:
                return "mixed", None
            if sides:
                return ("good" if sides[0] == "positive" else "bad"), None
        elif p.tool == "explain_change" and fs and fs[0].get("raw_change_pct") is not None:
            t = _tone(fs[0]["raw_change_pct"], "up", FLAT_PCT)
            if t:
                return t, None
        elif p.tool == "warnings":
            return ("bad" if any(f.get("rule") for f in fs) else "good"), None
    return None


_HINT = {"good": "the main figure is better than before: say so plainly",
         "bad": "the main figure is worse than before: say so plainly",
         "mixed": "some things improved and some got worse: say so plainly, naming the main one first",
         "flat": "the main figure is about the same as before: say so plainly"}


# ---------------------------------------------------------------------------------------------------- per tool
_CALENDAR_SPLITS = {"weekend_vs_weekday", "revenue_by_dow", "revenue_by_season"}


def _not_a_cause(group) -> bool:
    """A split that only partitions the period (weekday / weekend, day of week) or whose members are all 'not set' is no root cause."""
    if group[0].get("breakdown") in _CALENDAR_SPLITS:
        return True
    return all(re.fullmatch(r"\(.*\)", str(x.get("name") or "").strip()) for x in group)


def _explain(p, question, lang, b: Brief):
    facts = _facts(p)
    if not facts:
        return
    total = facts[0]
    delta = total.get("raw") or 0.0
    up = delta >= 0
    if total.get("now_period") and total.get("before_period"):      # "compared with the previous period" always says which dates
        b.notes.append(br("expl_baseline", lang, now=total["now_period"], before=total["before_period"]))
    by_part = {f["part"]: f for f in facts if f.get("part")}
    keep = {total["id"]}
    if AOV_Q.search(question or ""):
        order = [k for k in ("basket", "items_per_order", "value_per_item") if k in by_part]
    else:
        leaf = [by_part[k] for k in ("orders", "items_per_order", "value_per_item") if k in by_part]
        same = sorted((f for f in leaf if (f["raw"] > 0) == up), key=lambda f: -abs(f["raw"]))
        other = sorted((f for f in leaf if (f["raw"] > 0) != up), key=lambda f: -abs(f["raw"]))
        order = ([same[0]["part"]] if same else []) + ([other[0]["part"]] if other else [])
    keep |= {by_part[k]["id"] for k in order}
    groups: dict[str, list] = {}
    for f in facts:
        if f.get("breakdown") and f.get("raw") is not None:
            groups.setdefault(f["breakdown"], []).append(f)
    ranked = sorted((g for g in groups.values() if not _not_a_cause(g)), key=lambda g: -max(abs(x["raw"]) for x in g))
    for g in ranked[:2]:
        same = [x for x in g if (x["raw"] > 0) == up] or g
        keep.add(max(same, key=lambda x: abs(x["raw"]))["id"])
    b.keep = keep
    b.points.append(_pt(br("h_what_changed", lang), heading=True))
    for key, pk, ind in (("orders", "p_orders", 0), ("basket", "p_basket", 0), ("items_per_order", "p_items", 1), ("value_per_item", "p_price", 1)):
        f = by_part.get(key)
        if f and key in order and ": " in f["text"]:      # root causes only: the factors that explain the change, not every revenue figure
            b.points.append(_pt(f"{br(pk, lang)}: {f['text'].rsplit(': ', 1)[1]}", "good" if f["raw"] > 0 else "bad", indent=ind))
    for g in ranked[:2]:
        b.points.append(_pt(_label_title(g[0]["label"]), heading=True))
        for f in sorted(g, key=lambda x: -abs(x["raw"]))[:2]:
            b.points.append(_pt(_strip_title(f["text"], f.get("label", "")), "good" if f["raw"] > 0 else "bad"))
    for f in facts:
        if "recording change" in str(f.get("label", "")) and f.get("text"):
            b.notes.append(f["text"])
    bits = [f"{br(pk, lang)} {by_part[k]['text'].rsplit(': ', 1)[1]}" for k, pk in (("orders", "p_orders"), ("basket", "p_basket")) if k in by_part and ": " in by_part[k]["text"]]
    if ranked:
        top = max([x for x in ranked[0] if (x["raw"] > 0) == up] or ranked[0], key=lambda x: abs(x["raw"]))
        bits.append(_strip_title(top["text"], top.get("label", "")))
    b.rules = (total["text"].rstrip(".") + ". " + "; ".join(bits) + ".") if bits else None


def _compare(p, question, lang, b: Brief):
    facts = _facts(p)
    chg = next((f for f in facts if f.get("raw_a") is not None), None)
    others = [f for f in facts if f is not chg]
    b.keep = {f["id"] for f in facts}
    for f in others:
        b.points.append(_pt(f["text"]))
    if chg:
        mk = _mk(p)
        tone = _tone(chg.get("raw"), mk.get("good"), FLAT_PTS if mk.get("fmt") == "pct" else FLAT_PCT)
        b.points.append(_pt(f"{br('lbl_change', lang)}: {chg.get('value_text') or chg['text']}", tone))
        b.rules = chg["text"]
        vals = [f for f in others if f.get("raw") is not None and f.get("period")]
        if len(vals) >= 2 and chg.get("raw") is not None:         # the sentence is code-written: which figure belongs to which period is never left to the AI
            a, bb = vals[0], vals[1]
            key = "cmp_flat" if tone == "flat" else "cmp_up" if chg["raw"] > 0 else "cmp_down"
            kpi = str(a["label"]).rsplit(" (", 1)[0]
            b.headline = br(key, lang, kpi=kpi, a=a["value"], pa=a["period"], b=bb["value"], pb=bb["period"],
                            chg=C.unsigned(chg.get("value_text") or ""))


def _custom(p, question, lang, b: Brief):
    """custom_query: the tool already wrote the sentence (figures, period, row count); code uses it as the opening, lists
    the rows, and says how it was calculated. No AI sentence: a custom result is the one place a rewording could drift."""
    facts = _facts(p)
    head, rows = facts[0], [f for f in facts[1:] if f.get("rank") is not None]
    tail = [f for f in facts[1:] if f.get("rank") is None]
    b.keep = {head["id"]}
    b.headline = head["text"]
    for f in rows:
        b.points.append(_pt(f["text"]))
    for f in tail:
        b.points.append(_pt(f["text"]))
    how = ((p.result.get("meta") or {}).get("custom_query") or {}).get("how")
    if how:
        b.notes.append(how)


def _movers(p, question, lang, b: Brief):
    facts = [f for f in _facts(p) if f.get("direction") in ("fell", "rose")]
    other = [f for f in _facts(p) if f.get("direction") not in ("fell", "rose")]
    want = asked_direction(question, lang) or "down"
    first, second = ("fell", "rose") if want == "down" else ("rose", "fell")
    good = _mk(p).get("good")
    keep = []
    for d in (first, second):
        grp = [f for f in facts if f["direction"] == d]
        keep += [f["id"] for f in grp[: 2 if d == first else 1]]
        if grp:
            b.points.append(_pt(br("h_fell" if d == "fell" else "h_rose", lang), heading=True))
            for f in grp:
                tone = ("bad" if d == "fell" else "good") if good == "up" else (("good" if d == "fell" else "bad") if good == "down" else None)
                b.points.append(_pt(_strip_title(f["text"], f.get("label", "")), tone))
    for f in other:
        b.points.append(_pt(f["text"]))
    b.keep = set(keep)
    if p.result.get("partial"):
        b.notes.append(br("movers_partial", lang))
    if not any(f["direction"] == first for f in facts) and facts:
        b.notes.append(br("movers_none_fell" if first == "fell" else "movers_none_rose", lang))
    lead = next((f for f in facts if f["direction"] == first), None) or (facts[0] if facts else None)
    if lead:
        b.rules = _strip_title(lead["text"], lead.get("label", "")).rstrip(".") + "."


def _breakdown(p, question, lang, b: Brief):
    facts = _facts(p)
    items = [f for f in facts if f.get("rank") is not None or f.get("cells")]
    for i, f in enumerate(items, 1):
        if f.get("rank") is None:
            f["rank"] = i
    mk = _mk(p)
    if mk.get("period_mode") == "snapshot" or mk.get("viz") == "heatmap" and False:
        b.period_text = br("as_of", lang, date=C.nice_dates(str((p.result.get("meta") or {}).get("data_through") or ""), lang))
        for f in facts:
            b.period_of[f["id"]] = b.period_text
    keep = [f["id"] for f in items[:3]]
    if len(items) >= 5:
        keep.append(items[-1]["id"])                      # "best and worst": the last one is named too
    total = next((f for f in facts if str(f.get("label", "")).endswith(": total")), None)
    if total:
        keep.append(total["id"])
    b.keep = set(keep) or None
    shown = items[:MAX_POINTS - 4]
    lines = _row_texts(shown) if shown and all(f.get("cells") for f in shown) else [None] * len(shown)
    for f, line in zip(shown, lines):
        b.points.append(_pt(line or f["text"], rank=f["rank"]))
    if items:
        def _nm(f):
            if f.get("cells"):
                names, _, _ = C.split_cells(f["cells"])
                return names[0] if names else str(f.get("label", ""))
            return _strip_title(f["text"], f.get("label", ""))
        lab0, nm0 = str(items[0].get("label", "")), str(items[0].get("name") or "")
        title = lab0[:-len(nm0) - 2] if nm0 and lab0.endswith(": " + nm0) else (_label_title(lab0) if ": " in lab0 else "")      # a name may hold ": " itself ("Slow sellers: the last 10%")
        nm = [_nm(f) for f in items[:3]]
        top = "; ".join(nm) if len(set(nm)) == len(nm) else (lines[0] if lines and lines[0] else nm[0])
        b.rules = (f"{title}: " if title else "") + top + "."
    tbl = next((t for t in p.result.get("tables") or [] if t.get("rows")), None)
    if tbl and len(items) == 1 and tbl.get("total_rows") == 1:
        b.notes.append(br("only_one", lang, title=_label_title(items[0]["label"])))


LOW_Q = re.compile(r"\b(lowest|worst|weakest|smallest|least)\b|niedrig|schwach|geringst|schlechtest|thap nhat|thấp nhất|te nhat|tệ nhất|it nhat|ít nhất", re.I)
HIGH_Q = re.compile(r"\b(highest|best|strongest|biggest|largest|most)\b|hochst|höchst|beste|stärkst|starkst|groesst|größt|cao nhat|cao nhất|tot nhat|tốt nhất|nhieu nhat|nhiều nhất", re.I)


def _trend(p, question, lang, b: Brief):
    facts = [f for f in _facts(p) if f.get("highest") and f.get("lowest")]
    unusual = [f for f in _facts(p) if f.get("unusual")]
    if not facts:
        return
    if not re.search(r"order|đơn|don hang|bestell", question or "", re.I):
        facts = facts[:1]
    b.keep = {facts[0]["id"]}
    if UNUSUAL_Q.search(question or "") or any(f.get("unusual") for f in unusual):
        b.keep |= {f["id"] for f in unusual}
    for f in facts:
        title = _label_title(f["label"]) if ": " in str(f["label"]) else str(f["label"])
        b.points.append(_pt(f["label"], heading=True))
        g = f.get("grain")
        b.points.append(_pt(f"{br('lbl_highest', lang)}: {f['highest']} ({f['highest_at']})"))
        b.points.append(_pt(f"{br('lbl_lowest', lang)}: {f['lowest']} ({f['lowest_at']})"))
        b.points.append(_pt(f"{br('lbl_average', lang)}: {f['average']}"))
        if f.get("raw_average") and f.get("raw_highest") is not None:
            b.points.append(_pt(f"{br('lbl_peak', lang)}: {C.ratio_text(f['raw_highest'] / f['raw_average'], lang)}"))
        if f.get("second_half_vs_first_half"):
            b.points.append(_pt(f"{br('lbl_half', lang)}: {f['second_half_vs_first_half']}"))
        if f.get("note") and f["note"] not in b.notes:
            b.notes.append(f["note"])
    f0 = facts[0]
    hi = f"{br('lbl_highest', lang)} {f0['highest']} ({f0['highest_at']})"
    lo = f"{br('lbl_lowest', lang)} {f0['lowest']} ({f0['lowest_at']})"
    low_first = bool(LOW_Q.search(question or "")) and not HIGH_Q.search(question or "")      # "the lowest day": the answer comes first
    b.rules = f"{str(f0['label']).rsplit(': ', 1)[-1]}: {lo + ', ' + hi if low_first else hi + ', ' + lo}."
    asked_unusual = bool(UNUSUAL_Q.search(question or ""))
    extra = [_pt(f["text"], "good" if f.get("unusual") == "high" else "bad" if f.get("unusual") == "low" else None)
             for f in unusual if f.get("unusual") != "none" or asked_unusual]
    if asked_unusual and unusual:
        b.points = extra + b.points
        b.rules = unusual[0]["text"].rstrip(".") + "."
    else:
        b.points += extra


def _findings(p, question, lang, b: Brief):
    facts = _facts(p)
    headline_kpi = (load_story_config().get("headline") or {}).get("kpi") or "net_revenue"
    main = next((f for f in facts if f.get("rule") == f"chg_{headline_kpi}"), None)
    b.keep = {(main or facts[0])["id"]} if facts else None
    for side, key, tone in (("negative", "h_worse", "bad"), ("positive", "h_better", "good")):
        grp = [f for f in facts if f.get("side") == side]
        if grp:
            b.points.append(_pt(br(key, lang), heading=True))
            for f in grp:
                b.points.append(_pt(f["text"], tone))


BASELINE_Q = re.compile(r"normal|typical|usual|average|binh thuong|thong thuong|trung binh|bình thường|thông thường|normal\w*|ublich\w*|üblich\w*|durchschnittlich\w*", re.I)
BASELINE_RULES = {"recent_drop", "recent_spike"}


def _warnings(p, question, lang, b: Brief, with_others: bool = False):
    facts = _facts(p)
    if with_others and BASELINE_Q.search(question or "") and any(f.get("rule") in BASELINE_RULES for f in facts):
        facts = [f for f in facts if f.get("rule") in BASELINE_RULES or not f.get("rule")]
        b.rules = next(f["text"] for f in facts if f.get("rule") in BASELINE_RULES).rstrip(".") + "."
    real = [f for f in facts if f.get("rule")]
    extra = [f for f in facts if not f.get("rule")]
    if not real:
        b.headline = br("wn_none", lang)
        return
    b.headline = br("wn_one", lang) if len(real) == 1 else br("wn_many", lang, n=len(real))
    for f in real:
        b.points.append(_pt(f["text"], "bad" if f.get("severity") in ("urgent", "high", "critical") else None))
    for f in extra:
        b.points.append(_pt(f["text"]))


def _actions(p, question, lang, b: Brief, focus_kpi: str | None):
    facts = [f for f in _facts(p) if f.get("priority")]
    if not facts:
        return
    shown = facts
    note = None
    if focus_kpi:
        match = [f for f in facts if f.get("source") in (f"chg_{focus_kpi}", focus_kpi)]
        if match:
            shown = match
        else:
            note = br("act_none_for", lang)
    if note:
        b.notes.append(note)
    why = ui(lang, "tl_because")
    last = None
    for f in shown:
        pr = f.get("priority_text") or f.get("label") or ""
        if pr != last:
            b.points.append(_pt(pr, heading=True))
            last = pr
        b.points.append(_pt(f.get("action") or f["text"], None, meta=f.get("timeline_text"),
                            why=f"{why}: {f['because']}" if f.get("because") else None))
    b.headline = br("act_head", lang) if not focus_kpi else None


SEG_STOP = {"customer", "khach", "kunden", "kunde", "that", "them", "about", "need", "attention", "lose", "sleep"}


def C_fold(x: str) -> str:
    from ..understand.periods import fold
    return fold(x)


def _segments(p, question, lang, b: Brief):
    facts = _facts(p)
    seg = [f for f in facts if f.get("segment")]
    if seg:
        q = (question or "").lower()
        groups: dict[str, list] = {}
        for f in seg:
            groups.setdefault(_label_title(f["label"]), []).append(f)
        qf = C_fold(q)
        risk_q = bool(re.search(r"\brisk|\bleav|\bchurn|nguy co|\broi bo|abwander|\bverlier|\bverlass", qf))
        words = lambda f: [w.rstrip("s") for w in re.findall(r"[a-z]{4,}", C_fold(str(f["segment"]))) if w.rstrip("s") not in SEG_STOP]
        asked = [] if risk_q else [f for f in seg if any(w in qf for w in words(f))]
        band = [f for f in seg if f.get("group") == "churn"]
        main = [f for f in seg if f.get("group") != "churn"]
        # a named segment ("how many champions"): that one; "each level of risk": the bands; "each segment": all of them
        b.keep = {f["id"] for f in (asked or (band if (risk_q and band) else main or seg))}
        for title, grp in groups.items():
            b.points.append(_pt(title, heading=True))
            for f in sorted(grp, key=lambda x: -(x.get("raw") or 0)):
                b.points.append(_pt(f"{f['segment']}: {f['value']}"))
    rows = [f for f in facts if f.get("cells") and f.get("rank") is not None]
    for f, line in zip(rows[:10], _row_texts(rows[:10])):
        b.points.append(_pt(line, rank=f["rank"]))
    if rows and not seg:
        b.keep = {facts[0]["id"], *[f["id"] for f in rows[:3]]}
        if facts:
            b.rules = str(facts[0]["text"]).split(". ")[0].rstrip(".") + "."
        # the opening sentence is code, not the AI: a customer count must never be read as a risk or a spend
        head, named = facts[0], []
        for r in rows[:3]:
            nm = C.split_cells(r["cells"])[0]
            if nm:
                named.append(nm[0])
        if named and head.get("raw") is not None and not head.get("cells"):
            b.headline = br("cs_head", lang, what=str(head.get("label") or "").split(": ")[-1] or "", n=head.get("value"), names=", ".join(named))
            note = next((f for f in facts if f.get("label") == "risk_band_note" and f.get("text")), None)
            if note:                                     # "no customer is in the high-risk band yet; showing the next band": say it first
                b.headline = f"{note['text']} {b.headline}"


def _forecast(p, question, lang, b: Brief):
    facts = _facts(p)
    head = [f for f in facts if not f.get("cells")]
    rows = [f for f in facts if f.get("cells")]
    b.keep = {f["id"] for f in (head[:2] + rows[:3])}
    for f, line in zip(rows[:8], _row_texts(rows[:8], 2)):
        b.points.append(_pt(line, rank=f.get("rank")))
    if head:
        asks_which = re.search(r"\bwhich\b|\bwhat products\b|welche|nào|sản phẩm nào", question or "", re.I)
        names = [C.split_cells(f["cells"])[0][0] for f in rows[:3] if C.split_cells(f["cells"])[0]]
        total = re.sub(r"\s*\([^)]*\)\.?$", "", head[0]["text"]).rstrip(".") + "."
        b.rules = br("fc_top", lang, names=", ".join(names)) if (asks_which and names) else total
    if _mk(p).get("id") == "predicted_future_value":
        b.notes.append(br("future_value_note", lang))


def _default(p, question, lang, b: Brief):
    facts = _facts(p)
    b.keep = {f["id"] for f in facts[:4]}
    for f in facts[4:4 + 8]:
        b.points.append(_pt(f["text"]))


def _focus_kpi(parts) -> str | None:
    for p in _ok(parts):
        if p.tool in ("kpi_value", "compare_periods", "kpi_trend"):
            return _mk(p).get("id")
    return None


# ---------------------------------------------------------------------------------------------------- entry point
def build(parts, question: str, lang: str) -> Brief:
    b = Brief()
    ok = _ok(parts)
    if not ok:
        return b
    keep: set | None = set()
    only_code = True
    focus = _focus_kpi(parts)
    for p in ok:
        sub = Brief()
        if p.tool == "explain_change":
            _explain(p, question, lang, sub)
        elif p.tool == "compare_periods":
            _compare(p, question, lang, sub)
        elif p.tool == "top_movers":
            _movers(p, question, lang, sub)
        elif p.tool == "kpi_breakdown":
            _breakdown(p, question, lang, sub)
        elif p.tool == "kpi_trend":
            _trend(p, question, lang, sub)
        elif p.tool == "findings":
            _findings(p, question, lang, sub)
        elif p.tool == "warnings":
            _warnings(p, question, lang, sub, with_others=any(x.tool != 'warnings' for x in ok))
        elif p.tool == "recommended_actions":
            _actions(p, question, lang, sub, focus if any(x.tool != "recommended_actions" for x in ok) else None)
        elif p.tool == "customer_segments":
            _segments(p, question, lang, sub)
        elif p.tool == "forecast":
            _forecast(p, question, lang, sub)
        elif p.tool == "custom_query":
            _custom(p, question, lang, sub)
        elif p.tool == "kpi_value":
            facts = _facts(p)
            sub.keep = {f["id"] for f in facts}
            mk = _mk(p)
            if p.tool == "kpi_value" and facts:
                if mk.get("period_mode") == "snapshot" or mk.get("period_mode") == "monthly" and False:
                    pass
                rd = reading_for(mk.get("id", ""), facts[0].get("raw"), lang)
                if rd and rd[2]:
                    sub.notes.append(rd[2])
                if mk.get("period_mode") == "snapshot":
                    txt = br("as_of", lang, date=C.nice_dates(str((p.result.get("meta") or {}).get("data_through") or ""), lang))
                    sub.period_text = txt
                    for f in facts:
                        sub.period_of[f["id"]] = txt
        else:
            _default(p, question, lang, sub)
        if p.tool not in HEADLINE_BY_CODE:
            only_code = False
        if sub.keep is None and p.tool not in HEADLINE_BY_CODE:
            sub.keep = {f["id"] for f in _facts(p)[:3]}
        if sub.keep is None:
            keep = None
        elif keep is not None:
            keep |= sub.keep
        b.points += sub.points
        b.notes += [n for n in sub.notes if n not in b.notes]
        b.period_of.update(sub.period_of)
        b.period_text = b.period_text or sub.period_text
        b.rules = b.rules or sub.rules
        if sub.headline and not b.headline:
            b.headline = sub.headline
    b.keep = keep if keep else None
    if not only_code:
        b.headline = None if not (b.headline and all(p.tool in HEADLINE_BY_CODE for p in ok)) else b.headline
    v = _verdict_of(parts, lang)
    if v:
        tone, say = v
        b.verdict = {"tone": tone, "label": br(f"tone_{tone}", lang)}
        b.reading = say
        b.writer_hint = {"verdict": tone, "meaning": _HINT[tone]}
    b.points = b.points[: MAX_POINTS * 2]
    return b
