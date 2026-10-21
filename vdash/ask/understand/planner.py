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

from ...reports.story_rules import load_story_config  # noqa: E402
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
    same = [k for k in same if not k.sliced or kpi.sliced] or same         # a split by category is not the twin of the total revenue
    if same:
        same.sort(key=lambda k: (not k.available, len(k.template_kpis), k.id))
        if len(same) > 1:           # several twins share the template KPI (aov -> aov_trend or orders_trend?): the closest title wins
            ranked = kpi_match.KpiIndex(same).score(" ".join(kpi.titles.values()))
            if ranked and (len(ranked) == 1 or ranked[0][0] > ranked[1][0]) and ranked[0][1].available >= same[0].available:
                return ranked[0][1]
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
    if tool.name == "custom_query" and "entity" in clean:
        from ..tools import query_tool as qt
        from ..tools.common import ToolError
        try:
            qt.check_spec({k: v for k, v in clean.items() if k != "period"}, pctx.query_model)
        except ToolError as exc:
            allowed = exc.extra.get("allowed")
            errors.append(f"custom_query: {exc.message}" + (f" (allowed: {', '.join(map(str, allowed))})" if allowed else ""))
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
        if step["tool"] == "kpi_breakdown" and k.viz == "timeseries" and "kpi_trend" in pctx.tool_by_name:
            # "which day was highest": the model asked a split of a KPI that only has a time axis; the sibling split (revenue by
            # category) is another question altogether - the time series itself is what was meant
            notes.append(f"tool_fixed:kpi_breakdown->kpi_trend:{k.id}")
            step["tool"] = "kpi_trend"
            allowed = pctx.tool_by_name["kpi_trend"].params
            step["args"] = {a: v for a, v in step["args"].items() if a in allowed}
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
        if asks_change and len(periods) < 2 and s["tool"] == "kpi_value" and a.get("compare") in (None, "none") \
                and a.get("kpi_id") != "weekend_premium_pct":     # the premium already IS a comparison (weekend vs weekday days)
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
_EXT_WEATHER_Q = re.compile(r"\b(weather|rain\w*|temperature|hot days?|cold days?|heat|thoi tiet|mua|nhiet do|wetter|regen\w*|temperatur|hitze)\b")
_EXT_EVENT_Q = re.compile(r"\b(holidays?|festivals?|public holidays?|school (break|holiday)s?|ngay le|le hoi|nghi he|feiertag\w*|schulferien|ferien)\b")
_EXT_ECON_Q = re.compile(r"\b(fuel|petrol|gasoline|gas prices?|diesel|exchange rates?|currency|inflation|food prices?|oil prices?|xang|ty gia|lam phat|gia thuc pham|gia dau|kraftstoff|benzin|sprit|wechselkurs|lebensmittelpreis\w*|oelpreis\w*)\b")


def _ext_args(q: str) -> dict:
    """One outside-factor group when the question names exactly one; otherwise all of them."""
    hit = [g for g, rx in (("weather", _EXT_WEATHER_Q), ("events", _EXT_EVENT_Q), ("economy", _EXT_ECON_Q)) if rx.search(q)]
    return {"factor": hit[0]} if len(hit) == 1 else {}


