"""Talk-to-data Step 4: write the answer.

    ans = write_answer(question, results, lang, llm, steps=plan.steps, snapshot_ids=..., prompt=...)

* ``results``: one tool result per plan step (``registry.call`` output; it never raises).
* Empty and refused results get fixed, specific wording per language (no AI call).
* Otherwise the AI writes 2-3 sentences around placeholders ({T1}); the CODE inserts every figure, so a number can
  never be invented or misquoted. Any typed figure, unknown placeholder, banned term, invented name, wrong language or
  over-long text is a problem: one retry, then the rules answer (the tools' own formatted lines).
* The answer also carries the table / chart to show, caveats (e.g. "estimate") written by the code, and follow-ups.
"""
from __future__ import annotations

import json
import re
import logging
from dataclasses import dataclass, field
from pathlib import Path

from ...config import config_dir
from ...reports import story
from ..report_ask import _plain, invented_names
from . import brief as B
from . import compose as C
from .messages import br, follow_ups, msg

log = logging.getLogger("vdash.ask.answer")


@dataclass
class AnswerResult:
    text: str
    source: str                      # llm | rules | fixed
    kind: str                        # answer | empty | error | mixed
    facts_used: list[str] = field(default_factory=list)
    table: dict | None = None
    chart: dict | None = None
    follow_ups: list[str] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)
    llm: dict | None = None
    problems: list[str] = field(default_factory=list)
    headline: str = ""               # the opening sentence(s): the direct answer (text = headline + details + notes)
    points: list[dict] = field(default_factory=list)       # detail lines, built by code (see brief.py)
    verdict: dict | None = None      # {"tone": good | bad | mixed | flat, "label": ...}

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def plain_text(headline: str, points: list[dict], caveats: list[str]) -> str:
    """The whole answer as plain text (copy, history, audit): opening sentence, then the detail lines, then notes."""
    lines = [headline.strip()] if headline.strip() else []
    if points:
        lines.append("")
        for p in points:
            t = str(p.get("text") or "").strip()
            if not t:
                continue
            if p.get("heading"):
                lines.append(t)
            else:
                extra = " ".join(x for x in (f"({p['meta']})" if p.get("meta") else "", p.get("why") or "") if x)
                lines.append(("  " if p.get("indent") else "") + "• " + t + (f" {extra}" if extra else ""))
    if caveats:
        lines.append("")
        lines += [f"({c})" for c in caveats]
    return "\n".join(lines).strip()


def load_prompt(path: Path | None = None) -> dict:
    return json.loads((path or config_dir() / "ask_prompts" / "answer_writer.json").read_text(encoding="utf-8"))


def _fill(s: str, **kw) -> str:
    for k, v in kw.items():
        s = s.replace("{" + k + "}", str(v))
    return s


VI_SPECIFIC = re.compile(r"[ăâđêôơưạảãấầẩẫậắằẳẵặẹẻẽếềểễệịỉĩọỏốồổỗộớờởỡợụủứừửữựỳỵỷỹ]", re.I)
MORE_ITEMS = re.compile(r"\b(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|zwei|drei|vier|fünf|sechs|sieben|acht|neun|zehn)\s+"
                        r"(?:more|additional|other|further|weitere|zusätzliche|khác|nữa|mục)\b|\b(?:table|tabelle|bảng)\s+(?:shows|zeigt|hiển thị)\s+\d+", re.I)
NO_ONE_MOVED = re.compile(r"\b(no|none|nothing|not a single|keine?|kein|niemand|không có|không)\b.{0,60}\b(gain\w*|rose|rise\w*|grew|grow\w*|increas\w*|fell|fall\w*|lost|los\w*|declin\w*|"
                          r"gestiegen|gewachsen|gewonnen|gefallen|verloren|gesunken|tăng|giảm|mất)\b", re.I)
