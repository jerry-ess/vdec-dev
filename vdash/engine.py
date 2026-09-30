"""KPI engine: turns (tenant, KPI, period) into chart-ready JSON.

* Period handling - presets anchored on the tenant's latest data date, custom
  ranges, comparison periods (previous period / same period last year).
* Time buckets - day / week / month / quarter / year expressed with
  ``dim_date`` columns only, so the SQL is identical on every warehouse.
* Tenant isolation - the tenant id is bound by the engine from the tenant
  config; KPI SQL without ``:tenant_id`` never gets here (catalog validation).
* Caching - results are cached per (tenant, KPI, period, data version); a new
  warehouse load changes the data version and naturally invalidates the cache.
"""
from __future__ import annotations

import math
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Callable

from .catalog import Kpi
from .datasources import DataSource

GRAINS = ("day", "week", "month", "quarter", "year")
COMPARE_MODES = ("previous", "yoy", "none")

BUCKET_SQL = {
    "day": "d.full_date",
    # ISO weeks: a late-December date in week 1 belongs to next year's week 1 (and vice versa).
    "week": ("CASE WHEN d.week_of_year = 1 AND d.month = 12 THEN d.year + 1 "
             "WHEN d.week_of_year >= 52 AND d.month = 1 THEN d.year - 1 ELSE d.year END, d.week_of_year"),
    "month": "d.year_month",
    "quarter": "d.year, d.quarter",
    "year": "d.year",
}


# ------------------------------------------------------------------ periods
def _d(v: Any) -> date:
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10])