_Q = {
    "why": re.compile(r"\bwhy\b|\breason|\bcause|\bdue to\b|\bcaused by\b|\bvi sao\b|\btai sao\b|\bnguyen nhan\b|\bdo dau\b|\bwarum\b|\bwieso\b|\bweshalb\b|\bgrund\b|\bursache"),
    "movers": re.compile(r"\b(gained|lost|rose|fell|dropped|grew|increase[ds]?|decrease[ds]?)\b.*\b(most|biggest|largest)\b|\b(biggest|largest|most)\b.*\b(gain|loss|increase|decrease|drop|rise|fall)|\bmat doanh thu\b|\bmat nhieu\b.{0,25}\b(nhat|hon)\b|\b(tang|giam|mat)\b.{0,25}\bnhieu nhat\b|\b(tang|giam)\b.{0,25}\bmanh nhat\b|\bam meisten (gestiegen|gefallen|verloren|gewonnen)\b|\bverloren\b|\bgewonnen\b|\bmost (revenue )?(lost|gained)\b|\blost the most\b|\bgained the most\b"),
    "warn": re.compile(r"\bworry\b|\bwarning|\balert|\bneeds? attention\b|\brisks?\b|\bgoing wrong\b|\blo ngai\b|\bcanh bao\b|\bcan chu y\b|\bsorge\w*|\bwarnung|\bachten\b|\bprobleme\b"),
    "actions": re.compile(r"\bwhat (should|can|do) (i|we)\b|\bshould (i|we) (do|act|change|focus)\b|\bto[- ]do\b|\bnext steps?\b|\brecommend|\bnen lam gi\b|\blam gi\b|\bnen (xu ly|lam)\b|\bgoi y\b|\bwas (soll|sollte|kann) (ich|man)\b|\bmassnahme|\bempfehl|\bhandeln\b"),
    "findings": re.compile(r"\bhow did we do\b|\bhow are we doing\b|\bwent (well|badly)\b|\bwhat (is|was) going (well|badly)\b|\bsummary\b|\boverview\b|\bperformance\b|\bkinh doanh the nao\b|\btinh hinh\b|\btong quan\b|\btom tat\b|\bwie lief\b|\bwie laufen\b|\bubersicht\b|\bzusammenfassung\b"),
    "forecast": re.compile(r"\bforecast|\bwill (we )?sell\b|\bnext (week|7 days|month)\b|\bexpected\b|\bdu bao\b|\btuan toi\b|\b7 ngay toi\b|\bse ban\b|\bprognose\b|\bnachste woche\b|\bkommende|\bwerden .* verkauft\b|\bwird .* verkauf"),
    "compare": re.compile(r"\bcompar|\bversus\b|\bvs\b|\bso voi\b|\bso sanh\b|\bvergleich|\bgegenuber\b|\bthan\b|\bchange[ds]?\b|\bthay doi\b|\bandert\b|\bveranderung\b"),
    "trend": re.compile(r"\bover time\b|\bby (day|week|month)\b|\bper (day|week|month)\b|\bmonthly\b|\bdaily\b|\bweekly\b|\btrend\b|\btheo (ngay|tuan|thang)\b|\bxu huong\b|\bpro (tag|woche|monat)\b|\bmonatlich\b|\btaglich\b|\bwochentlich\b|\bverlauf\b|\b(highest|lowest|best|worst) day\b|\bwhich day\b|\bngay nao\b|\bwelche[mnr]? tag\b|\bunusual(ly)? (high|low) days?\b"),
    "baseline": re.compile(r"\b(normal|typical|usual|average|ordinary)\s+(week|day|month|period)\b|\b(tuan|ngay|thang)\s+(binh thuong|thong thuong|trung binh)\b|\b(normalen?|ublichen?|durchschnittlichen?)\s+(woche|tag|monat)\b"),
    "segment": re.compile(r"\bsegment|\bphan khuc\b|\bsegmente?\b|\bchurn|\blost customers?\b|\bat[- ]risk\b|\bnguy co\b|\babwanderung"),
}
# any wording that asks for a comparison, a change or a judgement (folded text, en / vi / de); a plain "what was X" has none
_CMP_ANY = re.compile(r"compar|\bvs\b|versus|\bthan\b|before|previous|prior|\blast\b|\bago\b|chang|grow|trend|\bup\b|\bdown\b|higher|lower|better|worse|"
                      r"\bmore\b|\bless\b|rise|rose|fall|fell|drop|declin|improv|increas|decreas|\bwhy\b|going|doing|\bgood\b|\bbad\b|"
                      r"vergleich|gegenuber|vorher|\bvor|zuvor|davor|mehr|weniger|hoher|niedriger|besser|schlechter|steig|sink|wachs|anderung|"
                      r"\bso voi\b|truoc|\bhon\b|\bkem\b|\btang\b|\bgiam\b|\bcao\b|\bthap\b|nhieu|\bit\b|thay doi|bien dong|\btot\b|\bxau\b|\bnam ngoai\b|\bngay qua\b|"
                      r"\bletzte|\bvergangen|\bthang qua\b")
