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
    sliced: bool = False               # a split of the measure by a dimension (category_trend): never the twin of the total

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
    query_model: dict = field(default_factory=dict)     # {entity: {"title", "columns": {key: {label, fmt, kind}}}} for custom_query

    def __post_init__(self):
        self.by_id = {k.id: k for k in self.kpis}
        self.tool_by_name = {t.name: t for t in self.tools}

    def role_ok(self, min_role: str) -> bool:
        return ROLE_ORDER.get(self.role, 0) >= ROLE_ORDER.get(min_role, 99)

    # ------------------------------------------------------------------------------------------------ builders
    @classmethod
    def from_tool_context(cls, ctx, lang: str | None = None) -> "PlanningContext":
        from ...i18n import t
        from ..tools import query_tool as qt
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
                synonyms=tuple(syn.get(k.id, ())), template_kpis=tuple(k.template_kpis or ()),
                sliced=getattr(k, "post", None) == "pivot"))
        tools = [ToolInfo(tl.name, tl.description, tl.params, tuple(tl.required), tl.min_role, tuple(tl.viz), tuple(tl.serves))
                 for tl in registry.TOOLS.values()]
        min_d = date.fromisoformat(ctx.st["min_date"]) if ctx.st.get("min_date") else None
        try:
            qm = qt.query_model(ctx, lang="en", include_pii=True)   # English names for the planner; people's names are cut per role
        except Exception:                                  # noqa: BLE001 - no custom questions, everything else works
            qm = {}
        if not qm:
            tools = [tl for tl in tools if tl.name != "custom_query"]
        return cls(lang=lang or ctx.lang, role=ctx.user.role, anchor=ctx.anchor(), min_date=min_d, kpis=kpis, tools=tools,
                   core=tuple(core_ids(ctx.tc.catalog)), query_model=qm)

    # ------------------------------------------------------------------------------------------------ snapshot
    def to_dict(self) -> dict:
        from dataclasses import asdict
        return {"lang": self.lang, "role": self.role, "anchor": self.anchor.isoformat(),
                "min_date": self.min_date.isoformat() if self.min_date else None,
                "core": list(self.core), "kpis": [asdict(k) for k in self.kpis], "tools": [asdict(t) for t in self.tools],
                "query_model": self.query_model}

    @classmethod
    def from_dict(cls, d: dict) -> "PlanningContext":
        kp = [KpiInfo(**{**k, "sections": tuple(k.get("sections") or ()), "synonyms": tuple(k.get("synonyms") or ()),
                          "template_kpis": tuple(k.get("template_kpis") or ())}) for k in d["kpis"]]
        tl = [ToolInfo(**{**t, "required": tuple(t.get("required") or ()), "viz": tuple(t.get("viz") or ()),
                          "serves": tuple(t.get("serves") or ())}) for t in d["tools"]]
        return cls(lang=d["lang"], role=d["role"], anchor=date.fromisoformat(d["anchor"]),
                   min_date=date.fromisoformat(d["min_date"]) if d.get("min_date") else None, kpis=kp, tools=tl,
                   core=tuple(d.get("core") or ()), query_model=d.get("query_model") or {})

    def with_role(self, role: str, lang: str | None = None) -> "PlanningContext":
        qm = self.query_model
        if ROLE_ORDER.get(role, 0) < ROLE_ORDER["manager"]:        # people's names are for managers only
            qm = {e: {**m, "columns": {k: c for k, c in m["columns"].items() if not c.get("pii")}} for e, m in qm.items()}
        return PlanningContext(lang=lang or self.lang, role=role, anchor=self.anchor, min_date=self.min_date,
                               kpis=self.kpis, tools=self.tools, core=self.core, query_model=qm)
