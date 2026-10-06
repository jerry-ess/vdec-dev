"""The report's first page, "At a glance" (v36) - written by rules, no AI.

    summary = at_a_glance(c, core, findings, warnings, actions)

* **Headline**: one verdict on the headline figure (net revenue by default) in five bands (clearly better ...
  clearly weaker) and its main reason: revenue = orders x average order value, so the bigger of the two
  changes (on a log scale, where they add up exactly) is named - "mainly because of fewer orders (-10.5%)".
* **Key messages** (up to 5): the best and the worst finding of the business evaluation, the most urgent early
  warning, the outlook (forecast), then the next most important findings - never repeating the headline figure.
* **Key figures** (up to 6) in a fixed business order (config/report_story.json -> key_figures).
* **Top priorities** (up to 3): the highest-ranked quick wins.

Everything is a translated template filled with the report's own numbers; the AI story writer later starts from
the same material (fact sheet) and this page is its fallback.
"""
from __future__ import annotations

import json
import math
import threading
from pathlib import Path

from ..config import config_dir
from ..evaluation import arg, pct_change

_LOCK = threading.Lock()
_CACHE: dict[str, tuple[float, dict]] = {}


def story_path() -> Path:
    return config_dir() / "report_story.json"


def load_story_config(path: Path | None = None) -> dict:
    path = path or story_path()
    mtime = path.stat().st_mtime
    with _LOCK:
        hit = _CACHE.get(str(path))
        if hit and hit[0] == mtime:
            return hit[1]
        raw = json.loads(path.read_text(encoding="utf-8"))
        _CACHE[str(path)] = (mtime, raw)
        return raw


def validate_story_config(cfg: dict, catalog) -> list[str]:
    errs = []
    h = cfg.get("headline") or {}
    for key in ("kpi", "orders_kpi", "basket_kpi"):
        if h.get(key) and h[key] not in catalog.by_id:
            errs.append(f"headline.{key}: unknown KPI '{h[key]}'")
    if not (isinstance(h.get("strong"), (int, float)) and isinstance(h.get("moderate"), (int, float))
            and 0 < h["moderate"] < h["strong"]):
        errs.append("headline: need 0 < moderate < strong")
    for key in ("key_figures", "outlook_kpis"):
        for k in cfg.get(key) or []:
            if k not in catalog.by_id:
                errs.append(f"{key}: unknown KPI '{k}'")
    for key in ("max_key_figures", "max_messages", "max_actions"):
        if not isinstance(cfg.get(key), int) or cfg[key] < 0:
            errs.append(f"{key}: must be a whole number >= 0")
    from .blocks import all_block_ids
    ids = set(all_block_ids(catalog))
    for b in cfg.get("evidence_parts") or []:
        if b not in ids:
            errs.append(f"evidence_parts: unknown report part '{b}'")
    an = cfg.get("analysis") or {}
    for role, k in (an.get("bridge") or {}).items():
        if k not in catalog.by_id:
            errs.append(f"analysis.bridge.{role}: unknown KPI '{k}'")
    for k in an.get("drivers") or []:
        if k not in catalog.by_id:
            errs.append(f"analysis.drivers: unknown KPI '{k}'")
        elif catalog.by_id[k].viz not in ("hbar", "bar", "donut"):
            errs.append(f"analysis.drivers: '{k}' is not a breakdown")
    if k_ := an.get("trend_kpi"):
        if k_ not in catalog.by_id:
            errs.append(f"analysis.trend_kpi: unknown KPI '{k_}'")
    for k in (cfg.get("facts") or {}).get("kpi_weight", {}):
        if k not in catalog.by_id:
            errs.append(f"facts.kpi_weight: unknown KPI '{k}'")
    return errs


def _num(v):
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def judgement(good: str | None, value, prev, fmt: str | None, flat_pct: float = 0.5) -> str:
    """good / bad / flat / none - is the change good news for the business?"""
    v, p = _num(value), _num(prev)
    if v is None or p is None:
        return "none"
    if fmt == "pct":
        if abs(v - p) < 0.05:
            return "flat"
    else:
        d = pct_change(v, p)
        if d is None or abs(d) < flat_pct:
            return "flat"
    if good not in ("up", "down"):
        return "none"
    return "good" if (v > p) == (good == "up") else "bad"


