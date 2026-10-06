"""Changes since your last report (v43, Step 21): what moved between two stored reports of the same kind.

The one rule behind everything here: **a comparison is shown only when it is certain to be meaningful**. Anything
doubtful is left out - an empty section is better than a confusing one. No language model is involved: the numbers
come from the two stored reports, the sentences are translated templates (the same ones the dashboard uses).

A new report is compared with the user's most recent stored report that is *the same kind*:
  same business, language, currency, industry, set of parts and comparison mode, and (almost) the same period
  length - calendar months of 28 to 31 days count as the same length;
  the earlier report covers an earlier period (at most half of it overlapping, at most one period apart);
  the closest such report is used - the search only steps over reports that overlap too much, never over others;
  both carry a ``snapshot`` (reports made before v43 do not, so they are never compared).

Three things are compared, each only when its own conditions hold:
  * key figures - both periods fully covered by data (or exactly the same number of days with data); only flow figures
    (not "as of today" ones); when the lengths differ, totals are compared per day and figures that do not scale
    with the length (distinct customers, repeat rate) are not compared at all; a change
    is reported only when it is clearly bigger than normal fluctuation (a minimum size, and for counts also more than
    ``count_sigma`` times the random noise of a count); never from a zero or tiny base;
  * early warnings - new / still open (and for how many reports in a row) / no longer flagged. "No longer flagged"
    needs proof that the rule was checked this time (its data was available), so a rule that simply could not be
    evaluated never counts as solved;
  * suggested actions - those still relevant from the last report.

The result is stored in the report (``doc["changes"]``): the report opens exactly the same way every time.
"""
from __future__ import annotations

import json
import logging
import math
import threading
from datetime import date
from pathlib import Path

from ..config import config_dir
from ..evaluation import arg, render_text
from ..i18n import ui

log = logging.getLogger("vdash.reports")
SNAPSHOT_VERSION = 1
CHANGES_VERSION = 1
_LOCK = threading.Lock()
_CACHE: dict[str, tuple[float, dict]] = {}


def config_path() -> Path:
    return config_dir() / "report_changes.json"


def load_config(path: Path | None = None) -> dict:
    path = path or config_path()
    mtime = path.stat().st_mtime
    with _LOCK:
        hit = _CACHE.get(str(path))
        if hit and hit[0] == mtime:
            return hit[1]
        raw = json.loads(path.read_text(encoding="utf-8"))
        _CACHE[str(path)] = (mtime, raw)
        return raw


def validate_config(cfg: dict) -> list[str]:
    errs: list[str] = []

    def num(path, v, lo, hi):
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not lo <= v <= hi:
            errs.append(f"{path}: a number from {lo} to {hi} is required")
    el, fg, wn, pg = (cfg.get(k) or {} for k in ("eligibility", "figures", "warnings", "page1"))
    num("eligibility.max_overlap_share", el.get("max_overlap_share"), 0, 1)
    num("eligibility.max_gap_periods", el.get("max_gap_periods"), 0, 12)
    num("eligibility.max_candidates", el.get("max_candidates"), 1, 10)
    num("eligibility.max_length_diff_days", el.get("max_length_diff_days"), 0, 10)
    num("eligibility.max_length_diff_share", el.get("max_length_diff_share"), 0, 0.5)
    if not isinstance(el.get("same_blocks"), bool):
        errs.append("eligibility.same_blocks: true or false is required")
    if not isinstance(fg.get("kpis"), list) or not fg["kpis"]:
        errs.append("figures.kpis: a non-empty list is required")
    sc = fg.get("scaling")
    if not isinstance(sc, dict) or any(v not in ("per_day", "same") for v in sc.values()) or set(sc) - set(fg.get("kpis") or []):
        errs.append("figures.scaling: {kpi: 'per_day' | 'same'} for listed KPIs is required")
    num("figures.min_change_pct", fg.get("min_change_pct"), 1, 100)
    num("figures.min_change_pts", fg.get("min_change_pts"), 0.1, 50)
    num("figures.count_sigma", fg.get("count_sigma"), 0, 10)
    num("figures.min_base_count", fg.get("min_base_count"), 1, 100000)
    num("figures.min_data_days", fg.get("min_data_days"), 1, 366)
    num("figures.max_lines", fg.get("max_lines"), 1, 20)
    num("warnings.max_lines", wn.get("max_lines"), 1, 20)
    num("page1.max_lines", pg.get("max_lines"), 1, 6)
    num("actions_max_lines", cfg.get("actions_max_lines"), 0, 10)
    return errs


