"""Quick wins (v28): concrete actions for the next days, matched from the report's own evidence.

Triggers live in ``config/report_quick_wins.json`` (no code change to add or tune one):

* ``warning``    - an early warning of rule X is active for this business;
* ``evaluation`` - the business evaluation of the report period has a finding of rule X on side Y;
* ``kpi``        - a KPI table has a row meeting typed conditions (e.g. a product pair bought together with
                   lift >= 2 in at least 3 baskets).

Every action is a translated template (``rpt_qw_*``) filled with the source's own typed numbers, and carries
``because`` = the finding's sentence (or the KPI it came from) - so each recommendation can be traced to the data.
Role rules apply: a warning or KPI the user may not see never produces an action. No language model involved.
"""
from __future__ import annotations

import json
import logging
import operator
import threading
from pathlib import Path

from ..catalog import Catalog, load_catalog
from ..config import config_dir
from ..evaluation import arg
from ..i18n import bundle, t, ui

log = logging.getLogger("vdash.reports")
OPS = {">=": operator.ge, ">": operator.gt, "<=": operator.le, "<": operator.lt, "==": operator.eq, "!=": operator.ne}
PRIORITY_RANK = {"high": 0, "medium": 1, "low": 2}
SOURCES = ("warning", "evaluation", "kpi")

_LOCK = threading.Lock()
_CACHE: dict[str, tuple[float, dict]] = {}


def config_path() -> Path:
    return config_dir() / "report_quick_wins.json"


def load_config(path: Path | None = None) -> dict:
    path = path or config_path()
    mtime = path.stat().st_mtime
    with _LOCK:
        hit = _CACHE.get(str(path))
        if hit and hit[0] == mtime:
            return hit[1]
        cfg = json.loads(path.read_text(encoding="utf-8"))
        _CACHE[str(path)] = (mtime, cfg)
        return cfg


def _placeholders(s: str) -> set[str]:
    import re
    return set(re.findall(r"\{(\w+)\}", s))


def validate_config(cfg: dict, catalog: Catalog) -> list[str]:
    errs: list[str] = []
    trig = cfg.get("triggers")
    if not isinstance(trig, list) or not trig:
        return ["triggers: a non-empty list is required"]
    ids = [x.get("id") for x in trig]
    if len(set(ids)) != len(ids):
        errs.append("triggers: ids must be unique")
    en_ui = bundle("en")["ui"]
    rules = catalog.rules_by_id
    for x in trig:
        w = f"trigger {x.get('id')}"
        src = x.get("source")
        if src not in SOURCES:
            errs.append(f"{w}: source must be one of {', '.join(SOURCES)}")
            continue
        if src in ("warning", "evaluation"):
            r = rules.get(x.get("rule"))
            if r is None:
                errs.append(f"{w}: unknown rule {x.get('rule')!r}")
            elif r.panel != ("warning" if src == "warning" else "evaluation"):
                errs.append(f"{w}: rule {x['rule']!r} is a {r.panel} rule, not {src}")
            if src == "evaluation" and x.get("side") not in ("positive", "negative"):
                errs.append(f"{w}: side must be positive or negative")
        else:
            k = catalog.by_id.get(x.get("kpi"))
            if k is None:
                errs.append(f"{w}: unknown KPI {x.get('kpi')!r}")
            else:
                cols = {c.key for c in k.columns}
                if k.viz != "table":
                    errs.append(f"{w}: KPI {k.id!r} is not a table")
                pick = x.get("pick") or {}
                if pick and (len(pick) != 1 or next(iter(pick)) not in ("max", "min") or next(iter(pick.values())) not in cols):
                    errs.append(f"{w}: pick must be {{\"max\" | \"min\": <column>}}")
                for cond in (x.get("where") or []) + (x.get("when") or []):
                    if cond.get("field") not in cols:
                        errs.append(f"{w}: unknown column {cond.get('field')!r}")
                    if cond.get("op") not in OPS:
                        errs.append(f"{w}: op must be one of {' '.join(OPS)}")
                    if not isinstance(cond.get("value"), (int, float)) or isinstance(cond.get("value"), bool):
                        errs.append(f"{w}: condition value must be a number")
                for col in (x.get("args") or {}).values():
                    if col not in cols:
                        errs.append(f"{w}: unknown column {col!r} in args")
                bc = x.get("because")
                if bc is not None:
                    btpl = en_ui.get(bc.get("template") or "")
                    if btpl is None:
                        errs.append(f"{w}: because.template {bc.get('template')!r} is not a UI text key")
                    elif not _placeholders(btpl) <= set(bc.get("args") or {}):
                        errs.append(f"{w}: because.template needs {sorted(_placeholders(btpl) - set(bc.get('args') or {}))}")
                    for col in (bc.get("args") or {}).values():
                        if col not in cols:
                            errs.append(f"{w}: unknown column {col!r} in because.args")
        tpl = en_ui.get(x.get("template") or "")
        if tpl is None:
            errs.append(f"{w}: template {x.get('template')!r} is not a UI text key")
        elif not _placeholders(tpl) <= set((x.get("args") or {})):
            errs.append(f"{w}: template needs {sorted(_placeholders(tpl) - set(x.get('args') or {}))} in args")
        if x.get("priority") not in PRIORITY_RANK:
            errs.append(f"{w}: priority must be high, medium or low")
        if not isinstance(x.get("timeline_days"), int) or x["timeline_days"] <= 0:
            errs.append(f"{w}: timeline_days must be a positive whole number")
        for u in x.get("unless") or []:
            if u not in ids:
                errs.append(f"{w}: unless refers to unknown trigger {u!r}")
    if not isinstance(cfg.get("max_actions", 6), int) or cfg.get("max_actions", 6) < 1:
        errs.append("max_actions must be a positive whole number")
    return errs