COMPARED_WORDS = re.compile(r"\b(compared|among the|of the compared|in both|top list|verglichen|unter den|in beiden|so sánh|trong số)\b", re.I)
AS_OF_FIGURE = re.compile(r"\b(as of|since|tính đến|kể từ|stand|seit|zum)\s+\{(T\d+(?:\.\d+)?)\}", re.I)
VAGUE_PERIOD = {"en": ("previous period", "last period", "prior period"), "de": ("vorherigen zeitraum", "vorigen zeitraum", "vorperiode"),
                "vi": ("kỳ trước", "kì trước", "giai đoạn trước")}      # a period named only as "the previous one" - nobody knows which dates
BUSINESS_WORDS = {"de": "Geschäft Geschäfts Geschäftes Ihr Ihre Ihrem Ihren Ihres Ihrer Unternehmen Unternehmens Betrieb",
                  "vi": "cửa hàng doanh nghiệp", "en": ""}        # what the writer may call the shop without it being an invented name


NAME_STOP = {"the", "and", "for", "with", "von", "der", "die", "das", "und", "của", "các", "và", "last", "first", "next"}


def _is_named(name: str, low_text: str) -> bool:
    """Is a listed item named in the answer? Whole name, or one of its words (case aside; "Fr" is the start of "Freitag")."""
    n = name.strip().lower()
    if len(n) < 2:
        return False
    if n in low_text:
        return True
    words = re.findall(r"[^\W\d_]+", low_text)
    toks = [t for t in re.findall(r"[^\W\d_]+", n) if t not in NAME_STOP]
    long_ = [t for t in toks if len(t) >= 3]
    for t in (long_ or toks[:1]):
        if any(w.startswith(t) for w in words):
            return True
    return False


