"""One registry for every Ask tool: name, description, JSON schema, function, the result shapes it serves.

The planner prompt (Step 3) and the argument validator are both generated from this file, so a schema is never
copied by hand. ``call`` is the only entry point the chat uses: it validates, runs, and turns every failure into a
typed ``{"ok": False, ...}`` result (a tool never raises into the chat).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable

from ...services import ServiceError
from . import analysis_tools as at
from . import catalog_tools as ct
from . import detail_tools as dt
from . import insight_tools as it
from . import query_tool as qt
from .common import ToolContext, ToolError

log = logging.getLogger("vdash.ask.tools")

PERIOD_SCHEMA = {
    "description": "Preset id relative to the latest loaded date, or an explicit range.",
    "oneOf": [{"type": "string", "enum": ["last_7_days", "last_30_days", "last_90_days", "mtd", "qtd", "ytd",
                                           "last_12_months", "prev_month", "prev_quarter", "prev_year", "all", "all_time"]},
              {"type": "object", "properties": {"start": {"type": "string", "format": "date"},
                                                "end": {"type": "string", "format": "date"}},
               "required": ["start", "end"], "additionalProperties": False}],
}
KPI_ID = {"type": "string", "description": "Dashboard KPI id (or template KPI id such as KPI-SALES-001)."}


@dataclass
class Tool:
    name: str
    description: str
    params: dict                       # JSON-schema "properties"
    required: list[str]
    fn: Callable
    serves: list[str] = field(default_factory=list)      # question-bank result shapes
    viz: list[str] = field(default_factory=list)         # chart types of the KPIs it can use
    min_role: str = "viewer"

    def schema(self) -> dict:
        return {"name": self.name, "description": self.description,
                "parameters": {"type": "object", "properties": self.params, "required": self.required,
                               "additionalProperties": False}}


TOOLS: dict[str, Tool] = {t.name: t for t in [
    Tool("kpi_value", "One number (a KPI total) for a period, optionally with the change against an earlier period.",
         {"kpi_id": KPI_ID, "period": PERIOD_SCHEMA, "compare": {"type": "string", "enum": ["none", "previous", "yoy"]}},
         ["kpi_id"], ct.kpi_value, serves=["single_value"], viz=["metric"]),
    Tool("compare_periods", "The same KPI for two periods side by side with the change.",
         {"kpi_id": KPI_ID, "period_a": PERIOD_SCHEMA, "period_b": PERIOD_SCHEMA,
          "against": {"type": "string", "enum": ["previous", "yoy"], "description": "Used when period_b is not given."}},
         ["kpi_id"], ct.compare_periods, serves=["single_value"], viz=["metric"]),
    Tool("kpi_breakdown", "A KPI split by something (category, channel, product, weekday ...) or a ranked list; "
                          "the largest items first.",
         {"kpi_id": KPI_ID, "period": PERIOD_SCHEMA, "top_n": {"type": "integer", "minimum": 1, "maximum": ct.MAX_TOP_N}},
         ["kpi_id"], ct.kpi_breakdown, serves=["breakdown", "ranked_list", "table"], viz=["donut", "hbar", "bar", "table", "heatmap"]),
    Tool("kpi_trend", "How a KPI moves over time (day, week, month or quarter).",
         {"kpi_id": KPI_ID, "period": PERIOD_SCHEMA,
          "grain": {"type": "string", "enum": ["auto", "day", "week", "month", "quarter"]}},
         ["kpi_id"], ct.kpi_trend, serves=["time_series"], viz=["timeseries"]),
    Tool("explain_change", "Why revenue changed between two periods: the change split into orders, order value, items per "
                           "order, price, customers and spend per customer, plus the items that fell or rose most.",
         {"period": PERIOD_SCHEMA, "against": {"type": "string", "enum": ["previous", "yoy"]},
          "period_b": PERIOD_SCHEMA},
         [], at.explain_change, serves=["explanation"], viz=[]),
    Tool("top_movers", "Which items of a breakdown (category, channel, product ...) rose or fell most between two periods.",
         {"kpi_id": KPI_ID, "period": PERIOD_SCHEMA, "against": {"type": "string", "enum": ["previous", "yoy"]},
          "top_n": {"type": "integer", "minimum": 1, "maximum": 10}, "period_b": PERIOD_SCHEMA},
         ["kpi_id"], at.top_movers, serves=["movers"], viz=["donut", "hbar", "bar"]),
    Tool("warnings", "Early warnings that are active now for this business (what needs attention).",
         {"top_n": {"type": "integer", "minimum": 1, "maximum": it.MAX_FINDINGS}},
         [], it.warnings, serves=["warnings"], viz=[]),
    Tool("findings", "What went well and what went badly in a period compared with an earlier one (business evaluation).",
         {"period": PERIOD_SCHEMA, "against": {"type": "string", "enum": ["previous", "yoy"]},
          "side": {"type": "string", "enum": ["both", "positive", "negative"]},
          "top_n": {"type": "integer", "minimum": 1, "maximum": it.MAX_FINDINGS}},
         [], it.findings, serves=["findings"], viz=[]),
    Tool("recommended_actions", "Concrete next steps suggested by the rules, each with the finding it comes from.",
         {"period": PERIOD_SCHEMA, "against": {"type": "string", "enum": ["previous", "yoy"]},
          "top_n": {"type": "integer", "minimum": 1, "maximum": 6}},
         [], it.recommended_actions, serves=["actions"], viz=[]),
    Tool("forecast", "What the predictions expect. focus=demand (default): the next days' total demand and the products expected to sell most. "
                     "focus=customers: what the current customers are expected to spend in the future (customer future value).",
         {"top_n": {"type": "integer", "minimum": 1, "maximum": 15},
          "focus": {"type": "string", "enum": ["demand", "customers"]}}, [], dt.forecast, serves=["forecast"], viz=[]),
    Tool("drill", "The rows behind one chart element of a KPI: one category (key) or one time bucket (bucket + grain).",
         {"kpi_id": KPI_ID, "period": PERIOD_SCHEMA, "key": {"type": "string"}, "bucket": {"type": "string"},
          "grain": {"type": "string", "enum": ["day", "week", "month", "quarter", "year"]},
          "top_n": {"type": "integer", "minimum": 1, "maximum": dt.MAX_ROWS}},
         ["kpi_id"], dt.drill, serves=["detail_rows"], viz=["donut", "hbar", "bar", "timeseries"]),
    Tool("customer_segments", "Customers of one buying-behaviour segment and/or risk-of-leaving band, biggest spenders first; "
                              "without arguments, how many customers each segment and band holds; with top=true, the most valuable customers "
                              "overall (total spend over their whole history), each with segment, risk of leaving and expected "
                              "future spend. Manager role.",
         {"segment": {"type": "string"}, "churn_band": {"type": "string"}, "top": {"type": "boolean"},
          "top_n": {"type": "integer", "minimum": 1, "maximum": dt.MAX_ROWS}},
         [], dt.customer_segments, serves=["customer_list"], viz=[], min_role="manager"),
    Tool("custom_query", "A calculation or list no other tool gives, over single orders, sales lines, waste events or goods "
                         "receipts: a median or percentile, a total/average/count with filters, a count of different values, "
                         "a value per group, or a list of single rows (for example orders above an amount). Write a spec, never SQL: "
                         "entity and column keys come from query_model; amounts are plain numbers in the shop's currency "
                         "(20 million = 20000000). Use ONLY when no KPI or other tool answers the question.",
         {"entity": {"type": "string"}, "agg": {"type": "string", "enum": list(qt.AGGS)},
          "measure": {"type": "string"}, "percentile": {"type": "number", "minimum": 1, "maximum": 99},
          "group_by": {"type": "string"},
          "filters": {"type": "array", "items": {"type": "object", "properties": {
              "column": {"type": "string"}, "op": {"type": "string", "enum": list(qt.OPS)},
              "value": {"description": "number, text, date (YYYY-MM-DD) or a list for in / between"}},
              "required": ["column", "op", "value"]}},
          "order": {"type": "string", "enum": ["asc", "desc"]}, "order_by": {"type": "string"},
          "columns": {"type": "array", "items": {"type": "string"}},
          "limit": {"type": "integer", "minimum": 1, "maximum": qt.MAX_ROWS}, "period": PERIOD_SCHEMA},
         ["entity"], qt.custom_query, serves=["custom"], viz=[]),
]}


def schemas() -> list[dict]:
    return [t.schema() for t in TOOLS.values()]


def validate_args(tool: Tool, args) -> dict:
    """Strict check: object, no unknown names (a model cannot smuggle in tenant_id or SQL), required present,
    basic types and enums. Returns the cleaned dict or raises ToolError('bad_args')."""
    if not isinstance(args, dict):
        raise ToolError("bad_args", "arguments must be an object")
    unknown = sorted(set(args) - set(tool.params))
    if unknown:
        raise ToolError("bad_args", f"unknown argument(s): {', '.join(unknown)}")
    missing = [r for r in tool.required if args.get(r) in (None, "")]
    if missing:
        raise ToolError("bad_args", f"missing argument(s): {', '.join(missing)}")
    for name, v in args.items():
        spec = tool.params[name]
        if v is None:
            continue
        if spec.get("type") == "string" and not isinstance(v, str):
            raise ToolError("bad_args", f"{name} must be text")
        if spec.get("type") == "boolean" and not isinstance(v, bool):
            raise ToolError("bad_args", f"{name} must be true or false")
        if spec.get("type") == "integer" and (not isinstance(v, int) or isinstance(v, bool)):
            raise ToolError("bad_args", f"{name} must be a whole number")
        if spec.get("enum") and v not in spec["enum"]:
            raise ToolError("bad_args", f"{name} must be one of {', '.join(spec['enum'])}")
    return {k: v for k, v in args.items() if v is not None}


def call(ctx: ToolContext, name: str, args) -> dict:
    tool = TOOLS.get(name)
    if tool is None:
        return {"ok": False, "tool": name, "error": "unknown_tool", "message": f"Unknown tool '{name}'.", "meta": ctx.meta()}
    try:
        if not ctx.user.has_role(tool.min_role):
            raise ToolError("forbidden", "Your role does not allow this question.")
        clean = validate_args(tool, args)
        out = tool.fn(ctx, **clean)
        out.setdefault("meta", ctx.meta())["aliases"] = dict(ctx.aliases)
        from .money_style import restyle_result
        return restyle_result(out, ctx.lang, ctx.currency)       # findings / warnings carry amounts written by the report engine
    except ToolError as e:
        return {"ok": False, "tool": name, "error": e.code, "message": e.message, "meta": ctx.meta(),
                **{k: v for k, v in e.extra.items() if k != "reasons"}}
    except ServiceError as e:
        return {"ok": False, "tool": name, "error": "failed", "message": e.detail, "meta": ctx.meta()}
    except Exception:                                    # a tool never raises into the chat
        log.exception("tool %s failed", name)
        return {"ok": False, "tool": name, "error": "failed", "message": "The data could not be read right now.",
                "meta": ctx.meta()}
