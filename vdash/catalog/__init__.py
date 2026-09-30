"""KPI catalog model.

A catalog is a plain-data Python module (one per domain template, e.g.
``retail_trade_004``) exposing ``SECTIONS`` and ``KPIS``. Each KPI carries:

* the SQL to run (portable ANSI subset, ``:tenant_id``/``:start``/``:end``
  binds, ``@BUCKET@`` for time-bucketed series),
* how to show it (``viz``, formats, series),
* what data it needs (``requires`` / ``requires_any``) - used by the
  availability probe to hide KPIs a tenant has no data for,
* who may see it (``min_role``),
* traceability to the template's canonical KPI ids (``template_kpis``).

Adding a KPI = adding one dict entry; no UI code changes.
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from typing import Any

VIZ_TYPES = {"metric", "timeseries", "bar", "hbar", "donut", "stacked", "heatmap", "table", "funnel"}
PERIOD_MODES = {"range", "monthly", "snapshot"}
FORMATS = {"currency", "number", "int", "pct", "ratio", "days", "hours", "score", "text", "date", "month"}


@dataclass
class Req:
    """Data requirement: ``table.column`` has >= ``min_distinct`` non-null values for the tenant."""
    table: str
    column: str
    min_distinct: int = 1
    nonzero: bool = False

    def key(self) -> str:
        return f"{self.table}.{self.column}>={self.min_distinct}{'!0' if self.nonzero else ''}"

    def describe(self) -> str:
        if self.min_distinct > 1:
            return f"{self.table}.{self.column} needs at least {self.min_distinct} distinct values"
        return f"{self.table}.{self.column} has no {'non-zero ' if self.nonzero else ''}data"


@dataclass
class Series:
    col: str
    name: str
    type: str = "bar"          # bar | line | area
    fmt: str = "currency"
    axis: int = 0              # 0 = left, 1 = right
    stack: str | None = None
    trend: bool = False        # UI may overlay a moving-average / linear trend when there are > 5 points


@dataclass
class Column:
    key: str
    label: str
    fmt: str = "text"


@dataclass
class Drill:
    """How a click on this KPI's chart narrows down to detail rows (see ``DetailSource``).

    * ``by_bucket`` - time series: the clicked period bucket narrows the dates;
    * ``key_expr``  - breakdowns: the clicked category is ``key_expr = <value>`` (same expression as the chart);
    * ``x_expr`` / ``y_expr`` - heatmaps: the clicked cell.
    * ``key_member``   - breakdowns whose category is only known after ranking the whole period (e.g. ABC
      class): ``(row_expr, subquery)``; the clicked category keeps rows whose ``row_expr`` is IN the subquery,
      which returns the members of class ``:dv`` over the selected period (``:tenant_id``/``:start``/``:end``).
    ``extra`` is the KPI's own row filter (e.g. cancelled lines excluded), so the rows add up to the chart value.
    """
    source: str
    extra: str | None = None
    by_bucket: bool = False
    key_expr: str | None = None
    x_expr: str | None = None
    y_expr: str | None = None
    note: str | None = None    # UI text key shown above the rows (e.g. chart is an average, rows add up to a total)
    key_member: tuple[str, str] | None = None

    def public(self) -> dict:
        return {"bucket": self.by_bucket, "key": bool(self.key_expr or self.key_member),
                "xy": bool(self.x_expr and self.y_expr), "note": self.note}


@dataclass
class DetailColumn:
    key: str
    label: str
    expr: str
    fmt: str = "text"
    pii: bool = False          # hidden below manager role


@dataclass
class DetailSource:
    """Row-level view behind a family of KPIs (e.g. every sales line), used by drill-down."""
    id: str
    from_sql: str              # fact + LEFT JOINed dimensions, same aliases the KPI SQL uses
    alias: str                 # fact alias carrying tenant_id
    columns: list[DetailColumn]
    order_by: str
    total_expr: str | None = None   # summed in the drill summary (e.g. line total)
    total_fmt: str = "currency"


@dataclass
class Kpi:
    id: str
    title: str
    sections: list[str]
    viz: str
    sql: str
    fmt: str = "currency"
    period_mode: str = "range"
    desc: str = ""
    good: str | None = "up"              # up | down | None: which delta direction is favourable
    requires: list[Req] = field(default_factory=list)
    requires_any: list[Req] = field(default_factory=list)
    min_role: str = "viewer"
    span: int = 1                        # grid columns (metric cards ignore)
    template_kpis: list[str] = field(default_factory=list)
    series: list[Series] = field(default_factory=list)
    columns: list[Column] = field(default_factory=list)
    spark_sql: str | None = None
    compare: bool = True
    post: str | None = None
    x_fmt: str = "text"
    label_map: dict[str, str] = field(default_factory=dict)
    note: str = ""
    drill: Drill | None = None

    def to_public(self) -> dict[str, Any]:
        return {
            "id": self.id, "title": self.title, "sections": self.sections, "viz": self.viz, "fmt": self.fmt,
            "period_mode": self.period_mode, "desc": self.desc, "good": self.good, "span": self.span,
            "template_kpis": self.template_kpis, "note": self.note, "x_fmt": self.x_fmt,
            "series": [s.__dict__ for s in self.series], "columns": [c.__dict__ for c in self.columns],
            "drill": self.drill.public() if self.drill else None,
        }


@dataclass
class ExportColumn:
    key: str
    label: str
    expr: str
    fmt: str = "text"


@dataclass
class ExportJoin:
    """Optional LEFT JOIN: included only when ``table`` exists in the tenant's warehouse."""
    table: str
    alias: str
    on: str
    columns: list[ExportColumn] = field(default_factory=list)


