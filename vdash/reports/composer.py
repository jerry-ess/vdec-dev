"""Report composer (v27): turns a report request into a complete, data-only report document.

    doc = compose(dash, user, tc, {"blocks": [...], "start": "2026-08-28", "end": "2026-09-26",
                                   "compare": "previous", "lang": "vi"})

The document is a plain JSON-ready dict (layers -> chapters -> blocks) that every output format renders from:
HTML / PDF (Step 8-9), Excel (Step 12), report history (Step 11), the scheduler (Step 15).

Rules (no hallucination):
* Every number comes from the dashboard's own KPI results, evaluation and early warnings for the same user,
  tenant and period (``DashboardService``) - including role filtering and personal-data rules.
* Every sentence is a translated template (``i18n/<lang>.json``) filled with typed values and formatted like the
  page (``vdash.fmt``); there is no free text and no language model here.
* Blocks are emitted in the catalog order (layer -> chapter -> block), whatever order they were requested in.
* A requested block that is not usable now (no data, role, past period, no comparison) is listed in
  ``skipped`` with its plain-language reason instead of failing the whole report.
"""
from __future__ import annotations

import time
from datetime import date

from ..catalog import Kpi, load_catalog
from ..engine import compare_range
from ..evaluation import arg, pct_change, render_text
from ..fmt import fmt_bucket, fmt_month, fmt_value
from ..i18n import norm_lang, t, translate_kpi_public, ui
from ..services import ServiceError
from .blocks import BlockResolver, all_block_ids, load_spec

DOC_VERSION = 2                                # v36: + summary (the 'At a glance' page)
ADDITIVE = {"currency", "int", "number"}       # a share of the total makes sense only for these
MAX_FINDING_ROWS = 10                          # rows listed under one finding in a report
MAX_TABLE_ROWS = 20                            # rows of a table block
FLAT_PCT = 0.5                                 # |change| below this % reads as "steady"


def _num(v):
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


