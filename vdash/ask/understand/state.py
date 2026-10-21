"""Conversation state: what the last turns asked, so "and the month before?" / "what about by units?" can be understood.
Plain data, small (3 turns), safe to keep in the browser session or the server; it never holds answers or names."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

MAX_TURNS = 3


@dataclass
class Turn:
    question: str
    kpi_ids: list[str] = field(default_factory=list)
    periods: list[dict] = field(default_factory=list)       # [{"start","end"}]
    tools: list[str] = field(default_factory=list)
    grain: str | None = None
    top_n: int | None = None
    query: dict | None = None                                 # a custom_query spec without its period, reused by a follow-up


@dataclass
class ConversationState:
    turns: list[Turn] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.turns

    def last(self) -> Turn | None:
        return self.turns[-1] if self.turns else None

    def last_period(self) -> dict | None:
        for t in reversed(self.turns):
            if t.periods:
                return t.periods[0]
        return None

    def last_kpis(self) -> list[str]:
        for t in reversed(self.turns):
            if t.kpi_ids:
                return t.kpi_ids
        return []

    def add(self, turn: Turn) -> None:
        self.turns.append(turn)
        del self.turns[:-MAX_TURNS]

    def brief(self) -> dict | None:
        t = self.last()
        if not t:
            return None
        return {"question": t.question[:160], "kpi_ids": t.kpi_ids, "periods": t.periods, "tools": t.tools,
                **({"grain": t.grain} if t.grain else {}), **({"top_n": t.top_n} if t.top_n else {}),
                **({"custom_query": t.query} if t.query else {})}

    def to_dict(self) -> dict:
        return {"turns": [asdict(t) for t in self.turns]}

    @classmethod
    def from_dict(cls, d: dict | None) -> "ConversationState":
        out = cls()
        for t in (d or {}).get("turns", [])[-MAX_TURNS:]:
            out.turns.append(Turn(**{k: t.get(k) for k in ("question", "kpi_ids", "periods", "tools", "grain", "top_n", "query") if k in t}))
        return out