_TOPN = re.compile(r"\btop\s*(\d{1,2})\b|\b(\d{1,2})\s*(?:best|top|most|san pham|khach|products?|customers?|items?|produkte?|kunden)\b")
_GRAIN = [("day", re.compile(r"\bby day\b|\bdaily\b|\bper day\b|\beach day\b|\bwhich day\b|\b(best|worst|highest|lowest) day\b|\btheo ngay\b|\bngay nao\b|\bpro tag\b|\btaglich\b|\bwelche[mnr]? tag\b")),
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

    if _Q["baseline"].search(q):                           # "last 7 days vs a normal week": is it unusual, and by how much
        head = (load_story_config().get("headline") or {}).get("kpi") or "net_revenue"
        mk = pctx.by_id.get(head) or _best_kpi(cands, pctx, ("metric",))
        if mk:
            return "answer", [{"tool": "warnings", "args": {}}, {"tool": "compare_periods", "args": {"kpi_id": mk.id}}]
    if "external_context" in pctx.tool_by_name and router.EXT_BIZ.search(q):    # "does rain affect my sales?" - outside factors
        return "answer", [{"tool": "external_context", "args": _ext_args(q)}]
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


# ===================================================================================================== plan normalising
# The same question must get the same tool in every language and on every run: these slips of the model (or of a
# recorded plan) are corrected by code after the plan is validated.
_DAYS_Q = re.compile(r"\b(best|worst|highest|lowest|top|biggest|weakest|strongest)\b.{0,40}\bdays\b|\bdays\b.{0,40}\b(best|worst|highest|lowest)\b|"
                     r"\bngay\b.{0,30}\b(tot|te|cao|thap|nhieu|it)\b.{0,12}\bnhat\b|\b(besten?|schlechtesten?|umsatzstarksten?|umsatzschwachsten?)\b.{0,30}\btage?\b|\btage?\b.{0,30}\b(hochst|niedrigst|best|schlechtest)")
_WHICH_DAY_Q = re.compile(r"\b(on )?which day\b|\bwhat day\b|\bngay nao\b|\b(an )?welche[mnr]? tag\b")
_TIMEOFDAY_Q = re.compile(r"time of day|daypart|morning|afternoon|evening|lunch|\bhours?\b|khung gio|buoi|gio nao|tageszeit|vormittag|nachmittag|abend|mittag|uhrzeit|stunde")
_FACTOR_Q = re.compile(r"\b(factor|factors|driver|drivers|yeu to|faktor\w*|einflussfaktor\w*)\b.{0,50}\b(hurt\w*|harm\w*|damag\w*|drag\w*|ton hai|giam|\w*schad\w*|\w*belast\w*|gedruckt|gedrueckt)\b|"
                       r"\b(hurt\w*|harm\w*|damag\w*|drag\w*|\w*schad\w*|\w*belast\w*)\b.{0,50}\b(factor|driver|faktor\w*)\b")
_GRAIN_Q = re.compile(r"\b(day|days|daily|per day|week|weeks|weekly|month|months|monthly|quarter|quarters|quarterly|year|yearly|annual\w*|hourly|hour)\b|"
                      r"\b(ngay|hang ngay|tuan|hang tuan|thang|hang thang|quy|hang quy|nam|hang nam|gio)\b|"
                      r"\b(tag\w*|taeglich\w*|taglich\w*|woche\w*|wochentlich\w*|monat\w*|quartal\w*|jahr\w*|stunde\w*)\b")
_UNITS_Q = re.compile(r"\b(units?|quantit\w*|pieces?|items sold|sold|volume)\b|\b(so luong|don vi|ban duoc bao nhieu)\b|\b(menge\w*|stuck\w*|stueck\w*|einheit\w*|anzahl|verkauft\w*)\b")
_WHICH_Q = re.compile(r"\b(which|welche\w*)\b|\bnao\b")
# a customer group named in the question (RFM segment words in the launch languages) -> the segment value the tool matches
_SEGMENT_WORDS = [(re.compile(r"\b(champions?|vo dich|meister\w*)\b"), "Champion"),
                  (re.compile(r"\b(hibernating|ngu dong|inaktiv\w*|winterschlaf\w*)\b"), "Hibernating"),
                  (re.compile(r"\b(potential loyalists?|tiem nang trung thanh|potenziell\w* treue\w*)\b"), "Potential"),
                  (re.compile(r"\b(loyal customers?|khach hang trung thanh|treue kunden)\b"), "Loyal"),
                  (re.compile(r"\b(can.?t lose|khong the de mat|nicht verlieren)\b"), "Can't lose"),
                  (re.compile(r"\b(need(s)? attention|can quan tam|aufmerksamkeit)\b"), "Need attention"),
                  (re.compile(r"\b(about to sleep|sap ngu dong|kurz vor dem einschlafen)\b"), "About to sleep"),
                  (re.compile(r"\b(at risk|co nguy co mat|gefahrdet\w*)\b"), "At risk")]
_LOST_Q = re.compile(r"\b(lost customers?|customers? (are |is |were |have been |got )?lost)\b|\bda (roi bo|mat)\b|\bkhach hang (bi )?mat\b|"
                     r"\bverlorene? kunden\b|\bkunden (sind |waren )?(verloren|abgewandert)\b|\babgewanderte kunden\b")
_BETTER_Q = re.compile(r"\bwhich (one |quarter |month |week |year |period )?(was|is|were) (better|higher|lower|worse|bigger|stronger|weaker)\b|"
                       r"\b(quy|thang|tuan|nam|ky) nao (tot|cao|thap|manh|yeu) hon\b|\bwelche[rsm]? (quartal|monat|woche|jahr|zeitraum)\b.{0,30}\b(besser|schlechter|hoher|niedriger|starker)\b")
_WEEKDAY_Q = re.compile(r"\bweekdays?\b|\bday of (the )?week\b|\bthu may\b|\bthu trong tuan\b|\bwochentag|\bnach wochentag")
_WEEKEND_CMP_Q = re.compile(r"\b(weekends?|cuoi tuan|wochenend\w*)\b.{0,60}\b(more|less|higher|lower|better|worse|nhieu hon|it hon|cao hon|thap hon|mehr|weniger|hoher|niedriger|besser|schlechter)\b|"
                            r"\b(more|less|higher|lower|nhieu hon|it hon|cao hon|thap hon|mehr|weniger|hoher|niedriger)\b.{0,60}\b(weekends?|cuoi tuan|wochenend\w*)\b")
_SUPERLATIVE_DAY_Q = re.compile(r"\b(which|what) day\b|\bngay nao\b|\bwelche[rn]? tag\b|\ban welchem tag\b|\b(best|worst|highest|lowest|busiest|slowest)( \w+)? days?\b")
_FEWER_ITEMS_Q = re.compile(r"\b(it|nhieu) san pham\b|\bban duoc (it|nhieu)\b")
_REVENUE_ANY_Q = re.compile(r"\b(revenue|sales|takings|turnover|income|umsatz|erlos\w*|doanh thu|doanh so)\b")
_PRODUCT_Q = re.compile(r"\b(products?|items?|skus?|san pham|produkte?\w*|artikel\w*)\b")
_CATEGORY_Q = re.compile(r"\b(categor\w*|danh muc|nhom hang|kategori\w*|warengruppe\w*)\b")
_MOVE_Q = re.compile(r"\b(los(e|es|ing|t)|gain(s|ed|ing)?|ris(e|es|ing)|rose|fall(s|ing)?|fell|declin\w*|grow\w*|drop\w*|"
                     r"tang|giam|mat doanh|verlier\w*|gewinn\w*|steig\w*|sink\w*|fallen)\b")
_NOT_PRODUCT_SPLITS = {"revenue_by_brand", "revenue_by_category", "revenue_per_sku_by_category"}
_BRAND_Q = re.compile(r"\b(brands?|thuong hieu|marken?\w*)\b")
COHORT_LAG_KPIS = {"repeat_next_month_rate"}          # KPIs read against the month AFTER the period: the latest month has no follow-up yet
_SUPERLATIVE_Q = re.compile(r"^(which|what)\b.{0,60}\b(most|highest|best|biggest|largest|top|lowest|least|worst)\b|"
                            r"\bnao\b.{0,60}\bnhat\b|\bthu may\b.{0,60}\bnhat\b|^welche[rsmn]?\b.{0,60}\b(meisten|hochste\w*|beste\w*|groesste\w*|grosste\w*|niedrigste\w*|wenigsten|schlechteste\w*)\b")
_TOPCUST_Q = re.compile(r"\btop[- ]?\d*\s*(customers?|kunden)\b|\btop[- ]?kunden\b|\b(customers?|kunden)\b.{0,12}\btop\b|\bkhach hang (hang dau|top)\b|\btop \d* ?khach\b|"
                        r"\bwho are (our|my|the) (top|biggest|best) customers\b|\bwer sind (unsere|meine) (top|besten|groessten|grossten)[- ]?kunden\b|"
                        r"\bkhach hang (tot nhat|lon nhat|chi tieu nhieu nhat|mua nhieu nhat)\b|\b(besten|wichtigsten|groessten|grossten) kunden\b")


def normalize_steps(question: str, steps: list[dict], pctx: PlanningContext, pp: PeriodParse, cands, state: ConversationState,
                    notes: list[str]) -> list[dict]:
    q = fold(question)
    m = _TOPN.search(q)
    asked_n = int(next(g for g in m.groups() if g)) if m else None
    out = []
    for st in steps:
        st = {"tool": st["tool"], "args": dict(st["args"])}
        a, kid = st["args"], st["args"].get("kpi_id", "")
        # best / worst DAYS are a trend over days, not the weekday breakdown
        if st["tool"] == "kpi_breakdown" and (kid.endswith("_by_dow") and _DAYS_Q.search(q) and not _WEEKDAY_Q.search(q)
                                              or kid.endswith("_by_daypart") and (_DAYS_Q.search(q) or _WHICH_DAY_Q.search(q))
                                              and not _TIMEOFDAY_Q.search(q) and not _WEEKDAY_Q.search(q)):
            k = pctx.by_id.get(kid)
            tw = pctx.by_id.get("revenue_trend") if kid.startswith("revenue") and "revenue_trend" in pctx.by_id else (sibling_kpi(pctx, k, ("timeseries",)) if k else None)
            if tw and pctx.role_ok(tw.min_role):
                st = {"tool": "kpi_trend", "args": {"kpi_id": tw.id, "grain": "day", **({"period": a["period"]} if "period" in a else {})}}
                notes.append(f"days_not_weekdays:{kid}->{tw.id}")
        # the premium asked about as a yes / no ("do we earn more on weekends?") is a level, not a change against the previous window
        elif st["tool"] == "kpi_value" and kid == "weekend_premium_pct" and a.get("compare") in ("previous", "yoy") and not pp.compare \
                and _WEEKEND_CMP_Q.search(q):
            a.pop("compare")
            notes.append("weekend_premium_level")
        # "do we earn more on weekends?": the per-day premium, not weekday vs weekend TOTALS (five days against two always looks like weekdays win)
        elif st["tool"] == "kpi_breakdown" and kid == "weekend_vs_weekday" and _WEEKEND_CMP_Q.search(q) and "weekend_premium_pct" in pctx.by_id \
                and pctx.role_ok(pctx.by_id["weekend_premium_pct"].min_role) and "kpi_value" in pctx.tool_by_name:
            st = {"tool": "kpi_value", "args": {"kpi_id": "weekend_premium_pct", **({"period": a["period"]} if "period" in a else {})}}
            notes.append("weekend_premium_per_day")
        # "which factor hurt revenue the most?": the factor breakdown of the change, in every language (the model sometimes picks the findings list)
        elif st["tool"] == "findings" and _FACTOR_Q.search(q) and "explain_change" in pctx.tool_by_name:
            st = {"tool": "explain_change", "args": {**{k: v for k, v in a.items() if k in ("period", "period_b")}, "against": a.get("against") or "previous"}}
            notes.append("factor_question_explain_change")
        # "why are we selling fewer units?": the bridge explains revenue; a question about units gets the units' own change (and says so when
        # the premise is wrong - units may have risen)
        elif st["tool"] == "explain_change" and (_UNITS_Q.search(q) or _FEWER_ITEMS_Q.search(q)) and not _REVENUE_ANY_Q.search(q) \
                and "units_sold" in pctx.by_id and pctx.role_ok(pctx.by_id["units_sold"].min_role) and "kpi_value" in pctx.tool_by_name:
            st = {"tool": "kpi_value", "args": {"kpi_id": "units_sold", "compare": a.get("against") or "previous",
                                                **({"period": a["period"]} if "period" in a else {})}}
            notes.append("explain_units_not_revenue")
        # "which time of day explains the change?": the movers of the daypart breakdown, not the whole bridge
        elif st["tool"] == "explain_change" and _TIMEOFDAY_Q.search(q) and _WHICH_Q.search(q) and "revenue_by_daypart" in pctx.by_id \
                and "top_movers" in pctx.tool_by_name:
            st = {"tool": "top_movers", "args": {"kpi_id": "revenue_by_daypart", "against": a.get("against") or "previous",
                                                 **{k: v for k, v in a.items() if k in ("period", "period_b")}}}
            notes.append("daypart_change_movers")
        # "top customers" when the business has no customer names: the customer segments (decision of 19 Oct 2026), in every language
        elif _TOPCUST_Q.search(q) and "top_customers" in pctx.by_id and not pctx.by_id["top_customers"].available \
                and "customer_segments" in pctx.tool_by_name and (st["tool"] == "customer_segments" and a.get("top") or kid == "top_customers"):
            st = {"tool": "customer_segments", "args": {"top": True, "top_n": asked_n or a.get("top_n") or 10}}
            notes.append("top_customers_segments_no_names")
        # "top customers": one answer in every language (the period's top spenders), not a segment list in one of them
        elif _TOPCUST_Q.search(q) and "top_customers" in pctx.by_id and st["tool"] in ("customer_segments", "kpi_breakdown") \
                and (st["tool"] == "customer_segments" and a.get("top") or kid == "top_customers"):
            st = {"tool": "kpi_breakdown", "args": {"kpi_id": "top_customers", "top_n": asked_n or a.get("top_n") or 10,
                                                    **({"period": a["period"]} if "period" in a else {})}}
            if st != {"tool": steps[0]["tool"], "args": steps[0]["args"]}:
                notes.append("top_customers_canonical")
        # "which X gained / lost the most": a change between two periods, so top_movers - a plain breakdown shows who is biggest, not who moved
        elif st["tool"] == "kpi_breakdown" and _Q["movers"].search(q) and "top_movers" in pctx.tool_by_name and pctx.by_id.get(kid) \
                and pctx.by_id[kid].viz in pctx.tool_by_name["top_movers"].viz:
            st = {"tool": "top_movers", "args": {"kpi_id": kid, **({"period": a["period"]} if "period" in a else {}),
                                                 **({"top_n": asked_n} if asked_n and "top_n" in pctx.tool_by_name["top_movers"].params else {})}}
            notes.append(f"baseline_plan_movers:{kid}")
        # "how many customers are lost": the Lost segment in every language, not the whole overview (which opens with the risk bands)
        elif st["tool"] == "customer_segments" and _LOST_Q.search(q) and not (a.get("segment") or a.get("churn_band") or a.get("top")):
            st = {"tool": "customer_segments", "args": {"segment": "Lost"}}
            notes.append("lost_customers_segment")
        # "how many champion customers": the named segment in every language (one language got the whole overview and a wrong lead figure)
        elif st["tool"] == "customer_segments" and not (a.get("segment") or a.get("churn_band") or a.get("top")) \
                and any(rx.search(q) for rx, _ in _SEGMENT_WORDS):
            seg = next(v for rx, v in _SEGMENT_WORDS if rx.search(q))
            st = {"tool": "customer_segments", "args": {"segment": seg}}
            notes.append(f"named_segment:{seg}")
        # "which quarter was better?" after two turns about the same KPI: compare exactly those two periods, not "the last one against the one before it"
        elif st["tool"] in ("compare_periods", "kpi_value") and _BETTER_Q.search(q) and not pp.periods and kid and _two_turn_periods(state, kid):
            pa, pb = _two_turn_periods(state, kid)
            st = {"tool": "compare_periods", "args": {"kpi_id": kid, "period_a": pa, "period_b": pb}}
            notes.append("compare_last_two_turns")
        # a plain "what was X this period" is a value; a comparison the model added (in one language only) makes a verdict nobody asked for
        if len(steps) == 1 and st["tool"] == "kpi_value" and a.get("compare") == "previous" and not pp.compare and len(pp.periods) < 2 and not _CMP_ANY.search(q):
            a.pop("compare")
            notes.append("compare_removed_not_asked")
        # the model wrote the same dates twice ("the period against itself": a change of 0)
        if st["tool"] in ("explain_change", "top_movers", "compare_periods"):
            same = a.get("period_b") and a["period_b"] in (a.get("period"), a.get("period_a"))
            if same:
                a.pop("period_b")
                notes.append("period_b_same_as_period")
        # an unrequested tiny list ("top 2") hides the answer: show at least three
        if "top_n" in pctx.tool_by_name[st["tool"]].params and asked_n is None and isinstance(a.get("top_n"), int) and a["top_n"] < 3:
            st["args"]["top_n"] = 3
            notes.append("top_n_floor")
        # the model adds a list limit in one language and not in another: unless the question names a number, every language gets the tool's own list
        if st["tool"] == "kpi_breakdown" and asked_n is None and "top_n" in st["args"]:
            st["args"].pop("top_n")
            notes.append("top_n_dropped")
        # "over time" without a day / week / month word: the tool picks the grain (the model chose months in one language, days in another)
        if st["tool"] == "kpi_trend" and st["args"].get("grain") not in (None, "auto") and not _GRAIN_Q.search(q):
            st["args"]["grain"] = "auto"
            notes.append("grain_auto")
        # "which products rose or fell": revenue, unless the question is about units
        if st["tool"] == "top_movers" and st["args"].get("kpi_id") == "top_products_units" and not _UNITS_Q.search(q) \
                and "top_products_revenue" in pctx.by_id and pctx.role_ok(pctx.by_id["top_products_revenue"].min_role):
            st["args"]["kpi_id"] = "top_products_revenue"
            notes.append("movers_revenue_not_units")
        # "which products gained / lost": products, not the brand split the model sometimes picks in one language only
        if st["tool"] == "top_movers" and st["args"].get("kpi_id") in _NOT_PRODUCT_SPLITS and _PRODUCT_Q.search(q) \
                and not _BRAND_Q.search(q) and not _CATEGORY_Q.search(q) \
                and "top_products_revenue" in pctx.by_id and pctx.role_ok(pctx.by_id["top_products_revenue"].min_role):
            st["args"]["kpi_id"] = "top_products_revenue"
            notes.append("movers_products_not_brands")
        # "which products are losing / gaining revenue" asked as a brand or category breakdown: the products' own movers
        if st["tool"] == "kpi_breakdown" and st["args"].get("kpi_id") in _NOT_PRODUCT_SPLITS and _PRODUCT_Q.search(q) \
                and _MOVE_Q.search(q) and not _BRAND_Q.search(q) and not _CATEGORY_Q.search(q) and "top_movers" in pctx.tool_by_name \
                and "top_products_revenue" in pctx.by_id and pctx.role_ok(pctx.by_id["top_products_revenue"].min_role):
            st = {"tool": "top_movers", "args": {"kpi_id": "top_products_revenue", "against": st["args"].get("against") or "previous",
                                                 **{k: v for k, v in st["args"].items() if k in ("period", "period_b")}}}
            notes.append("breakdown_products_to_movers")
        # a cohort KPI needs the next month loaded: with no period named, use the last month whose follow-up month exists
        if st["tool"] == "kpi_value" and st["args"].get("kpi_id") in COHORT_LAG_KPIS and not st["args"].get("period") and not pp.periods:
            # the six cohort months up to the last one with a follow-up month: one month can hold too few new customers to answer
            first = pctx.anchor.replace(day=1)
            end = first - timedelta(days=1)
            start = end.replace(day=1)
            for _ in range(5):
                start = (start - timedelta(days=1)).replace(day=1)
            st["args"]["period"] = {"start": start.isoformat(), "end": end.isoformat()}
            notes.append("cohort_lag_period")
        out.append(st)
    # a summary (findings) already holds the change of the headline KPI against its own previous window; a second
    # comparison of the same KPI against a different window ("September vs all of August") would give two baselines in one answer
    if any(s["tool"] == "findings" for s in out) and any(s["tool"] == "compare_periods" for s in out):
        out = [s for s in out if s["tool"] != "compare_periods"]
        notes.append("compare_dropped_one_baseline")
    # "the last 7 days against a normal week": warnings + comparison, not a weekday split
    if _Q["baseline"].search(q) and not any(s["tool"] == "warnings" for s in out) \
            and any(s["tool"] == "kpi_breakdown" and (s["args"].get("kpi_id", "").endswith("_by_dow") or "weekend" in s["args"].get("kpi_id", "")) for s in out):
        b2, raw2 = rules_plan(question, pctx, pp, cands, state)
        if b2 == "answer" and raw2:
            out = raw2
            notes.append("baseline_plan")
    return out


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


def _query_digest(pctx: PlanningContext) -> list[dict]:
    from ..tools import query_tool as qt
    return qt.model_digest(pctx.query_model)


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
        **({"query_model": _query_digest(pctx)} if pctx.query_model and "custom_query" in pctx.tool_by_name else {}),
    }


