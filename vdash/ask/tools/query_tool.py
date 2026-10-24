"""``custom_query``: the one general tool for questions no ready-made KPI or tool answers (a median, a list of single
orders above an amount, a count with a filter, an average per group ...).

The planner writes a small JSON *spec* - never SQL. Code compiles the spec against the catalog's row sets (the same
``DetailSource`` definitions the dashboard drill-down uses), so:

* only the tables, columns and joins the catalog declares can be read; the tenant filter, the period and the "cancelled
  rows do not count" rule are added by the server, never by the model;
* people's names (customer, salesperson) need the manager role, cannot be filtered on, and are replaced by
  Customer A / Customer B ... before they leave the server;
* every value in a filter is a bound parameter; results are capped (rows shown, rows read for a percentile);
* the answer says in plain words how it was calculated (``meta.custom_query.how``), so a wrong reading is visible.

Spec (all keys optional unless noted)::

    {"entity": "orders",                      # required: a row set from the model list
     "agg": "median",                         # count | count_distinct | sum | avg | median | percentile | min | max | list
     "measure": "total_v",                    # a column key; not needed for count / list
     "percentile": 90,                        # with agg=percentile
     "group_by": "channel_v",                 # a text or date column
     "filters": [{"column": "total_v", "op": ">", "value": 20000000}],
     "order": "desc", "order_by": "total_v",  # list: the column to sort by; grouped: sorted by the value
     "columns": ["order_no", "total_v"],      # list: which columns to show
     "limit": 10, "period": {...}}
"""
from __future__ import annotations

import math
import re
import time
from collections import defaultdict
from datetime import date, datetime

from ...engine import bind_params
from ...fmt import fmt_date
from ...i18n import t
from ...reports.story_rules import load_story_config
from .common import ToolContext, ToolError, _num

MAX_ROWS = 20                    # rows / groups shown
MAX_READ = 300_000               # rows read for a median / percentile
MAX_FILTERS = 6
MAX_IN = 20
AGGS = ("count", "count_distinct", "sum", "avg", "median", "percentile", "min", "max", "list")
NUMERIC_FMT = {"currency", "number", "int", "days", "pct"}
OPS = {"=": "=", "!=": "<>", ">": ">", ">=": ">=", "<": "<", "<=": "<=", "contains": "LIKE", "in": "IN", "between": "BETWEEN"}
_ALIAS = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\.[A-Za-z_]")

# ------------------------------------------------------------------------------------------------ wording
_T = {
    "agg": {
        "en": {"sum": "Sum of {m}", "avg": "Average {m}", "median": "Median {m}", "min": "Lowest {m}", "max": "Highest {m}",
               "count": "Number of {m}", "count_distinct": "Distinct count of {m}", "percentile": "{p}th percentile of {m}",
               "list": "List of {m}"},
        "vi": {"sum": "Tổng {m}", "avg": "{m} trung bình", "median": "{m} trung vị", "min": "{m} thấp nhất", "max": "{m} cao nhất",
               "count": "Số lượng {m}", "count_distinct": "Số {m} khác nhau", "percentile": "Phân vị thứ {p} của {m}",
               "list": "Danh sách {m}"},
        "de": {"sum": "Summe {m}", "avg": "Durchschnitt {m}", "median": "Median {m}", "min": "Minimum {m}", "max": "Maximum {m}",
               "count": "Anzahl {m}", "count_distinct": "Anzahl verschiedener {m}", "percentile": "{p}. Perzentil von {m}",
               "list": "Liste {m}"},
    },
    "entity": {
        "orders": {"vi": "Đơn hàng", "de": "Bestellungen"}, "sales_lines": {"vi": "Dòng bán hàng", "de": "Verkaufspositionen"},
        "waste_events": {"vi": "Sự kiện hao hụt", "de": "Schwundereignisse"}, "receipts": {"vi": "Phiếu nhập hàng", "de": "Wareneingänge"},
    },
    "scalar": {
        "en": "{what}, {period}: {value} ({entity} counted: {n})",
        "vi": "{what}, {period}: {value} (số {entity} được tính: {n})",
        "de": "{what}, {period}: {value} (berücksichtigte {entity}: {n})"},
    "scalar_count": {
        "en": "{what}, {period}: {value}", "vi": "{what}, {period}: {value}", "de": "{what}, {period}: {value}"},
    "group": {
        "en": "{what} by {by}, {period}", "vi": "{what} theo {by}, {period}", "de": "{what} nach {by}, {period}"},
    "list": {
        "en": "{n} {entity} match, {period}",
        "vi": "{n} {entity} phù hợp, {period}",
        "de": "{n} {entity} passen, {period}"},
    "top_desc": {
        "en": "Highest {k} {entity} by {col} ({n} in the period), {period}",
        "vi": "{k} {entity} có {col} cao nhất ({n} trong kỳ), {period}",
        "de": "Höchste {k} {entity} nach {col} ({n} im Zeitraum), {period}"},
    "top_asc": {
        "en": "Lowest {k} {entity} by {col} ({n} in the period), {period}",
        "vi": "{k} {entity} có {col} thấp nhất ({n} trong kỳ), {period}",
        "de": "Niedrigste {k} {entity} nach {col} ({n} im Zeitraum), {period}"},
    "none": {
        "en": "No {entity} match, {period}: there are {n} {entity} in the period, but none fits.",
        "vi": "Không có {entity} nào phù hợp, {period}: kỳ này có {n} {entity} nhưng không có cái nào khớp.",
        "de": "Keine {entity} passen, {period}: im Zeitraum gibt es {n} {entity}, aber keine erfüllt die Bedingung."},
    "values": {
        "en": " '{asked}' is not a value of {col}; the values are: {values}.",
        "vi": " '{asked}' không phải là giá trị của {col}; các giá trị là: {values}.",
        "de": " '{asked}' ist kein Wert von {col}; die Werte sind: {values}."},
    "none_hi": {
        "en": " The highest {col} is {value}.", "vi": " {col} cao nhất là {value}.", "de": " Der höchste Wert bei {col} ist {value}."},
    "none_lo": {
        "en": " The lowest {col} is {value}.", "vi": " {col} thấp nhất là {value}.", "de": " Der niedrigste Wert bei {col} ist {value}."},
    "more": {
        "en": "{n} more not shown", "vi": "còn {n} dòng không hiển thị", "de": "{n} weitere nicht angezeigt"},
    "how": {
        "en": "Custom calculation: {what} over {entity}{where}. {excl}Period: {period}.",
        "vi": "Tính toán tùy chỉnh: {what} trên {entity}{where}. {excl}Kỳ: {period}.",
        "de": "Individuelle Berechnung: {what} über {entity}{where}. {excl}Zeitraum: {period}."},
    "where": {"en": ", where {cond}", "vi": ", với điều kiện {cond}", "de": ", wobei {cond}"},
    "excl": {"en": "Cancelled rows are not counted. ", "vi": "Không tính các dòng đã hủy. ",
             "de": "Stornierte Zeilen werden nicht mitgezählt. "},
}