class Composer:
    def __init__(self, dash, user, tc, st: dict, lang: str):
        self.dash, self.user, self.tc, self.st = dash, user, tc, st
        self.lg = norm_lang(lang)
        self.cur = tc.currency
        self.catalog = load_catalog(tc.catalog)

    def text(self, key: str, args: dict) -> str:
        return render_text(ui(self.lg, key), args, self.cur, self.lg)

    # ------------------------------------------------------------ KPI blocks
    def kpi_public(self, k: Kpi) -> dict:
        p = translate_kpi_public(k.to_public(), self.lg)
        p.pop("template_kpis", None)            # internal codes never leave the server in a report
        return p

    def metric_text(self, k: Kpi, res: dict) -> str:
        v, prev = _num(res.get("value")), _num(res.get("compare_value"))
        label = arg(t(self.lg, k.title), "text")
        if v is None:
            return ""
        if prev is None:
            return self.text("rpt_metric_value", {"kpi": label, "value": arg(v, k.fmt)})
        dp = pct_change(v, prev)
        delta, dfmt = (v - prev, "pts") if k.fmt == "pct" else (dp, "pct")
        if delta is None or (abs(dp or 0) < FLAT_PCT and (k.fmt != "pct" or abs(v - prev) < 0.05)):
            return self.text("rpt_metric_flat", {"kpi": label, "value": arg(v, k.fmt), "cmp": arg(prev, k.fmt)})
        key = "ev_rose" if v > prev else "ev_fell"          # same sentences as the business evaluation
        return self.text(key, {"kpi": label, "value": arg(v, k.fmt), "cmp": arg(prev, k.fmt), "delta": arg(abs(delta), dfmt)})

    def metric_content(self, k: Kpi, res: dict) -> dict:
        out = {"type": "metric", "fmt": k.fmt, "good": k.good, "value": res.get("value"),
               "compare_value": res.get("compare_value"), "delta_pct": res.get("delta_pct"),
               "delta_pts": res.get("delta_pts"), "spark": res.get("spark"),
               "value_text": fmt_value(res.get("value"), k.fmt, self.lg, self.cur),
               "compare_text": fmt_value(res.get("compare_value"), k.fmt, self.lg, self.cur)
               if res.get("compare_value") is not None else None,
               "text": self.metric_text(k, res)}
        if res.get("caveat"):
            out["note"] = res["caveat"]
        return out

    def chart_text(self, k: Kpi, r: dict) -> str:
        """One factual sentence about a chart, from its own numbers."""
        typ = r.get("type")
        if typ == "timeseries" and r.get("series") and r.get("x"):
            s = r["series"][0]
            pts = [(x, _num(v)) for x, v in zip(r["x"], s.get("data", [])) if _num(v) is not None]
            if len(pts) < 2:
                return ""
            hi, lo = max(pts, key=lambda p: p[1]), min(pts, key=lambda p: p[1])
            fmt = s.get("fmt") or k.fmt
            return self.text("rpt_ts_peak", {"what": arg(s.get("name") or t(self.lg, k.title), "text"),
                                             "when": arg(fmt_bucket(hi[0], r.get("grain", "day"), self.lg), "text"),
                                             "value": arg(hi[1], fmt),
                                             "low_when": arg(fmt_bucket(lo[0], r.get("grain", "day"), self.lg), "text"),
                                             "low_value": arg(lo[1], fmt)})
        if typ == "breakdown" and r.get("items"):
            items = [i for i in r["items"] if _num(i.get("value")) is not None]
            if not items or any(i.get("label_key") for i in items):
                return ""                                 # labels that carry their own numbers (ABC classes)
            top = max(items, key=lambda i: _num(i["value"]))
            total = sum(_num(i["value"]) for i in items)
            if k.fmt in ADDITIVE and total > 0 and all(_num(i["value"]) >= 0 for i in items) and len(items) > 1:
                return self.text("rpt_bd_top_share", {"top": arg(top["label"], "text"), "value": arg(top["value"], k.fmt),
                                                      "share": arg(100.0 * _num(top["value"]) / total, "pct")})
            return self.text("rpt_bd_top", {"top": arg(top["label"], "text"), "value": arg(top["value"], k.fmt)})
        if typ == "heatmap" and r.get("cells"):
            xi, yi, v = max(r["cells"], key=lambda c: _num(c[2]) if _num(c[2]) is not None else float("-inf"))
            x = r["x"][xi]
            xl = f"{str(x).zfill(2)}h" if k.id == "hourly_heatmap" else f"M{x}"
            y = r["y"][yi]
            yl = fmt_month(y, self.lg) if len(str(y)) == 7 and str(y)[4] == "-" else y
            return self.text("rpt_heat_peak", {"y": arg(yl, "text"), "x": arg(xl, "text"), "value": arg(v, k.fmt)})
        return ""

    def kpi_content(self, k: Kpi, res: dict) -> dict:
        r = self.dash.translate(k, res, self.lg, self.user)
        if r.get("type") == "error":
            return {"type": "unavailable", "text": ui(self.lg, "rpt_block_failed")}
        if r.get("empty"):
            return {"type": "empty", "text": ui(self.lg, "empty_period")}
        if r["type"] == "metric":
            return self.metric_content(k, r)
        if r["type"] == "table":
            rows = r.get("rows", [])
            cols = [{"key": c.key, "label": t(self.lg, c.label), "fmt": c.fmt} for c in k.columns]
            return {"type": "table", "columns": cols, "rows": rows[:MAX_TABLE_ROWS], "total_rows": len(rows),
                    "truncated": len(rows) > MAX_TABLE_ROWS}
        r.pop("ms", None); r.pop("cached", None)
        out = {"type": "chart", "kpi_public": self.kpi_public(k), "result": r, "text": self.chart_text(k, r)}
        if r.get("caveat"):
            out["note"] = r["caveat"]
        return out

    # ------------------------------------------------------------ insight blocks
    def findings_content(self, findings: list[dict]) -> dict:
        items = []
        for f in findings:
            it = {"rule": f["rule"], "title": f["title"], "text": self.text(f["msg"], f["args"]),
                  "side": f.get("side"), "severity": f.get("severity"), "basis": f.get("basis"),
                  "is_new": bool(f.get("is_new"))}
            if f.get("items") and not f.get("items_hidden"):
                it["rows"] = f["items"][:MAX_FINDING_ROWS]
                it["columns"] = f.get("items_cols", [])
                it["total_rows"] = f.get("items_total") or len(f["items"])
            items.append(it)
        return {"type": "findings", "items": items}