def _rules_fallback_safe(question, pctx, pp, cands, state) -> bool:
    """After an AI failure: True when the rules planner's answer is one KPI that clearly is the question (a strong lead over the
    next candidate and no word of the question left unexplained by a segment / group name); else the caller asks to retry."""
    if not cands or cands[0][0] < 3.0:
        return False
    if len(cands) > 1 and cands[0][0] - cands[1][0] < 1.0:
        return False
    q = fold(question)
    if any(rx.search(q) for rx, _ in _SEGMENT_WORDS):
        return False
    return True


def _llm_json(llm, system: str, user: str, prompt: dict):
    res = llm.complete_json(system, user, max_tokens=int(prompt.get("max_tokens", 700)),
                            temperature=float(prompt.get("temperature", 0)),      # planning should repeat itself
                            **({"thinking": prompt["thinking"]} if prompt.get("thinking") else {}))
    return res.data, res.meta()


def _two_turn_periods(state, kid: str):
    """The periods of the last two turns that asked the same KPI over different periods -> (newer, older), or None."""
    seen = []
    for t in reversed(state.turns):
        if kid in (t.kpi_ids or []) and t.periods:
            p = t.periods[0]
            if p not in seen:
                seen.append(p)
        if len(seen) == 2:
            return seen[0], seen[1]
    return None