def _w(key: str, lang: str, **kw) -> str:
    d = _T[key]
    s = d.get(lang) or d["en"]
    for k, v in kw.items():
        s = s.replace("{" + k + "}", str(v))
    return s


def _entity_title(ctx: ToolContext, src) -> str:
    return (_T["entity"].get(src.id) or {}).get(ctx.lang) or t(ctx.lang, src.title)


def _noun(ctx: ToolContext, title: str) -> str:
    """The row set's name inside a sentence: lower case in English and Vietnamese (German nouns keep their capital)."""
    return title if ctx.lang == "de" else title.lower()


# ------------------------------------------------------------------------------------------------ the model the planner sees
def _kind(fmt: str) -> str:
    return "number" if fmt in NUMERIC_FMT else "date" if fmt == "date" else "text"


def _aliases_in(expr: str, known) -> set[str]:
    return {a for a in _ALIAS.findall(expr) if a in known}


_JOIN_RX = re.compile(r"\b(LEFT\s+)?JOIN\b")


def _split_from(src):
    """-> (base sql, {alias: join}). The base is the fact, ``dim_date`` and every inner JOIN (always needed); each LEFT
    JOIN is optional and only used when a column needs it. A join is {"sql", "table" (None for a derived table), "needs"
    (tables it reads), "deps" (aliases its ON clause leans on)}."""
    text, cuts = src.from_sql, []
    depth_at, d = [], 0
    for ch in text:
        d += ch == "("
        d -= ch == ")"
        depth_at.append(d)
    for m in _JOIN_RX.finditer(text):
        if m.start() > 0 and depth_at[m.start() - 1] == 0:
            cuts.append((m.start(), bool(m.group(1))))
    if not cuts:
        return text, {}
    base, joins = text[:cuts[0][0]], {}
    for n, (i, left) in enumerate(cuts):
        seg = text[i:cuts[n + 1][0] if n + 1 < len(cuts) else len(text)]
        if not left:
            base += seg
            continue
        head = _JOIN_RX.sub("", seg, count=1).strip()
        if head.startswith("("):
            d, j = 0, 0
            for j, ch in enumerate(head):
                d += ch == "("
                d -= ch == ")"
                if d == 0:
                    break
            inner, rest = head[:j + 1], head[j + 1:].split()
            table, alias, needs = None, rest[0], set(re.findall(r"\b(?:FROM|JOIN)\s+(\w+)", inner))
        else:
            w = head.split()
            table, alias, needs = w[0], w[1], {w[0]}
        joins[alias] = {"sql": " " + seg.strip(), "table": table, "needs": needs, "on": seg.split(" ON ", 1)[-1]}
    for alias, jn in joins.items():
        jn["deps"] = _aliases_in(jn["on"], joins) - {alias}
    return base, joins


