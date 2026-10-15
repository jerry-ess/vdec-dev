"""AI story writer (v38, AI plan Step 15): fact sheet -> LLM -> checked story, with rule-written fallbacks.

    story = write_story(doc, catalog, llm, store, tenant_id, month)     # -> doc["story"]

Flow
1. ``facts.build_fact_sheet(doc)`` - numbered facts, numbers already written in the report language, names hidden.
2. One JSON request (``config/report_prompts/story.json``: system text, style per language, limits), answered from
   the cache when the same report from the same data was written before (``llm.cached_complete_json``).
3. **Checks** (``check_story``), whatever the prompt says:
   * structure and limits (counts, words; for Chinese: characters / 2);
   * every cited fact / chart id exists, every paragraph / message / action cites at least one fact;
   * **number guard** - every number in the text must appear, as written, somewhere in the fact sheet (small
     counting numbers 0-10 are allowed when no %, currency or unit follows); this is what makes "no invented
     numbers" a rule instead of a hope;
   * no internal / technical words, no fact or chart ids inside the text;
   * the text is in the report's language.
4. Problems -> one retry with the list of problems. Still wrong -> **per part fallback**: a failing headline /
   messages / chapter / action plan is replaced by rule-written text from the same facts; good parts are kept.
   No AI at all (switched off, no key, error, quota) -> the whole story is rule-written (``source = "rules"``).
5. Placeholders ("Customer A") are replaced by the real names again; chart ids C1.. become report part ids.

The stored story records provider, model, prompt version, tokens, cost, time, attempts, checks and fallbacks.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from pathlib import Path

from ..config import config_dir
from ..llm import LLMError, cached_complete_json
from .facts import CHAPTERS, build_fact_sheet, prompt_json

log = logging.getLogger("vdash.reports")
STORY_VERSION = 1
_LOCK = threading.Lock()
_CACHE: dict[str, tuple[float, dict]] = {}

NUM_RE = re.compile(r"\d+(?:[.,]\d+)*")
UNIT_AFTER = re.compile(r"^[\s  ]*(%|‰|₫|\$|€|¥|元|万|亿|Tr\b|N\b|T\b|Tỷ|tỷ|triệu|nghìn|ngàn|đồng|Mio|Mrd|Tsd|"
                        r"[KMB]\b|k\b|pts?\b|điểm|Prozentpunkt|个百分点|percent|Prozent|phần trăm|VND|USD|EUR)", re.I)
VI_CHARS = set("ăâđêôơưàảãáạằẳẵắặầẩẫấậèẻẽéẹềểễếệìỉĩíịòỏõóọồổỗốộờởỡớợùủũúụừửữứựỳỷỹýỵ")
STOP = {"en": {"the", "and", "of", "to", "is", "in", "your", "for", "with", "was"},
        "de": {"der", "die", "das", "und", "ist", "nicht", "mit", "ihr", "sie", "für", "von", "wurde"}}
ID_RE = re.compile(r"\[?\b[FC]\d{1,3}\b\]?")
# v39.2: short English role / timing words that must not appear alone in a Vietnamese or Chinese report
EN_SHORT = {"owner", "manager", "managers", "staff", "sales", "salesperson", "marketing", "purchasing", "team", "cashier",
            "accountant", "store", "shift", "lead", "this", "next", "week", "weeks", "month", "months", "within", "days",
            "day", "today", "now", "immediately", "asap", "and", "or", "the", "all", "by", "end", "of"}


def prompt_path() -> Path:
    return config_dir() / "report_prompts" / "story.json"


def load_prompt(path: Path | None = None) -> dict:
    path = path or prompt_path()
    mtime = path.stat().st_mtime
    with _LOCK:
        hit = _CACHE.get(str(path))
        if hit and hit[0] == mtime:
            return hit[1]
        raw = json.loads(path.read_text(encoding="utf-8"))
        _CACHE[str(path)] = (mtime, raw)
        return raw


def validate_prompt(p: dict) -> list[str]:
    errs = []
    for k in ("version", "system", "style", "request", "retry", "limits", "language_names"):
        if k not in p:
            errs.append(f"report_prompts/story.json: missing '{k}'")
    for lg in ("en", "de", "vi", "zh"):
        if lg not in (p.get("style") or {}) or lg not in (p.get("language_names") or {}):
            errs.append(f"report_prompts/story.json: no style / language name for '{lg}'")
    if p.get("retry_thinking") not in (None, "off", "low", "high", "max"):
        errs.append("report_prompts/story.json: retry_thinking must be off, low, high or max")
    if not isinstance(p.get("repair_without_retry", 2), int) or p.get("repair_without_retry", 2) < 0:
        errs.append("report_prompts/story.json: repair_without_retry must be a whole number >= 0")
    if "{chapters}" not in (p.get("request") or ""):
        errs.append("report_prompts/story.json: 'request' must contain {chapters}")
    return errs


# ---------------------------------------------------------------- checks
def _norm(s: str) -> str:
    return (s or "").replace(" ", " ").replace(" ", " ")


def _strings(x):
    if isinstance(x, str):
        yield x
    elif isinstance(x, dict):
        for k, v in x.items():
            yield from _strings(v)
    elif isinstance(x, list):
        for v in x:
            yield from _strings(v)


def _unit_class(u: str | None) -> str:
    """What a number is: a percentage, percentage points, an amount (money / thousands / millions) or plain."""
    if not u:
        return ""
    u = u.lower()
    if u in ("%", "‰", "percent", "prozent", "phần trăm"):
        return "%"
    if u in ("pts", "pt", "điểm", "prozentpunkt", "个百分点"):
        return "pts"
    return "amount"


def _numbers(text: str):
    t = _norm(text)
    for m in NUM_RE.finditer(t):
        rest = t[m.end():]
        unit = UNIT_AFTER.match(rest)
        if unit and unit.group(1) in ("₫", "$", "€", "¥") and re.match(r"\d", rest[unit.end():].lstrip()):
            unit = None             # v39.4: a prefix symbol of the NEXT number ("Sep 4 ₫83.01M", "29 $5")
        yield m.group(0).strip(".,"), (unit.group(1) if unit else None)


def allowed_numbers(sent: dict) -> set:
    """Every number of the fact sheet, alone and with the kind of unit written after it ("16,1" and ("16,1", "%"))."""
    out = set()
    for s in _strings(sent):
        for tok, unit in _numbers(s):
            out.add(tok)
            out.add((tok, _unit_class(unit)))
    return out


def number_problems(text: str, allowed: set) -> list[str]:
    """Numbers in ``text`` that are not in the fact sheet as written. A number with % / points / an amount unit
    must appear with the same kind of unit (so "23%" is not excused by a date "23 Sep"); plain numbers must
    appear somewhere; counting numbers 0-10 without a unit are always fine ("3 things", "within 7 days")."""
    bad = []
    for tok, unit in _numbers(text):
        cls = _unit_class(unit)
        if cls:
            if (tok, cls) in allowed:
                continue
        elif tok in allowed or (tok.isdigit() and int(tok) <= 10):
            continue
        bad.append(tok + (unit or ""))
    return bad


def data_names(sent: dict) -> list[str]:
    """Product, customer and other names from the data (any language) - left out of the language check."""
    names = set()
    for f in sent.get("facts") or []:
        for it in f.get("items") or []:
            names.update(v for v in it.values() if isinstance(v, str) and not NUM_RE.fullmatch(v.strip()))
        for key in ("top", "lowest"):
            xs = f.get(key)
            for it in (xs if isinstance(xs, list) else [xs] if isinstance(xs, dict) else []):
                if isinstance(it.get("name"), str):
                    names.add(it["name"])
        for sr in f.get("series") or []:
            if isinstance(sr, dict) and isinstance(sr.get("series"), str):
                names.add(sr["series"])
        for key in ("biggest_drops", "biggest_gains", "share_shifts"):
            for it in f.get(key) or []:
                if isinstance(it, dict) and isinstance(it.get("name"), str):
                    names.add(it["name"])
        for key in ("new_items", "gone_items"):
            names.update(x for x in f.get(key) or [] if isinstance(x, str))
    return sorted((n for n in names if len(n) >= 3), key=len, reverse=True)


def _words(text: str, lg: str) -> int:
    if lg == "zh":
        return (len(re.sub(r"\s", "", text)) + 1) // 2
    return len(text.split())


def language_ok(text: str, lg: str) -> bool:
    letters = [c for c in text.lower() if c.isalpha()]
    if len(letters) < 40:
        return True
    if lg == "zh":
        cjk = sum(1 for c in letters if "一" <= c <= "鿿")
        return cjk / len(letters) >= 0.4
    vi = sum(1 for c in letters if c in VI_CHARS) / len(letters)
    if lg == "vi":
        return vi >= 0.04
    if vi >= 0.04 or sum(1 for c in letters if "一" <= c <= "鿿") / len(letters) > 0.2:
        return False
    words = re.findall(r"[a-zäöüß]+", text.lower())
    own = sum(1 for w in words if w in STOP[lg])
    other = sum(1 for w in words if w in STOP["de" if lg == "en" else "en"])
    return own >= other


def short_english(text: str, lg: str) -> bool:
    """v39.2: a short text (a 'who' or 'when') written in English inside a Vietnamese or Chinese report."""
    if lg not in ("vi", "zh") or not isinstance(text, str):
        return False
    if lg == "zh" and any("\u4e00" <= c <= "\u9fff" for c in text):
        return False
    if lg == "vi" and any(c in VI_CHARS for c in text.lower()):
        return False
    words = re.findall(r"[a-z]+", text.lower())
    return bool(words) and sum(1 for w in words if w in EN_SHORT) >= max(1, len(words) // 2)


def fix_labels(x, names: list[str]):
    """v39.2: put back data labels the writer altered by dropping a symbol ("Staff 3400" -> "Staff •3400")."""
    pairs = []
    for n in names:
        if re.match(r"^[+\-−]?\d", n) or any(c in n for c in "₫$€£¥%"):
            continue                # v39.6: formatted numbers are not labels ("-19,91 Tr ₫" became "₫ ₫")
        plain = re.sub(r"\s+", " ", re.sub(r"[^\w\s.,%-]", " ", n)).strip()
        if plain and plain != n and len(plain) >= 3:
            pairs.append((re.compile(r"(?<![\w•])" + re.escape(plain) + r"(?!\w)"), n))
    if not pairs:
        return x

    def walk(v):
        if isinstance(v, str):
            for pat, n in pairs:
                if n not in v:
                    v = pat.sub(n, v)
            return v
        if isinstance(v, list):
            return [walk(i) for i in v]
        if isinstance(v, dict):
            return {k: walk(i) for k, i in v.items()}
        return v
    return walk(x)


def check_story(ans: dict, sent: dict, lg: str, prompt: dict, chapters: list[str]) -> dict[str, list[str]]:
    """Problems per part: {"headline": [...], "messages": [...], "chapter:<id>": [...], "actions": [...],
    "structure": [...]}. Empty dict = all good."""
    lim = prompt.get("limits") or {}
    fact_ids = {f["id"] for f in sent.get("facts") or []}
    chart_ids = {c["id"] for c in sent.get("charts") or []}
    allowed = allowed_numbers(sent)
    banned = [b.lower() for b in prompt.get("banned_terms") or []]
    names = data_names(sent)
    probs: dict[str, list[str]] = {}
    # words of the fact sheet written in another script (e.g. Vietnamese product names quoted in an English
    # report, Latin names in a Chinese one) are data, not the writer's language
    vocab = {w.lower() for x in _strings(sent) for w in re.findall(r"\w+", x)}

    def foreign(w: str) -> bool:
        if lg == "zh":
            return not any("\u4e00" <= c <= "\u9fff" for c in w)
        if lg in ("en", "de"):
            return any(c not in "abcdefghijklmnopqrstuvwxyzäöüß0123456789_" for c in w.lower())
        return False

    def without_names(text: str) -> str:
        for n in names:
            text = text.replace(n, " ")
        text = ID_RE.sub("", text)
        return " ".join(w for w in re.findall(r"\w+|[^\w\s]", text) if not (w.lower() in vocab and foreign(w)))

    def add(part, msg):
        probs.setdefault(part, []).append(msg)

    def text_check(part, label, text, max_words, need_facts=None):
        if not isinstance(text, str) or not text.strip():
            add(part, f"{label}: empty")
            return
        limit = max_words * float((prompt.get("word_factor") or {}).get(lg, 1.0))   # v39.7: Vietnamese words are syllables
        if _words(text, lg) > limit * 1.2:
            add(part, f"{label}: too long ({_words(text, lg)} words, max {round(limit)})")
        for n in number_problems(text, allowed):
            add(part, f"{label}: the number '{n}' is not in the fact sheet - copy numbers exactly or leave them out")
        low = text.lower()
        for b in banned:
            if b in low:
                add(part, f"{label}: technical word '{b}' - use plain business words")
        # v39.4: fact / chart ids written inside a text are removed when the answer is cleaned (_clean), so they
        # no longer cost a second attempt or a fallback
        if not language_ok(without_names(text), lg):
            add(part, f"{label}: not written in {prompt.get('language_names', {}).get(lg, lg)}")
        if need_facts is not None:
            ids = need_facts if isinstance(need_facts, list) else []
            if not ids:
                add(part, f"{label}: cite at least one fact id in 'facts'")
            unknown = [i for i in ids if i not in fact_ids]
            if unknown:
                add(part, f"{label}: unknown fact ids {unknown[:5]}")

    if not isinstance(ans, dict):
        return {"structure": ["the answer is not a JSON object"]}
    text_check("headline", "headline", ans.get("headline"), lim.get("headline_words", 25))
    msgs = ans.get("key_messages")
    lo, hi = lim.get("messages", [3, 5])
    if not isinstance(msgs, list) or not (lo <= len(msgs) <= hi):
        add("messages", f"key_messages: need {lo} to {hi}")
    for i, m in enumerate(msgs if isinstance(msgs, list) else [], 1):
        m = m if isinstance(m, dict) else {}
        text_check("messages", f"key message {i}", m.get("text"), lim.get("message_words", 30), m.get("facts") or [])
        if m.get("tone") not in ("good", "bad", "warning", "info"):
            add("messages", f"key message {i}: tone must be good, bad, warning or info")
    got = {c.get("id"): c for c in ans.get("chapters") or [] if isinstance(c, dict)}
    for cid in chapters:
        c = got.get(cid)
        part = f"chapter:{cid}"
        if c is None:
            add(part, f"chapter '{cid}' is missing")
            continue
        paras = c.get("paragraphs")
        lo, hi = lim.get("paragraphs", [1, 3])
        if not isinstance(paras, list) or not (lo <= len(paras) <= hi):
            add(part, f"chapter '{cid}': need {lo} to {hi} paragraphs")
        for i, p in enumerate(paras if isinstance(paras, list) else [], 1):
            p = p if isinstance(p, dict) else {}
            text_check(part, f"chapter '{cid}' paragraph {i}", p.get("text"), lim.get("paragraph_words", 90), p.get("facts") or [])
        charts = c.get("charts") or []
        if not isinstance(charts, list) or len(charts) > lim.get("charts", [0, 2])[1]:
            add(part, f"chapter '{cid}': at most {lim.get('charts', [0, 2])[1]} charts")
        for j, ch in enumerate(charts if isinstance(charts, list) else [], 1):
            ch = ch if isinstance(ch, dict) else {}
            if ch.get("id") not in chart_ids:
                add(part, f"chapter '{cid}' chart {j}: unknown chart id {ch.get('id')!r}")
            text_check(part, f"chapter '{cid}' chart {j} caption", ch.get("caption"), lim.get("caption_words", 25))
    extra = [k for k in got if k not in chapters]
    if extra:
        add("structure", f"write only the chapters {chapters}, not {extra}")
    acts = ans.get("actions")
    lo, hi = lim.get("actions", [3, 6])
    if "do" in chapters:
        if not isinstance(acts, list) or not (lo <= len(acts) <= hi):
            add("actions", f"actions: need {lo} to {hi}")
        for i, a in enumerate(acts if isinstance(acts, list) else [], 1):
            a = a if isinstance(a, dict) else {}
            text_check("actions", f"action {i} title", a.get("title"), lim.get("title_words", 15), a.get("facts") or [])
            text_check("actions", f"action {i} why", a.get("why"), lim.get("why_words", 40))
            steps = a.get("steps")
            slo, shi = lim.get("steps", [1, 3])
            if not isinstance(steps, list) or not (slo <= len(steps) <= shi):
                add("actions", f"action {i}: need {slo} to {shi} steps")
            for j, s in enumerate(steps if isinstance(steps, list) else [], 1):
                text_check("actions", f"action {i} step {j}", s, lim.get("why_words", 40))
            for k in ("who", "when"):
                if a.get(k) is not None:
                    text_check("actions", f"action {i} {k}", a.get(k), 8)
                    if short_english(a.get(k), lg):
                        add("actions", f"action {i} {k}: '{a.get(k)}' is English - write it in "
                                       f"{prompt.get('language_names', {}).get(lg, lg)}")
            if a.get("expected_effect") is not None:
                text_check("actions", f"action {i} expected effect", a.get("expected_effect"), lim.get("effect_words", 25))
    # v39: the analyst's diagnosis
    rcs = ans.get("root_causes")
    lo, hi = lim.get("root_causes", [2, 4])
    if not isinstance(rcs, list) or not (lo <= len(rcs) <= hi):
        add("root_causes", f"root_causes: need {lo} to {hi}")
    for i, rc in enumerate(rcs if isinstance(rcs, list) else [], 1):
        rc = rc if isinstance(rc, dict) else {}
        text_check("root_causes", f"root cause {i}", rc.get("cause"), lim.get("cause_words", 30), rc.get("facts") or [])
        text_check("root_causes", f"root cause {i} evidence", rc.get("evidence"), lim.get("evidence_words", 50))
        if rc.get("confidence") not in ("confirmed", "likely", "possible"):
            add("root_causes", f"root cause {i}: confidence must be confirmed, likely or possible")
    clues = ans.get("hidden_clues")
    lo, hi = lim.get("hidden_clues", [1, 3])
    if not isinstance(clues, list) or not (lo <= len(clues) <= hi):
        add("hidden_clues", f"hidden_clues: need {lo} to {hi}")
    for i, c in enumerate(clues if isinstance(clues, list) else [], 1):
        c = c if isinstance(c, dict) else {}
        text_check("hidden_clues", f"hidden clue {i}", c.get("text"), lim.get("clue_words", 50), c.get("facts") or [])
        if len(set(c.get("facts") or [])) < 2:
            add("hidden_clues", f"hidden clue {i}: a connection needs at least 2 different facts")
    qs = ans.get("questions_to_check", [])
    lo, hi = lim.get("questions", [0, 3])
    if not isinstance(qs, list) or not (lo <= len(qs) <= hi):
        add("questions", f"questions_to_check: need {lo} to {hi}")
    for i, q in enumerate(qs if isinstance(qs, list) else [], 1):
        text_check("questions", f"question {i}", q, lim.get("question_words", 25))
    every = " ".join(_strings({k: ans.get(k) for k in ("headline", "key_messages", "chapters", "actions", "root_causes",
                                                        "hidden_clues")}))
    if not language_ok(without_names(every), lg):
        add("structure", f"the text is not in {prompt.get('language_names', {}).get(lg, lg)}")
    return probs


# ---------------------------------------------------------------- rule-written parts (fallback)
def _fact_sentence(f: dict) -> str | None:
    if f.get("kind") == "bridge":
        parts = "; ".join(f"{p['part']} {p.get('change') or ''} ({p.get('effect_on_revenue')})" for p in f.get("split") or [])
        return f"{f['topic']}: {f.get('revenue_before')} → {f.get('revenue_now')} ({f.get('revenue_change')}). {parts}."
    if f.get("kind") == "driver":
        top = (f.get("biggest_drops") or []) + (f.get("biggest_gains") or [])
        if top:
            return f"{f['topic']}: " + "; ".join(f"{r['name']} {r['before']} → {r['now']} ({r['change']})" for r in top[:2]) + "."
    return f.get("text") or (f"{f['topic']}: {f['value']}" if f.get("value") else None)


def rules_story(doc: dict, sheet: dict, chapters: list[str]) -> dict:
    s = doc.get("summary") or {}
    tone = {"good": "good", "bad": "bad", "warn": "warning", "info": "info"}
    out = {"headline": (s.get("headline") or {}).get("text") or "",
           "key_messages": [{"text": m["text"], "tone": tone.get(m.get("tone"), "info"), "facts": []}
                            for m in s.get("messages") or []],
           "chapters": [], "actions": []}
    for cid in chapters:
        facts = [f for f in sheet["facts"] if f["chapter"] == cid and f["kind"] != "action"]
        sents = [x for x in (_fact_sentence(f) for f in facts[:3]) if x]
        if sents:
            out["chapters"].append({"id": cid, "paragraphs": [{"text": " ".join(sents), "facts": [f["id"] for f in facts[:3]]}],
                                    "charts": []})
    out["root_causes"], out["hidden_clues"], out["questions_to_check"] = [], [], []
    for f in [f for f in sheet["facts"] if f["kind"] == "bridge"][:1]:
        for p in (f.get("split") or [])[:2]:
            out["root_causes"].append({"cause": f"{p['part']}: {p.get('change') or ''}", "confidence": "confirmed",
                                       "evidence": f"{p['before']} → {p['now']} ({p.get('effect_on_revenue')})", "facts": [f["id"]]})
    for f in [f for f in sheet["facts"] if f["kind"] == "driver" and not f.get("note")][:2]:
        top = (f.get("biggest_drops") or f.get("biggest_gains") or [None])[0]
        if top:
            out["root_causes"].append({"cause": f"{f['topic']}: {top['name']} ({top['change']})", "confidence": "confirmed",
                                       "evidence": f"{top['before']} → {top['now']}", "facts": [f["id"]]})
    for f in [f for f in sheet["facts"] if f["kind"] == "action"][:6]:
        out["actions"].append({"title": f["topic"], "why": f.get("because") or "", "steps": [], "who": None,
                               "when": f.get("when"), "facts": [f["id"]]})
    return out


# ---------------------------------------------------------------- the writer
def _chapters(sheet: dict) -> list[str]:
    have = {f["chapter"] for f in sheet["facts"]}
    return [c for c in CHAPTERS if c in have]


def _restore(x, aliases: dict[str, str]):
    if not aliases:
        return x
    names = sorted(aliases, key=len, reverse=True)
    pat = re.compile("|".join(re.escape(a) for a in names))
    def walk(v):
        if isinstance(v, str):
            return pat.sub(lambda m: aliases[m.group(0)], v)
        if isinstance(v, list):
            return [walk(i) for i in v]
        if isinstance(v, dict):
            return {k: walk(i) for k, i in v.items()}
        return v
    return walk(x)


def _clean(ans: dict, chapters: list[str]) -> dict:
    """Keep only the agreed keys, in the agreed order; strip stray ids from texts."""
    def t(s):
        if not isinstance(s, str):
            return s
        s = DUP_CUR_RE.sub(r"\1", ID_RE.sub("", s))        # v39.3: "+12,53 Tr ₫ ₫" -> "+12,53 Tr ₫"
        s = EMPTY_PAREN_RE.sub("", s)                      # v39.4: "(F3, F4)" -> "" after the ids are removed
        s = re.sub(r"\s+([,.;:])", r"\1", re.sub(r"\s{2,}", " ", s))
        s = re.sub(r"([;,.])\s*[:;,]+", r"\1", s)         # v39.6: "F12: x; F13: y" -> "x; y" (no ";:")
        return re.sub(r"^[\s:;,.\-–]+", "", s).strip()
    got = {c.get("id"): c for c in ans.get("chapters") or [] if isinstance(c, dict)}
    return {"headline": t(ans.get("headline")),
            "key_messages": [{"text": t(m.get("text")), "tone": m.get("tone"), "facts": list(m.get("facts") or [])}
                             for m in ans.get("key_messages") or [] if isinstance(m, dict)],
            "chapters": [{"id": cid, "paragraphs": [{"text": t(p.get("text")), "facts": list(p.get("facts") or [])}
                                                    for p in got[cid].get("paragraphs") or [] if isinstance(p, dict)],
                          "charts": [{"id": c.get("id"), "caption": t(c.get("caption"))}
                                     for c in got[cid].get("charts") or [] if isinstance(c, dict)]}
                         for cid in chapters if cid in got],
            "actions": [{"title": t(a.get("title")), "why": t(a.get("why")),
                         "steps": [t(x) for x in a.get("steps") or [] if isinstance(x, str)],
                         "who": t(a.get("who")), "when": t(a.get("when")), "expected_effect": t(a.get("expected_effect")),
                         "facts": list(a.get("facts") or [])}
                        for a in ans.get("actions") or [] if isinstance(a, dict)],
            "root_causes": [{"cause": t(r.get("cause")), "evidence": t(r.get("evidence")), "confidence": r.get("confidence"),
                             "facts": list(r.get("facts") or [])} for r in ans.get("root_causes") or [] if isinstance(r, dict)],
            "hidden_clues": [{"text": t(c.get("text")), "facts": list(c.get("facts") or [])}
                             for c in ans.get("hidden_clues") or [] if isinstance(c, dict)],
            "questions_to_check": [t(q) for q in ans.get("questions_to_check") or [] if isinstance(q, str)]}


EMPTY_PAREN_RE = re.compile(r"\s*[(\[（]\s*(?:[,;、，]\s*)*[)\]）]")
DUP_CUR_RE = re.compile(r"([₫€$£¥])(?:\s*\1)+")
ITEM_RE = re.compile(r"^(key message|action|root cause|hidden clue|question) (\d+)\b")
ITEM_PARTS = {"messages": ("key_messages", "key message", "messages", 3), "actions": ("actions", "action", "actions", 3),
              "root_causes": ("root_causes", "root cause", "root_causes", 2),
              "hidden_clues": ("hidden_clues", "hidden clue", "hidden_clues", 1),
              "questions": ("questions_to_check", "question", "questions", 0)}


def repair_items(clean: dict, probs: dict, ans: dict, lg: str, prompt: dict,
                 chart_titles: dict | None = None) -> tuple[dict, list[str]]:
    """v39.3: small faults no longer cost a whole part.
    * a wrong 'who' / 'when' (e.g. English in a Vietnamese report) is left out of that action; the action stays;
    * a list item with a problem (one action using a banned word, one root cause with an unknown number ...) is
      left out when enough good items remain (the minimum count of the prompt limits); otherwise the part falls back."""
    repaired = []
    lim = prompt.get("limits") or {}
    acts = probs.get("actions") or []
    if acts:
        keep = []
        for p in acts:
            m = re.match(r"action (\d+) (who|when)\b", p)
            if m and int(m.group(1)) <= len(clean["actions"]):
                clean["actions"][int(m.group(1)) - 1][m.group(2)] = None
                repaired.append(f"action {m.group(1)}: {m.group(2)} left out")
            else:
                keep.append(p)
        if keep:
            probs["actions"] = keep
        else:
            probs.pop("actions")
    plo, phi = lim.get("paragraphs", [1, 3])
    cmax = lim.get("charts", [0, 2])[1]
    raw_ch = {c.get("id"): c for c in ans.get("chapters") or [] if isinstance(c, dict)}
    for ch in clean["chapters"]:
        part = f"chapter:{ch['id']}"
        ps = probs.get(part)
        rc = raw_ch.get(ch["id"]) or {}
        if not ps or len(rc.get("paragraphs") or []) != len(ch["paragraphs"]) or len(rc.get("charts") or []) != len(ch["charts"]):
            continue
        bad_p, bad_c, cut_p, cut_c, other = set(), set(), False, False, False
        fix_c = set()
        for p in ps:
            mp = re.match(r"chapter '[^']+' paragraph (\d+)\b", p)
            mc = re.match(r"chapter '[^']+' chart (\d+)\b", p)
            mcap = re.match(r"chapter '[^']+' chart (\d+) caption\b", p)
            if mp:
                bad_p.add(int(mp.group(1)))
            elif mcap and (chart_titles or {}).get((ch["charts"][int(mcap.group(1)) - 1] or {}).get("id")):
                fix_c.add(int(mcap.group(1)))   # v39.7: a faulty caption becomes the chart's own title
            elif mc:
                bad_c.add(int(mc.group(1)))
            elif re.match(r"chapter '[^']+': need \d+ to \d+ paragraphs", p) and len(ch["paragraphs"]) > phi:
                cut_p = True
            elif re.match(r"chapter '[^']+': at most \d+ charts", p):
                cut_c = True
            else:
                other = True
        paras = [x for i, x in enumerate(ch["paragraphs"], 1) if i not in bad_p][:phi]
        if other or len(paras) < max(plo, 1):
            continue
        fix_c -= bad_c
        for j in fix_c:
            ch["charts"][j - 1]["caption"] = chart_titles[ch["charts"][j - 1]["id"]]
        charts = [x for j, x in enumerate(ch["charts"], 1) if j not in bad_c][:cmax]
        did = [f"paragraph {i}" for i in sorted(bad_p)] + [f"chart {j}" for j in sorted(bad_c)]
        if cut_p and len(ch["paragraphs"]) - len(bad_p) > phi:
            did.append(f"paragraphs after {phi}")
        if cut_c and len(ch["charts"]) - len(bad_c) > cmax:
            did.append(f"charts after {cmax}")
        ch["paragraphs"], ch["charts"] = paras, charts
        probs.pop(part)
        if did:
            repaired.append(f"{part}: left out {', '.join(did)}")
        if fix_c:
            repaired.append(f"{part}: caption of chart {', '.join(map(str, sorted(fix_c)))} replaced by the chart title")
    for part, (key, label, lim_key, default_lo) in ITEM_PARTS.items():
        ps = probs.get(part)
        if not ps:
            continue
        raw = ans.get(key)
        if not isinstance(raw, list) or len(raw) != len(clean[key]):
            continue
        bad = set()
        for p in ps:
            m = ITEM_RE.match(p)
            if not m or m.group(1) != label:
                bad = None                          # a problem with the part as a whole (count, structure)
                break
            bad.add(int(m.group(2)))
        lo = (lim.get(lim_key) or [default_lo])[0]
        if bad and len(clean[key]) - len(bad) >= max(lo, 0 if part == "questions" else 1):
            clean[key] = [x for i, x in enumerate(clean[key], 1) if i not in bad]
            probs.pop(part)
            repaired.append(f"{part}: left out {', '.join(f'{label} {i}' for i in sorted(bad))}")
    return probs, repaired


def _merge(ai: dict, rules: dict, probs: dict, chapters: list[str]) -> tuple[dict, list[str]]:
    """Keep the AI parts that passed; replace failing parts by the rule-written ones."""
    if probs.get("structure"):
        return rules, ["all"]
    out, fallbacks = dict(ai), []
    if probs.get("headline"):
        out["headline"] = rules["headline"]; fallbacks.append("headline")
    if probs.get("messages"):
        out["key_messages"] = rules["key_messages"]; fallbacks.append("messages")
    rch = {c["id"]: c for c in rules["chapters"]}
    ach = {c["id"]: c for c in ai["chapters"]}
    chs = []
    for cid in chapters:
        if probs.get(f"chapter:{cid}") or cid not in ach:
            if cid in rch:
                chs.append(rch[cid])
            fallbacks.append(f"chapter:{cid}")
        else:
            chs.append(ach[cid])
    out["chapters"] = chs
    if probs.get("actions"):
        out["actions"] = rules["actions"]; fallbacks.append("actions")
    if probs.get("root_causes"):
        out["root_causes"] = rules["root_causes"]; fallbacks.append("root_causes")
    for part, key in (("hidden_clues", "hidden_clues"), ("questions", "questions_to_check")):
        if probs.get(part):
            out[key] = []; fallbacks.append(part)              # no rule-written version: left out
    every = {"headline", "messages", "actions"} | {f"chapter:{c}" for c in chapters}
    if every <= set(fallbacks) or (every - {"actions"} <= set(fallbacks) and "do" not in chapters):
        return rules, ["all"]                      # nothing usable was left
    return out, fallbacks


def write_story(doc: dict, catalog, llm, store, tenant_id: str, month: str, allow_ai: bool = True,
                fresh: str = "") -> dict:
    """The story for a composed report (never raises for AI problems)."""
    t0 = time.perf_counter()
    prompt = load_prompt()
    lg = doc["meta"]["lang"]
    sheet = build_fact_sheet(doc, catalog)
    sent_json = prompt_json(sheet)
    sent = json.loads(sent_json)
    chapters = _chapters(sheet)
    rules = rules_story(doc, sheet, chapters)
    meta = {"story_version": STORY_VERSION, "prompt_version": prompt.get("version"), "facts": len(sheet["facts"]),
            "chapters": chapters, "fallbacks": [], "problems": {}, "attempts": 0, "tokens_in": 0, "tokens_out": 0,
            "cost_usd": 0.0, "cached": False}

    def finish(story: dict, source: str, why: str | None = None) -> dict:
        story = _restore(story, sheet["aliases"])
        parts = sheet.get("chart_parts") or {}
        for c in story.get("chapters") or []:
            c["charts"] = [{**x, "part": parts.get(x["id"])} for x in c.get("charts") or [] if parts.get(x["id"])]
        meta.update(source=source, ms=int((time.perf_counter() - t0) * 1000))
        if why:
            meta["reason"] = why
        return {**story, "source": source, "meta": meta}

    if not allow_ai:
        return finish(rules, "rules", "quota")
    if not getattr(llm, "enabled", False) or not getattr(llm, "key", ""):
        return finish(rules, "rules", "disabled" if not getattr(llm, "enabled", False) else "no_key")
    meta.update(provider=llm.provider, model=llm.model)
    lang_name = prompt["language_names"].get(lg, lg)
    m = doc["meta"]
    fill = {"language": lang_name, "business": m.get("business") or "", "industry": m.get("industry") or "",
            "chapters": json.dumps(chapters)}

    def fmt(text: str, **more) -> str:          # {name} placeholders only - the prompt also contains JSON braces
        for k, v in {**fill, **more}.items():
            text = text.replace("{" + k + "}", str(v))
        return text
    system = fmt("\n".join(prompt["system"])) + "\n\n" + prompt["style"].get(lg, "")
    user = fmt(prompt["request"]) + "\n\nFACT SHEET:\n" + sent_json
    key_parts = [tenant_id, m.get("data_version"), hashlib.sha256(sent_json.encode()).hexdigest(), lg,
                 prompt.get("version"), hashlib.sha256(system.encode()).hexdigest()[:16]]
    if fresh:                                       # v39.5: a new value = new answers (test runs, not the cache)
        key_parts.append(f"fresh:{fresh}")
    kw = {"max_tokens": int(prompt.get("max_output_tokens") or 5000), "temperature": prompt.get("temperature")}
    titles = {c["id"]: c.get("title") for c in sent.get("charts") or [] if c.get("title")}
    ans, probs = None, None
    for attempt in (1, 2):
        u = user if attempt == 1 else (user + "\n\n" + fmt(
            prompt["retry"], problems="\n".join(f"- {p}" for ps in probs.values() for p in ps[:6])))
        try:
            r = cached_complete_json(llm, store, tenant_id, month, key_parts + [attempt, u if attempt > 1 else ""],
                                     system, u, **kw)
        except LLMError as e:
            meta["attempts"] += 1
            meta["tokens_in"] += getattr(e, "tokens_in", 0)
            meta["tokens_out"] += getattr(e, "tokens_out", 0)
            meta["cost_usd"] = round(meta["cost_usd"] + getattr(e, "cost_usd", 0.0), 6)
            meta["error"] = e.kind
            if ans is None:
                return finish(rules, "rules", f"ai_error:{e.kind}")
            break
        meta["attempts"] += 1
        meta["tokens_in"] += r.tokens_in
        meta["tokens_out"] += r.tokens_out
        meta["thinking_tokens"] = meta.get("thinking_tokens", 0) + getattr(r, "thinking_tokens", 0)
        meta["cost_usd"] = round(meta["cost_usd"] + r.cost_usd, 6)
        meta["cached"] = meta["cached"] or r.cached
        meta.update(provider=r.provider, model=r.model)          # v88: the model that really answered (a backup may have)
        ans = r.data
        probs = check_story(ans, sent, lg, prompt, chapters)
        if attempt == 1 and probs:
            meta["first_problems"] = probs          # v39.3: why a second (paid) attempt was needed
            # v39.3: small faults that can be repaired (a wrong who/when, one or two bad list items) do not
            # justify a second call that doubles time and cost
            rest, rep = repair_items(_clean(ans, chapters), dict(probs), ans, lg, prompt, titles)
            left_out = sum(r.count(",") + 1 for r in rep if ": left out " in r and not r.startswith("action "))
            if not rest and left_out <= int(prompt.get("repair_without_retry", 2)):
                meta["retry_skipped"] = True
                break
            if prompt.get("retry_thinking"):
                kw["thinking"] = prompt["retry_thinking"]
        if not probs:
            break
    meta["problems"] = probs or {}
    if ans is None:
        return finish(rules, "rules", "no_answer")
    clean = _clean(ans, chapters)
    if probs:
        meta["last_problems"] = probs               # the last answer's problems, before repairs
    probs, repaired = repair_items(clean, dict(probs or {}), ans, lg, prompt, titles)
    meta["problems"] = probs                        # what is left after repairs -> rule-written fallbacks
    if repaired:
        meta["repaired"] = repaired
    clean = fix_labels(clean, data_names(sent))
    story, fallbacks = _merge(clean, rules, probs, chapters)
    meta["fallbacks"] = fallbacks
    if fallbacks == ["all"]:
        return finish(rules, "rules", "checks_failed")
    return finish(story, "ai" if not fallbacks else "mixed")