class Glance:
    def __init__(self, c, cfg: dict):
        self.c, self.cfg = c, cfg

    def headline(self, core: dict) -> dict | None:
        """``core``: kpi id -> {"kpi": Kpi, "res": translated metric result}."""
        h = self.cfg["headline"]
        main = core.get(h["kpi"])
        if not main or _num(main["res"].get("value")) is None:
            return None
        k, r = main["kpi"], main["res"]
        from ..i18n import t
        name = arg(t(self.c.lg, k.title), "text")
        v, p = _num(r.get("value")), _num(r.get("compare_value"))
        if p is None or p == 0:
            return {"text": self.c.text("story_head_nocmp", {"kpi": name, "value": arg(v, k.fmt)}), "tone": "none",
                    "kpi": k.id}
        d = pct_change(v, p)
        band = ("strong_up" if d >= h["strong"] else "up" if d >= h["moderate"] else
                "strong_down" if d <= -h["strong"] else "down" if d <= -h["moderate"] else "flat")
        args = {"kpi": name, "value": arg(v, k.fmt), "cmp": arg(p, k.fmt), "delta": arg(abs(d), "pct")}
        text = self.c.text("story_head_" + band, args)
        driver = self.driver(core, d) if band != "flat" else None
        if driver:
            text = self.c.text("story_head_with_driver", {"head": arg(text, "text"), "driver": arg(driver, "text")})
        tone = judgement(k.good, v, p, k.fmt)
        return {"text": text, "tone": tone, "band": band, "kpi": k.id}

    def driver(self, core: dict, rev_change: float) -> str | None:
        h = self.cfg["headline"]
        o, b = core.get(h["orders_kpi"]), core.get(h["basket_kpi"])
        parts = []
        for name, x in (("orders", o), ("basket", b)):
            if not x:
                continue
            v, p = _num(x["res"].get("value")), _num(x["res"].get("compare_value"))
            if v and p and v > 0 and p > 0:
                parts.append((name, math.log(v / p), pct_change(v, p)))
        same_way = [x for x in parts if (x[1] > 0) == (rev_change > 0) and abs(x[2]) >= 0.5]
        if not same_way:
            return None
        name, _, d = max(same_way, key=lambda x: abs(x[1]))
        key = {("orders", True): "story_drv_more_orders", ("orders", False): "story_drv_fewer_orders",
               ("basket", True): "story_drv_bigger_baskets", ("basket", False): "story_drv_smaller_baskets"}[(name, d > 0)]
        return self.c.text(key, {"delta": arg(d, "signed_pct")})

    def figures(self, core: dict) -> list[dict]:
        out = []
        for kid in self.cfg.get("key_figures") or []:
            x = core.get(kid)
            if not x or _num(x["res"].get("value")) is None:
                continue
            out.append({"kpi": kid, "title": x["title"], **x["content"]})
            if len(out) >= self.cfg.get("max_key_figures", 6):
                break
        return out

    def messages(self, positives: list[dict], negatives: list[dict], warnings: list[dict], outlook: list[dict],
                 skip_kpis: set[str], rule_kpi) -> list[dict]:
        """Each input item: {"text", "rule"?}. Order: best, worst, most urgent warning, outlook, then the rest."""
        def ok(f):
            return rule_kpi(f.get("rule")) not in skip_kpis
        pos = [{"text": f["text"], "tone": "good", "ref": f.get("ref")} for f in positives if ok(f)]
        neg = [{"text": f["text"], "tone": "bad", "ref": f.get("ref")} for f in negatives if ok(f)]
        sev = {"urgent": 0, "warning": 1, "info": 2}
        warn = [{"text": f["text"], "tone": "warn", "ref": f.get("ref")}
                for f in sorted(warnings, key=lambda f: sev.get(f.get("severity"), 3))]
        look = [{"text": f["text"], "tone": "info", "ref": f.get("ref")} for f in outlook if f.get("text")]
        order = [pos[:1], neg[:1], warn[:1], look[:1], neg[1:2], pos[1:2], warn[1:2], neg[2:], pos[2:], look[1:]]
        out, seen = [], set()
        for group in order:
            for m in group:
                if m["text"] not in seen and len(out) < self.cfg.get("max_messages", 5):
                    seen.add(m["text"])
                    out.append(m)
        return out