def _tables_of(src) -> dict[str, str]:
    """alias -> table for the base fact, dim_date, inner joins and plain LEFT JOINs (derived tables are left out)."""
    base, joins = _split_from(src)
    res = {}
    m = re.match(r"\s*(\w+)\s+(\w+)\b", base)
    if m:
        res[m.group(2)] = m.group(1)
    for m2 in re.finditer(r"\bJOIN\s+(\w+)\s+(\w+)\b", base):
        res[m2.group(2)] = m2.group(1)
    for a, jn in joins.items():
        if jn["table"]:
            res[a] = jn["table"]
    return res


def _closure(aliases: set, joins: dict) -> set:
    need, todo = set(), list(aliases & set(joins))
    while todo:
        a = todo.pop()
        if a not in need:
            need.add(a)
            todo += list(joins[a]["deps"] - need)
    return need


def usable_columns(src, available: dict, role_ok_pii: bool) -> dict:
    """{key: DetailColumn} the warehouse can serve (tables/columns exist, joins possible) and the user may see."""
    amap = _tables_of(src)
    _, joins = _split_from(src)
    out = {}
    for c in src.columns:
        if c.pii and not role_ok_pii:
            continue
        ok = True
        for a, col in re.findall(r"\b([A-Za-z_]\w*)\.([A-Za-z_]\w*)\b", c.expr):
            tb = amap.get(a)
            if tb is not None and col not in available.get(tb, set()):
                ok = False
                break
        if ok and any(not jn["needs"] <= set(available) for a, jn in joins.items() if a in _closure(_aliases_in(c.expr, joins), joins)):
            ok = False
        if ok:
            out[c.key] = c
    return out


VALUES_MAX = 12                  # a text column with at most this many different values lists them for the planner
VALUES_TTL = 600                 # seconds the lists are reused (a new status value appears after the next load)
VALUES_BUDGET = 4.0              # seconds one model build may spend looking for values
_VALUES: dict = {}


def column_values(ctx: ToolContext, ds, sid: str, src, cols: dict, avail: dict, key: str):
    """The different values of a text column when there are few of them (status, reason, type), else None.
    Read through the same compiled row set as a question, so the tenant filter and the validity rule apply."""
    import time
    ck = (id(ds), ctx.tc.tenant_id, sid, key)
    hit = _VALUES.get(ck)
    if hit and time.time() - hit[0] < VALUES_TTL:
        return hit[1]
    lifted = [{"column": key, "op": "contains", "value": ""}] if src.valid_unless == key else []       # "Status": the cancelled rows are the point
    spec = {"entity": sid, "agg": "count", "measure": None, "group_by": key, "filters": lifted, "order": "desc", "limit": VALUES_MAX + 1}
    q = compile_query(src, spec, cols, avail, ctx.tc.tenant_id, date(1990, 1, 1), date(2100, 1, 1), ds.dialect, "en")
    rows = ds.query(f"{q['sql']} LIMIT {VALUES_MAX + 1}", q["params"])
    vals = [str(r.get("g")).strip() for r in rows if r.get("g") not in (None, "")]
    out = vals if 0 < len(vals) <= VALUES_MAX else None
    _VALUES[ck] = (time.time(), out)
    return out


def _column_never_filled(ctx: ToolContext, ds, sid: str, src, cols: dict, avail: dict, key: str) -> bool:
    """True when no row of the row set (any period) has a value in this text column."""
    lifted = [{"column": key, "op": "contains", "value": ""}] if src.valid_unless == key else []
    spec = {"entity": sid, "agg": "count", "measure": None, "group_by": key, "filters": lifted, "order": "desc", "limit": 3}
    q = compile_query(src, spec, cols, avail, ctx.tc.tenant_id, date(1990, 1, 1), date(2100, 1, 1), ds.dialect, "en")
    rows = ds.query(f"{q['sql']} LIMIT 3", q["params"])
    return not any(r.get("g") not in (None, "") and str(r.get("g")).strip() for r in rows)


def query_model(ctx: ToolContext, lang: str | None = None, include_pii: bool | None = None) -> dict:
    """{entity id: {"title", "columns": {key: {"label", "fmt", "kind", "pii"}}}} for this tenant and this user: what a
    custom question may read. Empty when the warehouse has none of the row sets."""
    ds = ctx.ds
    avail = ds.columns()
    pii_ok = ctx.user.has_role("manager") if include_pii is None else include_pii
    lang = lang or ctx.lang
    out = {}
    t0 = time.time()
    for sid, src in ctx.catalog.details.items():
        if not getattr(src, "title", ""):
            continue
        cols = usable_columns(src, avail, pii_ok)
        if "dt" not in cols or not any(_kind(c.fmt) == "number" for c in cols.values()):
            continue
        out[sid] = {"title": (_T["entity"].get(sid) or {}).get(lang) or t(lang, src.title),
                    "columns": {k: {"label": t(lang, c.label), "fmt": c.fmt or "text", "kind": _kind(c.fmt or "text"),
                                    **({"pii": True} if c.pii else {})} for k, c in cols.items()}}
        if time.time() - t0 < VALUES_BUDGET:
            for k, c in cols.items():
                if c.pii or _kind(c.fmt or "text") != "text" or k in ("dt", "tm") or time.time() - t0 > VALUES_BUDGET:
                    continue
                try:
                    vals = column_values(ctx, ds, sid, src, cols, avail, k)
                except Exception:                              # noqa: BLE001 - no value list is only a missing hint
                    vals = None
                if vals:
                    out[sid]["columns"][k]["values"] = vals
    return out