def _unavailable_substitute(raw_steps, ai_reason: str, cands, pctx):
    """The KPI the question is about when the business has no data for it but the plan rebuilds something else: a custom
    query for a cancelled-order count whose KPI the model itself calls unavailable, a season split rebuilt per date, a promo
    lift standing in for 'seasonal events'. Returns that KPI (the tool then says honestly 'no data yet'), else None."""
    steps = [s for s in raw_steps if isinstance(s, dict)]
    if not steps or not cands:
        return None
    summary_only = all(s.get("tool") == "findings" for s in steps)          # a general summary standing in for a specific question
    if not summary_only and not all(s.get("tool") == "custom_query" or (s.get("args") or {}).get("kpi_id") for s in steps):
        return None
    used = {(s.get("args") or {}).get("kpi_id") for s in steps} - {None}
    only_custom = all(s.get("tool") == "custom_query" for s in steps)
    low = (ai_reason or "").lower()
    if low and re.search(r"unavailable|not available|no data|closest|instead|proxy|approximat|stand.?in", low):
        for sc, k in cands:
            if k.available or not pctx.role_ok(k.min_role):
                continue
            names = {k.id.lower(), k.id.lower().replace("_", " "), str((k.titles or {}).get("en") or "").lower()} - {""}
            if any(n in low for n in names) and k.id not in used:
                return k
    sc, top = cands[0]
    if not top.available and sc >= (2.5 if summary_only else 2.0) and pctx.role_ok(top.min_role) and top.id not in used \
            and not (used & {k.id for _, k in cands[:8]}):
        return top
    return None


