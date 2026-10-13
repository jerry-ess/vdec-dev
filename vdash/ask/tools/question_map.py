"""Reviewed map: template question id -> tool + dashboard KPI (+ extra calls). It belongs to the TEMPLATE and names no
tenant. Built by tools/ask_build_question_map.py from the Step 7 libraries of the tenants checked so far and a human
review (what each tenant said is kept in <catalog>.verification.json); the router (Step 3) picks a question id, this
file says which tool answers it. States: mapped | review (several candidates, first used) | gap (no KPI of the
shape) | unverified (no checked tenant has the data for it; mapped once a tenant with the data is added)."""
from __future__ import annotations

import json
import re

from ...config import ROOT
from ...i18n import t

_CACHE: dict[str, tuple[float, dict]] = {}


def load_question_map(catalog_name: str) -> dict:
    path = ROOT / "config" / "ask_questions" / f"{catalog_name}.json"
    if not path.exists():
        return {"questions": [], "by_id": {}}
    m = path.stat().st_mtime
    hit = _CACHE.get(catalog_name)
    if hit and hit[0] == m:
        return hit[1]
    d = json.loads(path.read_text(encoding="utf-8"))
    d["by_id"] = {q["question_id"]: q for q in d["questions"]}
    _CACHE[catalog_name] = (m, d)
    return d


def plan_for_question(catalog_name: str, question_id: str) -> list[dict] | None:
    """The tool calls that answer a mapped question: [{"tool", "args"}, ...] (None when it is not mapped)."""
    q = load_question_map(catalog_name)["by_id"].get(question_id)
    if not q or q["state"] not in ("mapped", "review") or not q.get("kpi"):
        return None
    calls = [{"tool": q["tool"], "args": {"kpi_id": q["kpi"], **(q.get("args") or {})}}]
    for x in q.get("extra") or []:
        calls.append({"tool": x["tool"], "args": {"kpi_id": x["kpi"], **(x.get("args") or {})}})
    return calls


# Business wording for "what data is missing" when no checked tenant could answer a question. Metric-level
# reasons first (they name no table), then "grouped by ..." reasons, then the catalog's own table/column areas.
METRIC_AREAS = [
    (r"GROSS-PROFIT|GROSS-MARGIN|MARGIN", "Product cost prices"),
    (r"MET-AR-|OUTSTANDING|accounts_receivable", "Customer balances owed"),
    (r"CASH-FLOW", "Cash flow"),
    (r"FREIGHT|SHIPPING", "Shipping costs"),
    (r"COLLECTION", "Customer payments and collections"),
    (r"INSTALLMENT", "Payment installments"),
    (r"grouped by category", "Product categories"),
    (r"grouped by (region|country|state|segment|customer_type|role)", "Customer details (location, gender, age)"),
]


def missing_area(catalog, reason: str) -> str:
    for pat, label in METRIC_AREAS:
        if re.search(pat, reason or "", re.I):
            return label
    for tbl in re.findall(r"\b((?:fact|dim|mart)_\w+)", reason or ""):      # "no data in fact_inventory_snapshot"
        area = catalog.data_area(f"table {tbl}")
        if area != "Additional sales details":
            return area
    return catalog.data_area(reason or "")


def answer_unavailable(ctx, question_id: str) -> dict | None:
    """Typed 'not available' result for a question no checked tenant could answer (None when mapped)."""
    q = load_question_map(ctx.tc.catalog)["by_id"].get(question_id)
    if q is None or q["state"] != "unverified":
        return None
    return {"ok": False, "tool": None, "error": "not_available", "message": "This business has no data for that yet.",
            "missing": t(ctx.lang, q.get("missing") or "Additional sales details"), "meta": ctx.meta(),
            "question_id": question_id}