def model_digest(model: dict) -> list[dict]:
    """Compact form for the planner prompt."""
    return [{"entity": e, "title": m["title"],
             "columns": [f"{k} ({c['label']}; {c['kind']}{'; people names' if c.get('pii') else ''}{'; values: ' + ' | '.join(c['values']) if c.get('values') else ''})"
                        for k, c in m["columns"].items()]}
            for e, m in model.items()]


# ------------------------------------------------------------------------------------------------ spec checking
def _fail(msg: str, **extra):
    raise ToolError("bad_args", msg, **extra)


def _col(cols: dict, key, what: str):
    if not isinstance(key, str) or key not in cols:
        _fail(f"{what}: unknown column '{key}'", allowed=sorted(cols))
    return cols[key]


def check_spec(spec: dict, model: dict) -> dict:
    """Validate against the model and fill defaults. Raises ToolError('bad_args') with the allowed values."""
    if not isinstance(spec, dict):
        _fail("the query must be an object")
    entity = spec.get("entity")
    if entity not in model:
        _fail(f"unknown entity '{entity}'", allowed=sorted(model))
    cols = model[entity]["columns"]
    agg = spec.get("agg") or "list"
    if agg not in AGGS:
        _fail(f"agg must be one of {', '.join(AGGS)}")
    out = {"entity": entity, "agg": agg}
    measure = spec.get("measure")
    if agg in ("sum", "avg", "median", "percentile"):
        if _col(cols, measure, "measure")["kind"] != "number":
            _fail(f"{agg} needs a numeric measure", allowed=[k for k, c in cols.items() if c["kind"] == "number"])
        out["measure"] = measure
    elif agg in ("min", "max"):
        if _col(cols, measure, "measure")["kind"] == "text":
            _fail(f"{agg} needs a numeric or date column")
        out["measure"] = measure
    elif agg == "count_distinct":
        _col(cols, measure, "measure")
        out["measure"] = measure
    elif agg == "count" and measure is not None:
        _col(cols, measure, "measure")
        out["measure"] = measure                    # counts the rows where the column has a value
    if agg == "percentile":
        p = _num(spec.get("percentile"))
        if p is None or not 1 <= p <= 99:
            _fail("percentile must be a number from 1 to 99")
        out["percentile"] = p
    g = spec.get("group_by")
    if g not in (None, ""):
        gc = _col(cols, g, "group_by")
        if gc["kind"] == "number":
            _fail("group_by needs a text or date column", allowed=[k for k, c in cols.items() if c["kind"] != "number"])
        if agg == "list":
            _fail("a list cannot be grouped; use an aggregation")
        out["group_by"] = g
    filters = spec.get("filters") or []
    if not isinstance(filters, list) or len(filters) > MAX_FILTERS:
        _fail(f"filters must be a list of at most {MAX_FILTERS}")
    clean = []
    for f in filters:
        if not isinstance(f, dict):
            _fail("each filter needs column, op and value")
        c = _col(cols, f.get("column"), "filter")
        if c.get("pii"):
            _fail("people's names cannot be used as a filter")
        op = f.get("op", "=")
        if op not in OPS:
            _fail(f"filter op must be one of {', '.join(OPS)}")
        v = f.get("value")
        kind = c["kind"]
        if op == "in":
            if not isinstance(v, list) or not v or len(v) > MAX_IN:
                _fail(f"'in' needs a list of 1 to {MAX_IN} values")
            vals = [_value(kind, x) for x in v]
        elif op == "between":
            if not isinstance(v, list) or len(v) != 2:
                _fail("'between' needs [low, high]")
            vals = [_value(kind, x) for x in v]
        else:
            if op == "contains" and kind != "text":
                _fail("'contains' works on text columns")
            if kind == "text" and op not in ("=", "!=", "contains"):
                _fail("text columns support =, !=, contains and in")
            vals = _value(kind, v)
        known = c.get("values")
        if known and kind == "text" and op in ("=", "!=", "contains", "in"):         # the column's values are known: a value that is none of them can only match nothing
            low = [x.lower() for x in known]
            asked = vals if isinstance(vals, list) else [vals]
            for a in asked:
                a_ = str(a).lower()
                if not (any(a_ in x for x in low) if op == "contains" else a_ in low):
                    _fail(f"'{a}' is not a value of {f['column']}", allowed=known)
        clean.append({"column": f["column"], "op": op, "value": vals})
    out["filters"] = clean
    out["order"] = "asc" if spec.get("order") == "asc" else "desc"
    if spec.get("order_by") not in (None, ""):
        oc = _col(cols, spec["order_by"], "order_by")
        if oc.get("pii"):
            _fail("people's names cannot be used to sort")
        out["order_by"] = spec["order_by"]
    lim = spec.get("limit", 10)
    if isinstance(lim, bool) or not isinstance(lim, int) or not 1 <= lim <= MAX_ROWS:
        _fail(f"limit must be a whole number from 1 to {MAX_ROWS}")
    out["limit"] = lim
    show = spec.get("columns")
    if show not in (None, []):
        if not isinstance(show, list) or len(show) > 8:
            _fail("columns must be a list of at most 8 column keys")
        # a display column this user cannot see (a name for a viewer) or that does not exist is left out of the list
        # instead of failing the whole question; filters, sorting and grouping stay strict
        keep = [k for k in dict.fromkeys(show) if isinstance(k, str) and k in cols]
        if keep:
            out["columns"] = keep
    return out


