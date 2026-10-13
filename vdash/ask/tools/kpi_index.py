"""Link between the template's KPI ids (KPI-SALES-001 ... in the question bank) and the dashboard catalog's KPIs.

``Kpi.template_kpis`` already names the template KPI(s) each dashboard KPI belongs to. One template KPI usually has
several dashboard KPIs (a metric card, a trend, a breakdown); the *shape* a question needs
(single_value / time_series / breakdown / ranked_list / table) picks one of them.
"""
from __future__ import annotations

from collections import defaultdict

from ...catalog import Catalog, Kpi

# result shape of the question bank -> dashboard chart types that can answer it, best first
SHAPE_VIZ = {
    "single_value": ("metric",),
    "time_series": ("timeseries",),
    "breakdown": ("donut", "hbar", "bar", "table", "heatmap"),
    "ranked_list": ("hbar", "table", "bar", "donut"),
    "table": ("table", "heatmap", "hbar"),
}


class KpiIndex:
    def __init__(self, catalog: Catalog):
        self.catalog = catalog
        self.by_template: dict[str, list[Kpi]] = defaultdict(list)
        for k in (*catalog.kpis, *catalog.ask_kpis):
            for tk in k.template_kpis:
                self.by_template[tk].append(k)

    def candidates(self, template_kpi: str, shape: str) -> list[Kpi]:
        vizes = SHAPE_VIZ.get(shape, ())
        ks = [k for k in self.by_template.get(template_kpi, []) if k.viz in vizes]
        return sorted(ks, key=lambda k: (vizes.index(k.viz), k.id in {a.id for a in self.catalog.ask_kpis}))

    def choose(self, template_kpi: str, shape: str) -> Kpi | None:
        c = self.candidates(template_kpi, shape)
        return c[0] if c else None

    def coverage(self, questions: list[dict]) -> list[dict]:
        """One row per answerable question: which dashboard KPI serves it, or why none does."""
        rows = []
        for q in questions:
            k = self.choose(q["kpi_id"], q["expected_result_shape"])
            have = [(x.id, x.viz) for x in self.by_template.get(q["kpi_id"], [])]
            rows.append({"question_id": q["question_id"], "template_kpi": q["kpi_id"], "shape": q["expected_result_shape"],
                         "kpi": k.id if k else None, "viz": k.viz if k else None,
                         "ask_only": bool(k and k in self.catalog.ask_kpis),
                         "gap": None if k else ("no dashboard KPI" if not have else f"only {sorted({v for _, v in have})}")})
        return rows


_CACHE: dict[str, KpiIndex] = {}


def index_for(catalog: Catalog) -> KpiIndex:
    if catalog.name not in _CACHE:
        _CACHE[catalog.name] = KpiIndex(catalog)
    return _CACHE[catalog.name]


def resolve_kpi(catalog: Catalog, ref: str, viz_ok: tuple[str, ...]) -> Kpi | None:
    """``ref`` = dashboard KPI id ('revenue_trend') or template KPI id ('KPI-SALES-001'); returns a KPI whose chart
    type the calling tool can use, or None."""
    if not isinstance(ref, str):
        return None
    k = catalog.by_id.get(ref)
    if k is not None:
        return k if k.viz in viz_ok else None
    for tk in index_for(catalog).by_template.get(ref, []):
        if tk.viz in viz_ok:
            return tk
    return None


def describe(catalog: Catalog, avail: dict, user, lang: str) -> list[dict]:
    """KPIs this user can ask about (role ok AND data available) - the planner's list (Step 3)."""
    from ...i18n import t
    out = []
    for k in (*catalog.kpis, *catalog.ask_kpis):
        a = avail.get(k.id, {})
        if a.get("available") and user.has_role(k.min_role):
            out.append({"id": k.id, "title": t(lang, k.title), "viz": k.viz, "fmt": k.fmt,
                        "template_kpis": list(k.template_kpis), "desc": t(lang, k.desc) if k.desc else ""})
    return out