# ------------------------------------------------------------------ what a report keeps for the next one
def build_snapshot(catalog, user, tc, st: dict, core: dict, warnings: list[dict] | None, actions: list[dict]) -> dict:
    """The compact, comparable facts of this report. ``core``: KPI id -> {kpi, res, title}; ``warnings``: the early
    warnings this user sees now, or None when they were not evaluated for this report."""
    from ..evaluation import active_rules, rule_role
    cfg = load_config()
    figs = {}
    for kid in cfg["figures"]["kpis"]:
        x = core.get(kid)
        if not x or x["kpi"].period_mode != "range" or x["res"].get("value") is None:
            continue
        figs[kid] = {"value": float(x["res"]["value"]), "fmt": x["kpi"].fmt, "good": x["kpi"].good, "title": x["title"]}
    snap = {"v": SNAPSHOT_VERSION, "figures": figs, "warnings": None, "checked": None,
            "actions": [{"id": a["id"], "text": a.get("text") or a.get("title") or ""} for a in actions or [] if a.get("id")]}
    if warnings is not None:
        snap["warnings"] = {w["rule"]: {"title": w.get("title") or "", "severity": w.get("severity")} for w in warnings}
        snap["checked"] = sorted(r.id for r in active_rules(catalog, "warning", st.get("availability", {}), tc.alerts)
                                 if user.has_role(rule_role(r, catalog)))
    return snap


# ------------------------------------------------------------------ is the earlier report comparable?
def _d(s) -> date | None:
    try:
        return date.fromisoformat(str(s)[:10])
    except (TypeError, ValueError):
        return None


def _span(doc: dict):
    p = doc["meta"]["period"]
    s, e = _d(p.get("start")), _d(p.get("end"))
    return (s, e, (e - s).days + 1) if s and e and e >= s else (None, None, 0)


def data_days(doc: dict) -> int:
    """Days of the report's period that the data actually covers (the data may end before the period does)."""
    s, e, n = _span(doc)
    if not n:
        return 0
    md = _d(doc["meta"]["period"].get("max_date"))
    return max(0, ((min(e, md) if md else e) - s).days + 1)


OVERLAP = "periods overlap too much"


def _length_ok(a: int, b: int, cfg: dict) -> bool:
    d = abs(a - b)
    el = cfg["eligibility"]
    return d == 0 or (d <= el["max_length_diff_days"] and d <= el["max_length_diff_share"] * min(a, b))


def eligible(prev: dict, cur: dict, cfg: dict | None = None) -> tuple[bool, str]:
    """(comparable?, why not). Strict on purpose."""
    cfg = cfg or load_config()
    el = cfg["eligibility"]
    pm, cm = prev.get("meta") or {}, cur.get("meta") or {}
    for key in ("tenant_id", "lang", "currency", "industry"):
        if pm.get(key) != cm.get(key):
            return False, f"different {key}"
    if el["same_blocks"] and set(pm.get("blocks_requested") or []) != set(cm.get("blocks_requested") or []):
        return False, "different parts"
    if (pm.get("period") or {}).get("compare") != (cm.get("period") or {}).get("compare"):
        return False, "different comparison"
    for d in (prev, cur):
        if not isinstance(d.get("snapshot"), dict) or d["snapshot"].get("v") != SNAPSHOT_VERSION:
            return False, "no snapshot"
    ps, pe, pn = _span(prev)
    cs, ce, cn = _span(cur)
    if not (pn and cn) or not _length_ok(pn, cn, cfg):
        return False, "different period length"
    if not (pe < ce and ps < cs):
        return False, "not an earlier period"
    if float(pm.get("generated_at") or 0) >= float(cm.get("generated_at") or 0):
        return False, "not an earlier report"
    overlap = max(0, (min(pe, ce) - max(ps, cs)).days + 1)
    if overlap > el["max_overlap_share"] * min(pn, cn):
        return False, OVERLAP
    gap = (cs - pe).days - 1
    if gap > el["max_gap_periods"] * cn:
        return False, "too long ago"
    return True, ""


