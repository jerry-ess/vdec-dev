"""Insight tools: ``warnings``, ``findings`` and ``recommended_actions``.

They return what the dashboard's own rules already found (early warnings, the business evaluation, the quick wins of
the report), in the same wording and with the same role rules. Nothing is calculated or judged here: the rules are
the template's rule files, the tool only reads their results.
"""
from __future__ import annotations

from ...reports.composer import Composer
from ...reports.quick_wins import quick_wins
from ...reports.story_rules import load_story_config
from .common import ToolContext, ToolError

MAX_FINDINGS = 10
SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}


def _panel(ctx: ToolContext, name: str) -> None:
    if not ctx.dash.panels_for(ctx.user, ctx.tc, ctx.st).get(name):
        raise ToolError("not_available", "This business has no data for that yet.", panel=name)


def _rule_kind(ctx: ToolContext, rule_id: str) -> str | None:
    """'customer' / 'staff' when the rule reads people's names (same rule as the report's fact sheet)."""
    cols = (load_story_config().get("personal_data") or {}).get("columns") or {}
    r = ctx.catalog.rules_by_id.get(rule_id)
    if r is None:
        return None
    for q in list(r.requires) + list(r.requires_any):
        if q.column in cols:
            return cols[q.column]
    return "customer" if r.items_min_role != "viewer" else None


def _finding_fact(ctx: ToolContext, c: Composer, f: dict, period: str, side: str | None = None) -> dict:
    text = c.text(f["msg"], f["args"])
    kind = _rule_kind(ctx, f["rule"])
    if kind:                                         # personal names never leave the server: Customer A, Staff B ...
        keys = set((load_story_config().get("personal_data") or {}).get("row_keys") or [])
        real = {str(v) for row in f.get("items") or [] for k, v in row.items() if k in keys and v}
        for name in sorted(real, key=len, reverse=True):
            text = text.replace(name, ctx.alias(kind, name))
    extra = {"rule": f["rule"], "severity": f.get("severity"), "new": bool(f.get("is_new")) or None,
             "side": side or f.get("side"), "basis": f.get("basis")}
    return ctx.fact(f["title"], None, None, period, text=text, **extra)


def warnings(ctx: ToolContext, top_n: int = 5) -> dict:
    """Early warnings that are active now."""
    if not isinstance(top_n, int) or isinstance(top_n, bool) or not 1 <= top_n <= MAX_FINDINGS:
        raise ToolError("bad_args", f"top_n must be a whole number from 1 to {MAX_FINDINGS}")
    _panel(ctx, "warnings")
    res = ctx.dash.warnings_result(ctx.user, ctx.tc, ctx.st)
    found = ctx.dash.present(res["warnings"], ctx.user, ctx.tc, ctx.lang)
    c = Composer(ctx.dash, ctx.user, ctx.tc, ctx.st, ctx.lang)
    asof = res.get("asof") or ctx.st.get("max_date") or ""
    facts = [_finding_fact(ctx, c, f, str(asof)) for f in found[:top_n]]
    if not facts:
        facts = [ctx.fact(ctx.tx("tl_no_warnings"), 0, "int", str(asof), text=ctx.tx("tl_no_warnings"), checked=res.get("checked"))]
    elif len(found) > top_n:
        facts.append(ctx.fact(ctx.tx("tl_more_warnings", n=len(found) - top_n), len(found) - top_n, "int", str(asof),
                              text=ctx.tx("tl_more_warnings", n=len(found) - top_n)))
    return {"ok": True, "tool": "warnings", "facts": facts, "tables": [], "charts": [],
            "meta": ctx.meta(asof=str(asof), active=len(found), checked=res.get("checked"))}


def findings(ctx: ToolContext, period=None, against: str = "previous", side: str = "both", top_n: int = 5) -> dict:
    """What went well and what went badly in a period compared with the one before (the business evaluation)."""
    if against not in ("previous", "yoy"):
        raise ToolError("bad_args", "against must be 'previous' or 'yoy'")
    if side not in ("both", "positive", "negative"):
        raise ToolError("bad_args", "side must be both, positive or negative")
    if not isinstance(top_n, int) or isinstance(top_n, bool) or not 1 <= top_n <= MAX_FINDINGS:
        raise ToolError("bad_args", f"top_n must be a whole number from 1 to {MAX_FINDINGS}")
    _panel(ctx, "evaluation")
    req = ctx.period(period, against)
    res = ctx.dash.evaluation_result(ctx.user, ctx.tc, ctx.st, req)
    c = Composer(ctx.dash, ctx.user, ctx.tc, ctx.st, ctx.lang)
    ptxt, facts = ctx.period_text(req), []
    for s in ("positive", "negative"):
        if side not in ("both", s):
            continue
        for f in ctx.dash.present(res[s], ctx.user, ctx.tc, ctx.lang)[:top_n]:
            facts.append(_finding_fact(ctx, c, f, ptxt, s))
    if not facts:
        raise ToolError("no_data", "Nothing notable was found for that period.")
    return {"ok": True, "tool": "findings", "facts": facts, "tables": [], "charts": [], "meta": ctx.meta(req)}


def recommended_actions(ctx: ToolContext, period=None, against: str = "previous", top_n: int = 3) -> dict:
    """Concrete next steps from the rules (each one carries the finding it comes from)."""
    if against not in ("previous", "yoy"):
        raise ToolError("bad_args", "against must be 'previous' or 'yoy'")
    if not isinstance(top_n, int) or isinstance(top_n, bool) or not 1 <= top_n <= 6:
        raise ToolError("bad_args", "top_n must be a whole number from 1 to 6")
    req = ctx.period(period, against)
    c = Composer(ctx.dash, ctx.user, ctx.tc, ctx.st, ctx.lang)
    acts = quick_wins(c, ctx.dash, ctx.user, ctx.tc, ctx.st, req, {})[:top_n]
    if not acts:
        raise ToolError("no_data", "No action is suggested for that period.")
    ptxt = ctx.period_text(req)
    facts = [ctx.fact(f"{a['priority_text']}", None, None, ptxt,
                      text=f"{a['text']} ({a['priority_text']}, {a['timeline_text']}). {ctx.tx('tl_because')}: {a['because']}",
                      priority=a["priority"], timeline_days=a["timeline_days"], because=a["because"],
                      action=a["text"], priority_text=a["priority_text"], timeline_text=a["timeline_text"],
                      source=a["source"].get("rule") or a["source"].get("kpi")) for a in acts]
    return {"ok": True, "tool": "recommended_actions", "facts": facts, "tables": [], "charts": [], "meta": ctx.meta(req)}
