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
import logging
from dataclasses import dataclass, field
from pathlib import Path

from ...config import config_dir
from ...reports import story
from ..report_ask import _plain, invented_names
from . import compose as C
from .messages import follow_ups, msg

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

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def load_prompt(path: Path | None = None) -> dict:
    return json.loads((path or config_dir() / "ask_prompts" / "answer_writer.json").read_text(encoding="utf-8"))


def _fill(s: str, **kw) -> str:
    for k, v in kw.items():
        s = s.replace("{" + k + "}", str(v))
    return s


def check(text, values: dict, names: list[str], vocab_sent: dict, question: str, lang: str, prompt: dict,
          change_keys: list[str] | None = None) -> list[str]:
    """Problems of a written answer (empty = fine). ``text`` still contains the placeholders."""
    if not isinstance(text, str) or not text.strip():
        return ["answer: empty"]
    probs = []
    _, unknown = C.substitute(text, values)
    for u in unknown:
        probs.append(f"answer: the placeholder {{{u}}} does not exist - use only the placeholders listed in the facts")
    for n in C.stray_numbers(text):
        probs.append(f"answer: the figure '{n}' was typed - write figures only as placeholders such as {{T1}}")
    used = {m.group(1) + (f".{m.group(2)}" if m.group(2) else "") for m in C.PLACEHOLDER.finditer(text)}
    must = [k for k in values if k.endswith(".2") and (k[:-2] + ".1") in values]          # a change was asked: show it
    must = must + list(change_keys or [])
    for k in must[:1]:
        if k not in used:
            probs.append(f"answer: the change {{{k}}} must appear in the answer as evidence")
    if len(used & set(values)) < min(2, len(values)):
        probs.append("answer: give at least two figures from the facts as evidence, written as placeholders")
    limit = float(prompt.get("max_words", 70)) * float((prompt.get("word_factor") or {}).get(lang, 1.0))
    words = story._words(C.PLACEHOLDER.sub("x", text), lang)
    if words > limit * 1.25:
        probs.append(f"answer: too long ({words} words, max {round(limit)})")
    bare = C.PLACEHOLDER.sub(" ", text)
    for nm in invented_names(bare, vocab_sent, question):
        probs.append(f"answer: the name '{nm}' is not in the facts - copy names exactly as given")
    low = text.lower()
    for b in prompt.get("banned_terms") or []:
        if b.lower() in low:
            probs.append(f"answer: technical word '{b}' - use plain business words")
    if not story.language_ok(_plain(bare, names), lang):
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
                 snapshot_ids=(), prompt: dict | None = None, period_text: str = "", reveal_names: bool = True) -> AnswerResult:
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

    shown, values = C.payload_facts(parts, lang)
    period = period_text or next((((p.result.get("meta") or {}).get("period") or {}).get("text", "") for p in ok), "")
    names = [n for f in shown for n in f.get("names") or []] + [str(f.get("label") or "") for f in shown]
    vocab_sent = {"facts": [{"text": " ".join(names)}], "business": "", "industry": "", "period": period}
    first_tool = ok[0].tool
    change_keys = [e["id"] for e in shown if e.get("direction") and e["id"] in values]    # a change fact must be quoted
    text, source, ai_meta, problems, fups = "", "rules", None, [], []

    if llm is not None:
        system = _fill(prompt["system"], language=(prompt.get("language_names") or {}).get(lang, lang),
                       style=(prompt.get("style") or {}).get(lang, ""))
        pn = C.period_names(steps, lang)
        user = json.dumps({"question": question, "period": period, **({"period_names": pn} if pn else {}), "facts": shown}, ensure_ascii=False) + "\n\n" + \
            _fill(prompt["request"], question=question, period=period)
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
            problems = check(raw, values, names, vocab_sent, question, lang, prompt, change_keys)
            if not problems:
                text, _ = C.substitute(raw.strip(), values)
                source = "llm"
                break
    if source != "llm":
        text = C.rules_text(parts, lang)

    if not text:
        text = msg("nothing", lang)
    if len(ok) < len(parts) and notes:                 # some steps worked, some did not
        text += " " + " ".join(notes)
    aliases = C.alias_map(parts) if reveal_names else {}
    text = C.reveal_text(text, aliases)               # the AI only ever saw 'Customer A'; the reader gets the real names
    cav = C.caveats_of(parts)
    for c in cav:
        if c not in text:
            text += f" ({c})"
    return AnswerResult(text=text, source=source, kind="answer" if len(ok) == len(parts) else "mixed",
                        facts_used=[f["id"] for f in shown], table=C.reveal_table(C.pick_table(parts), aliases), chart=C.pick_chart(parts, reveal=reveal_names),
                        follow_ups=fups or follow_ups(first_tool, lang)[:2], caveats=cav, llm=ai_meta, problems=problems)