def compose(dash, user, tc, spec: dict, generated_by: str | None = None) -> dict:
    """Build the report document. Raises ServiceError(400) for an invalid request."""
    if not isinstance(spec, dict) or not isinstance(spec.get("blocks"), list) or not spec["blocks"]:
        raise ServiceError(400, "Choose at least one block for the report")
    _, _, _, st = dash.visible(user, tc)
    req = dash.period(spec.get("start"), spec.get("end"), "auto", spec.get("compare", "previous"), st)
    lg = norm_lang(spec.get("lang"))
    catalog = load_catalog(tc.catalog)
    blocks_spec = load_spec()
    wanted = list(dict.fromkeys(spec["blocks"]))
    unknown = [b for b in wanted if b not in all_block_ids(catalog, blocks_spec)]
    if unknown:
        raise ServiceError(400, f"Unknown report block(s): {', '.join(unknown[:5])}")
    states = BlockResolver(dash, user, tc, st, blocks_spec).resolve(req.start, req.end, req.compare, lg)
    c = Composer(dash, user, tc, st, lg)
    want = set(wanted)

    # data, fetched once and in parallel (the dashboard's result cache makes prewarmed periods instant)
    usable = {b["id"]: b for ly in states["layers"] for ch in ly["chapters"] for b in ch["blocks"]
              if b["id"] in want and b["state"] == "available"}
    kpi_ids = [b["kpi"] for b in usable.values() if b["kind"] == "kpi"]
    head_spec = next((b for b in blocks_spec.insights if b["id"] == "exec_scorecard"), {})
    head_ids = [k for k in head_spec.get("requires_any_kpi", [])
                if k in catalog.by_id and user.has_role(catalog.by_id[k].min_role)
                and (st.get("availability", {}).get(k) or {}).get("available")] if "exec_scorecard" in usable else []
    kpis = [catalog.by_id[k] for k in dict.fromkeys(kpi_ids + head_ids)]
    results = dash.run_many(user, tc, kpis, req, st) if kpis else {}
    evaluation = warnings = None
    if {"eval_positive", "eval_negative"} & set(usable):
        res = dash.evaluation_result(user, tc, st, req)
        evaluation = {s: dash.present(res[s], user, tc, lg, max_items=MAX_FINDING_ROWS) for s in ("positive", "negative")}
    if "early_warnings" in usable:
        warnings = dash.present(dash.warnings_result(user, tc, st)["warnings"], user, tc, lg, max_items=MAX_FINDING_ROWS)

    def build(b: dict) -> dict:
        base = {"id": b["id"], "kind": b["kind"], "title": b["title"], "desc": b["desc"], "viz": b.get("viz")}
        if b["kind"] == "kpi":
            return {**base, "kpi": b["kpi"], "snapshot": b.get("snapshot", False),
                    **c.kpi_content(catalog.by_id[b["kpi"]], results[b["kpi"]])}
        if b["id"] == "exec_scorecard":
            tiles = []
            for kid in head_ids:
                k = catalog.by_id[kid]
                r = dash.translate(k, results[kid], lg, user)
                if r.get("type") == "metric" and not r.get("empty"):
                    tiles.append({"kpi": kid, "title": t(lg, k.title), **c.metric_content(k, r)})
            return {**base, "type": "tiles", "tiles": tiles}
        if b["id"] in ("eval_positive", "eval_negative"):
            return {**base, **c.findings_content(evaluation["positive" if b["id"] == "eval_positive" else "negative"])}
        if b["id"] == "early_warnings":
            return {**base, **c.findings_content(warnings)}
        if b["id"] == "quick_wins":
            from .quick_wins import quick_wins       # Step 7
            return {**base, "type": "actions", "items": quick_wins(c, dash, user, tc, st, req, results)}
        return {**base, "type": "unavailable", "text": ui(lg, "rpt_block_failed")}

    built: dict[str, dict] = {}

    layers, skipped, finding_ids = [], [], []
    for ly in states["layers"]:
        chapters = []
        for ch in ly["chapters"]:
            out = []
            for b in ch["blocks"]:
                if b["id"] not in want:
                    continue
                if b["state"] != "available":
                    skipped.append({"id": b["id"], "title": b["title"], "state": b["state"], "reason": b["reason"]})
                    continue
                blk = build(b)
                built[b["id"]] = blk
                if blk.get("type") == "findings":
                    finding_ids += [f"{b['id']}:{i['rule']}" for i in blk["items"]]
                out.append(blk)
            if out:
                chapters.append({"id": ch["id"], "title": ch["title"], "desc": ch["desc"], "blocks": out})
        if chapters:
            layers.append({"id": ly["id"], "title": ly["title"], "chapters": chapters})

    # v36: evidence - parts the story writer may draw on to explain "why", even if not ticked (not printed)
    from .story_rules import load_story_config
    scfg = load_story_config()
    ev_ids = [x for x in scfg.get("evidence_parts") or [] if x not in want][: int(scfg.get("max_evidence_parts", 10))]
    place = {b["id"]: (ly, ch, b) for ly in states["layers"] for ch in ly["chapters"] for b in ch["blocks"]}
    ev_use = [place[x] for x in ev_ids if x in place and place[x][2]["state"] == "available" and place[x][2]["kind"] == "kpi"]
    more = [catalog.by_id[b["kpi"]] for _, _, b in ev_use if b["kpi"] not in results]
    if more:
        results = {**results, **dash.run_many(user, tc, more, req, st)}
    evidence = []
    for ly, ch, b in ev_use:
        blk = build(b)
        if blk.get("type") in ("metric", "chart", "table"):
            evidence.append({"layer": ly["id"], "chapter": ch["id"], "chapter_title": ch["title"], **blk})

    summary = at_a_glance(dash, user, tc, st, req, lg, c, catalog, states, results, built, evaluation, warnings)

    max_d = st.get("max_date")
    cmp = compare_range(req.start, req.end, req.compare)
    period_text = c.text("rpt_period", {"start": arg(req.start.isoformat(), "date"), "end": arg(req.end.isoformat(), "date")})
    compare_text = ""
    if cmp:
        compare_text = c.text("rpt_cmp_" + req.compare, {"start": arg(cmp[0].isoformat(), "date"),
                                                         "end": arg(cmp[1].isoformat(), "date")})
    now = time.time()
    return {
        "doc_version": DOC_VERSION,
        "meta": {"tenant_id": tc.tenant_id, "business": tc.display_name, "currency": tc.currency, "lang": lg,
                 "industry": tc.industry,
                 "period": {"start": req.start.isoformat(), "end": req.end.isoformat(), "compare": req.compare,
                            "compare_start": cmp[0].isoformat() if cmp else None,
                            "compare_end": cmp[1].isoformat() if cmp else None,
                            "is_current": states["period"]["is_current"], "max_date": max_d},
                 "data_version": st.get("data_version"), "generated_at": now,
                 "generated_by": generated_by or user.username, "blocks_requested": wanted},
        "cover": {"title": ui(lg, "rpt_report_title"), "business": tc.display_name, "period_text": period_text,
                  "compare_text": compare_text,
                  "data_text": c.text("rpt_data_through", {"date": arg(max_d, "date")}) if max_d else "",
                  "generated_text": c.text("rpt_generated_on", {"date": arg(date.fromtimestamp(now).isoformat(), "date")}),
                  "past_note": states["period"]["note"],
                  "data_note": ""},            # set when a stored copy is opened after the data changed (v34)
        "summary": summary,
        "evidence": evidence,
        "layers": layers,
        "skipped": skipped,
        "finding_ids": finding_ids,
    }