def check(text, values: dict, names: list[str], vocab_sent: dict, question: str, lang: str, prompt: dict,
          change_keys: list[str] | None = None, positions: dict | None = None, directions: dict | None = None, statements=(), trend_ids=(), movers_limited=False) -> list[str]:
    """Problems of a written answer (empty = fine). ``text`` still contains the placeholders."""
    if not isinstance(text, str) or not text.strip():
        return ["answer: empty"]
    probs = []
    _, unknown = C.substitute(text, values)
    for u in unknown:
        probs.append(f"answer: the placeholder {{{u}}} does not exist - use only the placeholders listed in the facts")
    plain = text
    for nm in sorted({x for x in names if x}, key=len, reverse=True):      # product names may hold digits ("Shampoo 500ml")
        plain = plain.replace(nm, " ")
    qdigits = {re.sub(r"\D", "", t) for t in re.findall(r"\d[\d.,]*", question or "")}        # "the top 20%": the reader's own number may be repeated
    for n in C.stray_numbers(plain):
        if re.sub(r"\D", "", n) in qdigits:
            continue
        probs.append(f"answer: the figure '{n}' was typed - write figures only as placeholders such as {{T1}}")
    for m in AS_OF_FIGURE.finditer(text):                                              # "as of {T3}": a figure placed where a date belongs
        shown = str(values.get(m.group(2)) or "").strip()
        if re.fullmatch(r"[+\-−–]?[\d.,\s]+", shown or "x"):
            probs.append(f"answer: '{m.group(1)} {{{m.group(2)}}}' - {{{m.group(2)}}} is a figure, not a date; leave the date out or use the period given in the facts")
            break
    used = {m.group(1) + (f".{m.group(2)}" if m.group(2) else "") for m in C.PLACEHOLDER.finditer(text)}
    must = list(change_keys or [])                                                      # a change was asked: show it
    for k in must[:1]:
        if k not in used:
            probs.append(f"answer: the change {{{k}}} must appear in the answer as evidence")
    low, qlow = text.lower(), (question or "").lower()
    if not change_keys:                                                              # no comparison in the facts: "the previous period" would name dates nobody gave
        for w in VAGUE_PERIOD.get(lang, ()):
            if w in low and w not in qlow:
                probs.append(f"answer: do not say '{w}' - no comparison period is in the facts; name the period only through its own dates or leave it out")
                break
    figs = {k for k in values if not k.endswith((".9", ".12", ".14", ".3", ".18"))}                # period and date wording is not evidence
    named = sum(1 for nm in (positions or {}) if _is_named(str(nm), low))                      # ranked items named from the facts count as evidence too
    lettered = [n for n in (positions or {}) if re.search(r"[^\W\d_]", str(n))]                  # "2", "4-5" (basket sizes) are figures, not names the answer could say
    if len(lettered) >= 2 and not named:
        probs.append("answer: name at least the first item of the list from the facts - figures alone do not say which one it is")
    if len(used & figs) + named < min(2, len(figs)):
        probs.append("answer: give at least two figures from the facts as evidence, written as placeholders")
    for nm, why in C.ordering_errors(text, positions or {}, lang):
        probs.append(f"answer: '{nm}' is the {why} one in the facts, so it cannot be called the opposite")
    for k, d in C.direction_errors(text, directions or {}, lang):
        probs.append(f"answer: the facts say {{{k}}} went {d}, but the sentence says the opposite - state the real direction (the question may assume the wrong one)")
    for sent in re.split(r"(?<=[.!?])\s+", text):
        ks = {m.group(1) + (f".{m.group(2)}" if m.group(2) else "") for m in C.PLACEHOLDER.finditer(sent)}
        for fid in trend_ids:
            if fid in ks and f"{fid}.9" in ks:
                probs.append(f"answer: {{{fid}}} is the most recent point of the series, not a figure for the whole period {{{fid}.9}} - name its own month with {{{fid}.18}}")
    if movers_limited and NO_ONE_MOVED.search(C.PLACEHOLDER.sub(" ", text)) and not COMPARED_WORDS.search(text):
        probs.append("answer: only items in the top list of both periods are compared - say 'among the compared items', never that no item at all rose or fell")
    if MORE_ITEMS.search(text):
        probs.append("answer: do not say how many more items there are - say that the table shows the rest")
    rep = [k for k in {m.group(0) for m in C.PLACEHOLDER.finditer(text)} if k.endswith(".9}") and text.count(k) > 1]
    if rep:
        probs.append(f"answer: the period {rep[0]} is written twice - name the period once")
    if len({m.group(1) for m in C.PLACEHOLDER.finditer(text) if m.group(1) in statements}) > 2:
        probs.append("answer: use at most two of the finished-sentence facts (the ones that matter most), not all of them")
    for k in C.statement_errors(text, statements):
        probs.append(f"answer: {{{k}}} is a finished sentence - write it alone as its own sentence, do not build other words around it")
    limit = float(prompt.get("max_words", 70)) * float((prompt.get("word_factor") or {}).get(lang, 1.0))
    words = story._words(C.PLACEHOLDER.sub("x", text), lang)
    if words > limit * 1.25:
        probs.append(f"answer: too long ({words} words, max {round(limit)})")
    bare = C.PLACEHOLDER.sub(" ", text)
    for nm in invented_names(bare, vocab_sent, question):
        if lang == "de" and not VI_SPECIFIC.search(nm):          # German nouns are capitalised: "Frühe Morgen" is not a person's name
            continue
        probs.append(f"answer: the name '{nm}' is not in the facts - copy names exactly as given")
    low = text.lower()
    for b in prompt.get("banned_terms") or []:
        if b.lower() in low:
            probs.append(f"answer: technical word '{b}' - use plain business words")
    known = {w.lower() for n in names for w in re.findall(r"\w+", n)}                  # words of names and labels from the facts
    spoken = " ".join(w for w in re.findall(r"\w+", _plain(bare, names)) if w.lower() not in known)
    if not story.language_ok(spoken, lang):
        probs.append(f"answer: not written in {(prompt.get('language_names') or {}).get(lang, lang)}")
    return probs


def _clean_follow_ups(xs, lang: str, prompt: dict, limit: int = 2) -> list[str]:
    out = []
    for x in xs if isinstance(xs, list) else []:
        if not isinstance(x, str) or not x.strip() or len(x) > 120 or any(c.isdigit() for c in x) or "{" in x:
            continue
        if any(b.lower() in x.lower() for b in prompt.get("banned_terms") or []):
            continue
        if not story.language_ok(x, lang) or x in out:
            continue
        out.append(x.strip())
    return out[:limit]