def _value(kind: str, v):
    if isinstance(v, bool) or v is None:
        _fail("a filter value must be a number, text or date")
    if kind == "number":
        n = _num(v) if not isinstance(v, str) else _num(re.sub(r"[ ,_]", "", v))
        if n is None:
            _fail(f"'{v}' is not a number")
        return n
    if kind == "date":
        try:
            return date.fromisoformat(str(v)[:10]).isoformat()
        except ValueError:
            _fail(f"'{v}' is not a date (YYYY-MM-DD)")
    if not isinstance(v, (str, int, float)):
        _fail("a filter value must be text")
    return str(v)[:80]


# ------------------------------------------------------------------------------------------------ compiling
def _from_for(src, avail: dict, exprs: list[str]) -> tuple[str, set]:
    """The FROM clause with only the LEFT JOINs the used expressions need (and those they lean on)."""
    base, joins = _split_from(src)
    need = _closure(set().union(*[_aliases_in(e, joins) for e in exprs]) if exprs else set(), joins)
    for a in need:
        if not joins[a]["needs"] <= set(avail):
            raise ToolError("not_available", "This business has no data for that yet.")
    return base + "".join(jn["sql"] for a, jn in joins.items() if a in need), need


def compile_query(src, spec: dict, cols: dict, avail: dict, tenant_id: str, start, end, dialect: str, lang: str):
    """-> dict(kind, sql, params, count_sql, ...). Only catalog expressions are put into the SQL text."""
    e = {k: c.expr for k, c in cols.items()}
    agg, a0 = spec["agg"], src.alias
    params = bind_params(tenant_id, start, end, dialect)
    where = [f"{a0}.tenant_id = :tenant_id", "d.full_date BETWEEN :start AND :end"]
    used = [e["dt"]]
    filtered = {f["column"] for f in spec["filters"]}
    if src.valid and not (src.valid_unless and src.valid_unless in filtered):
        where.append(f"({src.valid})")
        used.append(src.valid)
    for i, f in enumerate(spec["filters"]):
        ex, op, v = e[f["column"]], f["op"], f["value"]
        used.append(ex)
        p = f"q{i}"
        kind = _kind(cols[f["column"]].fmt or "text")
        if kind == "date" and dialect != "sqlite":
            v = [date.fromisoformat(x) for x in v] if isinstance(v, list) else date.fromisoformat(v)
        if op == "in":
            names = []
            for j, x in enumerate(v):
                params[f"{p}_{j}"] = x.lower() if kind == "text" else x
                names.append(f":{p}_{j}")
            where.append(f"{'LOWER(' + ex + ')' if kind == 'text' else '(' + ex + ')'} IN ({', '.join(names)})")
        elif op == "between":
            params[f"{p}_a"], params[f"{p}_b"] = v
            where.append(f"({ex}) BETWEEN :{p}_a AND :{p}_b")
        elif op == "contains":
            params[p] = "%" + re.sub(r"[%_]", "", v.lower()) + "%"
            where.append(f"LOWER({ex}) LIKE :{p}")
        elif kind == "text":
            params[p] = v.lower()
            where.append(f"LOWER({ex}) {OPS[op]} :{p}")
        else:
            params[p] = v
            where.append(f"({ex}) {OPS[op]} :{p}")
    g = spec.get("group_by")
    m = spec.get("measure")
    if agg == "list":
        order_by = spec.get("order_by") or next((k for k in cols if _kind(cols[k].fmt or "text") == "number"), "dt")
        show = spec.get("columns")
        if not show:                                   # the date, the row's id, what it is sorted by, what it is filtered by, then a few more
            ids = [k for k in cols if k.endswith("_no") or k in ("order_no",)][:1]
            show = list(dict.fromkeys(["dt", *ids, order_by, *[f["column"] for f in spec["filters"]],
                                       *[k for k in cols if not cols[k].pii and k not in ("tm",)]]))[:6]
        if "dt" not in show:
            show = ["dt", *show]
        used += [e[k] for k in show] + [e[order_by]]
        frm, _ = _from_for(src, avail, used)
        cond = " AND ".join(f"({w})" for w in where)
        sel = ", ".join(f"{e[k]} AS {k}" for k in show)
        return {"kind": "list", "show": show, "order_by": order_by,
                "sql": f"SELECT {sel} FROM {frm} WHERE {cond} ORDER BY {e[order_by]} {spec['order'].upper()}, {e['dt']} DESC",
                "count_sql": f"SELECT COUNT(*) AS n FROM {frm} WHERE {cond}", "params": params, "limit": spec["limit"]}
    if m:
        used.append(e[m])
    if g:
        used.append(e[g])
    frm, _ = _from_for(src, avail, used)
    cond = " AND ".join(f"({w})" for w in where)
    gx = f"{e[g]} AS g, " if g else ""
    grp = f" GROUP BY {e[g]}" if g else ""
    if agg in ("median", "percentile"):
        nn = f" AND ({e[m]}) IS NOT NULL"
        return {"kind": "values", "sql": f"SELECT {gx}{e[m]} AS m FROM {frm} WHERE {cond}{nn} LIMIT {MAX_READ + 1}",
                "params": params}
    fn = {"sum": f"SUM({e.get(m, '1')})", "avg": f"AVG({e.get(m, '1')})", "min": f"MIN({e.get(m, '1')})",
          "max": f"MAX({e.get(m, '1')})", "count": f"COUNT({e[m]})" if m else "COUNT(*)",
          "count_distinct": f"COUNT(DISTINCT {e.get(m, '1')})"}[agg]
    order = f" ORDER BY v {spec['order'].upper()}, n DESC" + (", g" if g else "") if g else ""
    return {"kind": "agg", "sql": f"SELECT {gx}{fn} AS v, COUNT(*) AS n FROM {frm} WHERE {cond}{grp}{order}", "params": params}


