"""What the understanding layer knows about one tenant and one user: plain data, so it can be unit-tested and
scored without a database. ``PlanningContext.from_tool_context`` fills it from the Step 2 ToolContext."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

ROLE_ORDER = {"viewer": 1, "manager": 2, "tenant_admin": 3, "platform_admin": 4}
LANGS = ("en", "vi", "de")


@dataclass
class KpiInfo:
    id: str
    titles: dict                       # lang -> title
    viz: str = "metric"
    min_role: str = "viewer"
    available: bool = True
    desc: str = ""
    personal: str | None = None        # 'customer' / 'staff' when it holds people's names
    sections: tuple = ()
    ask_only: bool = False
    synonyms: tuple = ()               # template-level words in any launch language
    template_kpis: tuple = ()          # the template KPI ids it implements (twins share them: net_revenue / revenue_trend)

    def title(self, lang: str) -> str:
        return self.titles.get(lang) or self.titles.get("en") or self.id


@dataclass
class ToolInfo:
    name: str
    description: str
    params: dict
    required: tuple = ()
    min_role: str = "viewer"
    viz: tuple = ()
    serves: tuple = ()


@dataclass
class PlanningContext:
    lang: str
    role: str
    anchor: date
    min_date: date | None
    kpis: list[KpiInfo]
    tools: list[ToolInfo]
    core: tuple = ()                  # KPI ids always offered to the planner
    by_id: dict = field(default_factory=dict)
    tool_by_name: dict = field(default_factory=dict)

    def __post_init__(self):
        self.by_id = {k.id: k for k in self.kpis}
        self.tool_by_name = {t.name: t for t in self.tools}

    def role_ok(self, min_role: str) -> bool:
        return ROLE_ORDER.get(self.role, 0) >= ROLE_ORDER.get(min_role, 99)

    # ------------------------------------------------------------------------------------------------ builders
    @classmethod
    def from_tool_context(cls, ctx, lang: str | None = None) -> "PlanningContext":
        from ...i18n import t
        from ..tools import registry
        from .synonyms import core_ids, load_synonyms
        syn = load_synonyms(ctx.tc.catalog)
        avail = ctx.st.get("availability") or {}
        ask_ids = {k.id for k in getattr(ctx.catalog, "ask_kpis", [])}
        kpis = []
        for k in (*ctx.catalog.kpis, *getattr(ctx.catalog, "ask_kpis", [])):
            kpis.append(KpiInfo(
                id=k.id, titles={lg: t(lg, k.title) for lg in LANGS}, viz=k.viz, min_role=k.min_role,
                available=bool((avail.get(k.id) or {}).get("available")), desc=t("en", k.desc) if k.desc else "",
                personal=ctx.personal_kind(k), sections=tuple(k.sections or ()), ask_only=k.id in ask_ids,
                synonyms=tuple(syn.get(k.id, ())), template_kpis=tuple(k.template_kpis or ())))
        tools = [ToolInfo(tl.name, tl.description, tl.params, tuple(tl.required), tl.min_role, tuple(tl.viz), tuple(tl.serves))
                 for tl in registry.TOOLS.values()]
        min_d = date.fromisoformat(ctx.st["min_date"]) if ctx.st.get("min_date") else None
        return cls(lang=lang or ctx.lang, role=ctx.user.role, anchor=ctx.anchor(), min_date=min_d, kpis=kpis, tools=tools,
                   core=tuple(core_ids(ctx.tc.catalog)))

    # ------------------------------------------------------------------------------------------------ snapshot
    def to_dict(self) -> dict:
        from dataclasses import asdict
        return {"lang": self.lang, "role": self.role, "anchor": self.anchor.isoformat(),
                "min_date": self.min_date.isoformat() if self.min_date else None,
                "core": list(self.core), "kpis": [asdict(k) for k in self.kpis], "tools": [asdict(t) for t in self.tools]}

    @classmethod
    def from_dict(cls, d: dict) -> "PlanningContext":
        kp = [KpiInfo(**{**k, "sections": tuple(k.get("sections") or ()), "synonyms": tuple(k.get("synonyms") or ()),
                          "template_kpis": tuple(k.get("template_kpis") or ())}) for k in d["kpis"]]
        tl = [ToolInfo(**{**t, "required": tuple(t.get("required") or ()), "viz": tuple(t.get("viz") or ()),
                          "serves": tuple(t.get("serves") or ())}) for t in d["tools"]]
        return cls(lang=d["lang"], role=d["role"], anchor=date.fromisoformat(d["anchor"]),
                   min_date=date.fromisoformat(d["min_date"]) if d.get("min_date") else None, kpis=kp, tools=tl,
                   core=tuple(d.get("core") or ()))

    def with_role(self, role: str, lang: str | None = None) -> "PlanningContext":
        return PlanningContext(lang=lang or self.lang, role=role, anchor=self.anchor, min_date=self.min_date,
                               kpis=self.kpis, tools=self.tools, core=self.core)