def find_previous(store, tenant_id: str, username: str, doc: dict, cfg: dict | None = None) -> dict | None:
    """The user's closest earlier comparable stored report (with its document), or None. Only reports that overlap
    too much are stepped over; any other mismatch ends the search - 'last report' never silently means an older one."""
    cfg = cfg or load_config()
    cm = doc["meta"]
    cs, ce, cn = _span(doc)
    if not cn:
        return None
    blocks = set(cm.get("blocks_requested") or [])
    rows = []
    for r in store.list_reports(tenant_id, username, limit=50):
        sp = r.get("spec") or {}
        s, e = _d(sp.get("start")), _d(sp.get("end"))        # cheap pre-check on the history row
        if (sp.get("lang") != cm.get("lang") or sp.get("compare") != cm["period"].get("compare") or not s or not e
                or e < s or e >= ce or s >= cs or not _length_ok((e - s).days + 1, cn, cfg)
                or (cfg["eligibility"]["same_blocks"] and set(sp.get("blocks") or []) != blocks)):
            continue
        rows.append((e, r))
    rows.sort(key=lambda x: x[0], reverse=True)               # closest earlier period first (newest made first on ties)
    for _, r in rows[: cfg["eligibility"]["max_candidates"]]:
        full = store.get_report(tenant_id, username, r["id"], with_doc=True)
        if not full:
            continue
        ok, why = eligible(full["doc"], doc, cfg)
        if ok:
            return full
        if why != OVERLAP:
            return None
    return None


# ------------------------------------------------------------------ the comparison
def _figure_change(cur: float, prev: float, fmt: str, f: dict, counts: tuple[float, float] | None = None) -> tuple[float, str] | None:
    """(size, unit) when the change is clearly more than normal fluctuation, else None. ``counts``: for a count compared
    per day, the two raw counts on the same length (now, and before scaled to the new length) for the noise test."""
    if fmt == "pct":                                   # a rate: percentage points
        d = cur - prev
        return (d, "pts") if abs(d) >= f["min_change_pts"] else None
    if prev <= 0 or cur < 0:                           # no base to measure against
        return None
    pct = 100.0 * (cur - prev) / prev
    if abs(pct) < f["min_change_pct"]:
        return None
    if fmt == "int":
        nc, npv = counts or (cur, prev)
        if npv < f["min_base_count"] or abs(nc - npv) < f["count_sigma"] * math.sqrt(nc + npv):
            return None                                # a small count moves by chance
    return pct, "pct"