def pct_of(sorted_vals: list[float], p: float) -> float:
    k = (len(sorted_vals) - 1) * p / 100.0
    lo, hi = math.floor(k), math.ceil(k)
    return sorted_vals[lo] if lo == hi else sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


# ------------------------------------------------------------------------------------------------ the tool
def _show(ctx: ToolContext, src, c, v) -> str:
    if c.fmt == "date":
        return fmt_date(v, ctx.lang)
    if c.fmt in ("text", None, ""):
        if c.pii:
            kind = next((k for col, k in ((load_story_config().get("personal_data") or {}).get("columns") or {}).items()
                         if col in c.expr), "customer")
            return ctx.alias(kind, v)
        return t(ctx.lang, str(v))
    return ctx.money(v, c.fmt) or "-"


def _what(ctx: ToolContext, spec: dict, cols: dict, entity: str) -> str:
    lang = ctx.lang
    tpl = (_T["agg"].get(lang) or _T["agg"]["en"])[spec["agg"]]
    noun = _noun(ctx, entity)
    meas = t(lang, cols[spec["measure"]].label) if spec.get("measure") else noun
    meas = meas if lang == "de" else meas.lower()
    if spec["agg"] in ("count", "list") and spec.get("measure") and spec["agg"] == "count":
        meas = f"{noun} ({meas})"
    out = tpl.replace("{p}", f"{spec.get('percentile', 0):g}").replace("{m}", meas)
    return out[:1].upper() + out[1:] if lang != "de" else out


def _cond_text(ctx: ToolContext, spec: dict, cols: dict, src) -> str:
    bits = []
    for f in spec["filters"]:
        c = cols[f["column"]]
        v = f["value"]
        if isinstance(v, list):
            vs = " – ".join(_show(ctx, src, c, x) for x in v) if f["op"] == "between" else ", ".join(_show(ctx, src, c, x) for x in v)
        else:
            vs = _show(ctx, src, c, v) if c.fmt != "text" else str(v)
        bits.append(f"{t(ctx.lang, c.label)} {f['op']} {vs}")
    return {"en": " and ", "vi": " và ", "de": " und "}.get(ctx.lang, " and ").join(bits)