def write_answer(question: str, results: list[dict], lang: str, llm=None, *, steps: list[dict] | None = None,
                 snapshot_ids=(), prompt: dict | None = None, period_text: str = "", reveal_names: bool = True,
                 previous: dict | None = None) -> AnswerResult:
    lang = lang if lang in ("en", "vi", "de") else "en"
    prompt = prompt or load_prompt()
    steps = steps or [{"tool": (r or {}).get("tool", "")} for r in results]
    parts = [C.classify((steps[i].get("tool") if i < len(steps) else "") or (r or {}).get("tool", ""), r, lang, snapshot_ids)
             for i, r in enumerate(results)]
    ok = [p for p in parts if p.kind == "ok"]
    notes = []
    for p in parts:
        if p.kind != "ok" and p.fixed not in notes:
            notes.append(p.fixed)

    if not ok:                                         # nothing to write about: fixed, specific wording, no AI call
        kind = "error" if any(p.kind == "error" for p in parts) else "empty"
        return AnswerResult(" ".join(notes) or msg("nothing", lang), "fixed", kind)

    brief = B.build(parts, question, lang)
    shown, values = C.payload_facts(parts, lang, keep=brief.keep, period_of=brief.period_of)
    period = brief.period_text or period_text or next((((p.result.get("meta") or {}).get("period") or {}).get("text", "") for p in ok), "")
    names = [n for f in shown for n in f.get("names") or []] + [str(f.get("label") or "") for f in shown] + [str(f["name"]) for f in shown if f.get("name")]
    names += [part.strip() for n in list(names) for part in re.split(r"\s*(?::|/)\s*", n) if len(part.strip()) >= 4]       # "Top products: Shampoo 500ml" -> "Shampoo 500ml"
    vocab_sent = {"facts": [{"text": " ".join(names)}], "business": BUSINESS_WORDS.get(lang, ""), "industry": "", "period": period}
    first_tool = ok[0].tool
    positions = {n: e["position"] for e in shown if e.get("position") for n in e.get("names") or []}
    directions = {e["id"]: e["direction"] for e in shown if e.get("direction") and e.get("id")}
    directions.update({e["id"] + ".16": e["half_direction"] for e in shown if e.get("half_direction") and e.get("id")})
    statements = {e["id"] for e in shown if e.get("statement")}
    change_keys = [e["id"] for e in shown if e.get("direction") and e["id"] in values and not e.get("now") and not e.get("statement")]
    change_keys = [e["id"] + ".2" for e in shown if e.get("previous_value") and e["id"] + ".2" in values] + change_keys   # a change fact must be quoted
    ranked_names = {str(e["name"]): e["rank"] for e in shown if e.get("name") and e.get("rank") is not None}
    text, source, ai_meta, problems, fups = "", "rules", None, [], []

    if brief.headline:                                 # a list answer (warnings, actions): fixed opening sentence, no AI call
        text = brief.headline
    elif llm is not None:
        system = _fill(prompt["system"], language=(prompt.get("language_names") or {}).get(lang, lang),
                       style=(prompt.get("style") or {}).get(lang, ""))
        pn = C.period_names(steps, lang)
        prev = C.previous_brief(previous, lang)
        payload = {"question": question, "period": period, **({"period_names": pn} if pn else {}),
                   **({"previous_turn": prev} if prev else {}),
                   **({"verdict": brief.writer_hint} if brief.writer_hint else {}),
                   **({"the_system_adds_after_your_answer": "a detail list and a one-sentence reading of the main figure"}
                      if brief.points or brief.reading else {}),
                   "facts": shown}
        user = json.dumps(payload, ensure_ascii=False) + "\n\n" + _fill(prompt["request"], question=question, period=period)
        lead = C.lead_expected(ranked_names, question, lang)
        for attempt in (0, 1):
            try:
                res = llm.complete_json(system, user if not problems else user + "\n\n" + _fill(
                    prompt["retry"], problems="\n".join(f"- {x}" for x in problems), language=(prompt.get("language_names") or {}).get(lang, lang)),
                    max_tokens=int(prompt.get("max_tokens", 600)), temperature=float(prompt.get("temperature", 0.2)))
            except Exception as exc:        # noqa: BLE001 - any AI failure falls back to the rules answer
                log.info("answer writer AI unavailable (%s)", type(exc).__name__)
                problems = [f"ai_failed:{getattr(exc, 'kind', type(exc).__name__)}"]
                break
            ai_meta = res.meta() if hasattr(res, "meta") else None
            data = res.data if isinstance(res.data, dict) else {}
            raw = data.get("answer")
            problems = check(raw, values, names, vocab_sent, question, lang, prompt, change_keys, positions, directions, statements,
                             {e["id"] for e in shown if e.get("latest_at") and e.get("id")}, first_tool == "top_movers")
            if not problems and lead:
                first = C.first_ranked_name(raw, ranked_names)
                if first and first != lead:
                    problems = [f"answer: start with '{lead}' - it is the one the question asks for, not '{first}'"]
            if not problems:
                text, _ = C.substitute(raw.strip(), values)
                source = "llm"
                break
    if not text:
        text = rules_headline(parts, brief, shown, values, lang)

    if not text:
        text = msg("nothing", lang)
    if len(ok) < len(parts) and notes:                 # some steps worked, some did not
        text += " " + " ".join(notes)
    if brief.reading:                                  # the code's own reading of the figure ("demand barely reacts to price")
        text = text.rstrip() + " " + brief.reading
    text = C.tidy(text)
    aliases = C.alias_map(parts) if reveal_names else {}
    text = C.reveal_text(text, aliases)               # the AI only ever saw 'Customer A'; the reader gets the real names
    points = [{**pt, **{k: C.reveal_text(pt[k], aliases) for k in ("text", "why", "meta") if pt.get(k)}} for pt in brief.points]
    cav = [C.nice_dates(c, lang) for c in C.caveats_of(parts)]          # "starts 2026-10-04" -> "starts 4 Oct 2026"
    for c in brief.notes:
        c = C.nice_dates(c, lang)
        if c not in cav:
            cav.append(c)
    cav = [C.reveal_text(c, aliases) for c in cav]
    return AnswerResult(text=plain_text(text, points, cav), source=source, kind="answer" if len(ok) == len(parts) else "mixed",
                        facts_used=[f["id"] for f in shown], table=C.reveal_table(C.pick_table(parts), aliases), chart=C.pick_chart(parts, reveal=reveal_names),
                        follow_ups=fups or follow_ups(first_tool, lang)[:2], caveats=cav, llm=ai_meta, problems=problems,
                        headline=text, points=points, verdict=brief.verdict)