def _row_ok(row: dict, conds: list[dict]) -> bool:
    for cnd in conds or []:
        v = row.get(cnd["field"])
        try:
            if v is None or not OPS[cnd["op"]](float(v), float(cnd["value"])):
                return False
        except (TypeError, ValueError):
            return False
    return True


def quick_wins(c, dash, user, tc, st, req, results) -> list[dict]:
    """Actions for this report. ``c`` is the report Composer (language, template texts)."""
    catalog = load_catalog(tc.catalog)
    cfg = load_config()
    errs = validate_config(cfg, catalog)
    if errs:
        for e in errs:
            log.error("config/report_quick_wins.json: %s", e)
        return []
    lg = c.lg
    panels = dash.panels_for(user, tc, st)
    warnings = {}
    if panels.get("warnings"):
        for f in dash.present(dash.warnings_result(user, tc, st)["warnings"], user, tc, lg):
            warnings.setdefault(f["rule"], f)
    findings = {}
    if panels.get("evaluation") and req.compare != "none":
        res = dash.evaluation_result(user, tc, st, req)
        for side in ("positive", "negative"):
            for f in dash.present(res[side], user, tc, lg):
                findings.setdefault((f["rule"], side), f)

    actions, fired = [], set()
    for order, x in enumerate(cfg["triggers"]):
        if any(u in fired for u in x.get("unless") or []):
            continue
        args, because, score = None, "", 0.0
        if x["source"] in ("warning", "evaluation"):
            f = warnings.get(x["rule"]) if x["source"] == "warning" else findings.get((x["rule"], x["side"]))
            if f is None or not all(src in f["args"] for src in x["args"].values()):
                continue
            args = {name: f["args"][src] for name, src in x["args"].items()}
            because, score = c.text(f["msg"], f["args"]), float(f.get("score") or 0)
        else:
            k = catalog.by_id[x["kpi"]]
            if not user.has_role(k.min_role) or not (st.get("availability", {}).get(k.id) or {}).get("available"):
                continue
            res = results.get(k.id) or dash.run_many(user, tc, [k], req, st)[k.id]
            r = dash.translate(k, res, lg, user)
            rows = [row for row in r.get("rows") or [] if _row_ok(row, x.get("where"))]
            if not rows:
                continue
            if x.get("pick"):
                how, col = next(iter(x["pick"].items()))
                rows = [row for row in rows if row.get(col) is not None]
                if not rows:
                    continue
                row = (max if how == "max" else min)(rows, key=lambda rw: float(rw[col]))
            else:
                row = rows[0]
            if not _row_ok(row, x.get("when")):
                continue
            fmt = {col.key: col.fmt for col in k.columns}
            args = {name: arg(row.get(col), fmt.get(col, "text")) for name, col in x["args"].items()}
            if x.get("because"):
                bc = x["because"]
                because = c.text(bc["template"], {n: arg(row.get(col), fmt.get(col, "text")) for n, col in bc["args"].items()})
            else:
                because = ui(lg, "rpt_qw_from_kpi", kpi=t(lg, k.title))
        fired.add(x["id"])
        actions.append({"id": x["id"], "priority": x["priority"], "timeline_days": x["timeline_days"],
                        "priority_text": ui(lg, f"rpt_prio_{x['priority']}"),
                        "timeline_text": ui(lg, "rpt_within_days", n=x["timeline_days"]),
                        "text": c.text(x["template"], args), "because": because,
                        "source": {"kind": x["source"], "rule": x.get("rule"), "kpi": x.get("kpi")},
                        "_sort": (PRIORITY_RANK[x["priority"]], x["timeline_days"], -score, order)})
    actions.sort(key=lambda a: a["_sort"])
    for a in actions:
        del a["_sort"]
    return actions[: int(cfg.get("max_actions", 6))]
