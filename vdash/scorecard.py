"""Scorecard (v69): every sector page and the Executive Overview tell a story without any AI.

Each sector has a few key measures with default targets (config/scorecard/<catalog>.json, optional per-tenant
override config/scorecard/<tenant_id>.json). A measure is judged on its level against the target and on its change
against the previous period. The sector gets a verdict, a headline and, for measures that are off target, a "behind
the numbers" line taken from the breakdown charts (largest contributors). The Overview gets a board of all sectors,
"needs attention first" and "going well" lists. All wording is ui keys + typed args; the page formats them.
"""
from __future__ import annotations

import json
import logging
import math
from pathlib import Path

from .catalog import load_catalog
from .config import config_dir
from .i18n import t

log = logging.getLogger("vdash.scorecard")
RANK = {"red": 0, "amber": 1, "green": 2, "na": 3}


def _num(x):
    return x if isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) else None


def load_targets(catalog_id: str, tenant_id: str) -> list[dict]:
    d = config_dir() / "scorecard"
    out: list[dict] = []
    try:
        out = json.loads((d / f"{catalog_id}.json").read_text(encoding="utf-8")).get("measures", [])
    except (OSError, ValueError) as e:
        log.warning("scorecard targets %s not loaded: %s", catalog_id, e)
        return []
    try:
        ov = json.loads((d / f"{tenant_id}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        ov = {}
    res = []
    for m in out:
        o = (ov.get("measures") or {}).get(m["kpi"]) or {}
        if o.get("off"):
            continue
        m = {**m, **{k: v for k, v in o.items() if k in ("level", "change", "weight")}}
        res.append(m)
    return res


def judge_level(v: float, lvl: dict, dirn: str) -> str:
    g, a = lvl["green"], lvl["amber"]
    if dirn == "down":
        return "green" if v <= g else "amber" if v <= a else "red"
    return "green" if v >= g else "amber" if v >= a else "red"


def judge_change(fav: float, chg: dict) -> str:
    return "green" if fav >= chg["green"] else "amber" if fav >= chg["amber"] else "red"


def measure(m: dict, k, res: dict) -> dict | None:
    if not res or res.get("type") != "metric" or res.get("empty"):
        return None
    v, prev = _num(res.get("value")), _num(res.get("compare_value"))
    if v is None:
        return None
    pts = k.fmt == "pct"
    delta = fav = None
    if prev is not None:
        if pts:
            delta = v - prev
        elif prev != 0:
            delta = 100.0 * (v - prev) / abs(prev)
        if delta is not None:
            fav = delta if m["dir"] == "up" else -delta
    trend = "flat"
    if fav is not None and abs(delta) >= (0.3 if pts else 1.0):
        trend = "good" if fav > 0 else "bad"
    lvl, chg = m.get("level"), m.get("change")
    if lvl:
        status = judge_level(v, lvl, m["dir"])
    elif chg and fav is not None:
        status = judge_change(fav, chg)
    else:
        status = "na"
    return {"kpi": k.id, "title": k.title, "fmt": k.fmt, "value": v, "prev": prev, "delta": delta,
            "delta_fmt": "pts" if pts else "pct", "trend": trend, "status": status, "weight": m.get("weight", 1),
            "dir": m["dir"], "target": lvl, "change_rule": None if lvl else chg, "section": m["section"],
            "drivers": m.get("drivers") or [], "fav": fav}


def verdict(ms: list[dict]) -> str:
    ms = [x for x in ms if x["status"] != "na"]
    if not ms:
        return "na"
    if any(x["status"] == "red" and x["weight"] >= 2 for x in ms):
        return "red"
    if any(x["status"] in ("red", "amber") for x in ms):
        return "amber"
    return "green"


def sort_key(x: dict):
    return (RANK[x["status"]], -x["weight"], -(abs(x["fav"]) if x.get("fav") is not None and x["fav"] < 0 else 0))


def drivers_for(dash, user, tc, st, req, catalog, bad: list[dict], shown_ids: set, lg: str) -> None:
    want = {kid for x in bad for kid in x["drivers"] if kid in shown_ids}
    if not want:
        return
    ks = [catalog.by_id[i] for i in want]
    res = dash.run_many(user, tc, ks, req, st)
    for x in bad:
        why = []
        for kid in x["drivers"]:
            r = res.get(kid)
            if not r or r.get("type") != "breakdown" or r.get("empty"):
                continue
            items = [i for i in r["items"] if _num(i.get("value")) and i["value"] > 0]
            tot = sum(i["value"] for i in items)
            if tot <= 0:
                continue
            top = sorted(items, key=lambda i: -i["value"])[:3]
            share = top[0]["value"] / tot
            if share < 0.2 and len(items) > 4:       # evenly spread: nothing to point at
                continue
            why.append({"kpi": kid, "title": t(lg, catalog.by_id[kid].title),
                        "items": [{"label": t(lg, i["label"]), "share": 100.0 * i["value"] / tot} for i in top]})
        x["why"] = why[:2]


def build(dash, user, tc, st, req, lg: str) -> dict:
    catalog = load_catalog(tc.catalog)
    shown, _, _, _ = dash.visible(user, tc)
    shown_ids = {k.id for k in shown}
    defs = [m for m in load_targets(tc.catalog, tc.tenant_id) if m["kpi"] in shown_ids]
    if not defs:
        return {"available": False, "sections": [], "issues": [], "good": []}
    res = dash.run_many(user, tc, [catalog.by_id[m["kpi"]] for m in defs], req, st)
    ms = []
    for d in defs:
        x = measure(d, catalog.by_id[d["kpi"]], res.get(d["kpi"]))
        if x:
            ms.append(x)
    bad = [x for x in ms if x["status"] in ("red", "amber")]
    try:
        drivers_for(dash, user, tc, st, req, catalog, bad, shown_ids, lg)
    except Exception as e:                           # the story still works without the "why" lines
        log.warning("scorecard drivers failed: %s", e)
    sections = []
    for s in catalog.sections:
        sm = sorted([x for x in ms if x["section"] == s.id], key=sort_key)
        if not sm:
            continue
        v = verdict(sm)
        n_bad = sum(x["status"] in ("red", "amber") for x in sm)
        first = next((x for x in sm if x["status"] in ("red", "amber")), None)
        head = ({"msg": "sc_h_ok", "args": {"n": {"v": len(sm), "fmt": "int"}}} if first is None else
                {"msg": "sc_h_bad", "args": {"bad": {"v": n_bad, "fmt": "int"}, "n": {"v": len(sm), "fmt": "int"},
                                              "kpi": {"v": t(lg, first["title"]), "fmt": "text"}}})
        sections.append({"id": s.id, "title": t(lg, s.title), "icon": s.icon, "status": v, "headline": head,
                         "measures": [{**x, "title": t(lg, x["title"]), "drivers": None} for x in sm]})
    why = {x["kpi"]: x.get("why") or [] for x in ms}
    for sct in sections:
        for x in sct["measures"]:
            x["why"] = why.get(x["kpi"], [])
    allm = [x for s in sections for x in s["measures"]]
    for x in allm:
        x["section_title"] = next(s["title"] for s in sections if s["id"] == x["section"])
    issues = sorted([x for x in allm if x["status"] in ("red", "amber")], key=sort_key)
    good = sorted([x for x in allm if x["status"] == "green"], key=lambda x: (-x["weight"], -(x.get("fav") or 0)))
    return {"available": True, "sections": sections, "issues": issues, "good": good,
            "counts": {s: sum(x["status"] == s for x in allm) for s in ("red", "amber", "green")}}
