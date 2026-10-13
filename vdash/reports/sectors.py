"""Sector verdicts for the report's first page (v79).

The dashboard's storyteller (``vdash.scorecard``, v69) judges a few key measures per sector against default targets
(rule based, no AI). The report used to ignore it. This module runs the same ``scorecard.build`` for the report's
user, tenant and period and turns the result into finished text lines, so that HTML, PDF and the one-page brief print
exactly what the dashboard shows - same statuses, same targets, same "behind the numbers" lines.

Shape of ``summary["sectors"]`` (absent when the tenant has no scorecard targets or the build failed):
    {"title", "counts": {red, amber, green}, "areas": [{id, title, status, status_text, headline}],
     "attention": [{section, title, status, status_text, value, change, target, why}],   # max MAX_ATTENTION
     "good": [{section, title, value, change}]}                                          # max MAX_GOOD
Every string is already formatted in the report language. Stored reports without the key keep rendering as before.
"""
from __future__ import annotations

import logging

from ..evaluation import arg, render_text
from ..fmt import fmt_value
from ..i18n import ui

log = logging.getLogger("vdash.reports.sectors")
MAX_ATTENTION = 5
MAX_GOOD = 3


def _change_text(m: dict, lg: str) -> str:
    d = m.get("delta")
    if d is None:
        return ""
    from ..fmt import _num
    unit = (" " + ui(lg, "pts")) if m.get("delta_fmt") == "pts" else "%"
    sign = "+" if d > 0.05 else ("-" if d < -0.05 else "")
    return f"{sign}{_num(abs(d), lg, 1)}{unit}"


def _target_text(m: dict, lg: str, cur: str) -> str:
    from ..fmt import _num
    if m.get("target"):
        return ui(lg, "sc_target_down" if m["dir"] == "down" else "sc_target_up",
                  v=fmt_value(m["target"]["green"], m["fmt"], lg, cur))
    c = m.get("change_rule")
    if not c:
        return ""
    if c["green"] >= 0:
        return ui(lg, "sc_hold_down" if m["dir"] == "down" else "sc_hold_up")
    unit = (" " + ui(lg, "pts")) if m["fmt"] == "pct" else "%"
    return ui(lg, "sc_dip_down" if m["dir"] == "down" else "sc_dip_up", v=_num(abs(c["green"]), lg, 1) + unit)


def _why_text(m: dict, lg: str) -> str:
    from ..fmt import _num
    parts = []
    for w in (m.get("why") or [])[:1]:                      # one line per measure keeps the first page short
        items = " · ".join(f'{i["label"]} {_num(i["share"], lg, 0)}%' for i in w["items"])
        parts.append(ui(lg, "sc_why", what=w["title"]) + " " + items)
    return " ".join(parts)


def sector_story(dash, user, tc, st: dict, req, lg: str) -> dict | None:
    """Never raises: the first page is complete without it."""
    try:
        from .. import scorecard
        sc = scorecard.build(dash, user, tc, st, req, lg)
    except Exception as e:                                  # noqa: BLE001 - a scorecard problem must not break a report
        log.warning("sector verdicts not built: %s", e)
        return None
    if not sc.get("available") or not sc.get("sections"):
        return None
    cur = tc.currency

    def head(h: dict) -> str:
        return render_text(ui(lg, h["msg"]), {k: arg(v["v"], v["fmt"]) for k, v in h["args"].items()}, cur, lg)

    def line(m: dict) -> dict:
        return {"section": m.get("section_title") or "", "title": m["title"], "status": m["status"],
                "status_text": ui(lg, "sc_status_" + m["status"]),
                "value": fmt_value(m["value"], m["fmt"], lg, cur), "change": _change_text(m, lg),
                "was": fmt_value(m["prev"], m["fmt"], lg, cur) if m.get("prev") is not None else "",
                "target": _target_text(m, lg, cur), "why": _why_text(m, lg)}

    areas = [{"id": s["id"], "title": s["title"], "status": s["status"], "status_text": ui(lg, "sc_status_" + s["status"]),
              "headline": head(s["headline"])} for s in sc["sections"] if s["status"] != "na"]
    return {"title": ui(lg, "sc_board_title"), "counts": sc.get("counts") or {}, "areas": areas,
            "attention": [line(m) for m in sc["issues"][:MAX_ATTENTION]],
            "good": [line(m) for m in sc["good"][:MAX_GOOD]],
            "attention_title": ui(lg, "sc_issues"), "good_title": ui(lg, "sc_good")}
