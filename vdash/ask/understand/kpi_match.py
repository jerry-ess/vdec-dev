"""Entity resolver for KPIs: question words -> ranked candidate KPIs (English, Vietnamese, German; accents optional).

Deterministic and cheap: an inverted index over every KPI's titles in all launch languages, its description and the
template's synonym words, weighted by how rare a word is. The planner gets the top candidates only (a prompt with all
~250 KPIs would cost more and confuse the model); the embedder, when on, can re-rank later without changing this API.
"""
from __future__ import annotations

import math
import re
from collections import Counter

from .context import KpiInfo, PlanningContext
from .periods import fold

STOP = set("""a an the of for to in on at by with and or is are was were be been do does did we our my me you your it its this that
these those what which who whom how much many from than then there their about over per into has have had can could will would
should show tell give list please number numbers figure figures data stat stats metric metrics report reports other one ones thing things kpi kpis info information result results
la cac nhung mot cua cho trong tren o voi va hoac la co khong bao nhieu nao the nao gi toi chung ta minh ban cho xem hay
nhu nao khi thi se da dang duoc so lieu chi so con
der die das ein eine einen einem eines von fur im in am an auf mit und oder ist sind war waren wir unser unsere mein ich sie ihr
wie viel viele was welche welcher welches wo wann zeigen sie mir bitte nach bei zu zum zur den dem des dass zahl zahlen daten kennzahl kennzahlen andere anderen anderes anderer""".split())
MIN_KPI_SCORE = 1.8        # below this the best match is word noise, not a metric the user named
TOKEN = re.compile(r"[a-z0-9]+")


def stem(w: str) -> str:
    """Cheap suffix folding applied until stable, the same way to titles, synonyms and questions: 'orders' = 'order',
    'unusually' = 'unusual', German 'besten' = 'beste' = 'best', 'Kunden' = 'Kunde'. Crude on purpose; both sides agree."""
    while True:
        n = w
        for suf, keep in (("ly", 4), ("ing", 4), ("ed", 4), ("es", 4), ("en", 4), ("er", 4), ("em", 4), ("e", 4), ("s", 3)):
            if len(w) - len(suf) >= keep and w.endswith(suf) and not w.endswith("ss"):
                w = w[:-len(suf)]
                break
        if w == n:
            return w


def words(text: str) -> list[str]:
    return [stem(w) for w in TOKEN.findall(fold(text)) if w not in STOP and len(w) > 1]


def _grams(ws: list[str]) -> list[str]:
    return ws + [f"{a}_{b}" for a, b in zip(ws, ws[1:])]


class KpiIndex:
    def __init__(self, kpis: list[KpiInfo]):
        self.kpis = kpis
        self.docs: dict[str, Counter] = {}
        df: Counter = Counter()
        for k in kpis:
            c: Counter = Counter()
            for t in k.titles.values():
                for g in _grams(words(t)):
                    c[g] += 2
            for s in k.synonyms:
                for g in _grams(words(s)):
                    c[g] += 2
            for g in _grams(words(k.desc)):
                c[g] += 1
            self.docs[k.id] = c
            df.update(c.keys())
        n = max(len(kpis), 1)
        self.idf = {g: math.log(1 + n / v) for g, v in df.items()}
        # whole phrases (a title or a synonym) as token tuples: when one appears in the question it is a strong signal
        # even if its words are common (German "Umsatz" appears in dozens of titles)
        self.phrases: dict[str, set] = {}
        for k in kpis:
            ph = set()
            for src in (*k.titles.values(), *k.synonyms):
                w = tuple(words(src))
                if w:
                    ph.add(w)
            self.phrases[k.id] = ph

    def phrase_len(self, k: KpiInfo, qw: list[str]) -> int:
        """Length in words of the longest title / synonym phrase of ``k`` that appears whole in the question (0 = none)."""
        best = 0
        for ph in self.phrases.get(k.id, ()):
            m = len(ph)
            if m > best and any(tuple(qw[i:i + m]) == ph for i in range(len(qw) - m + 1)):
                best = m
        return best

    def score(self, question: str) -> list[tuple[float, KpiInfo]]:
        qw = words(question)
        q = _grams(qw)
        out = []
        for k in self.kpis:
            doc = self.docs[k.id]
            s = 0.0
            for g in set(q):
                if g in doc:
                    w = self.idf.get(g, 1.0) * (1.6 if "_" in g else 1.0)
                    s += w * min(doc[g], 3) / 3 + 0.15 * w
            best_ph = self.phrase_len(k, qw)
            if best_ph:
                s += 2.5 + 1.0 * (best_ph - 1)
            if s:
                s /= (1 + 0.04 * sum(doc.values())) ** 0.5          # a very long description should not win by size
                if k.ask_only:
                    s *= 0.98
            out.append((s, k))
        out.sort(key=lambda x: (-x[0], x[1].id))
        return out


_INDEX: dict[int, KpiIndex] = {}


def index_for(pctx: PlanningContext) -> KpiIndex:
    key = id(pctx.kpis)
    ix = _INDEX.get(key)
    if ix is None or ix.kpis is not pctx.kpis:
        ix = _INDEX[key] = KpiIndex(pctx.kpis)
    return ix


def candidates(question: str, pctx: PlanningContext, n: int = 20, pin: list[str] | None = None,
               min_score: float = 0.0) -> list[tuple[float, KpiInfo]]:
    """Top ``n`` KPIs for the question, plus pinned ids (the previous turn's KPIs) so a follow-up can keep them.
    Unavailable KPIs stay in the list (the answer then says honestly that the data is missing) but rank below
    available ones with a similar score."""
    ranked = [(s * (1.0 if k.available else 0.6), k) for s, k in index_for(pctx).score(question) if s > min_score]
    ranked.sort(key=lambda x: (-x[0], x[1].id))
    top = ranked[:n]
    have = {k.id for _, k in top}
    for pid in [*(pin or []), *pctx.core]:
        k = pctx.by_id.get(pid)
        if k and k.id not in have:
            top.append((0.0, k))
    return top
