"""Evaluation and early-warning rules loaded from a config file (v12).

``config/rules/<catalog>.json`` says WHICH rules run and HOW (check type, KPI or data set, thresholds, weight,
link, roles, titles). The Python code keeps only what must stay code:

* the check logic (``vdash.evaluation`` - ``CHECKS`` lists every check type and its parameters with ranges),
* the data sets checks read (``RULE_DATA`` in the catalog module - SQL with a mandatory tenant filter).

Loading validates everything and collects *all* problems in one message. The file is re-read when it
changes (checked on use, at most every ``CHECK_SECONDS``); an invalid edit is rejected and the last valid rules
stay in use, with the errors kept on the catalog (``rules_error``) for the admin status page and logs.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from pathlib import Path

from .config import config_dir

log = logging.getLogger("vdash.rules")
CHECK_SECONDS = 2.0
ROLES = ["viewer", "manager", "tenant_admin", "platform_admin"]
ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
ALLOWED_KEYS = {"id", "enabled", "panel", "check", "kpi", "data", "title", "weight", "link", "show_names_to",
                "visible_to", "basis", "params"}
_lock = threading.Lock()
_state: dict[str, dict] = {}     # catalog name -> {"sig": (mtime, size), "checked": t}


class RulesConfigError(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


def rules_path(catalog_name: str) -> Path:
    base = os.environ.get("VDASH_RULES_DIR")
    return (Path(base) if base else config_dir() / "rules") / f"{catalog_name}.json"


def parse_rules(doc: dict, catalog) -> list:
    """Turn the rule file's JSON into ``Rule`` objects, or raise ``RulesConfigError`` listing every problem."""
    from .catalog import Column, Rule
    from .evaluation import CHECKS, check_params
    errors: list[str] = []
    if not isinstance(doc, dict) or not isinstance(doc.get("rules"), list):
        raise RulesConfigError(["the file must be a JSON object with a \"rules\" list"])
    if doc.get("catalog") not in (None, catalog.name):
        errors.append(f"file is for catalog '{doc.get('catalog')}', not '{catalog.name}'")
    rules, seen = [], set()
    for i, r in enumerate(doc["rules"]):
        where = f"rule #{i + 1}" + (f" ({r.get('id')})" if isinstance(r, dict) and r.get("id") else "")
        if not isinstance(r, dict):
            errors.append(f"{where}: must be an object")
            continue
        extra = {k for k in r if not k.startswith("_")} - ALLOWED_KEYS
        if extra:
            errors.append(f"{where}: unknown field(s) {', '.join(sorted(extra))}")
        rid = r.get("id")
        if not isinstance(rid, str) or not ID_RE.match(rid):
            errors.append(f"{where}: 'id' must be letters, digits or _ (starting with a letter)")
            continue
        if rid in seen:
            errors.append(f"{where}: duplicate id")
            continue
        seen.add(rid)
        check = r.get("check")
        if check not in CHECKS:
            errors.append(f"{where}: unknown check '{check}' (allowed: {', '.join(CHECKS)})")
            continue
        spec = CHECKS[check]
        panel = r.get("panel", spec["panel"])
        if panel != spec["panel"]:
            errors.append(f"{where}: check '{check}' belongs to panel '{spec['panel']}', not '{panel}'")
        enabled = r.get("enabled", True)
        if not isinstance(enabled, bool):
            errors.append(f"{where}: 'enabled' must be true or false")
        kpi = data = None
        items_cols, items_floor, sql, requires = [], "viewer", {}, []
        if spec.get("needs_kpi"):
            kpi = r.get("kpi")
            k = catalog.by_id.get(kpi)
            if k is None:
                errors.append(f"{where}: unknown kpi '{kpi}'")
            elif k.viz != "metric" or not k.good:
                errors.append(f"{where}: kpi '{kpi}' cannot be judged up/down (needs a single figure with a good direction)")
            if "data" in r:
                errors.append(f"{where}: check 'kpi_change' takes 'kpi', not 'data'")
        else:
            data = r.get("data")
            rd = catalog.rule_data.get(data)
            if rd is None:
                usable = [n for n, d in catalog.rule_data.items() if d.check == check]
                errors.append(f"{where}: unknown data set '{data}' (for check '{check}' use: {', '.join(usable) or 'none'})")
            elif rd.check != check:
                errors.append(f"{where}: data set '{data}' is for check '{rd.check}', not '{check}'")
            else:
                sql, requires, items_cols, items_floor = rd.sql, rd.requires, rd.items_cols, rd.items_min_role
            if "kpi" in r:
                errors.append(f"{where}: only check 'kpi_change' takes 'kpi'")
        params, perr = check_params(check, r.get("params") or {}, where)
        errors += perr
        weight = r.get("weight", 1.0)
        if isinstance(weight, bool) or not isinstance(weight, (int, float)) or not (0 < weight <= 100):
            errors.append(f"{where}: 'weight' must be a number between 0 and 100")
            weight = 1.0
        link = r.get("link", kpi)
        if link is not None and link not in catalog.by_id:
            errors.append(f"{where}: unknown link KPI '{link}'")
        visible_to = r.get("visible_to", "viewer")
        names_to = r.get("show_names_to", items_floor)
        for field_, val in (("visible_to", visible_to), ("show_names_to", names_to)):
            if val not in ROLES:
                errors.append(f"{where}: '{field_}' must be one of {', '.join(ROLES)}")
        if names_to in ROLES and ROLES.index(names_to) < ROLES.index(items_floor):
            errors.append(f"{where}: 'show_names_to' cannot be lower than '{items_floor}' for this data (personal data)")
        basis = r.get("basis", spec["basis"])
        if panel == "warning" and basis not in ("trend", "forecast", "model", "data"):
            errors.append(f"{where}: 'basis' must be trend, forecast, model or data")
        title = r.get("title")
        title_i18n: dict[str, str] = {}
        if isinstance(title, dict):
            title_i18n = {str(k): str(v) for k, v in title.items() if isinstance(v, str) and v.strip()}
            title = title_i18n.get("en")
            if not title:
                errors.append(f"{where}: a per-language 'title' needs an \"en\" entry")
        elif title is not None and (not isinstance(title, str) or not title.strip()):
            errors.append(f"{where}: 'title' must be text")
        if not title:
            if kpi and kpi in catalog.by_id:
                title = catalog.by_id[kpi].title
            elif not errors or all(not e.startswith(where) for e in errors):
                errors.append(f"{where}: 'title' is required for check '{check}'")
        rules.append(Rule(
            id=rid, panel=panel, title=title or rid, fn=check, kpi=kpi, sql=dict(sql), requires=list(requires),
            min_role=visible_to if visible_to in ROLES else "viewer",
            items_min_role=names_to if names_to in ROLES else items_floor, basis=basis if panel == "warning" else None,
            params=params, weight=float(weight), link_kpi=link,
            items_cols=[Column(c.key, c.label, c.fmt) for c in items_cols], enabled=enabled is not False,
            data=data, title_i18n=title_i18n))
    if errors:
        raise RulesConfigError(errors)
    try:
        catalog.validate_rules(rules)
    except ValueError as exc:
        raise RulesConfigError([str(exc)])
    return rules