def _plain_aggregate(args) -> bool:
    """A bare total / average / count / min / max with no filter, split or row list: the shape in which a custom query
    is only a stand-in for a KPI. A median, a list, a filter or a split is something no KPI does."""
    a = args if isinstance(args, dict) else {}
    return a.get("agg") in ("sum", "avg", "count", "count_distinct", "min", "max") and not (a.get("filters") or a.get("group_by")
                                                                          or a.get("columns") or a.get("order_by"))


_REVENUE_WORD = re.compile(r"\b(revenue|sales|takings|turnover|umsatz|doanh thu)\b", re.I)
_SINGLE_ORDER_WORD = re.compile(r"\b(single|biggest|largest|highest order|one order|order value|bill|receipt|basket|bestellung|einzel|gr[oö](ß|ss)te|"
                                r"đơn|don hang|hoa don|hóa đơn)", re.I)


def _is_amount(args, pctx) -> bool:
    """The measure is a money column (an order total, a line amount): summing it per group gives a revenue."""
    col = ((pctx.query_model.get(args.get("entity")) or {}).get("columns") or {}).get(args.get("measure")) or {}
    return col.get("fmt") == "currency"


def _plain_ranking(args) -> bool:
    """One total / count per value of one column, no filter and no row list: the shape of a KPI breakdown chart."""
    a = args if isinstance(args, dict) else {}
    gb = a.get("group_by")
    return a.get("agg") in ("sum", "count", "avg") and bool(gb) and (isinstance(gb, str) or len(gb) == 1) \
        and not (a.get("filters") or a.get("columns"))


