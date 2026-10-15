"""Template-level words people use for a KPI (English, Vietnamese, German). They belong to the catalog, never to a tenant:
config/ask_synonyms/<catalog>.json -> {kpi_id: [words]}."""
from __future__ import annotations

import json

from ...config import ROOT

_CACHE: dict = {}


def load_synonyms(catalog_name: str) -> dict:
    path = ROOT / "config" / "ask_synonyms" / f"{catalog_name}.json"
    if not path.exists():
        return {}
    m = path.stat().st_mtime
    hit = _CACHE.get(catalog_name)
    if hit and hit[0] == m:
        return hit[1]
    d = {k: v for k, v in json.loads(path.read_text(encoding="utf-8")).items() if not k.startswith("_")}
    _CACHE[catalog_name] = (m, d)
    return d


def core_ids(catalog_name: str) -> list[str]:
    """The few KPIs that answer a vague question ("how did we do?"): always offered to the planner."""
    path = ROOT / "config" / "ask_synonyms" / f"{catalog_name}.json"
    if not path.exists():
        return []
    return list(json.loads(path.read_text(encoding="utf-8")).get("_core") or [])