def custom_query(ctx: ToolContext, entity: str | None = None, agg: str | None = None, measure: str | None = None,
                 percentile: float | None = None, group_by: str | None = None, filters: list | None = None,
                 order: str | None = None, order_by: str | None = None, columns: list | None = None,
                 limit: int | None = None, period=None) -> dict:
    model = query_model(ctx)
    if not model:
        raise ToolError("not_available", "This business has no data for that yet.")
    spec = check_spec({k: v for k, v in dict(entity=entity, agg=agg, measure=measure, percentile=percentile,
                                             group_by=group_by, filters=filters, order=order, order_by=order_by,
                                             columns=columns, limit=limit).items() if v is not None}, model)
    src = ctx.catalog.details[spec["entity"]]
    ds = ctx.ds
    avail = ds.columns()
    cols = usable_columns(src, avail, ctx.user.has_role("manager"))
    req = ctx.period(period, "none")
    q = compile_query(src, spec, cols, avail, ctx.tc.tenant_id, req.start, req.end, ds.dialect, ctx.lang)
    ptxt, ent, lang = ctx.period_text(req), _entity_title(ctx, src), ctx.lang
    what = _what(ctx, spec, cols, ent)
    lifted = src.valid_unless and src.valid_unless in {f["column"] for f in spec["filters"]}
    how = _w("how", lang, what=what, entity=_noun(ctx, ent), period=ptxt,
             where=_w("where", lang, cond=_cond_text(ctx, spec, cols, src)) if spec["filters"] else "",
             excl=_w("excl", lang) if src.valid and not lifted else "")
    meas_fmt = (cols[spec["measure"]].fmt if spec.get("measure") else None) or "int"
    if spec["agg"] in ("count", "count_distinct"):
        meas_fmt = "int"
    facts, tables = [], []
    agg = spec["agg"]
    if True:
        if q["kind"] == "list":
            total = int(ds.query(q["count_sql"], q["params"])[0]["n"] or 0)
            if total == 0:
                return _none(ctx, ds, src, spec, cols, avail, req, what, ent, ptxt, how)
            rows = ds.query(f"{q['sql']} LIMIT {q['limit']}", q["params"])
            topk = not spec["filters"]
            head = (_w("top_asc" if spec["order"] == "asc" else "top_desc", lang, k=min(len(rows), q["limit"]), entity=_noun(ctx, ent),
                       col=t(lang, cols[q["order_by"]].label), n=ctx.money(total, "int"), period=ptxt) if topk
                    else _w("list", lang, n=ctx.money(total, "int"), entity=_noun(ctx, ent), period=ptxt))
            facts.append(ctx.fact(what, total, "int", ptxt, text=head, rows=total))
            shown = []
            for i, r in enumerate(rows, 1):
                item, parts = {}, []
                for k in q["show"]:
                    v = r.get(k)
                    if v is None:
                        continue
                    lab, s = t(lang, cols[k].label), _show(ctx, src, cols[k], v)
                    item[lab] = s
                    parts.append(f"{lab}: {s}")
                shown.append(item)
                facts.append(ctx.fact(f"#{i}", None, None, ptxt, text="; ".join(parts), rank=i, cells=item))
            if total > len(shown) and not topk:
                facts.append(ctx.fact(f"{what}: rows not shown", total - len(shown), "int", ptxt,
                                      text=_w("more", lang, n=total - len(shown))))
            tables.append({"title": what, "columns": list(shown[0]) if shown else [], "rows": shown, "total_rows": total})
        elif q["kind"] == "values":
            rows = ds.query(q["sql"], q["params"])
            if len(rows) > MAX_READ:
                raise ToolError("too_big", "That question reads too many rows; narrow the period or add a filter.")
            p = 50.0 if agg == "median" else float(spec["percentile"])
            buckets = defaultdict(list)
            for r in rows:
                x = _num(r.get("m"))
                if x is not None:
                    buckets[r.get("g") if spec.get("group_by") else None].append(x)
            if not buckets:
                return _none(ctx, ds, src, spec, cols, avail, req, what, ent, ptxt, how)
            res = [(g_, pct_of(sorted(v), p), len(v)) for g_, v in buckets.items()]
            facts, tables = _result_facts(ctx, spec, cols, src, res, what, ptxt, ent, meas_fmt)
        else:
            rows = ds.query(q["sql"], q["params"])
            res = [(r.get("g") if spec.get("group_by") else None, _num(r.get("v")), int(r.get("n") or 0)) for r in rows]
            res = [x for x in res if x[1] is not None and x[2] > 0]
            if not res:
                return _none(ctx, ds, src, spec, cols, avail, req, what, ent, ptxt, how)
            facts, tables = _result_facts(ctx, spec, cols, src, res, what, ptxt, ent, meas_fmt)
    return {"ok": True, "tool": "custom_query", "facts": facts, "tables": tables, "charts": [],
            "meta": ctx.meta(req, None, custom_query={"how": how, "spec": spec, "entity": spec["entity"]})}