def load_rules_file(path: Path, catalog) -> tuple[list, str]:
    raw = path.read_bytes()
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RulesConfigError([f"{path.name} is not valid JSON: {exc}"])
    return parse_rules(doc, catalog), hashlib.sha256(raw).hexdigest()[:12]


def refresh_rules(catalog, force: bool = False) -> bool:
    """(Re)load the catalog's rules when the file changed. Returns True when new rules were applied."""
    path = rules_path(catalog.name)
    now = time.time()
    with _lock:
        st = _state.setdefault(catalog.name, {"sig": None, "checked": 0.0})
        if not force and now - st["checked"] < CHECK_SECONDS:
            return False
        st["checked"] = now
        try:
            s = path.stat()
            sig = (s.st_mtime_ns, s.st_size)
        except FileNotFoundError:
            sig = None
        if not force and sig == st["sig"]:
            return False
        st["sig"] = sig
        if sig is None:
            catalog.rules_error = [f"rule file not found: {path}"]
            log.error("evaluation/warning rules: %s - no rules active", catalog.rules_error[0])
            if force:
                catalog.set_rules([], "")
                catalog.rules_error = [f"rule file not found: {path}"]
            return False
        try:
            rules, version = load_rules_file(path, catalog)
        except RulesConfigError as exc:
            catalog.rules_error = exc.errors
            kept = f"keeping the previous {len(catalog.rules)} rule(s)" if catalog.rules else "no rules active"
            log.error("rule file %s rejected (%s): %s", path.name, kept, " | ".join(exc.errors))
            return False
        catalog.set_rules(rules, version)
        log.info("loaded %d evaluation/warning rule(s) from %s (version %s)", len(rules), path.name, version)
        return True


def validate_tenant_alerts(catalog, alerts: dict, where: str) -> list[str]:
    """Problems in a tenant's tenants.json ``alerts`` block (unknown rules, bad parameters)."""
    from .evaluation import check_params
    errors = []
    if not isinstance(alerts, dict):
        return [f"{where}: 'alerts' must be an object"]
    ids = {r.id for r in catalog.rules}
    for key in ("disabled", "enabled"):
        vals = alerts.get(key, []) or []
        if not isinstance(vals, list):
            errors.append(f"{where}: 'alerts.{key}' must be a list of rule ids")
            continue
        errors += [f"{where}: alerts.{key} names unknown rule '{v}'" for v in vals if v not in ids]
    params = alerts.get("params", {}) or {}
    if not isinstance(params, dict):
        return errors + [f"{where}: 'alerts.params' must be an object"]
    for rid, p in params.items():
        rule = catalog.rules_by_id.get(rid)
        if rule is None:
            errors.append(f"{where}: alerts.params names unknown rule '{rid}'")
            continue
        _, perr = check_params(rule.fn, {**rule.params, **(p or {})}, f"{where} alerts.params.{rid}")
        errors += perr
    return errors
