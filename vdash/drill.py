"""Drill-down: from a chart element to the rows behind it.

The chart's own SQL is never reused as text. Instead each KPI declares a
``Drill`` (see catalog): which detail source holds its rows (sales lines,
orders), its row filter, and how a click narrows the rows - by time bucket,
by the same category expression the chart grouped on, or by heatmap cell.
The drill query therefore returns exactly the rows the clicked value
aggregates (tests check the sums match).

Security is the same as for the chart: the KPI must be visible to the user,
``:tenant_id`` is bound by the server, clicked values are bound parameters
(never pasted into SQL), and personal columns (customer / staff names) are
removed below manager role.
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from .catalog import DetailSource, Kpi
from .engine import add_months, bind_params

MAX_PREVIEW = 200
MAX_EXPORT = 100_000


class DrillError(ValueError):
    pass


def bucket_range(bucket: str, grain: str) -> tuple[date, date]:
    """Date range of a chart bucket label ('2025-07-14' day/week, '2025-07', '2025-Q3', '2025')."""
    b = str(bucket)
    try:
        if len(b) == 10:
            d0 = date.fromisoformat(b)
            return (d0, d0 + timedelta(days=6)) if grain == "week" else (d0, d0)
        if len(b) == 7 and b[4] == "-" and b[5] != "Q":
            d0 = date(int(b[:4]), int(b[5:7]), 1)
            return d0, add_months(d0, 1) - timedelta(days=1)
        if len(b) == 7 and b[5] == "Q":
            q = int(b[6])
            d0 = date(int(b[:4]), 3 * (q - 1) + 1, 1)
            return d0, add_months(d0, 3) - timedelta(days=1)
        if len(b) == 4:
            return date(int(b), 1, 1), date(int(b), 12, 31)
    except (ValueError, IndexError):
        pass
    raise DrillError(f"unrecognised period bucket '{bucket}'")


def _eq(expr: str, name: str, value: Any, params: dict) -> str:
    if value is None:
        return f"{expr} IS NULL"
    params[name] = value
    return f"{expr} = :{name}"


def build(kpi: Kpi, src: DetailSource, f: dict, start: date, end: date, dialect: str, tenant_id: str,
          show_pii: bool, available: dict[str, set[str]] | None = None) -> tuple[str, str, dict, list, str]:
    """-> (rows_sql_without_limit, summary_sql, params, columns, description_of_filter_parts)"""
    d = kpi.drill
    if d is None:
        raise DrillError("this KPI has no detail rows")
    s, e = start, end
    parts: list[str] = []
    where: list[str] = [f"{src.alias}.tenant_id = :tenant_id", "d.full_date BETWEEN :start AND :end"]
    params: dict[str, Any] = {}
    if d.extra:
        where.append(d.extra)
    if "bucket" in f:
        if not d.by_bucket:
            raise DrillError("this chart cannot be narrowed by period")
        bs, be = bucket_range(f["bucket"], f.get("grain", "day"))
        s, e = max(s, bs), min(e, be)
        if s > e:
            raise DrillError("the clicked period is outside the selected period")
        parts.append("bucket")
    if "key" in f:
        if d.key_member:
            # ranked class (e.g. ABC): the members of the clicked class, ranked over the selected period in SQL
            if f["key"] is None:
                raise DrillError("this chart has no blank category")
            row_expr, members_sql = d.key_member
            params["dv"] = f["key"]
            where.append(f"{row_expr} IN ({members_sql})")
        elif d.key_expr:
            where.append(_eq(d.key_expr, "dv", f["key"], params))
        else:
            raise DrillError("this chart cannot be narrowed by category")
        parts.append("key")
    if "y" in f or "x" in f or "x_range" in f:
        if not (d.x_expr and d.y_expr):
            raise DrillError("this chart has no cells")
        if "y" in f:
            where.append(_eq(d.y_expr, "dy", f["y"], params))
        if "x_range" in f:
            lo, hi = f["x_range"]
            params["dx1"], params["dx2"] = lo, hi
            where.append(f"{d.x_expr} BETWEEN :dx1 AND :dx2")
        elif "x" in f:
            where.append(_eq(d.x_expr, "dx", f["x"], params))
        parts.append("cell")
    params.update(bind_params(tenant_id, s, e, dialect))
    cols = [c for c in src.columns if show_pii or not c.pii]
    if available is not None:  # drop columns whose tables/columns this warehouse does not have
        cols = [c for c in cols if _expr_ok(c.expr, src, available)]
    where_sql = " AND ".join(f"({w})" for w in where)
    rows_sql = (f"SELECT {', '.join(f'{c.expr} AS {c.key}' for c in cols)} FROM {src.from_sql} "
                f"WHERE {where_sql} ORDER BY {src.order_by}")
    total = f", SUM({src.total_expr}) AS total_v" if src.total_expr else ""
    summary_sql = f"SELECT COUNT(*) AS n{total} FROM {src.from_sql} WHERE {where_sql}"
    return rows_sql, summary_sql, params, cols, f"{s.isoformat()}..{e.isoformat()}"


def _expr_ok(expr: str, src: DetailSource, available: dict[str, set[str]]) -> bool:
    """Keep a column only if every alias.column it references exists (e.g. no gross_profit column -> dropped)."""
    alias_table = {alias: table for table, alias in re.findall(r"(?:FROM|JOIN)\s+(\w+)\s+(\w+)", src.from_sql)}
    for a, col in re.findall(r"\b(\w+)\.(\w+)\b", expr):
        table = alias_table.get(a)
        if table and col not in available.get(table, set()):
            return False
    return True


def drop_empty_columns(cols: list, rows: list[dict]) -> list:
    return [c for c in cols if any(r.get(c.key) not in (None, "") for r in rows)]


def sample_click(res: dict) -> dict | None:
    """The click payload web/app.js would send for the biggest element of a chart result (used by checks)."""
    t = res.get("type")
    if t == "breakdown":
        it = next((i for i in res.get("items", []) if i.get("key") != "__other"), None)
        return {"key": it["key"]} if it else None
    if t == "heatmap" and res.get("cells"):
        xi, yi, _ = max(res["cells"], key=lambda c: c[2] or 0)
        return {"x": res["x_keys"][xi], "y": res["y_keys"][yi]}
    if t == "timeseries" and res.get("series") and res.get("x"):
        s = res["series"][0]
        j = max(range(len(res["x"])), key=lambda i: s["data"][i] or 0)
        f = {"bucket": res["x"][j], "grain": res.get("grain", "month")}
        if "key" in s and s.get("drill", True):
            f["key"] = s["key"]
        return f
    return None