def add_months(d: date, n: int) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    y += d.year
    m += 1
    last = [31, 29 if (y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1]
    return date(y, m, min(d.day, last))


def presets(anchor: date, min_date: date) -> list[dict]:
    """Named ranges relative to the tenant's latest data date."""
    q_start = date(anchor.year, 3 * ((anchor.month - 1) // 3) + 1, 1)
    prev_m_end = date(anchor.year, anchor.month, 1) - timedelta(days=1)
    prev_q_end = q_start - timedelta(days=1)
    out = [
        ("last_7_days", "Last 7 days", anchor - timedelta(days=6), anchor),
        ("last_30_days", "Last 30 days", anchor - timedelta(days=29), anchor),
        ("last_90_days", "Last 90 days", anchor - timedelta(days=89), anchor),
        ("mtd", "Month to date", date(anchor.year, anchor.month, 1), anchor),
        ("qtd", "Quarter to date", q_start, anchor),
        ("ytd", "Year to date", date(anchor.year, 1, 1), anchor),
        ("last_12_months", "Last 12 months", add_months(anchor, -12) + timedelta(days=1), anchor),
        ("prev_month", "Previous month", date(prev_m_end.year, prev_m_end.month, 1), prev_m_end),
        ("prev_quarter", "Previous quarter", date(prev_q_end.year, 3 * ((prev_q_end.month - 1) // 3) + 1, 1), prev_q_end),
        ("prev_year", "Previous year", date(anchor.year - 1, 1, 1), date(anchor.year - 1, 12, 31)),
        ("all", "All time", min_date, anchor),
    ]
    return [{"id": i, "label": lbl, "start": s.isoformat(), "end": e.isoformat()} for i, lbl, s, e in out]


def auto_grain(start: date, end: date) -> str:
    days = (end - start).days + 1
    if days <= 62:
        return "day"
    if days <= 200:
        return "week"
    if days <= 1100:
        return "month"
    return "quarter"


def spark_grain(start: date, end: date) -> str:
    days = (end - start).days + 1
    return "day" if days <= 45 else "week" if days <= 300 else "month"


def compare_range(start: date, end: date, mode: str) -> tuple[date, date] | None:
    if mode == "previous":
        n = (end - start).days + 1
        return start - timedelta(days=n), start - timedelta(days=1)
    if mode == "yoy":
        return add_months(start, -12), add_months(end, -12)
    return None


def bucket_label(k: Any, grain: str) -> str:
    """Label for a bucket from its first date ('YYYY-MM' keys from monthly marts pass through)."""
    s = str(k)
    if len(s) == 7:
        return s
    d = _d(s)
    if grain == "day":
        return d.isoformat()
    if grain == "week":
        return (d - timedelta(days=d.weekday())).isoformat()
    if grain == "month":
        return d.strftime("%Y-%m")
    if grain == "quarter":
        return f"{d.year}-Q{(d.month - 1) // 3 + 1}"
    return str(d.year)


@dataclass
class PeriodRequest:
    start: date
    end: date
    grain: str = "auto"
    compare: str = "previous"

    def resolved_grain(self) -> str:
        return auto_grain(self.start, self.end) if self.grain == "auto" else self.grain

    def key(self) -> str:
        return f"{self.start}|{self.end}|{self.resolved_grain()}|{self.compare}"


def bind_params(tenant_id: str, start: date, end: date, dialect: str) -> dict:
    as_param: Callable[[date], Any] = (lambda d: d.isoformat()) if dialect == "sqlite" else (lambda d: d)
    return {
        "tenant_id": tenant_id,
        "start": as_param(start),
        "end": as_param(end),
        "end_minus_90": as_param(end - timedelta(days=90)),
        "start_month": start.strftime("%Y-%m"),
        "end_month": end.strftime("%Y-%m"),
    }


# ------------------------------------------------------------------ cache
class ResultCache:
    def __init__(self, max_entries: int = 2000, ttl_seconds: int = 3600):
        self.max_entries = max_entries
        self.ttl = ttl_seconds
        self._d: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._lock = threading.Lock()
        self.hits = self.misses = 0

    def get(self, key: str) -> Any:
        with self._lock:
            item = self._d.get(key)
            if item is None or time.time() - item[0] > self.ttl:
                if item is not None:
                    del self._d[key]
                self.misses += 1
                return None
            self._d.move_to_end(key)
            self.hits += 1
            return item[1]

    def put(self, key: str, value: Any) -> None:
        with self._lock:
            self._d[key] = (time.time(), value)
            self._d.move_to_end(key)
            while len(self._d) > self.max_entries:
                self._d.popitem(last=False)

    def invalidate_tenant(self, tenant_id: str) -> None:
        with self._lock:
            for k in [k for k in self._d if k.startswith(tenant_id + "|")]:
                del self._d[k]

    def stats(self) -> dict:
        return {"entries": len(self._d), "hits": self.hits, "misses": self.misses}


# ------------------------------------------------------------------ post-processing
def _num(v: Any) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
        return None if math.isnan(f) or math.isinf(f) else f
    except (TypeError, ValueError):
        return None


def _top_pct_share(rows: list[dict], pct: float) -> float | None:
    vals = sorted((v for v in (_num(r.get("v")) for r in rows) if v is not None), reverse=True)
    total = sum(vals)
    if not vals or total <= 0:
        return None
    n = max(1, math.ceil(len(vals) * pct / 100.0))
    return 100.0 * sum(vals[:n]) / total


def _abc(rows: list[dict]) -> list[dict]:
    """Rows already classified in SQL (catalog ABC_CLASSES): label = class, v = SKU count, rev_v = class revenue.
    The class is computed in SQL, not here, so the drill-down can select exactly the same SKUs (v8)."""
    counts = {"A": 0, "B": 0, "C": 0}
    revenue = {"A": 0.0, "B": 0.0, "C": 0.0}
    for r in rows:
        cls = str(r.get("label"))
        if cls in counts:
            counts[cls] += int(_num(r.get("v")) or 0)
            revenue[cls] += _num(r.get("rev_v")) or 0.0
    total = sum(revenue.values())
    # label_key/label_args are formatted in the viewer's language by the API (i18n.translate_result)
    return [{"key": c, "label": c, "value": counts[c], "label_key": "abc_label",
             "label_args": {"cls": c, "pct": round(100.0 * revenue[c] / total) if total else 0, "n": counts[c]}}
            for c in ("A", "B", "C") if counts[c]]


def _pivot(rows: list[dict], grain: str, max_series: int = 6) -> tuple[list[str], dict[str, list]]:
    totals: dict[str, float] = {}
    for r in rows:
        lbl = str(r.get("label") if r.get("label") is not None else "(blank)")
        totals[lbl] = totals.get(lbl, 0.0) + (_num(r.get("v")) or 0.0)
    ranked = sorted(totals, key=lambda k: -totals[k])
    keep = set(ranked[:max_series])
    x: list[str] = []
    cells: dict[tuple[str, str], float] = {}
    for r in rows:
        b = bucket_label(r["k"], grain)
        if b not in x:
            x.append(b)
        lbl = str(r.get("label") if r.get("label") is not None else "(blank)")
        lbl = lbl if lbl in keep else "Other"
        cells[(b, lbl)] = cells.get((b, lbl), 0.0) + (_num(r.get("v")) or 0.0)
    x.sort()
    names = [n for n in ranked if n in keep] + (["Other"] if len(ranked) > max_series else [])
    return x, {n: [cells.get((b, n)) for b in x] for n in names}


# ------------------------------------------------------------------ engine
class KpiEngine:
    def __init__(self, cache: ResultCache):
        self.cache = cache

    @staticmethod
    def _sql(sql: str, grain: str) -> str:
        return sql.replace("@BUCKET@", BUCKET_SQL[grain])

    def _q(self, ds: DataSource, sql: str, tenant_id: str, start: date, end: date, grain: str = "day") -> list[dict]:
        if ":tenant_id" not in sql:  # defence in depth - catalog validation already guarantees this
            raise PermissionError("refusing to run SQL without a tenant filter")
        return ds.query(self._sql(sql, grain), bind_params(tenant_id, start, end, ds.dialect))

    def run(self, ds: DataSource, tenant_id: str, kpi: Kpi, req: PeriodRequest, data_version: str) -> dict:
        key = f"{tenant_id}|{data_version}|{kpi.id}|{req.key()}"
        hit = self.cache.get(key)
        if hit is not None:
            return {**hit, "cached": True}
        t0 = time.perf_counter()
        try:
            out = self._run(ds, tenant_id, kpi, req)
        except Exception as exc:  # one broken KPI must never take down the page
            out = {"type": "error", "error": f"{type(exc).__name__}: {str(exc).splitlines()[0][:300]}"}
        out["ms"] = round((time.perf_counter() - t0) * 1000, 1)
        if out.get("type") != "error":
            self.cache.put(key, out)
        return out

    def _run(self, ds: DataSource, tenant_id: str, kpi: Kpi, req: PeriodRequest) -> dict:
        grain = req.resolved_grain()
        rows = self._q(ds, kpi.sql, tenant_id, req.start, req.end, grain)
        post = (kpi.post or "").split(":")
        if kpi.viz == "metric":
            return self._metric(ds, tenant_id, kpi, req, rows, post)
        if kpi.viz == "timeseries":
            if post[0] == "pivot":
                x, data = _pivot(rows, grain)
                s0 = kpi.series[0] if kpi.series else None
                series = [{"name": n, "key": None if n in ("Other", "(blank)") else n, "drill": n != "Other", "type": s0.type if s0 else "line", "fmt": kpi.fmt, "axis": 0,
                           "stack": s0.stack if s0 else None, "data": v} for n, v in data.items()]
            else:
                x = [bucket_label(r["k"], grain) for r in rows]
                series = []
                for s in kpi.series:
                    vals = [_num(r.get(s.col)) for r in rows]
                    if any(v is not None for v in vals):  # a series with no data for this tenant is dropped
                        series.append({"name": s.name, "type": s.type, "fmt": s.fmt, "axis": s.axis, "trend": s.trend,
                                       "stack": s.stack, "data": vals})
            return {"type": "timeseries", "grain": grain if kpi.period_mode == "range" else "month",
                    "x": x, "series": series, "empty": not x or not series}
        if kpi.viz == "heatmap":
            xs = sorted({r["x"] for r in rows if r.get("x") is not None}, key=_sort_key)
            ys = sorted({r["y"] for r in rows if r.get("y") is not None}, key=_sort_key)
            xi = {v: i for i, v in enumerate(xs)}
            yi = {v: i for i, v in enumerate(ys)}
            cells = [[xi[r["x"]], yi[r["y"]], _num(r.get("v"))] for r in rows
                     if r.get("x") is not None and r.get("y") is not None and _num(r.get("v")) is not None]
            cells.sort(key=lambda c: (c[1], c[0]))
            return {"type": "heatmap", "x": [str(v) for v in xs], "x_keys": [_key(v) for v in xs],
                    "y_keys": [_key(v) for v in ys],
                    "y": [kpi.label_map.get(str(v), str(v)) for v in ys], "cells": cells, "empty": not cells}
        if kpi.viz == "table":
            out_rows = [{c.key: r.get(c.key) for c in kpi.columns} for r in rows]
            return {"type": "table", "rows": out_rows, "empty": not out_rows}
        # breakdowns: bar / hbar / donut / stacked / funnel
        if post[0] == "abc":
            items = _abc(rows)
        else:
            items = [{"key": _key(r.get("label")),
                      "label": kpi.label_map.get(str(r.get("label")), str(r.get("label")) if r.get("label") is not None
                                                 else "(not set)"), "value": _num(r.get("v"))} for r in rows]
            items = [i for i in items if i["value"] is not None]
        return {"type": "breakdown", "items": items, "empty": not items}

    def _metric_value(self, rows: list[dict], post: list[str]) -> float | None:
        if post and post[0] == "top_pct_share":
            return _top_pct_share(rows, float(post[1]) if len(post) > 1 else 20.0)
        return _num(rows[0].get("v")) if rows else None

    def _metric(self, ds, tenant_id, kpi: Kpi, req: PeriodRequest, rows, post) -> dict:
        value = self._metric_value(rows, post)
        out: dict[str, Any] = {"type": "metric", "value": value, "empty": value is None}
        if rows:
            # v11: raw model caveats stored in the warehouse are technical notes for the data team (method names,
            # scores, file names). They are never sent to business users; each KPI's "About" text explains it.
            if rows[0].get("caveat_from"):
                out["caveat"] = f"Forecast window starts {str(rows[0]['caveat_from'])[:10]}."
                out["caveat_key"], out["caveat_args"] = "forecast_from", {"date": str(rows[0]["caveat_from"])[:10]}
        if kpi.compare and kpi.period_mode != "snapshot" and req.compare != "none":
            cr = compare_range(req.start, req.end, req.compare)
            if cr:
                prev = self._metric_value(self._q(ds, kpi.sql, tenant_id, cr[0], cr[1]), post)
                out["compare_value"] = prev
                out["compare_start"], out["compare_end"] = cr[0].isoformat(), cr[1].isoformat()
                if prev not in (None, 0) and value is not None:
                    out["delta_pct"] = 100.0 * (value - prev) / abs(prev)
                    if kpi.fmt == "pct":
                        out["delta_pts"] = value - prev
        if kpi.spark_sql and value is not None:
            g = spark_grain(req.start, req.end)
            srows = self._q(ds, kpi.spark_sql, tenant_id, req.start, req.end, g)
            out["spark"] = [[bucket_label(r["k"], g), _num(r.get("v"))] for r in srows]
        return out


def _key(v: Any) -> Any:
    """Raw value a chart element stands for (kept for drill-down / downloads; labels get translated)."""
    if v is None:
        return None
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return v if isinstance(v, (int, float, str)) else str(v)


def _sort_key(v: Any):
    try:
        return (0, float(v), "")
    except (TypeError, ValueError):
        return (1, 0.0, str(v))