def at_a_glance(dash, user, tc, st, req, lg, c, catalog, states, results, built, evaluation, warnings) -> dict:
    """v36: the report's first page by rules (see ``story_rules``). Uses the core figures, the evaluation, the
    warnings and the quick wins whenever this user may see them for this period - even if those parts were not
    ticked - because the first page answers "how are we doing?" for every report."""
    from .blocks import kpi_block_id
    from .story_rules import Glance, load_story_config
    cfg = load_story_config()
    h = cfg["headline"]
    state_of = {b["id"]: b["state"] for ly in states["layers"] for ch in ly["chapters"] for b in ch["blocks"]}
    wanted_core = list(dict.fromkeys([h["kpi"], h["orders_kpi"], h["basket_kpi"]] + list(cfg.get("key_figures") or [])
                                     + list(cfg.get("outlook_kpis") or [])))
    usable = [k for k in wanted_core if k in catalog.by_id and state_of.get(kpi_block_id(k)) == "available"]
    missing = [catalog.by_id[k] for k in usable if k not in results]
    if missing:
        results = {**results, **dash.run_many(user, tc, missing, req, st)}
    core = {}
    for kid in usable:
        k = catalog.by_id[kid]
        r = dash.translate(k, results[kid], lg, user)
        if r.get("type") == "metric" and not r.get("empty"):
            core[kid] = {"kpi": k, "res": r, "title": t(lg, k.title), "content": c.metric_content(k, r)}

    def items(block_id, raw):
        if block_id in built:
            its = built[block_id].get("items") or []
        else:
            its = c.findings_content(raw)["items"] if raw is not None else []
        return [{**i, "ref": f"{block_id}:{i['rule']}"} for i in its]

    pos = neg = warn = []
    if state_of.get("eval_positive") == "available":
        if evaluation is None:
            res = dash.evaluation_result(user, tc, st, req)
            evaluation = {sd: dash.present(res[sd], user, tc, lg, max_items=MAX_FINDING_ROWS) for sd in ("positive", "negative")}
        pos = items("eval_positive", evaluation["positive"])
        neg = items("eval_negative", evaluation["negative"])
    if state_of.get("early_warnings") == "available":
        if warnings is None and "early_warnings" not in built:
            warnings = dash.present(dash.warnings_result(user, tc, st)["warnings"], user, tc, lg, max_items=MAX_FINDING_ROWS)
        warn = items("early_warnings", warnings)
    actions = []
    if state_of.get("quick_wins") == "available":
        if "quick_wins" in built:
            actions = built["quick_wins"].get("items") or []
        else:
            from .quick_wins import quick_wins
            actions = quick_wins(c, dash, user, tc, st, req, results)
    g = Glance(c, cfg)
    outlook = [{"text": core[k]["content"].get("text"), "ref": kpi_block_id(k)} for k in cfg.get("outlook_kpis") or [] if k in core]
    rules = catalog.rules_by_id
    head = g.headline(core)
    return {"source": "rules", "title": ui(lg, "story_title"),
            "headline": head,
            "messages": g.messages(pos, neg, warn, outlook, {h["kpi"], h["orders_kpi"], h["basket_kpi"]} if head else set(),
                                   lambda rid: rules[rid].kpi if rid in rules else None),
            "figures": g.figures(core),
            "actions": actions[: cfg.get("max_actions", 3)]}