@dataclass
class ExportFilter:
    param: str                 # binds :f_<param> and :f_<param>_all
    label: str
    expr: str
    needs: str | None = None   # alias of the join it depends on (None = base table)


@dataclass
class Export:
    """A full-length list download (no LIMIT), e.g. every customer with its segment."""
    id: str
    title: str
    desc: str
    base_from: str
    base_where: str
    columns: list[ExportColumn]
    joins: list[ExportJoin] = field(default_factory=list)
    filters: list[ExportFilter] = field(default_factory=list)
    order_by: str = "1"
    requires: list[Req] = field(default_factory=list)
    min_role: str = "viewer"
    kpis: dict[str, str] = field(default_factory=dict)   # KPI id -> filter param preselected by a click on that chart
    max_rows: int = 200_000

    def to_public(self) -> dict[str, Any]:
        return {"id": self.id, "title": self.title, "desc": self.desc, "kpis": self.kpis}


RULE_PANELS = {"evaluation", "warning"}
RULE_BASES = {"trend", "forecast", "model", "data"}


@dataclass
class Rule:
    """One business-evaluation or early-warning check (v9).

    * ``panel="evaluation"`` - Executive Overview, judged on the *selected period* vs its comparison;
      a finding lands in "going well" or "needs attention".
    * ``panel="warning"``    - AI Insights "Early warnings", judged on the tenant's *latest data*
      (as-of = last data date), looking 7-30 days ahead. ``basis`` says what the warning rests on.

    ``fn`` names a generic evaluator in ``vdash.evaluation`` (``kpi_change`` reuses a catalog KPI, so the
    finding's number is the KPI tile's number). Other checks read a code-defined data set (``data`` ->
    ``RuleData``: portable SQL with ``:tenant_id`` mandatory, data requirements, item columns).

    v12: rules are no longer written in Python. They are loaded from ``config/rules/<catalog>.json`` by
    ``vdash.rules_config`` (validated, reloaded on change); a tenant can override thresholds in tenants.json
    (``alerts.params.<rule id>``), switch rules off (``alerts.disabled``) or on (``alerts.enabled``).
    """
    id: str
    panel: str
    title: str
    fn: str
    kpi: str | None = None
    sql: dict[str, str] = field(default_factory=dict)
    requires: list[Req] = field(default_factory=list)
    requires_any: list[Req] = field(default_factory=list)
    min_role: str = "viewer"
    items_min_role: str = "viewer"     # role needed to see the item list (customer names -> manager)
    basis: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    weight: float = 1.0                # ranking weight inside a column
    link_kpi: str | None = None        # "See chart" target
    items_cols: list[Column] = field(default_factory=list)
    enabled: bool = True
    data: str | None = None            # RuleData name (checks other than kpi_change)
    title_i18n: dict[str, str] = field(default_factory=dict)   # optional per-language titles from the rule file


