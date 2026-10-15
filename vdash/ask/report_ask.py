"""Ask this report (talk-to-data Step 1).

A question about ONE stored report is answered from that report's fact sheet only:

1. ``select_facts``   picks the facts that bear on the question (lexical match + chapter intent; no AI call).
2. ``answer_question`` asks the AI writer once (twice at most) for a short JSON answer, then checks it with the
   same guards as the report story (``vdash/reports/story.py``): every number must be written in the fact sheet,
   cited fact ids must exist, language, banned words, length. An answer that fails the checks is replaced by a
   rule-written answer built from the selected facts - the user always gets an answer, and a failed AI call is
   never charged to the plan.
3. Evidence (the cited facts as written in the report) is rendered by code, never by the AI.

Pure logic: no HTTP, no quota bookkeeping (the caller passes ``reserve`` / ``release`` callbacks).
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import unicodedata
from pathlib import Path
from typing import Callable

from ..config import config_dir
from ..llm import LLMError, cache_key, cached_complete_json
from ..reports import story
from ..reports.facts import prompt_json

ASK_VERSION = 1
log = logging.getLogger("vdash.ask")
_CACHE: dict[str, tuple[float, dict]] = {}


def prompt_path() -> Path:
    return config_dir() / "ask_prompts" / "ask_report.json"


def load_prompt(path: Path | None = None) -> dict:
    path = path or prompt_path()
    mtime = path.stat().st_mtime
    hit = _CACHE.get(str(path))
    if hit and hit[0] == mtime:
        return hit[1]
    raw = json.loads(path.read_text(encoding="utf-8"))
    _CACHE[str(path)] = (mtime, raw)
    return raw


def validate_prompt(p: dict) -> list[str]:
    errs = []
    for k in ("version", "language_names", "system", "style", "request", "retry", "max_words", "max_facts"):
        if k not in p:
            errs.append(f"ask prompt: missing '{k}'")
    return errs


# ---------------------------------------------------------------- text helpers
def fold(s: str) -> str:
    """Lower-case without accents (so 'doanh thu' matches 'Doanh thu' and 'doanh số' matches 'doanh so')."""
    s = unicodedata.normalize("NFD", (s or "").lower().replace("đ", "d"))
    return "".join(c for c in s if unicodedata.category(c) != "Mn")


STOP = set("""the and for with was are you your our what why how did does this that these those from have has had will
would should could can about into than then them they there their which when where who whom been being but not any all
just more most some such only over under very per last next week month year tell show give please
cua va la cac nhung mot cho voi trong khi nay no den tu thi ma co khong duoc nhu the nao sao gi ra vao
der die das und ist ein eine nicht mit von zu den dem des auf fur wie was warum haben wir ich""".split())


# words a question uses for what the fact sheet writes differently (folded; question side only)
SYN = {"best": ["highest", "top", "cao nhat"], "worst": ["lowest", "bottom", "thap nhat"], "weakest": ["lowest"],
       "strongest": ["highest"], "busiest": ["highest", "peak"], "slowest": ["lowest"], "biggest": ["highest", "top"],
       "sales": ["revenue", "doanh thu"], "turnover": ["revenue"], "income": ["revenue"], "umsatz": ["revenue"],
       "declined": ["fell"], "dropped": ["fell"], "decrease": ["fell"], "grew": ["rose"], "increased": ["rose"],
       "customers": ["customer"], "clients": ["customer"], "kunden": ["customer"], "khach": ["customer", "khach hang"]}


def tokens(text: str, expand: bool = False) -> list[str]:
    t = fold(text)
    if expand:
        t += " " + " ".join(x for w in re.findall(r"[a-z]+", t) for x in SYN.get(w, []))
        for vi, en in (("cao nhat", "highest"), ("thap nhat", "lowest"), ("te nhat", "lowest")):
            if vi in t:
                t += " " + en
    out = [w for w in re.findall(r"[a-z]+|\d+", t) if (len(w) >= 3 or w.isdigit()) and w not in STOP and not w.isdigit()]
    cjk = re.findall(r"[一-鿿]", text or "")
    out += ["".join(cjk[i:i + 2]) for i in range(len(cjk) - 1)] + (cjk if len(cjk) == 1 else [])
    return out


# chapter / kind intent from question wording (folded). Values: (chapter, kind) boosts.
INTENTS = [
    ({"why", "reason", "cause", "caused", "explain", "because", "driver", "drove", "behind", "warum", "wieso", "grund",
      "tai sao", "vi sao", "nguyen nhan", "ly do", "为什么", "原因", "为何"}, {"chapter": "why", "kinds": {"bridge", "driver"}, "w": 1.5}),
    ({"worry", "risk", "problem", "wrong", "concern", "warning", "alert", "issue", "sorgen", "risiko", "problem",
      "lo ngai", "van de", "rui ro", "canh bao", "担心", "风险", "问题"}, {"kinds": {"warning", "finding"}, "w": 3.0}),
    ({"should i do", "should we do", "to do", "do about", "action", "recommend", "advice", "improve", "fix", "todo", "plan", "was soll", "empfehl", "massnahme",
      "nen lam", "lam gi", "hanh dong", "de xuat", "khuyen", "cai thien", "应该", "建议", "怎么办", "行动"}, {"chapter": "do", "kinds": {"action"}}),
    ({"forecast", "expect", "predict", "next", "coming", "future", "outlook", "prognose", "erwart", "kunftig",
      "du bao", "sap toi", "du kien", "tuan toi", "预测", "未来", "预计"}, {"chapter": "next"}),
    ({"how", "overall", "summary", "summarize", "performance", "doing", "went", "happened", "wie", "zusammenfass",
      "tong quan", "tinh hinh", "ket qua", "thang nay", "tom tat", "kinh doanh", "ky nay", "怎么样", "总结", "概况", "表现"},
     {"chapter": "happened", "kinds": {"figure"}, "w": 2.0}),
    ({"product", "item", "seller", "selling", "best", "top", "worst", "produkt", "san pham", "mon", "ban chay", "产品", "商品"}, {"kinds": {"list", "driver"}}),
]
GENERIC = {"how did we do", "tong quan", "summary", "overview", "what happened", "tinh hinh"}


def intent_of(question: str) -> list[dict]:
    q = fold(question)
    raw = question or ""
    hits = []
    for words, spec in INTENTS:
        if any((fold(w) in q) if re.match(r"[a-z ]+$", fold(w)) else (w in raw) for w in words):
            hits.append(spec)
    return hits


def _fact_text(f: dict) -> str:
    return " ".join(story._strings({k: v for k, v in f.items() if k not in ("raw", "source", "importance", "id")}))


def _blend(scored: list, question: str, embedder) -> tuple[list, float]:
    """Re-rank with a local embedding model: score = word score (0..1) * (1-w) + similarity (0..1) * w, both scaled over
    the facts of this report. Returns (scored, best cosine). Any failure leaves the word ranking untouched."""
    try:
        qv = embedder.embed_query(question)
        dv = embedder.embed_docs([_fact_text(f) for _, _, f in scored])
    except Exception as exc:   # noqa: BLE001 - the embedder is optional
        log.warning("ask embedder failed, using word matching only: %s", exc)
        return scored, 0.0
    cos = [max(0.0, sum(a * b for a, b in zip(qv, d))) for d in dv]
    top_s = max((s for s, _, _ in scored), default=0.0) or 1.0
    lo, hi = min(cos), max(cos)
    w = float(getattr(embedder, "weight", 0.5))
    out = []
    for (s, m, f), c in zip(scored, cos):
        c01 = (c - lo) / (hi - lo) if hi > lo else 0.0
        out.append((round(((1 - w) * (max(s, 0.0) / top_s) + w * c01) * 10, 3), m, f))
    out.sort(key=lambda x: (-x[0], x[2]["id"]))
    return out, max(cos, default=0.0)


def select_facts(sheet: dict, question: str, k: int = 14, embedder=None) -> list[tuple[float, dict]]:
    """The facts that bear on the question, best first: [(score, fact)]. Always at least the most important
    facts (so a broad question like 'how did we do' has material); score 0 facts are only padding.
    With an ``embedder`` the order is a blend of word matching and meaning similarity (see embeddings.py)."""
    facts = [f for f in sheet.get("facts") or [] if f.get("kind") != "chart" or f.get("text")]
    q_tokens = set(tokens(question, expand=True))
    intents = intent_of(question)
    docs = []
    df: dict[str, int] = {}
    for f in facts:
        toks = set(tokens(_fact_text(f)))
        docs.append(toks)
        for t in toks:
            df[t] = df.get(t, 0) + 1
    n = max(len(facts), 1)
    scored = []
    for f, toks in zip(facts, docs):
        s = 0.0
        for t in q_tokens & toks:
            s += 1.0 + 2.0 * (1 - df[t] / n)                      # rarer words count more
        # a prefix match ("revenue" in "revenues", "payment" in "payments")
        for t in q_tokens - toks:
            if len(t) >= 5 and any(x.startswith(t[:5]) for x in toks):
                s += 0.6
        match = s
        for spec in intents:
            w = float(spec.get("w", 1.5))
            if spec.get("chapter") == f.get("chapter"):
                s += w
            if f.get("kind") in spec.get("kinds", ()):
                s += w
        s += float(f.get("importance") or 0) * 0.08
        scored.append((s, match, f))
    scored.sort(key=lambda x: (-x[0], x[2]["id"]))
    best_match = max((m for _, m, _ in scored), default=0.0)
    if embedder is not None and scored:
        scored, best_cos = _blend(scored, question, embedder)
        if best_cos >= float(getattr(embedder, "min_cos", 0.35)):
            best_match = max(best_match, 1.0)          # the meaning matches even if no word does
    return [(round(s, 3), f) for s, _, f in scored[:k]], best_match, bool(intents)


def fact_line(f: dict) -> str:
    return story._fact_sentence(f) or f.get("text") or ""


# ---------------------------------------------------------------- checking an AI answer
def _plain(text: str, names: list[str]) -> str:
    for n in names:
        text = text.replace(n, " ")
    return story.ID_RE.sub("", text)


_WORD = re.compile(r"[^\W\d_][\w'’-]*")


def invented_names(text: str, sent: dict, question: str = "") -> list[str]:
    """Names that are not in the facts sent: a run of two or more capitalised words (e.g. a customer the report only calls
    'Customer F' must not become 'Nguyen Thanh An'). The first word of a sentence is ignored (it is capitalised anyway);
    every other word of the run must occur in the facts or the question."""
    def exact(t: str) -> set:                       # accents kept: 'Nguyên nhân' (cause) must not vouch for 'Nguyễn' (a surname)
        return set(re.findall(r"\w+", unicodedata.normalize("NFC", t or "").lower()))
    vocab = set()
    for f in sent.get("facts") or []:
        vocab |= exact(_fact_text(f))
    for k in ("business", "industry", "period"):
        vocab |= exact(str(sent.get(k) or ""))
    vocab |= exact(question)
    text = text or ""
    words = [(m.group(0), m.start(), m.end()) for m in _WORD.finditer(text)]
    runs, cur = [], []
    for i, (w, st, en) in enumerate(words):
        before = text[:st].rstrip(" \t")
        first = not before or before[-1] in ".!?:;•—–\n"          # the first word of a sentence
        joined = bool(cur) and text[words[i - 1][2]:st] in (" ", "\u00a0")
        if w[0].isupper() and joined:
            cur.append((w, first))
        else:
            if cur:
                runs.append(cur)
            cur = [(w, first)] if w[0].isupper() else []
    if cur:
        runs.append(cur)
    bad = []
    for run in runs:
        ws = [w for w, _ in run][1:] if run[0][1] else [w for w, _ in run]    # drop a sentence's first word
        if len(ws) < 2:
            continue
        if any(len(p) >= 3 and p not in vocab for w in ws for p in exact(w)):
            bad.append(" ".join(ws))
    return bad[:3]


def check_answer(ans, sent: dict, lg: str, prompt: dict) -> list[str]:
    """Problems of an AI answer (empty = fine)."""
    if not isinstance(ans, dict):
        return ["the answer is not a JSON object"]
    probs = []
    allowed = story.allowed_numbers(sent)
    names = story.data_names(sent)
    fact_ids = {f["id"] for f in sent.get("facts") or []}
    text = ans.get("answer")
    if not isinstance(ans.get("in_scope"), bool):
        probs.append("in_scope must be true or false")
    if not isinstance(text, str) or not text.strip():
        return probs + ["answer: empty"]
    limit = float(prompt.get("max_words", 120)) * float((prompt.get("word_factor") or {}).get(lg, 1.0))
    if story._words(text, lg) > limit * 1.2:
        probs.append(f"answer: too long ({story._words(text, lg)} words, max {round(limit)})")
    for n in story.number_problems(text, allowed):
        probs.append(f"answer: the number '{n}' is not in the fact sheet - copy numbers exactly or leave them out")
    for nm in invented_names(text, sent):
        probs.append(f"answer: the name '{nm}' is not in the facts - use only names that appear there (e.g. 'Customer F' stays 'Customer F')")
    low = text.lower()
    for b in prompt.get("banned_terms") or []:
        if b.lower() in low:
            probs.append(f"answer: technical word '{b}' - use plain business words")
    if not story.language_ok(_plain(text, names), lg):
        probs.append(f"answer: not written in {prompt['language_names'].get(lg, lg)}")
    ids = ans.get("facts")
    ids = ids if isinstance(ids, list) else []
    if ans.get("in_scope") is True:
        if not ids:
            probs.append("answer: cite at least one fact id in 'facts'")
        unknown = [i for i in ids if i not in fact_ids]
        if unknown:
            probs.append(f"answer: unknown fact ids {unknown[:5]}")
    return probs


def clean_follow_ups(xs, sent: dict, lg: str, prompt: dict, limit: int = 3) -> list[str]:
    allowed = story.allowed_numbers(sent)
    out = []
    for x in xs if isinstance(xs, list) else []:
        if not isinstance(x, str) or not x.strip() or len(x) > 160:
            continue
        x = story.ID_RE.sub("", x).strip()
        if story.number_problems(x, allowed) or not story.language_ok(x, lg):
            continue
        if any(b.lower() in x.lower() for b in prompt.get("banned_terms") or []):
            continue
        if x not in out:
            out.append(x)
    return out[:limit]


# ---------------------------------------------------------------- the answer
def question_key(question: str) -> str:
    return hashlib.sha256(re.sub(r"\s+", " ", fold(question)).strip().encode()).hexdigest()[:24]


def rules_answer(selected: list[tuple[float, dict]], best_match: float, has_intent: bool, msgs: dict) -> dict:
    """The answer written by rules from the best facts (used when the AI is off, over quota, failed or checked out)."""
    if best_match <= 0 and not has_intent:
        return {"in_scope": False, "answer": msgs["ask_no_match"], "facts": []}
    picks = [f for s, f in selected if f.get("kind") != "chart"][:3]
    lines = [(f["id"], fact_line(f)) for f in picks]
    lines = [(i, t) for i, t in lines if t]
    if not lines:
        return {"in_scope": False, "answer": msgs["ask_no_match"], "facts": []}
    return {"in_scope": True, "answer": " ".join(t for _, t in lines), "facts": [i for i, _ in lines]}


def answer_question(doc: dict, sheet: dict, question: str, llm, store, tenant_id: str, month: str, *,
                    report_id: str, msgs: dict, allow_ai: bool = True,
                    reserve: Callable[[], bool] | None = None, release: Callable[[], None] | None = None,
                    embedder=None) -> dict:
    """Answer ``question`` from the report's fact sheet. Never raises for AI problems.

    ``reserve()`` is called only right before a real (paid) AI call; it returns False when the plan's monthly ask
    limit is used up. ``release()`` gives it back when the call failed or its answer was replaced by the rules.
    Returns {answer, in_scope, facts, evidence, follow_ups, source, reason, charged, meta}.
    source: ai | cache | rules."""
    t0 = time.perf_counter()
    prompt = load_prompt()
    lg = doc["meta"]["lang"]
    selected, best_match, has_intent = select_facts(sheet, question, int(prompt.get("max_facts", 14)), embedder)
    sel_ids = {f["id"] for _, f in selected}
    full = json.loads(prompt_json(sheet))
    sent = {**{k: v for k, v in full.items() if k not in ("facts", "charts", "chapters", "first_page_draft")},
            "facts": [f for f in full["facts"] if f["id"] in sel_ids]}
    by_id = {f["id"]: f for f in sent["facts"]}
    meta = {"ask_version": ASK_VERSION, "prompt_version": prompt.get("version"), "facts_sent": len(sent["facts"]), "sent_ids": sorted(sel_ids),
            "attempts": 0, "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0, "cached": False, "problems": []}
    result: dict = {"charged": False}

    def finish(core: dict, source: str, reason: str | None = None, follow=None) -> dict:
        ids = [i for i in dict.fromkeys(core.get("facts") or []) if i in by_id]
        text = story._restore(story.ID_RE.sub("", core["answer"]), sheet.get("aliases") or {})
        text = re.sub(r"\s{2,}", " ", text).strip()
        ev = [{"id": i, "text": story._restore(fact_line(by_id[i]), sheet.get("aliases") or {})} for i in ids]
        meta.update(ms=int((time.perf_counter() - t0) * 1000))
        return {"answer": text, "in_scope": bool(core.get("in_scope")), "facts": ids, "evidence": ev,
                "follow_ups": [story._restore(x, sheet.get("aliases") or {}) for x in follow or []],
                "source": source, "reason": reason, "charged": result["charged"], "meta": meta}

    fallback = rules_answer(selected, best_match, has_intent, msgs)
    if not allow_ai:
        return finish(fallback, "rules", "quota")
    if not getattr(llm, "enabled", False) or not getattr(llm, "key", ""):
        return finish(fallback, "rules", "disabled" if not getattr(llm, "enabled", False) else "no_key")
    meta.update(provider=llm.provider, model=llm.model)
    m = doc["meta"]
    fill = {"language": prompt["language_names"].get(lg, lg), "business": m.get("business") or "",
            "industry": m.get("industry") or "", "period": (doc.get("cover") or {}).get("period_text") or "",
            "max_words": prompt.get("max_words", 120), "question": question.strip()}

    def fmt(text: str, **more) -> str:
        for k, v in {**fill, **more}.items():
            text = text.replace("{" + k + "}", str(v))
        return text
    system = fmt("\n".join(prompt["system"])) + "\n\n" + prompt["style"].get(lg, "")
    sent_json = json.dumps(sent, ensure_ascii=False, separators=(",", ":"))
    user = fmt(prompt["request"]) + "\n\nFACT SHEET:\n" + sent_json
    key_parts = ["ask", tenant_id, report_id, m.get("data_version"), question_key(question), lg, prompt.get("version"),
                 hashlib.sha256(sent_json.encode()).hexdigest()[:16], hashlib.sha256(system.encode()).hexdigest()[:16]]
    kw = {"max_tokens": int(prompt.get("max_output_tokens") or 1500), "temperature": prompt.get("temperature"),
          "thinking": prompt.get("thinking")}
    try:                                     # a repeated question is free: it costs neither an AI call nor quota
        cached = store.get_llm_cache(cache_key(llm.provider, llm.model, *(key_parts + [1, ""]))) if store is not None else None
    except Exception:   # noqa: BLE001 - the cache is an optimisation
        cached = None
    if not cached and reserve is not None:
        if not reserve():
            return finish(fallback, "rules", "quota")
        result["charged"] = True
    ans, probs = None, []
    for attempt in (1, 2):
        u = user if attempt == 1 else user + "\n\n" + fmt(
            prompt["retry"], problems="\n".join(f"- {p}" for p in probs[:6]))
        try:
            r = cached_complete_json(llm, store, tenant_id, month, key_parts + [attempt, u if attempt > 1 else ""],
                                     system, u, **({**kw, "thinking": prompt.get("retry_thinking")} if attempt == 2 else kw))
        except LLMError as e:
            meta["attempts"] += 1
            meta["error"] = e.kind
            if ans is None:
                if result["charged"] and release:
                    release()
                    result["charged"] = False
                return finish(fallback, "rules", f"ai_error:{e.kind}")
            break
        meta["attempts"] += 1
        meta["tokens_in"] += r.tokens_in
        meta["tokens_out"] += r.tokens_out
        meta["cost_usd"] = round(meta["cost_usd"] + r.cost_usd, 6)
        meta["cached"] = meta["cached"] or r.cached
        meta.update(provider=r.provider, model=r.model)          # v88: the model that really answered (a backup may have)
        ans = r.data
        probs = check_answer(ans, sent, lg, prompt)
        if not probs:
            break
        meta["problems"] = probs
    if result["charged"] and release and meta["cached"]:
        release()                            # answered from the stored AI answer after all: free
        result["charged"] = False
    if probs or ans is None:
        if result["charged"] and release:
            release()                        # the user gets a rules answer: not charged
            result["charged"] = False
        return finish(fallback, "rules", "checks_failed")
    clean = {"in_scope": ans["in_scope"], "answer": story.fix_labels(ans["answer"].strip(), story.data_names(sent)),
             "facts": [i for i in ans.get("facts") or [] if i in by_id]}
    follow = clean_follow_ups(ans.get("follow_ups"), sent, lg, prompt)
    if not clean["in_scope"]:
        clean["facts"] = []
    return finish(clean, "cache" if meta["cached"] else "ai", None, follow)
