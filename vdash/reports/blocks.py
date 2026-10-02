"""Report content blocks and whether each one can be used right now (v26).

A report is built from blocks, grouped as   layer (what happened / why / what next / what to do)
                                             -> chapter (one per dashboard section + summary chapters)
                                             -> block.

* **KPI blocks** - one for EVERY KPI of the tenant's catalog (``kpi:<id>``), placed in the chapter of the KPI's first
  dashboard section. Nothing to maintain: a KPI added to the catalog appears in reports automatically.
* **Insight blocks** - built from the business evaluation, early warnings and quick wins
  (``config/report_blocks.json`` -> ``insight_blocks``).

Each block gets a state for this user, tenant and period - the same rules as the dashboard, on the same live
data checks (``tenant_state.availability``, refreshed whenever the warehouse changes):

    available         can be ticked
    locked            the user's role is too low (title shown, data never)
    no_data           the tenant has no data for it yet; ``reason`` says which data would switch it on
    historical        past period: forward-looking blocks and blocks that only show today's state are off
    needs_comparison  needs a comparison period (compare = none)
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from ..catalog import Catalog, Kpi, load_catalog
from ..config import config_dir
from ..i18n import t, ui

STATES = ("available", "locked", "no_data", "historical", "needs_comparison")
GROUP_OF_VIZ = {"metric": "figures", "table": "lists"}   # everything else: "charts"


@dataclass
class BlockSpec:
    raw: dict
    layers: list[dict]
    chapters: list[dict]
    insights: list[dict]
    historical_after_days: int = 7
    default_chapters: set[str] = field(default_factory=set)
    errors: list[str] = field(default_factory=list)

    @property
    def layer_of(self) -> dict[str, dict]:
        return {ly["id"]: ly for ly in self.layers}

    @property
    def chapter_of_section(self) -> dict[str, str]:
        return {c["section"]: c["id"] for c in self.chapters if c.get("section")}


_LOCK = threading.Lock()
_CACHE: dict[str, tuple[float, BlockSpec]] = {}


def blocks_path() -> Path:
    return config_dir() / "report_blocks.json"


def load_spec(path: Path | None = None) -> BlockSpec:
    """Read (and cache by modification time) config/report_blocks.json."""
    path = path or blocks_path()
    mtime = path.stat().st_mtime
    with _LOCK:
        hit = _CACHE.get(str(path))
        if hit and hit[0] == mtime:
            return hit[1]
        raw = json.loads(path.read_text(encoding="utf-8"))
        spec = BlockSpec(raw=raw, layers=list(raw.get("layers", [])), chapters=list(raw.get("chapters", [])),
                         insights=list(raw.get("insight_blocks", [])),
                         historical_after_days=int(raw.get("historical_after_days", 7)),
                         default_chapters=set((raw.get("default_on") or {}).get("chapters", [])))
        _CACHE[str(path)] = (mtime, spec)
        return spec


def validate(spec: BlockSpec, catalog: Catalog) -> list[str]:
    """Every problem in the block file for this catalog (empty list = valid)."""
    errs: list[str] = []
    layer_ids = [ly.get("id") for ly in spec.layers]
    if len(set(layer_ids)) != len(layer_ids) or not layer_ids:
        errs.append("layers: ids must be present and unique")
    ch_ids = [c.get("id") for c in spec.chapters]
    if len(set(ch_ids)) != len(ch_ids):
        errs.append("chapters: ids must be unique")
    sections = {s.id for s in catalog.sections}
    mapped = [c["section"] for c in spec.chapters if c.get("section")]
    for c in spec.chapters:
        if c.get("layer") not in layer_ids:
            errs.append(f"chapter {c.get('id')}: unknown layer {c.get('layer')!r}")
        if c.get("section") and c["section"] not in sections:
            errs.append(f"chapter {c.get('id')}: unknown dashboard section {c['section']!r}")
        if not c.get("section") and not c.get("title_key"):
            errs.append(f"chapter {c.get('id')}: needs a section or a title_key")
    for s in sorted(sections - set(mapped)):
        errs.append(f"dashboard section {s!r} has no chapter - its KPIs would be missing from reports")
    for s in sorted({x for x in mapped if mapped.count(x) > 1}):
        errs.append(f"dashboard section {s!r} is mapped to more than one chapter")
    kpi_ids = {k.id for k in catalog.kpis}
    ins_ids = [b.get("id") for b in spec.insights]
    if len(set(ins_ids)) != len(ins_ids):
        errs.append("insight_blocks: ids must be unique")
    for b in spec.insights:
        where = f"insight block {b.get('id')}"
        if b.get("chapter") not in ch_ids:
            errs.append(f"{where}: unknown chapter {b.get('chapter')!r}")
        if not b.get("title_key"):
            errs.append(f"{where}: title_key missing")
        for k in b.get("requires_any_kpi", []) + (b.get("requires_panel_or_kpi") or {}).get("kpi", []):
            if k not in kpi_ids:
                errs.append(f"{where}: unknown KPI {k!r}")
        panel = b.get("requires_panel") or (b.get("requires_panel_or_kpi") or {}).get("panel")
        if panel and panel not in ("evaluation", "warnings"):
            errs.append(f"{where}: unknown panel {panel!r}")
        if not (b.get("requires_any_kpi") or b.get("requires_panel") or b.get("requires_panel_or_kpi")):
            errs.append(f"{where}: needs requires_any_kpi, requires_panel or requires_panel_or_kpi")
    for c in spec.default_chapters - set(ch_ids):
        errs.append(f"default_on.chapters: unknown chapter {c!r}")
    return errs


GROUPS = ("summary", "figures", "charts", "lists")


def profiles_path() -> Path:
    return config_dir() / "report_profiles.json"


_PCACHE: dict[str, tuple[float, dict]] = {}


def load_profiles(path: Path | None = None) -> dict:
    """config/report_profiles.json (v30), cached by modification time; {} when the file does not exist."""
    path = path or profiles_path()
    if not path.is_file():
        return {}
    mtime = path.stat().st_mtime
    with _LOCK:
        hit = _PCACHE.get(str(path))
        if hit and hit[0] == mtime:
            return hit[1]
        cfg = json.loads(path.read_text(encoding="utf-8"))
        _PCACHE[str(path)] = (mtime, cfg)
        return cfg


def validate_profiles(cfg: dict, spec: BlockSpec, catalog: Catalog) -> list[str]:
    if not cfg:
        return []
    errs: list[str] = []
    profs = cfg.get("profiles") or []
    ids = [p.get("id") for p in profs]
    if not profs or len(set(ids)) != len(ids):
        errs.append("profiles: a non-empty list with unique ids is required")
    if cfg.get("default") not in ids:
        errs.append(f"default profile {cfg.get('default')!r} is not in the list")
    chapters = {c["id"] for c in spec.chapters}
    blocks = all_block_ids(catalog, spec)
    for p in profs:
        w = f"profile {p.get('id')}"
        if not p.get("title_key"):
            errs.append(f"{w}: title_key missing")
        for c in p.get("chapters", []):
            if c not in chapters:
                errs.append(f"{w}: unknown chapter {c!r}")
        for b in p.get("blocks", []):
            if b not in blocks:
                errs.append(f"{w}: unknown block {b!r}")
        for c, gs in (p.get("groups") or {}).items():
            if c not in chapters:
                errs.append(f"{w}: unknown chapter {c!r} in groups")
            for g in gs:
                if g not in GROUPS:
                    errs.append(f"{w}: unknown group {g!r} (use {', '.join(GROUPS)})")
        if not (p.get("all") or p.get("chapters") or p.get("blocks") or p.get("groups")):
            errs.append(f"{w}: selects nothing")
    return errs


def profile_selects(p: dict, chapter_id: str, block: dict) -> bool:
    return bool(p.get("all") or block["id"] in p.get("blocks", []) or chapter_id in p.get("chapters", [])
                or block.get("group") in (p.get("groups") or {}).get(chapter_id, []))


def kpi_block_id(kpi_id: str) -> str:
    return f"kpi:{kpi_id}"


def is_current_period(end: date, max_date: date | None, after_days: int) -> bool:
    """A period is current when it ends within ``after_days`` of the latest data (or later)."""
    return max_date is None or end >= max_date - timedelta(days=after_days)


class BlockResolver:
    """Block states for one user + tenant + period. Uses DashboardService for roles, data and rules."""

    def __init__(self, dash, user, tc, st: dict, spec: BlockSpec | None = None):
        self.dash, self.user, self.tc, self.st = dash, user, tc, st
        self.catalog = load_catalog(tc.catalog)
        self.spec = spec or load_spec()
        self.avail = st.get("availability", {})
        self.panels = dash.panels_for(user, tc, st)
        self.profiles = load_profiles()

    # ------------------------------------------------------------ helpers
    def _role_label(self, lg: str, role: str) -> str:
        return ui(lg, f"role_{role}")

    def _no_data_reason(self, lg: str, reasons: list[str]) -> tuple[str, list[str]]:
        what = sorted({t(lg, self.catalog.data_area(r)) for r in reasons}) or [ui(lg, "rpt_more_data")]
        return ui(lg, "rpt_state_no_data", what=", ".join(what)), list(reasons)

    def _kpi_state(self, k: Kpi, lg: str, current: bool, layer_current_only: bool) -> tuple[str, str, list[str]]:
        if not self.user.has_role(k.min_role):
            return "locked", ui(lg, "rpt_state_locked", role=self._role_label(lg, k.min_role)), []
        a = self.avail.get(k.id) or {"available": False, "reasons": ["not probed yet"]}
        if not a.get("available"):
            reason, detail = self._no_data_reason(lg, a.get("reasons") or [])
            return "no_data", reason, detail
        if not current and layer_current_only:
            return "historical", ui(lg, "rpt_state_forward"), []
        if not current and k.period_mode == "snapshot":
            return "historical", ui(lg, "rpt_state_snapshot"), []
        return "available", "", []

    def _insight_state(self, b: dict, lg: str, current: bool, compare: str, layer_current_only: bool
                       ) -> tuple[str, str, list[str]]:
        ok, reasons = False, []
        if b.get("requires_any_kpi"):
            vis = [self.catalog.by_id[k] for k in b["requires_any_kpi"] if k in self.catalog.by_id]
            ok = any(self.user.has_role(k.min_role) and (self.avail.get(k.id) or {}).get("available") for k in vis)
            reasons = [r for k in vis for r in (self.avail.get(k.id) or {}).get("reasons", [])]
        elif b.get("requires_panel"):
            ok = bool(self.panels.get(b["requires_panel"]))
        elif b.get("requires_panel_or_kpi"):
            req = b["requires_panel_or_kpi"]
            ok = bool(self.panels.get(req.get("panel"))) or any(
                self.user.has_role(self.catalog.by_id[k].min_role) and (self.avail.get(k) or {}).get("available")
                for k in req.get("kpi", []) if k in self.catalog.by_id)
        if not ok:
            if reasons:
                reason, detail = self._no_data_reason(lg, reasons)
                return "no_data", reason, detail
            return "no_data", ui(lg, "rpt_state_no_rules"), []
        if not current and (b.get("current_only") or layer_current_only):
            return "historical", ui(lg, "rpt_state_forward"), []
        if b.get("needs_comparison") and compare == "none":
            return "needs_comparison", ui(lg, "rpt_state_comparison"), []
        return "available", "", []

    def _default(self, chapter_id: str, block: dict, fallback: bool) -> bool:
        cfg = self.profiles
        if not cfg:
            return fallback
        p = next((x for x in cfg.get("profiles", []) if x.get("id") == cfg.get("default")), None)
        return profile_selects(p, chapter_id, block) if p else fallback

    # ------------------------------------------------------------ main
    def resolve(self, start: date, end: date, compare: str, lg: str) -> dict:
        max_d = date.fromisoformat(self.st["max_date"]) if self.st.get("max_date") else None
        current = is_current_period(end, max_d, self.spec.historical_after_days)
        admin = self.user.is_platform_admin
        sec_titles = {s.id: s for s in self.catalog.sections}
        chapters: dict[str, dict] = {}
        for c in self.spec.chapters:
            if c.get("section"):
                s = sec_titles[c["section"]]
                title, desc = t(lg, s.title), t(lg, s.desc)
            else:
                title, desc = ui(lg, c["title_key"]), ui(lg, c["desc_key"]) if c.get("desc_key") else ""
            chapters[c["id"]] = {"id": c["id"], "layer": c["layer"], "title": title, "desc": desc, "blocks": []}
        layer_cur = {ly["id"]: bool(ly.get("current_only")) for ly in self.spec.layers}

        def add(chapter_id: str, block: dict, state: tuple[str, str, list[str]]) -> None:
            st_, reason, detail = state
            block.update(state=st_, reason=reason)
            if admin and detail:
                block["detail"] = detail          # technical reasons (tables / columns) only for platform admins
            chapters[chapter_id]["blocks"].append(block)

        for b in self.spec.insights:
            lc = layer_cur[chapters[b["chapter"]]["layer"]]
            st = self._insight_state(b, lg, current, compare, lc)
            add(b["chapter"], {"id": b["id"], "kind": "insight", "title": ui(lg, b["title_key"]),
                               "desc": ui(lg, b["desc_key"]) if b.get("desc_key") else "", "viz": b.get("viz"),
                               "group": "summary", "snapshot": False,
                               "default_on": st[0] == "available" and self._default(b["chapter"], {"id": b["id"], "group": "summary"}, True)}, st)
        ch_of = self.spec.chapter_of_section
        for k in self.catalog.kpis:
            ch = ch_of[k.sections[0]]
            lc = layer_cur[chapters[ch]["layer"]]
            st = self._kpi_state(k, lg, current, lc)
            add(ch, {"id": kpi_block_id(k.id), "kind": "kpi", "kpi": k.id, "title": t(lg, k.title),
                     "desc": t(lg, k.desc), "viz": k.viz, "group": GROUP_OF_VIZ.get(k.viz, "charts"),
                     "snapshot": k.period_mode == "snapshot",
                     "default_on": st[0] == "available" and self._default(ch, {"id": kpi_block_id(k.id), "group": GROUP_OF_VIZ.get(k.viz, "charts")}, ch in self.spec.default_chapters)}, st)

        layers, counts = [], {s: 0 for s in STATES}
        by_layer: dict[str, list[dict]] = {}
        for c in chapters.values():
            by_layer.setdefault(c.pop("layer"), []).append(c)
        for ly in self.spec.layers:
            chs = [c for c in by_layer.get(ly["id"], []) if c["blocks"]]
            for c in chs:
                for b in c["blocks"]:
                    counts[b["state"]] += 1
                c["counts"] = {s: sum(b["state"] == s for b in c["blocks"]) for s in STATES}
            off = bool(ly.get("current_only")) and not current
            layers.append({"id": ly["id"], "title": ui(lg, ly["title_key"]), "current_only": bool(ly.get("current_only")),
                           "note": ui(lg, "rpt_state_forward") if off else "", "chapters": chs})
        counts["total"] = sum(counts[s] for s in STATES)
        profiles = []
        for p in (self.profiles or {}).get("profiles", []):
            sel = [b["id"] for ly in layers for c in ly["chapters"] for b in c["blocks"]
                   if b["state"] == "available" and profile_selects(p, c["id"], b)]
            profiles.append({"id": p["id"], "title": ui(lg, p["title_key"]),
                             "desc": ui(lg, p["desc_key"]) if p.get("desc_key") else "", "blocks": sel})
        return {"profiles": profiles, "default_profile": (self.profiles or {}).get("default"),
                "period": {"start": start.isoformat(), "end": end.isoformat(), "compare": compare,
                           "max_date": max_d.isoformat() if max_d else None, "is_current": current,
                           "note": "" if current else ui(lg, "rpt_period_past")},
                "layers": layers, "counts": counts}


def all_block_ids(catalog: Catalog, spec: BlockSpec | None = None) -> set[str]:
    spec = spec or load_spec()
    return {b["id"] for b in spec.insights} | {kpi_block_id(k.id) for k in catalog.kpis}