def _groups_by_what_the_kpi_ranks(args, kpi, pctx) -> bool:
    """The custom query splits by a column (its label, 'Store'); the KPI is the same split when its title says so
    ('Waste cost by store'). A chart that merely scores high on other words ('sell' -> revenue by employee) is not."""
    gb = args.get("group_by")
    gb = gb if isinstance(gb, str) else (gb or [None])[0]
    col = ((pctx.query_model.get(args.get("entity")) or {}).get("columns") or {}).get(gb) or {}
    label = str(col.get("label") or "").lower()
    title = str((kpi.titles or {}).get("en") or "").lower()
    words = {w for w in re.findall(r"[a-z]{3,}", label) if w not in ("name", "the")}
    return bool(words) and any(w in title or w.rstrip("s") in title for w in words)


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
            try:
                data, ai_meta = _llm_json(llm, system, user, prompt)
            except Exception:          # noqa: BLE001 - one retry (a dropped connection is the usual cause)
                data, ai_meta = _llm_json(llm, system, user, prompt)
                out.notes.append("ai_retry_ok")
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
            # the rules matcher guesses (a champion-customers question became the active-customers count): a wrong figure is worse than
            # asking again, so only a question the rules plan can name exactly goes on; the rest is said plainly
            if not _rules_fallback_safe(question, pctx, pp, cands, state):
                out.behavior, out.reason = "clarify", "ai_unavailable"
                out.message = msg("unavailable_ai", lang)
                return out
    if raw_steps is None:
        behavior, raw_steps = rules_plan(question, pctx, pp, cands, state)
        source = source if source == "rules_fallback" else "rules"
    out.source, out.llm = source, ai_meta
    if ai_reason:
        out.notes.append(f"ai_reason:{ai_reason}")

    # the model said "refuse / no tool / which metric?" for a metric this business simply has no data for ("what is our
    # return rate"): every language gets the same honest answer from the tool ("no data yet, missing: ...") instead
    if behavior in ("refuse", "adhoc", "clarify") and cands and not str(out.reason).startswith("role:"):
        best = cands[0][0]
        sc, top = next(((x, k) for x, k in cands[:3] if not k.available and x >= max(1.5, 0.75 * best)), cands[0])
        if sc >= 1.5 and not top.available and pctx.role_ok(top.min_role) and "kpi_value" in pctx.tool_by_name \
                and top.viz == "metric":
            out.notes.append(f"unavailable_kpi_answer:{behavior}->{top.id}")
            behavior, raw_steps, source = "answer", [{"tool": "kpi_value", "args": {"kpi_id": top.id}}], source

    # custom_query is for calculations no KPI covers; it must not stand in for a KPI this business has no data for
    # (gross margin, return rate...): the honest "no data yet" from kpi_value wins when that KPI is the best match
    if behavior == "answer" and raw_steps and cands and "kpi_value" in pctx.tool_by_name \
            and len(raw_steps) == 1 and isinstance(raw_steps[0], dict) and raw_steps[0].get("tool") == "custom_query" \
            and _plain_aggregate(raw_steps[0].get("args")):
        sc, top = cands[0]
        if sc >= 1.5 and not top.available and pctx.role_ok(top.min_role) and top.viz == "metric":
            out.notes.append(f"unavailable_kpi_over_custom:{top.id}")
            raw_steps = [{"tool": "kpi_value", "args": {"kpi_id": top.id}}]
        elif sc >= 2.0 and top.available and top is cands[0][1] and pctx.role_ok(top.min_role) and top.viz == "metric" \
                and raw_steps[0].get("args", {}).get("agg") == "count_distinct":
            # "how many different products did we sell": a KPI that already counts exactly this beats a rebuilt count
            out.notes.append(f"kpi_over_custom_count:{top.id}")
            raw_steps = [{"tool": "kpi_value", "args": {"kpi_id": top.id}}]

    if behavior == "answer" and raw_steps and cands:
        miss = _unavailable_substitute(raw_steps, ai_reason, cands, pctx)
        tname = _tool_for_viz(miss.viz) if miss else None
        if miss and tname in pctx.tool_by_name:
            out.notes.append(f"unavailable_kpi_not_substituted:{miss.id}")
            raw_steps = [{"tool": tname, "args": {"kpi_id": miss.id}}]

    # "on which day was revenue highest": a revenue per day is the SUM of the day's orders; the model sometimes asks for the
    # MAX of one order per day instead and the answer names the day of the biggest single order
    if behavior == "answer" and raw_steps and "custom_query" in pctx.tool_by_name:
        for st in raw_steps:
            a = st.get("args") if isinstance(st, dict) and st.get("tool") == "custom_query" else None
            if isinstance(a, dict) and a.get("agg") in ("max", "min") and a.get("group_by") and _REVENUE_WORD.search(question) \
                    and not _SINGLE_ORDER_WORD.search(question) and _is_amount(a, pctx):
                out.notes.append(f"custom_{a['agg']}_to_sum")
                a["agg"] = "sum"

    # "on which day was revenue highest / lowest": the dashboard's own daily revenue trend, not a rebuilt per-day sum (the answer is then the
    # same in every language and shows the whole month)
    if behavior == "answer" and raw_steps and len(raw_steps) == 1 and isinstance(raw_steps[0], dict) and raw_steps[0].get("tool") == "custom_query" \
            and "kpi_trend" in pctx.tool_by_name and "revenue_trend" in pctx.by_id and pctx.by_id["revenue_trend"].available:
        a0 = raw_steps[0].get("args") or {}
        if a0.get("agg") == "sum" and a0.get("group_by") == "dt" and not a0.get("filters") and _REVENUE_WORD.search(question) \
                and not _SINGLE_ORDER_WORD.search(question) and _is_amount(a0, pctx) and _SUPERLATIVE_DAY_Q.search(fold(question)):
            out.notes.append("custom_day_ranking_to_revenue_trend")
            raw_steps = [{"tool": "kpi_trend", "args": {"kpi_id": "revenue_trend", "grain": "day",
                                                         **({"period": a0["period"]} if a0.get("period") else {})}}]

    # a plain "which X is highest" ranking the AI rebuilt as a custom query, when a chart of this business already ranks exactly
    # that (the question's own wording matches the KPI strongly): the KPI is the dashboard's number, and shows the whole ranking
    if behavior == "answer" and raw_steps and cands and "kpi_breakdown" in pctx.tool_by_name \
            and len(raw_steps) == 1 and isinstance(raw_steps[0], dict) and raw_steps[0].get("tool") == "custom_query" \
            and _plain_ranking(raw_steps[0].get("args")):
        sc, top = cands[0]
        if sc >= 2.5 and top.available and pctx.role_ok(top.min_role) and top.viz in ("hbar", "bar", "donut") \
                and _groups_by_what_the_kpi_ranks(raw_steps[0]["args"], top, pctx):
            out.notes.append(f"kpi_over_custom_ranking:{top.id}")
            raw_steps = [{"tool": "kpi_breakdown", "args": {"kpi_id": top.id, **({"period": raw_steps[0]["args"]["period"]}
                                                                              if raw_steps[0]["args"].get("period") else {})}}]

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

    # a split the question names ("by channel") must not be swapped for another split just because the AI picked the
    # follow-up's KPI: when the question's own words point at a different breakdown KPI, use that (it may turn out
    # to be unavailable, and the answer then says so honestly instead of showing something else)
    for st in steps:
        if st["tool"] in ("kpi_breakdown", "top_movers") and any(n.startswith(f"kpi_fixed:") and n.endswith(f"->{st['args'].get('kpi_id')}") for n in notes):
            viz = pctx.tool_by_name[st["tool"]].viz
            c2 = [(sc, k) for sc, k in kpi_match.candidates(pp.residual or question, pctx, n=5, pin=[]) if k.viz in viz and pctx.role_ok(k.min_role)]
            if c2 and c2[0][0] >= 3.0 and c2[0][1].id != st["args"].get("kpi_id") and st["args"].get("kpi_id") not in [k.id for _, k in c2[:3]]:
                notes.append(f"kpi_dimension_guard:{st['args'].get('kpi_id')}->{c2[0][1].id}")
                st["args"]["kpi_id"] = c2[0][1].id

    steps = normalize_steps(question, steps, pctx, pp, cands, state, notes)
    steps, _, nn = validate_plan(steps, pctx, pp, state, max_steps, inherit) if any(n.startswith(("days_not", "baseline_plan", "top_customers")) for n in notes) else (steps, [], [])
    notes += nn

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
    cq = next(({k: v for k, v in s["args"].items() if k != "period"} for s in res.steps if s["tool"] == "custom_query"), None)
    state.add(Turn(question=question, kpi_ids=kpis, periods=per, tools=[s["tool"] for s in res.steps], grain=grain, top_n=top_n, query=cq))


def execute(ctx, res: PlanResult) -> list[dict]:
    """Run a validated plan through the Step 2 registry (which validates again). One result per step."""
    from ..tools import registry
    return [registry.call(ctx, s["tool"], s["args"]) for s in res.steps]