def rules_headline(parts, brief, shown, values, lang: str) -> str:
    """The opening sentence without the AI: the verdict in words (when the verdict compares with an earlier period),
    then the tools' own formatted line of the key fact(s)."""
    keep = brief.keep
    tools_ok = {p.tool for p in parts if p.kind == "ok"}
    lines = []
    for p in parts:
        if p.kind != "ok":
            continue
        for f in p.result["facts"]:
            if keep is not None and f["id"] not in keep:
                continue
            t = (f.get("text") or "").strip().rstrip(".")
            if t and t not in lines:
                lines.append(t)
    lines = [brief.rules.rstrip(".")] if brief.rules else lines[:1 if brief.verdict else 2]
    body = ". ".join(lines) + ("." if lines else "")
    period = brief.period_text or next((((p.result.get("meta") or {}).get("period") or {}).get("text", "") for p in parts if p.kind == "ok"), "")
    if period and body and not (tools_ok & {"forecast", "compare_periods", "explain_change"}) and period.lower() not in body.lower() and not any(c in body for c in "()"):
        body = f"{body[:-1]} ({period})."
    tools = {p.tool for p in parts if p.kind == "ok"}
    v = brief.verdict
    if v and not brief.reading and tools & {"kpi_value", "compare_periods", "findings", "explain_change"} and not (brief.rules and "top_movers" in tools) and "warnings" not in tools:
        key = {"bad": "say_worse", "good": "say_better", "flat": "say_same", "mixed": "say_mixed"}.get(v["tone"])
        if key:
            return C.nice_dates(f"{br(key, lang)}. {body}".strip(), lang)
    return C.nice_dates(body, lang)