@dataclass
class RuleData:
    """Code-defined data set a check reads (v12). The rule file refers to it by name; the SQL never lives in a
    config file, so a config edit cannot bypass tenant isolation."""
    check: str                         # the only check type that can read this data set
    sql: dict[str, str]
    requires: list[Req]
    items_cols: list[Column] = field(default_factory=list)
    items_min_role: str = "viewer"     # floor: the rule file may raise it, never lower it


@dataclass
class Section:
    id: str
    title: str
    icon: str
    desc: str = ""


@dataclass
class Catalog:
    name: str
    template_id: str
    sections: list[Section]
    kpis: list[Kpi]
    exports: list[Export] = field(default_factory=list)
    details: dict[str, DetailSource] = field(default_factory=dict)
    rules: list[Rule] = field(default_factory=list)
    rule_data: dict[str, RuleData] = field(default_factory=dict)
    rules_version: str = ""
    rules_error: list[str] = field(default_factory=list)
    data_areas: list[tuple[str, str]] = field(default_factory=list)   # (regex on "table.column", business label)

    def __post_init__(self) -> None:
        self.by_id = {k.id: k for k in self.kpis}
        self.rules_by_id = {r.id: r for r in self.rules}
        self.validate()

    def validate(self) -> None:
        seen: set[str] = set()
        section_ids = {s.id for s in self.sections}
        for k in self.kpis:
            if k.id in seen:
                raise ValueError(f"duplicate KPI id {k.id}")
            seen.add(k.id)
            if k.viz not in VIZ_TYPES:
                raise ValueError(f"{k.id}: unknown viz {k.viz}")
            if k.period_mode not in PERIOD_MODES:
                raise ValueError(f"{k.id}: unknown period_mode {k.period_mode}")
            if k.fmt not in FORMATS:
                raise ValueError(f"{k.id}: unknown fmt {k.fmt}")
            for s in k.sections:
                if s not in section_ids:
                    raise ValueError(f"{k.id}: unknown section {s}")
            for sql in filter(None, [k.sql, k.spark_sql]):
                if ":tenant_id" not in sql:
                    raise ValueError(f"{k.id}: SQL does not bind :tenant_id - tenant isolation would be broken")
            if not (k.requires or k.requires_any):
                raise ValueError(f"{k.id}: declare at least one data requirement")
        for e in self.exports:
            if ":tenant_id" not in e.base_where:
                raise ValueError(f"export {e.id}: base_where must bind :tenant_id")
            for kid in e.kpis:
                if kid not in self.by_id:
                    raise ValueError(f"export {e.id}: unknown KPI {kid}")
        self.exports_by_id = {e.id: e for e in self.exports}
        for k in self.kpis:
            if k.drill and k.drill.source not in self.details:
                raise ValueError(f"{k.id}: unknown drill source {k.drill.source}")
            if k.drill and k.drill.key_member:
                sub = k.drill.key_member[1]
                if ":tenant_id" not in sub or ":dv" not in sub:
                    raise ValueError(f"{k.id}: key_member subquery must bind :tenant_id and :dv")
                if k.drill.key_expr or k.drill.by_bucket:
                    # the class is ranked over the whole selected period; narrowing the dates would re-rank it
                    raise ValueError(f"{k.id}: key_member cannot be combined with key_expr or by_bucket")
        for d in self.details.values():
            if ":tenant_id" in d.from_sql:
                raise ValueError(f"detail {d.id}: tenant filter is added by the drill service, not in from_sql")
        for name, rd in self.rule_data.items():
            for qn, sql in rd.sql.items():
                if ":tenant_id" not in sql:
                    raise ValueError(f"rule data {name}: query {qn} does not bind :tenant_id")
            if not rd.requires:
                raise ValueError(f"rule data {name}: declare at least one data requirement")
        self.validate_rules(self.rules)

    def validate_rules(self, rules: list) -> None:
        rule_ids: set[str] = set()
        for r in rules:
            if r.id in rule_ids:
                raise ValueError(f"duplicate rule id {r.id}")
            rule_ids.add(r.id)
            if r.panel not in RULE_PANELS:
                raise ValueError(f"rule {r.id}: unknown panel {r.panel}")
            if r.panel == "warning" and r.basis not in RULE_BASES:
                raise ValueError(f"rule {r.id}: warnings need a basis ({', '.join(sorted(RULE_BASES))})")
            if r.fn == "kpi_change":
                if r.kpi not in self.by_id:
                    raise ValueError(f"rule {r.id}: unknown KPI {r.kpi}")
                if self.by_id[r.kpi].viz != "metric" or not self.by_id[r.kpi].good:
                    raise ValueError(f"rule {r.id}: kpi_change needs a metric KPI with a good direction")
            elif not (r.requires or r.requires_any):
                raise ValueError(f"rule {r.id}: declare at least one data requirement")
            for name, sql in r.sql.items():
                if ":tenant_id" not in sql:
                    raise ValueError(f"rule {r.id}: query {name} does not bind :tenant_id")
            for kid in filter(None, [r.link_kpi]):
                if kid not in self.by_id:
                    raise ValueError(f"rule {r.id}: unknown link KPI {kid}")

    def set_rules(self, rules: list, version: str) -> None:
        self.validate_rules(rules)
        self.rules, self.rules_version, self.rules_error = rules, version, []
        self.rules_by_id = {r.id: r for r in rules}

    def data_area(self, reason: str) -> str:
        """Business label for a technical availability reason ("table.column has no data", "table X not in
        warehouse", ...), so business admins learn *what data* unlocks a chart, never table names (v11)."""
        import re
        m = re.search(r"(?:table |column )?(\w+)\.(\w+)", reason) or re.search(r"table (\w+)", reason)
        ref = f"{m.group(1)}.{m.group(2)}" if m and m.lastindex == 2 else (f"{m.group(1)}." if m else "")
        for pattern, label in self.data_areas:
            if re.search(pattern, ref):
                return label
        return "Additional sales details"

    def kpis_for_section(self, section_id: str) -> list[Kpi]:
        return [k for k in self.kpis if section_id in k.sections]


_CACHE: dict[str, Catalog] = {}


def load_catalog(name: str) -> Catalog:
    if name not in _CACHE:
        mod = importlib.import_module(f"{__name__}.{name}")
        cat = Catalog(name=name, template_id=mod.TEMPLATE_ID, sections=mod.SECTIONS, kpis=mod.KPIS,
                      exports=getattr(mod, "EXPORTS", []), details=getattr(mod, "DETAILS", {}),
                      rule_data=getattr(mod, "RULE_DATA", {}), data_areas=getattr(mod, "DATA_AREAS", []))
        from ..rules_config import refresh_rules   # v12: rules come from config/rules/<catalog>.json
        refresh_rules(cat, force=True)
        _CACHE[name] = cat
    else:
        from ..rules_config import refresh_rules
        refresh_rules(_CACHE[name])                 # cheap file check; reloads when the rule file changed
    return _CACHE[name]