def _none(ctx, ds, src, spec, cols, avail, req, what, ent, ptxt, how):
    """Nothing fits the question. Three different truths: the business has none of these rows at all (no data yet), the period
    has none (no data for the period), or the period has rows but none passes the filter (an answer: zero, with how close it got)."""
    lang = ctx.lang
    bare = {**spec, "filters": [], "agg": "count", "measure": None, "group_by": None}
    base = compile_query(src, bare, cols, avail, ctx.tc.tenant_id, req.start, req.end, ds.dialect, lang)
    n = int((ds.query(base["sql"], base["params"]) or [{"v": 0}])[0].get("v") or 0)
    if n == 0:
        ever = compile_query(src, bare, cols, avail, ctx.tc.tenant_id, date(1990, 1, 1), date(2100, 1, 1), ds.dialect, lang)
        if int((ds.query(ever["sql"], ever["params"]) or [{"v": 0}])[0].get("v") or 0) == 0:
            raise ToolError("not_available", "This business has no data for that yet.")
        raise ToolError("no_data", "No rows in that period.")
    # a text filter on a column this business never fills (every row empty: no order status, no reason): "none fits" would read as
    # an answer (zero cancellations) when the truth is that the data does not record it at all
    for f in spec["filters"]:
        c = cols[f["column"]]
        if _kind(c.fmt or "text") == "text" and f["op"] in ("=", "!=", "contains", "in") and f["column"] not in ("dt", "tm"):
            try:
                empty = _column_never_filled(ctx, ds, spec["entity"], src, cols, avail, f["column"])
            except Exception:                                  # noqa: BLE001 - unknown stays the honest "none fits"
                empty = False
            if empty:
                raise ToolError("not_available", "This business has no data for that yet.")
    text = _w("none", lang, entity=_noun(ctx, ent), period=ptxt, n=ctx.money(n, "int"))
    for f in spec["filters"]:                                  # how close did it get? (numeric filter on the largest / smallest value)
        c = cols[f["column"]]
        if _kind(c.fmt or "text") != "number" or f["op"] not in (">", ">=", "<", "<="):
            continue
        hi = f["op"] in (">", ">=")
        one = {**bare, "agg": "max" if hi else "min", "measure": f["column"]}
        q = compile_query(src, one, cols, avail, ctx.tc.tenant_id, req.start, req.end, ds.dialect, lang)
        v = _num((ds.query(q["sql"], q["params"]) or [{}])[0].get("v"))
        if v is not None:
            text += _w("none_hi" if hi else "none_lo", lang, col=t(lang, c.label), value=ctx.money(v, c.fmt))
        break
    for f in spec["filters"]:                                  # a text filter that matched nothing: say which values the column has
        c = cols[f["column"]]
        if _kind(c.fmt or "text") != "text" or f["op"] not in ("=", "!=", "contains", "in"):
            continue
        try:
            known = column_values(ctx, ds, spec["entity"], src, cols, avail, f["column"])
        except Exception:                                      # noqa: BLE001
            known = None
        if known:
            asked = f["value"] if isinstance(f["value"], list) else [f["value"]]
            text += _w("values", lang, asked=", ".join(map(str, asked)), col=t(lang, c.label), values=", ".join(known))
            break
    fact = ctx.fact(what, 0, "int", ptxt, text=text, rows=0)
    return {"ok": True, "tool": "custom_query", "facts": [fact], "tables": [], "charts": [],
            "meta": ctx.meta(req, None, custom_query={"how": how, "spec": spec, "entity": spec["entity"], "empty": True})}


def _result_facts(ctx, spec, cols, src, res, what, ptxt, ent, fmt):
    lang, g = ctx.lang, spec.get("group_by")
    noun = _noun(ctx, ent)
    if not g:
        _, val, n = res[0]
        key = "scalar_count" if spec["agg"] in ("count", "count_distinct") else "scalar"
        return [ctx.fact(what, val, fmt, ptxt, text=_w(key, lang, what=what, period=ptxt, entity=noun, n=ctx.money(n, "int"),
                                                       value=ctx.money(val, fmt)), rows=n)], []
    desc = spec["order"] == "desc"
    res = sorted(res, key=lambda x: (x[1], str(x[0])), reverse=desc)
    gc, by = cols[g], t(lang, cols[g].label)
    by_w = by if lang == "de" else by.lower()
    top = res[:spec["limit"]]
    facts = [ctx.fact(what, len(res), "int", ptxt, text=_w("group", lang, what=what, by=by_w, period=ptxt), groups=len(res))]
    shown = []
    for i, (gk, val, n) in enumerate(top, 1):
        name = _show(ctx, src, gc, gk) if gk is not None else "-"
        item = {by: name, t(lang, "Value"): ctx.money(val, fmt)}
        shown.append(item)
        facts.append(ctx.fact(f"#{i} {name}", val, fmt, ptxt, text=f"{name}: {ctx.money(val, fmt)} ({ctx.money(n, 'int')} {noun})",
                              rank=i, cells=item, rows=n))
    if len(res) > len(top):
        facts.append(ctx.fact(f"{what}: groups not shown", len(res) - len(top), "int", ptxt, text=_w("more", lang, n=len(res) - len(top))))
    return facts, [{"title": what, "columns": list(shown[0]), "rows": shown, "total_rows": len(res)}]
