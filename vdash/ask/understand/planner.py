"""Planner (talk-to-data Step 3): question -> a validated plan of Step 2 tool calls.

    res = plan_question("Số đơn hàng tháng 9 so với tháng 8", pctx, llm, state)
    res.behavior   answer | adhoc | refuse | clarify
    res.steps      [{"tool": "compare_periods", "args": {"kpi_id": "orders", "period_a": {...}, "period_b": {...}}}]

Order of work (cheapest and safest first):
  1. router            rules: injection, SQL, other businesses, contact dumps, actions, off-topic, no-context -> no AI call
  2. period resolver   rules: the dates are worked out here and the model may not change them
  3. KPI matcher       rules: a short candidate list for the prompt
  4. planner           AI (``llm`` given) or the rules planner below (AI off, over quota, or the AI failed)
  5. validator         every call checked against the tool schemas, the user's role and the KPI's chart type;
                       fixable slips are repaired in code, anything else goes back to the model once
Roles: a tool, KPI or personal-data field above the user's role turns the plan into a refusal *before any tool runs*;
customer- and staff-level questions need manager or above (decision of 14 Oct 2026).
Nothing here knows a tenant or an industry. The model never sees data, only titles and tool names.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from ...config import ROOT
from . import kpi_match, router
from .kpi_match import MIN_KPI_SCORE
from .context import KpiInfo, PlanningContext, ToolInfo
from .messages import msg
from .periods import PeriodParse, fold, parse_periods, previous_calendar_span, shift_range
from .state import ConversationState, Turn

log = logging.getLogger("vdash.ask.understand")

SUSPICIOUS_ARGS = {"tenant", "tenant_id", "user", "username", "role", "sql", "query", "table", "schema", "where"}
BEHAVIORS = {"answer", "adhoc", "refuse", "clarify"}
BREAKDOWN_VIZ = ("donut", "hbar", "bar", "table", "heatmap")


@dataclass
class PlanResult:
    behavior: str
    steps: list[dict] = field(default_factory=list)
    message: str = ""
    reason: str = ""
    source: str = "rules"            # rules | llm | llm_fixed | rules_fallback
    periods: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    llm: dict | None = None
    candidates: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"behavior": self.behavior, "steps": self.steps, "message": self.message, "reason": self.reason,
                "source": self.source, "periods": self.periods, "notes": self.notes, "llm": self.llm,
                "candidates": self.candidates}


def load_prompt() -> dict:
    return json.loads((ROOT / "config" / "ask_prompts" / "planner.json").read_text(encoding="utf-8"))


# ===================================================================================================== validation
def _tool_for_viz(viz: str) -> str:
    return "kpi_value" if viz == "metric" else "kpi_trend" if viz == "timeseries" else "kpi_breakdown"


def sibling_kpi(pctx: PlanningContext, kpi: KpiInfo, want_viz: tuple) -> KpiInfo | None:
    """The chart-type twin of a KPI (net_revenue -> revenue_trend): the KPI of the wanted type that implements the same
    template KPI; failing that, a very close title match."""
    pool = [k for k in pctx.kpis if k.viz in want_viz and k.id != kpi.id and pctx.role_ok(k.min_role)]
    same = [k for k in pool if set(k.template_kpis) & set(kpi.template_kpis)]
    if same:
        same.sort(key=lambda k: (not k.available, len(k.template_kpis), k.id))
        return same[0]
    if not pool:
        return None
    best = kpi_match.KpiIndex(pool).score(" ".join(kpi.titles.values()))
    return best[0][1] if best and best[0][0] >= 6.0 else None


def _num(v) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v == int(v):
        return int(v)
    if isinstance(v, str) and re.fullmatch(r"\d+", v.strip()):
        return int(v.strip())
    return None


def _valid_range(p, pctx: PlanningContext):
    if not isinstance(p, dict):
        return None
    try:
        s, e = date.fromisoformat(str(p.get("start"))), date.fromisoformat(str(p.get("end")))
    except ValueError:
        return None
    if s > e or e > pctx.anchor or (pctx.min_date and e < pctx.min_date - timedelta(days=366)):
        return None
    return {"start": s.isoformat(), "end": e.isoformat()}


def _period_arg(v, pctx):
    """A period argument from the model: {"start","end"} or a preset id; anything else is dropped."""
    if isinstance(v, str):
        return v if re.fullmatch(r"[a-z0-9_]+", v) else None
    return _valid_range(v, pctx)


def clean_step(raw, pctx: PlanningContext, errors: list[str], notes: list[str]) -> dict | None:
    if not isinstance(raw, dict) or not isinstance(raw.get("tool"), str):
        errors.append("each plan item needs a 'tool' name")
        return None
    tool = pctx.tool_by_name.get(raw["tool"])
    if tool is None:
        errors.append(f"unknown tool '{raw['tool']}'")
        return None
    args = dict(raw.get("args") or {}) if isinstance(raw.get("args"), dict) else {}
    # flat start/end (a common slip) -> period
    if "period" in tool.params and "period" not in args and "start" in args and "end" in args:
        args["period"] = {"start": args.pop("start"), "end": args.pop("end")}
    clean: dict = {}
    for k, v in args.items():
        if v is None or v == "":
            continue
        spec = tool.params.get(k)
        if spec is None:
            notes.append("suspicious_arg" if k.lower() in SUSPICIOUS_ARGS else f"dropped_arg:{k}")
            continue
        if k in ("period", "period_a", "period_b"):
            pv = _period_arg(v, pctx)
            if pv is None:
                notes.append(f"dropped_period:{k}")
                continue
            clean[k] = pv
        elif spec.get("type") == "integer":
            n = _num(v)
            if n is None:
                errors.append(f"{tool.name}.{k} must be a whole number")
                continue
            clean[k] = max(spec.get("minimum", n), min(spec.get("maximum", n), n))
        elif spec.get("type") == "string":
            if not isinstance(v, str):
                errors.append(f"{tool.name}.{k} must be text")
                continue
            if spec.get("enum") and v not in spec["enum"]:
                errors.append(f"{tool.name}.{k} must be one of {', '.join(spec['enum'])}")
                continue
            clean[k] = v
        elif spec.get("type") == "boolean":
            if not isinstance(v, bool):
                errors.append(f"{tool.name}.{k} must be true or false")
                continue
            clean[k] = v
        else:
            clean[k] = v
    for r in tool.required:
        if r not in clean:
            errors.append(f"{tool.name} needs '{r}'")
    return {"tool": tool.name, "args": clean}


def fit_kpi_to_tool(step: dict, pctx: PlanningContext, notes: list[str], errors: list[str]) -> dict:
    """KPI id must exist; its chart type must suit the tool. Slips are repaired here (sibling KPI or the right tool)."""
    tool = pctx.tool_by_name[step["tool"]]
    kid = step["args"].get("kpi_id")
    if kid is None:
        return step
    k = pctx.by_id.get(kid)
    if k is None:
        errors.append(f"unknown kpi_id '{kid}' (use one from the candidate list)")
        return step
    if tool.viz and k.viz not in tool.viz:
        if step["tool"] in ("explain_change", "findings", "warnings", "recommended_actions", "forecast", "customer_segments"):
            return step
        sib = sibling_kpi(pctx, k, tool.viz)
        if sib is not None and step["tool"] in ("kpi_trend", "kpi_breakdown", "top_movers"):
            notes.append(f"kpi_fixed:{k.id}->{sib.id}")
            step["args"]["kpi_id"] = sib.id
        elif step["tool"] in ("kpi_value", "compare_periods", "kpi_breakdown", "kpi_trend", "top_movers", "drill"):
            new = _tool_for_viz(k.viz)
            notes.append(f"tool_fixed:{step['tool']}->{new}")
            step["tool"] = new
            allowed = pctx.tool_by_name[new].params
            step["args"] = {a: v for a, v in step["args"].items() if a in allowed}
        else:
            errors.append(f"{step['tool']} cannot use kpi '{k.id}'")
    return step


def role_refusal(steps: list[dict], pctx: PlanningContext) -> str | None:
    for s in steps:
        tool = pctx.tool_by_name[s["tool"]]
        if not pctx.role_ok(tool.min_role):
            return f"tool:{tool.name}"
        k = pctx.by_id.get(s["args"].get("kpi_id", ""))
        if k and (not pctx.role_ok(k.min_role) or (k.personal and not pctx.role_ok("manager"))):
            return f"kpi:{k.id}"
    return None


_CHANGE_Q = re.compile(r"\b(increas|decreas|grow|grew|rise|rose|risen|fall|fell|drop|declin|improv|worse|go(ne)? up|go(ne)? down|up or down|higher|lower|more than|less than|better than)\w*"
                       r"|\b(tang|giam|cao hon|thap hon|nhieu hon|it hon)\b|\b(gestiegen|gesunken|gefallen|gewachsen|zugenommen|abgenommen|hoher|niedriger|mehr als|weniger als)\b")


def apply_periods(steps: list[dict], pp: PeriodParse, state: ConversationState, pctx: PlanningContext,
                  notes: list[str], inherit: bool = False) -> list[dict]:
    """The dates found in the question win over whatever the model wrote; a follow-up with only a shift ("the month
    before") moves the previous turn's period. Tools that compare get both periods."""
    periods = list(pp.periods)
    if not periods and pp.shift and state.last_period():
        lp = state.last_period()
        s, e = shift_range(date.fromisoformat(lp["start"]), date.fromisoformat(lp["end"]), pp.shift["unit"], pp.shift["n"])
        from .periods import Period
        periods = [Period(s, e, "shift", "before", pp.shift["unit"])]
        notes.append("period_from_previous_turn")
    if inherit and not periods and state.last_period():
        lp = state.last_period()
        from .periods import Period
        periods = [Period(date.fromisoformat(lp["start"]), date.fromisoformat(lp["end"]), "inherited", "rel", "")]
        notes.append("period_inherited")
    asks_change = bool(_CHANGE_Q.search(pp.residual or "")) and not pp.compare
    for s in steps:
        tool = pctx.tool_by_name[s["tool"]]
        a = s["args"]
        if asks_change and len(periods) < 2 and s["tool"] == "kpi_value" and a.get("compare") in (None, "none"):
            a["compare"] = "previous"            # "did revenue increase in Sep?" needs the change, not only the total
            notes.append("compare_added_for_change_question")
        # dates come from code only: a period the model wrote where the question names none (or where the tool's own
        # default applies, "what should I do this week") is dropped, so the tool uses its default window
        explicit_dates = bool(re.search(r"\d", pp.residual or ""))      # "between 5 Aug and 3 Sep": the model reads these
        if (not periods and not explicit_dates) or (periods and s["tool"] == "recommended_actions"
                                                     and periods[0].label in ("this_week", "this_month")):
            for k in ("period", "period_a", "period_b"):
                if k in a:
                    a.pop(k)
                    notes.append(f"dropped_model_{k}")
        # a whole calendar month / quarter / year compared with "the previous period": compare it with the whole
        # month / quarter / year before (the dashboard's 'previous' is the same number of days, e.g. Aug 2-31)
        if len(periods) == 1 and s["tool"] in ("kpi_value", "compare_periods") and "kpi_id" in a:
            want_prev = (a.get("compare") == "previous" or pp.compare == "previous") if s["tool"] == "kpi_value" \
                else (a.get("against", "previous") == "previous" and not a.get("period_b") or pp.compare == "previous")
            span = previous_calendar_span(periods[0]) if want_prev and pp.compare != "yoy" else None
            if span:
                s["tool"] = "compare_periods"
                tool = pctx.tool_by_name["compare_periods"]
                a.pop("period", None), a.pop("compare", None), a.pop("against", None)
                a["period_a"] = periods[0].spec()
                a["period_b"] = {"start": span[0].isoformat(), "end": span[1].isoformat()}
                notes.append("previous_whole_calendar_span")
                continue
        if len(periods) == 1 and s["tool"] in ("explain_change", "top_movers") and "period_b" in tool.params \
                and a.get("against", "previous") == "previous" and pp.compare != "yoy":
            span = previous_calendar_span(periods[0])
            if span:                                        # "why did revenue fall in September": against all of August
                a["period"] = periods[0].spec()
                a["period_b"] = {"start": span[0].isoformat(), "end": span[1].isoformat()}
                a.pop("against", None)
                notes.append("previous_whole_calendar_span")
                continue
        if len(periods) >= 2 and s["tool"] in ("kpi_value", "compare_periods") and "kpi_id" in a:
            if s["tool"] == "kpi_value":
                notes.append("tool_fixed:kpi_value->compare_periods")
                s["tool"] = "compare_periods"
                tool = pctx.tool_by_name["compare_periods"]
                a.pop("period", None), a.pop("compare", None)
            a["period_a"], a["period_b"] = periods[0].spec(), periods[1].spec()
            a.pop("against", None)
            continue
        if s["tool"] == "compare_periods":
            if periods:
                a["period_a"] = periods[0].spec()
                if pp.compare:
                    a["against"] = pp.compare
                    a.pop("period_b", None)
            continue
        if "period" in tool.params and periods and not (s["tool"] == "recommended_actions" and periods[0].label in ("this_week", "this_month")):
            a["period"] = periods[0].spec()
        if pp.compare:
            if "compare" in tool.params:
                a["compare"] = pp.compare
            elif "against" in tool.params:
                a["against"] = pp.compare
    return steps


def validate_plan(raw_steps, pctx: PlanningContext, pp: PeriodParse, state: ConversationState, max_steps: int = 3,
                  inherit: bool = False):
    """-> (steps, errors, notes). ``errors`` is empty when the plan can run."""
    errors: list[str] = []
    notes: list[str] = []
    steps = []
    if not isinstance(raw_steps, list):
        return [], ["'plan' must be a list"], notes
    for raw in raw_steps[:max_steps]:
        st = clean_step(raw, pctx, errors, notes)
        if st:
            st = fit_kpi_to_tool(st, pctx, notes, errors)
            steps.append(st)
    if len(raw_steps) > max_steps:
        notes.append("plan_truncated")
    steps = apply_periods(steps, pp, state, pctx, notes, inherit)
    seen, uniq = set(), []
    for s in steps:
        key = json.dumps(s, sort_keys=True)
        if key not in seen:
            seen.add(key)
            uniq.append(s)
    return uniq, errors, notes


# ===================================================================================================== rules planner
_Q = {
    "why": re.compile(r"\bwhy\b|\breason|\bcause|\bdue to\b|\bcaused by\b|\bvi sao\b|\btai sao\b|\bnguyen nhan\b|\bdo dau\b|\bwarum\b|\bwieso\b|\bweshalb\b|\bgrund\b|\bursache"),
    "movers": re.compile(r"\b(gained|lost|rose|fell|dropped|grew|increase[ds]?|decrease[ds]?)\b.*\b(most|biggest|largest)\b|\b(biggest|largest|most)\b.*\b(gain|loss|increase|decrease|drop|rise|fall)|\bmat doanh thu\b|\b(tang|giam|mat)\b.{0,25}\bnhieu nhat\b|\b(tang|giam)\b.{0,25}\bmanh nhat\b|\bam meisten (gestiegen|gefallen|verloren|gewonnen)\b|\bverloren\b|\bgewonnen\b|\bmost (revenue )?(lost|gained)\b|\blost the most\b|\bgained the most\b"),
    "warn": re.compile(r"\bworry\b|\bwarning|\balert|\bneeds? attention\b|\brisks?\b|\bgoing wrong\b|\blo ngai\b|\bcanh bao\b|\bcan chu y\b|\bsorge\w*|\bwarnung|\bachten\b|\bprobleme\b"),
    "actions": re.compile(r"\bwhat (should|can|do) (i|we)\b|\bshould (i|we) (do|act|change|focus)\b|\bto[- ]do\b|\bnext steps?\b|\brecommend|\bnen lam gi\b|\blam gi\b|\bnen (xu ly|lam)\b|\bgoi y\b|\bwas (soll|sollte|kann) (ich|man)\b|\bmassnahme|\bempfehl|\bhandeln\b"),
    "findings": re.compile(r"\bhow did we do\b|\bhow are we doing\b|\bwent (well|badly)\b|\bwhat (is|was) going (well|badly)\b|\bsummary\b|\boverview\b|\bperformance\b|\bkinh doanh the nao\b|\btinh hinh\b|\btong quan\b|\btom tat\b|\bwie lief\b|\bwie laufen\b|\bubersicht\b|\bzusammenfassung\b"),
    "forecast": re.compile(r"\bforecast|\bwill (we )?sell\b|\bnext (week|7 days|month)\b|\bexpected\b|\bdu bao\b|\btuan toi\b|\b7 ngay toi\b|\bse ban\b|\bprognose\b|\bnachste woche\b|\bkommende|\bwerden .* verkauft\b|\bwird .* verkauf"),
    "compare": re.compile(r"\bcompar|\bversus\b|\bvs\b|\bso voi\b|\bso sanh\b|\bvergleich|\bgegenuber\b|\bthan\b|\bchange[ds]?\b|\bthay doi\b|\bandert\b|\bveranderung\b"),
    "trend": re.compile(r"\bover time\b|\bby (day|week|month)\b|\bper (day|week|month)\b|\bmonthly\b|\bdaily\b|\bweekly\b|\btrend\b|\btheo (ngay|tuan|thang)\b|\bxu huong\b|\bpro (tag|woche|monat)\b|\bmonatlich\b|\btaglich\b|\bwochentlich\b|\bverlauf\b|\b(highest|lowest|best|worst) day\b|\bwhich day\b|\bngay nao\b|\bwelcher tag\b|\bunusual(ly)? (high|low) days?\b"),
    "segment": re.compile(r"\bsegment|\bphan khuc\b|\bsegmente?\b|\bchurn|\blost customers?\b|\bat[- ]risk\b|\bnguy co\b|\babwanderung"),
}
_TOPN = re.compile(r"\btop\s*(\d{1,2})\b|\b(\d{1,2})\s*(?:best|top|most|san pham|khach|products?|customers?|items?|produkte?|kunden)\b")
_GRAIN = [("day", re.compile(r"\bby day\b|\bdaily\b|\bper day\b|\beach day\b|\bwhich day\b|\b(best|worst|highest|lowest) day\b|\btheo ngay\b|\bngay nao\b|\bpro tag\b|\btaglich\b|\bwelcher tag\b")),
          ("week", re.compile(r"\bby week\b|\bweekly\b|\bper week\b|\btheo tuan\b|\bpro woche\b|\bwochentlich\b")),
          ("month", re.compile(r"\bby month\b|\bmonthly\b|\bper month\b|\beach month\b|\btheo thang\b|\bpro monat\b|\bmonatlich\b"))]



def _pick(cands, pctx: PlanningContext, text: str = "") -> KpiInfo | None:
    """Best KPI the user may see. A KPI above the user's role is taken as the intended one only when the question names
    it by a phrase at least as long as the best allowed KPI's ("top customers" asked by a viewer): then the role gate
    refuses instead of quietly answering a different question."""
    if not cands or cands[0][0] < MIN_KPI_SCORE:
        return None
    ok = [(s, k) for s, k in cands if pctx.role_ok(k.min_role) and not (k.personal and not pctx.role_ok("manager"))]
    if ok and ok[0][0] >= MIN_KPI_SCORE:
        top = cands[0][1]
        if top is not ok[0][1]:
            ix, qw = kpi_match.index_for(pctx), kpi_match.words(text)
            if ix.phrase_len(top, qw) > 0 and ix.phrase_len(top, qw) >= ix.phrase_len(ok[0][1], qw):
                return top
        return ok[0][1]
    return cands[0][1]


def _best_kpi(cands, pctx, want_viz: tuple | None = None, prefer_available=True) -> KpiInfo | None:
    pool = [k for _, k in cands if (want_viz is None or k.viz in want_viz)]
    for k in pool:
        if k.available or not prefer_available:
            return k
    return pool[0] if pool else None


def rules_plan(question: str, pctx: PlanningContext, pp: PeriodParse, cands, state: ConversationState) -> tuple[str, list[dict]]:
    """The plan without AI: intent words pick the tool, the best-matching KPI the subject. Used when the AI is off, out
    of quota or failed, and as the baseline the AI planner must beat. Returns (behavior, steps)."""
    q = fold(question)
    first = _pick(cands, pctx, pp.residual or question)
    follow = router.FOLLOW.search(q) is not None
    last = state.last()
    top_n = None
    m = _TOPN.search(q)
    if m:
        top_n = int(next(g for g in m.groups() if g))
    grain = next((g for g, rx in _GRAIN if rx.search(q)), None)

    def with_top(args):
        if top_n:
            args["top_n"] = top_n
        return args

    if _Q["warn"].search(q):
        return "answer", [{"tool": "warnings", "args": {}}]
    if _Q["actions"].search(q):
        return "answer", [{"tool": "recommended_actions", "args": {}}]
    if _Q["findings"].search(q) and not first:
        return "answer", [{"tool": "findings", "args": {}}]
    if _Q["why"].search(q):
        return "answer", [{"tool": "explain_change", "args": {}}]
    if _Q["forecast"].search(q):
        fk = _best_kpi([c for c in cands if c[1].id.startswith("forecast")], pctx)
        if fk and fk.id == "forecast_top_products" or "product" in q or "san pham" in q or "produkt" in q:
            return "answer", [{"tool": "forecast", "args": with_top({})}]
        return "answer", [{"tool": "forecast", "args": {}}]
    if follow and last and not first:                       # "and the month before?" / "what about September?"
        kid = last.kpi_ids[0] if last.kpi_ids else None
        tool = last.tools[0] if last.tools else "kpi_value"
        if kid and tool in pctx.tool_by_name:
            args = {"kpi_id": kid}
            if last.grain:
                args["grain"] = last.grain
            return "answer", [{"tool": tool, "args": args}]
    if first is None:
        return "clarify", []
    if _Q["segment"].search(q) and first.personal is None and first.id in ("rfm_segments", "high_risk_customers", "rfm_segment_value"):
        return "answer", [{"tool": "customer_segments", "args": with_top({})}]
    if _Q["movers"].search(q):
        k = _best_kpi(cands, pctx, BREAKDOWN_VIZ) or first
        return "answer", [{"tool": "top_movers", "args": {"kpi_id": k.id}}]
    two = len(pp.periods) >= 2
    if first.viz == "timeseries" and not _Q["trend"].search(q):
        twin = next((k for sc, k in cands if k.viz != "timeseries" and k.available and sc >= 0.8 * cands[0][0]), None)
        if twin:
            first = twin
    if first.viz == "metric" or (first.viz != "timeseries" and not (_Q["trend"].search(q) or first.viz in BREAKDOWN_VIZ)):
        if _Q["trend"].search(q):
            t = sibling_kpi(pctx, first, ("timeseries",))
            if t:
                a = {"kpi_id": t.id}
                if grain:
                    a["grain"] = grain
                return "answer", [{"tool": "kpi_trend", "args": a}]
        if two or pp.compare or (_Q["compare"].search(q) and first.viz == "metric"):
            return "answer", [{"tool": "compare_periods" if two else "kpi_value",
                               "args": {"kpi_id": first.id} if two else {"kpi_id": first.id, "compare": pp.compare or "previous"}}]
        return "answer", [{"tool": "kpi_value", "args": {"kpi_id": first.id}}]
    if first.viz == "timeseries" or (_Q["trend"].search(q) and first.viz not in BREAKDOWN_VIZ):
        a = {"kpi_id": first.id}
        if grain:
            a["grain"] = grain
        return "answer", [{"tool": "kpi_trend", "args": a}]
    return "answer", [{"tool": "kpi_breakdown", "args": with_top({"kpi_id": first.id})}]


# ===================================================================================================== LLM planner
def tool_digest(pctx: PlanningContext) -> list[dict]:
    out = []
    for t in pctx.tools:
        parts = []
        for name, spec in t.params.items():
            if "enum" in spec:
                s = "|".join(spec["enum"])
            elif spec.get("type") == "integer":
                s = f"int {spec.get('minimum', '')}-{spec.get('maximum', '')}"
            elif name.startswith("period"):
                s = '{"start","end"}'
            else:
                s = spec.get("type", "")
            parts.append(f"{name}{'*' if name in t.required else ''}: {s}")
        out.append({"name": t.name, "does": t.description, "args": parts,
                    **({"role": t.min_role} if t.min_role != "viewer" else {})})
    return out


def build_user_payload(question: str, pctx: PlanningContext, pp: PeriodParse, cands, state: ConversationState) -> dict:
    lang = pctx.lang
    return {
        "question": question,
        "language": lang,
        "latest_data_day": pctx.anchor.isoformat(),
        "user_role": pctx.role,
        "resolved_periods": [{"start": p.start.isoformat(), "end": p.end.isoformat()} for p in pp.periods],
        "compare_hint": pp.compare,
        "period_shift_from_previous_turn": pp.shift,
        "previous_turn": state.brief(),
        "candidates": [{"id": k.id, "title": k.title(lang) if lang != "en" else k.titles.get("en", k.id),
                        "en": k.titles.get("en"), "kind": k.viz, "available": k.available,
                        **({"restricted": True} if not pctx.role_ok(k.min_role) or (k.personal and not pctx.role_ok("manager")) else {})}
                       for _, k in cands],
        "tools": tool_digest(pctx),
    }


def _llm_json(llm, system: str, user: str, prompt: dict):
    res = llm.complete_json(system, user, max_tokens=int(prompt.get("max_tokens", 700)),
                            temperature=float(prompt.get("temperature", 0)),      # planning should repeat itself
                            **({"thinking": prompt["thinking"]} if prompt.get("thinking") else {}))
    return res.data, res.meta()


def plan_question(question: str, pctx: PlanningContext, llm=None, state: ConversationState | None = None,
                  prompt: dict | None = None, n_candidates: int = 20) -> PlanResult:
    state = state or ConversationState()
    prompt = prompt or load_prompt()
    max_steps = int(prompt.get("max_steps", 3))
    question = " ".join((question or "").split())
    lang = pctx.lang
    pp = parse_periods(question, pctx.anchor, pctx.min_date)
    pin = state.last_kpis()
    cands = kpi_match.candidates(pp.residual or question, pctx, n=n_candidates, pin=pin)
    best = cands[0][0] if cands else 0.0
    out = PlanResult("answer", periods=pp.specs(), candidates=[k.id for _, k in cands])

    # 1. rules that never need the AI
    r = router.route(question, pctx, has_state=not state.empty, best_kpi_score=best, has_period=bool(pp.periods or pp.shift))
    if r.kind != "pass":
        out.behavior, out.message, out.reason = r.kind, r.message, r.reason
        return out

    # a follow-up that names no period of its own keeps the previous turn's period
    inherit = not state.empty and not pp.periods and (router.FOLLOW.search(fold(question)) is not None or best < MIN_KPI_SCORE
                                                      or (len(question.split()) <= 6 and not (cands and cands[0][0] >= 6)))

    # 2. the plan: AI when given, else rules
    behavior, raw_steps, ai_meta, source, ai_reason = "answer", None, None, "rules", ""
    if llm is not None:
        try:
            system = prompt["system"]
            user = json.dumps(build_user_payload(question, pctx, pp, cands, state), ensure_ascii=False)
            data, ai_meta = _llm_json(llm, system, user, prompt)
            behavior = data.get("behavior") if isinstance(data, dict) else None
            if behavior not in BEHAVIORS:
                raise ValueError("behavior missing or unknown")
            raw_steps, source = data.get("plan") or [], "llm"
            ai_reason = str(data.get("reason") or "")[:200]
            if behavior == "clarify":
                out.message = str(data.get("clarify_question") or msg("clarify", lang))[:300]
        except Exception as exc:          # noqa: BLE001 - any AI failure falls back to the rules planner
            log.info("planner AI unavailable (%s): rules planner used", type(exc).__name__)
            out.notes.append(f"ai_failed:{getattr(exc, 'kind', type(exc).__name__)}")
            behavior, raw_steps, source = "answer", None, "rules_fallback"
    if raw_steps is None:
        behavior, raw_steps = rules_plan(question, pctx, pp, cands, state)
        source = source if source == "rules_fallback" else "rules"
    out.source, out.llm = source, ai_meta
    if ai_reason:
        out.notes.append(f"ai_reason:{ai_reason}")

    if behavior in ("refuse", "adhoc", "clarify"):
        out.behavior = behavior
        out.reason = out.reason or ai_reason[:60] or behavior
        out.message = out.message or msg({"refuse": "refuse_scope", "adhoc": "adhoc", "clarify": "clarify"}[behavior], lang)
        return out

    # 3. validate (and repair once through the model)
    steps, errors, notes = validate_plan(raw_steps, pctx, pp, state, max_steps, inherit)
    if errors and llm is not None and source == "llm":
        try:
            fix_user = json.dumps({**build_user_payload(question, pctx, pp, cands, state),
                                   "your_previous_plan": raw_steps, "problems": errors}, ensure_ascii=False)
            data, meta2 = _llm_json(llm, prompt["system"] + "\n\n" + prompt["fix_prompt"].format(errors="; ".join(errors)),
                                    fix_user, prompt)
            if ai_meta and meta2:
                for k in ("tokens_in", "tokens_out", "cost_usd", "ms"):
                    ai_meta[k] = (ai_meta.get(k) or 0) + (meta2.get(k) or 0)
            steps, errors, notes2 = validate_plan((data or {}).get("plan"), pctx, pp, state, max_steps, inherit)
            notes += notes2
            out.source = "llm_fixed"
        except Exception as exc:          # noqa: BLE001
            notes.append(f"fix_failed:{type(exc).__name__}")
    if errors or not steps:
        b2, raw2 = rules_plan(question, pctx, pp, cands, state)
        steps2, errors2, notes2 = validate_plan(raw2, pctx, pp, state, max_steps, inherit) if b2 == "answer" else ([], ["no rules plan"], [])
        if steps2 and not errors2:
            steps, notes, out.source = steps2, notes + notes2 + ["rules_fallback_after_invalid_plan"], "rules_fallback"
        else:
            out.behavior, out.reason = "clarify", "unplannable"
            out.message = msg("clarify", lang)
            out.notes += notes + errors
            return out

    # 4. role gate: before any tool runs
    why = role_refusal(steps, pctx)
    if why:
        out.behavior, out.reason, out.message, out.steps = "refuse", f"role:{why}", msg("refuse_role", lang), []
        out.notes += notes
        return out
    out.steps, out.notes = steps, out.notes + notes
    return out


# ===================================================================================================== state + run
def update_state(state: ConversationState, question: str, res: PlanResult) -> None:
    if res.behavior != "answer" or not res.steps:
        return
    kpis, per, grain, top_n = [], [], None, None
    for s in res.steps:
        a = s["args"]
        if a.get("kpi_id") and a["kpi_id"] not in kpis:
            kpis.append(a["kpi_id"])
        for pk in ("period", "period_a"):
            if isinstance(a.get(pk), dict) and not per:
                per = [a[pk]]
        grain = grain or a.get("grain")
        top_n = top_n or a.get("top_n")
    if not per:
        per = res.periods[:1]
    state.add(Turn(question=question, kpi_ids=kpis, periods=per, tools=[s["tool"] for s in res.steps], grain=grain, top_n=top_n))


def execute(ctx, res: PlanResult) -> list[dict]:
    """Run a validated plan through the Step 2 registry (which validates again). One result per step."""
    from ..tools import registry
    return [registry.call(ctx, s["tool"], s["args"]) for s in res.steps]
