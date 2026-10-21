"""Plan scoring against the talk-to-data benchmark (ask_questions_*.json): did the understanding layer choose the right
behaviour, KPI, tool and period? Used by tools/ask_eval_plan.py and the tests. Answer quality (numbers, wording) is
scored by eval_ask.py after Step 4; this scores only the plan."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, timedelta

from .state import ConversationState, Turn

ROLE_OF = {"viewer": "viewer", "manager": "manager", "tenant_admin": "tenant_admin", "platform_admin": "platform_admin"}


def default_period(anchor: date) -> tuple[str, str]:
    return (anchor - timedelta(days=29)).isoformat(), anchor.isoformat()


def expected_kpis(q: dict) -> set[str]:
    out = set(q.get("kpis") or [])
    for s in q.get("expected_plan") or []:
        if s.get("args", {}).get("kpi_id"):
            out.add(s["args"]["kpi_id"])
    return out


def gold_turn(q: dict) -> Turn:
    """What the benchmark says was asked in question ``q`` (used as the previous turn of its follow-up)."""
    plan = q.get("expected_plan") or []
    per = q.get("period") or {}
    return Turn(question=q["text"], kpi_ids=[k for k in dict.fromkeys(s["args"]["kpi_id"] for s in plan if s["args"].get("kpi_id"))],
                periods=[{"start": per["start"], "end": per["end"]}] if per.get("start") else [],
                tools=[s["tool"] for s in plan], grain=next((s["args"].get("grain") for s in plan if s["args"].get("grain")), None),
                top_n=next((s["args"].get("top_n") for s in plan if s["args"].get("top_n")), None))


def state_for(q: dict, by_id: dict) -> ConversationState:
    chain, cur, seen = [], q, set()
    while cur.get("follow_up_of") and cur["follow_up_of"] in by_id and cur["follow_up_of"] not in seen:
        seen.add(cur["follow_up_of"])
        cur = by_id[cur["follow_up_of"]]
        chain.append(cur)
    st = ConversationState()
    for p in reversed(chain):
        st.add(gold_turn(p))
    return st


def plan_periods(res) -> list[tuple[str, str]]:
    out = []
    for s in res.steps:
        for k in ("period", "period_a"):
            p = s["args"].get(k)
            if isinstance(p, dict):
                out.append((p["start"], p["end"]))
    return out


def score_one(q: dict, res, anchor: date) -> dict:
    exp = q["expected_behavior"]
    got = res.behavior
    f = {"id": q["id"], "tier": q["tier"], "lang": q["lang"], "expected": exp, "got": got, "reason": res.reason,
         "source": res.source, "behavior_ok": got == exp}
    if exp == "answer":
        ek = expected_kpis(q)
        gk = {s["args"].get("kpi_id") for s in res.steps if s["args"].get("kpi_id")}
        etools = [s["tool"] for s in q.get("expected_plan") or []]
        gtools = [s["tool"] for s in res.steps]
        kpi_free = {"explain_change", "warnings", "findings", "recommended_actions", "forecast", "customer_segments", "custom_query"}
        f["kpi_ok"] = (not ek) or bool(ek & gk) or (not gk and bool(gtools) and set(gtools) <= kpi_free)
        # equivalences: a value with its change vs previous is a comparison; customer questions may be served by a customer list,
        # a breakdown of a customer KPI or a drill
        norm = set(gtools)
        if any(s["tool"] == "kpi_value" and s["args"].get("compare") not in (None, "none") for s in res.steps):
            norm.add("compare_periods")
        if "customer_segments" in etools:
            norm |= {"customer_segments"} if norm & {"kpi_breakdown", "drill", "customer_segments"} else set()
        f["tool_ok"] = (not etools) or bool(set(etools) & norm)
        p = q.get("period")
        if p and p.get("start"):
            pp = plan_periods(res) or [default_period(anchor)]
            f["period_ok"] = (p["start"], p["end"]) == pp[0]
        else:
            f["period_ok"] = True
        f["plan_ok"] = f["behavior_ok"] and f["kpi_ok"] and f["tool_ok"] and f["period_ok"]
        f["plan"] = [{"tool": s["tool"], "args": s["args"]} for s in res.steps]
    else:
        f["plan_ok"] = f["behavior_ok"]
    return f


def summarize(rows: list[dict]) -> dict:
    def rate(rs, key):
        rs = [r for r in rs if key in r]
        return round(sum(bool(r[key]) for r in rs) / len(rs), 3) if rs else None

    out = {"n": len(rows), "plan_ok": rate(rows, "plan_ok"), "behavior_ok": rate(rows, "behavior_ok")}
    groups = defaultdict(list)
    for r in rows:
        groups[f"tier_{r['tier']}"].append(r)
        groups[f"lang_{r['lang']}"].append(r)
        groups[f"expected_{r['expected']}"].append(r)
    out["groups"] = {g: {"n": len(rs), "plan_ok": rate(rs, "plan_ok"), "behavior_ok": rate(rs, "behavior_ok"),
                         "kpi_ok": rate(rs, "kpi_ok"), "tool_ok": rate(rs, "tool_ok"), "period_ok": rate(rs, "period_ok")}
                      for g, rs in sorted(groups.items())}
    out["behavior_confusion"] = {f"{a}->{b}": n for (a, b), n in sorted(Counter((r["expected"], r["got"]) for r in rows).items())}
    out["sources"] = dict(Counter(r["source"] for r in rows))
    return out