def compare(prev_row: dict, doc: dict, cfg: dict | None = None, story_cfg: dict | None = None) -> dict | None:
    """The ``changes`` block of ``doc`` against the stored report ``prev_row`` (None when nothing can be said)."""
    cfg = cfg or load_config()
    if story_cfg is None:
        from .story_rules import load_story_config
        story_cfg = load_story_config()
    prev = prev_row["doc"]
    ok, _ = eligible(prev, doc, cfg)
    if not ok:
        return None
    lg, cur_ccy = doc["meta"]["lang"], doc["meta"]["currency"]
    ps, cs = prev["snapshot"], doc["snapshot"]
    fcfg = cfg["figures"]
    weights = (story_cfg.get("facts") or {}).get("kpi_weight") or {}
    sev_score = (story_cfg.get("facts") or {}).get("severity") or {}
    items: list[dict] = []

    def text(key: str, **args) -> str:
        return render_text(ui(lg, key), args, cur_ccy, lg)

    # ---- key figures
    n_compared = 0
    _, _, pn = _span(prev)
    _, _, cn = _span(doc)
    same_len = pn == cn
    pd, cd = data_days(prev), data_days(doc)
    covered = (pd == cd >= fcfg["min_data_days"]) if same_len else (pd == pn and cd == cn and min(pd, cd) >= fcfg["min_data_days"])
    if covered:
        for kid in fcfg["kpis"]:
            a, b = cs["figures"].get(kid), ps["figures"].get(kid)
            if not a or not b or a["fmt"] != b["fmt"]:
                continue
            mode = fcfg["scaling"].get(kid)          # per_day | same | None (only comparable at equal length)
            if not same_len and mode is None:
                continue
            per_day = (not same_len) and mode == "per_day"
            av, bv = (a["value"] / cn, b["value"] / pn) if per_day else (a["value"], b["value"])
            n_compared += 1
            ch = _figure_change(av, bv, a["fmt"], fcfg, (a["value"], b["value"] * cn / pn) if per_day else None)
            if not ch:
                continue
            size, unit = ch
            up = av > bv
            good = a.get("good")
            tone = "info" if good not in ("up", "down") else ("good" if up == (good == "up") else "bad")
            title = text("chg_per_day", kpi=arg(a["title"], "text")) if per_day else a["title"]
            items.append({"kind": "figure", "chapter": "happened", "tone": tone, "ref": kid,
                          "score": weights.get(kid, 1.0) * abs(size) / (10.0 if unit == "pct" else 5.0),
                          "text": text("ev_rose" if up else "ev_fell", kpi=arg(title, "text"), value=arg(av, a["fmt"]),
                                       cmp=arg(bv, a["fmt"]), delta=arg(abs(size), unit))})
    figure_items = sorted((i for i in items if i["kind"] == "figure"), key=lambda i: -i["score"])[: fcfg["max_lines"]]
    items = list(figure_items)

    # ---- early warnings (state of the latest data at the time of each report)
    streaks: dict[str, int] = {}
    pasof, casof = _d(prev["meta"]["period"].get("max_date")), _d(doc["meta"]["period"].get("max_date"))
    warn_ok = (ps.get("warnings") is not None and cs.get("warnings") is not None and ps.get("checked") is not None
               and cs.get("checked") is not None and pasof and casof and casof > pasof)
    prev_streaks = (prev.get("changes") or {}).get("streaks") or {}
    if cs.get("warnings"):
        for rid in cs["warnings"]:
            streaks[rid] = (int(prev_streaks.get(rid) or 1) + 1) if (warn_ok and rid in ps["warnings"]) else 1
    n_warn_changes = 0
    if warn_ok:
        wl: list[dict] = []
        for rid, w in cs["warnings"].items():
            sev = sev_score.get(w.get("severity"), 3)
            if rid in ps["warnings"]:
                n = streaks[rid]
                wl.append({"kind": "warning_open", "chapter": "next", "tone": "warn", "ref": rid,
                           "score": sev * 0.7 + min(n, 5), "text": text("chg_warn_open", title=arg(w["title"], "text"), n=arg(n, "int"))})
            elif rid in ps["checked"]:                  # it was checked last time and was fine: genuinely new
                wl.append({"kind": "warning_new", "chapter": "next", "tone": "warn", "ref": rid, "score": sev + 2,
                           "text": text("chg_warn_new", title=arg(w["title"], "text"))})
        for rid, w in ps["warnings"].items():
            if rid not in cs["warnings"] and rid in cs["checked"]:       # proven: checked now and not flagged
                wl.append({"kind": "warning_resolved", "chapter": "next", "tone": "good", "ref": rid, "score": 3.0,
                           "text": text("chg_warn_resolved", title=arg(w["title"], "text"))})
        wl.sort(key=lambda i: -i["score"])
        wl = wl[: cfg["warnings"]["max_lines"]]
        n_warn_changes = len(wl)
        items += wl

    # ---- suggested actions that are still relevant
    pa = {a["id"] for a in ps.get("actions") or []}
    for a in (cs.get("actions") or [])[: int(cfg.get("actions_max_lines", 3))]:
        if a["id"] in pa and a["text"]:
            items.append({"kind": "action_open", "chapter": "do", "tone": "info", "ref": a["id"], "score": 0.0,
                          "text": text("chg_action_open", title=arg(a["text"], "text"))})

    steady = n_compared >= 3 and not figure_items and n_warn_changes == 0
    if not items and not steady:
        return None
    # the first page shows the few most important lines (never actions); the chapters hold all of them
    page1 = sorted((i for i, x in enumerate(items) if x["kind"] != "action_open"), key=lambda i: -items[i]["score"])
    return {"v": CHANGES_VERSION, "page1": page1[: int(cfg["page1"]["max_lines"])],
            "prev": {"id": prev_row["id"], "name": prev_row.get("name"), "generated_at": prev["meta"].get("generated_at"),
                     "period_text": (prev.get("cover") or {}).get("period_text") or "",
                     "generated_text": (prev.get("cover") or {}).get("generated_text") or ""},
            "steady": steady, "steady_text": text("chg_steady_all" if warn_ok else "chg_steady") if steady else "",
            "figures_compared": n_compared, "warnings_compared": bool(warn_ok), "items": items, "streaks": streaks}


def attach_changes(doc: dict, store, tenant_id: str, username: str) -> dict | None:
    """Find the comparable previous report and store the comparison in ``doc``. Never raises: a problem here only
    means the report has no 'since your last report' section."""
    try:
        if not isinstance(doc.get("snapshot"), dict):
            return None
        cfg = load_config()
        prev = find_previous(store, tenant_id, username, doc, cfg)
        ch = compare(prev, doc, cfg) if prev else None
        if ch:
            doc["changes"] = ch
        return ch
    except Exception:                 # noqa: BLE001
        log.exception("changes since the last report could not be worked out")
        return None
