"""Pure helpers of the answer writer: classify tool results, build the model payload (numbers as placeholders),
substitute the values back, check a written answer, write the rules answer, pick the table / chart."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .messages import msg

PLACEHOLDER = re.compile(r"\{(T\d+)(?:\.(\d+))?\}")
NUMBER = re.compile(r"\d[\d.,]*\s?%?")
MAX_FACTS = 12
TABLE_TOOLS = {"kpi_breakdown", "top_movers", "forecast", "customer_segments", "explain_change", "drill"}
CHART_TOOLS = {"kpi_trend", "kpi_breakdown", "top_movers"}


@dataclass
class Part:
    """One tool result, classified."""
    tool: str
    kind: str                       # ok | empty | error
    code: str = ""                  # error code
    fixed: str = ""                 # fixed wording (empty / error)
    result: dict = field(default_factory=dict)


def classify(tool: str, res: dict, lang: str, snapshot_ids=()) -> Part:
    if not isinstance(res, dict):
        return Part(tool, "error", "failed", msg("failed", lang), {})
    if not res.get("ok"):
        code = res.get("error") or "failed"
        meta = res.get("meta") or {}
        kpi = res.get("kpi") or (meta.get("kpi") or {}).get("id")
        if code == "forbidden":
            return Part(tool, "error", code, msg("forbidden", lang), res)
        if code == "not_available":
            if tool == "forecast":
                return Part(tool, "error", code, msg("no_forecast", lang), res)
            missing = res.get("missing")
            return Part(tool, "error", code, msg("not_available", lang, missing=msg("missing_in", lang, areas=missing) if missing else ""), res)
        if code == "no_data":
            if tool == "customer_segments":
                return Part(tool, "empty", code, msg("empty_segments", lang), res)
            if kpi in set(snapshot_ids):
                return Part(tool, "empty", code, msg("empty_now", lang), res)
            return Part(tool, "empty", code, msg("no_data", lang), res)
        return Part(tool, "error", code, msg("failed", lang), res)
    if not res.get("facts"):
        key = "empty_segments" if tool == "customer_segments" else "no_data"
        return Part(tool, "empty", "no_data", msg(key, lang), res)
    return Part(tool, "ok", "", "", res)


def split_cells(cells: dict) -> tuple[list[str], dict, dict]:
    """A ranked row's cells -> (names written as they are, {column index: display text of a figure}, {column index: column name})."""
    names, nums, cols = [], {}, {}
    for i, (col, v) in enumerate((cells or {}).items(), 1):
        s = "" if v is None else str(v)
        if re.search(r"\d", s):
            nums[i] = s
            cols[i] = str(col)
        elif s:
            names.append(s)
    return names, nums, cols


MONTHS = {"en": "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec", "de": "Jan Feb Mär Apr Mai Jun Jul Aug Sep Okt Nov Dez",
          "vi": "tháng 1|tháng 2|tháng 3|tháng 4|tháng 5|tháng 6|tháng 7|tháng 8|tháng 9|tháng 10|tháng 11|tháng 12"}
ISO = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def nice_dates(text: str, lang: str = "en") -> str:
    """'2026-08-02 – 2026-08-31' -> '2 Aug – 31 Aug 2026' (the year only once, at the end)."""
    ds = ISO.findall(text or "")
    if not ds:
        return text
    sep = "|" if lang == "vi" else " "
    names = MONTHS.get(lang, MONTHS["en"]).split(sep)

    def one(d, year=True):
        y, m, dd = d
        name = names[int(m) - 1]
        out = f"{int(dd)} {name}" if lang != "de" else f"{int(dd)}. {name}"
        return out + (f" {y}" if year else "")
    if len(ds) == 1:
        return one(ds[0])
    same_year = ds[0][0] == ds[-1][0]
    return f"{one(ds[0], not same_year)} – {one(ds[-1])}"


def unsigned(s: str) -> str:
    """The direction is told in words, so a change is shown without its sign ('-31.3%' -> '31.3%')."""
    return re.sub(r"^[\s+\-−–]+", "", str(s))


def payload_facts(parts: list[Part], lang: str = "en") -> tuple[list[dict], dict]:
    """(facts shown to the model, {placeholder: text}). Every figure is a placeholder; names are plain text."""
    shown, values = [], {}
    for p in parts:
        if p.kind != "ok":
            continue
        for f in p.result["facts"]:
            if len(shown) >= MAX_FACTS:
                break
            fid = f["id"]
            if f.get("cells"):
                names, nums, cols = split_cells(f["cells"])
                for i, s in nums.items():
                    values[f"{fid}.{i}"] = s
                shown.append({"id": fid, "tool": p.tool, "rank": f.get("rank"), "label": f.get("label"), "names": names,
                              "figures": {cols[i]: "{" + f"{fid}.{i}" + "}" for i in nums}})
                continue
            shown_text = f.get("value") if f.get("value") not in (None, "") else f.get("text")
            if f.get("value_text"):                              # a change fact (compare_periods): "-31.3%"
                shown_text = f["value_text"]
            values[fid] = str(shown_text if shown_text is not None else "")
            signed = f.get("unit") in ("signed_pct", "signed_pts") or bool(f.get("value_text")) \
                or bool(re.match(r"^\s*[+\-−–]", values[fid]))      # "-₫222.09M": the direction is told in words
            if signed:
                values[fid] = unsigned(values[fid])
            label = str(f.get("label") or "")
            per = str(f.get("period") or "")
            if " vs " in per:                                    # "A vs B": the period names say it; digits stay out
                label = label.replace(per, "").replace(f"({per})", "").strip(" -:")
                per = ""
            if per and per in label:                             # the dates stay out of the model's sight as digits
                label = label.replace(f"({per})", "").replace(per, "").strip(" -:")
            e = {"id": fid, "tool": p.tool, "label": label, "show": "{" + fid + "}"}
            if signed and isinstance(f.get("raw"), (int, float)):
                e["direction"] = "up" if f["raw"] > 0 else "down" if f["raw"] < 0 else "flat"
            if f.get("raw_effect") is not None or f.get("bridge"):
                e["meaning"] = "how much of the revenue change this factor accounts for (money)"
            elif f.get("breakdown"):
                e["meaning"] = "how much this item's own value changed between the periods"
            if per:
                values[f"{fid}.9"] = nice_dates(per, lang)
                e["period"] = "{" + f"{fid}.9" + "}"
            if f.get("change") and f.get("previous"):          # kpi_value with a comparison: previous value + change
                values[f"{fid}.1"], values[f"{fid}.2"] = str(f["previous"]), unsigned(f["change"])
                e["previous_value"], e["change"] = "{" + f"{fid}.1" + "}", "{" + f"{fid}.2" + "}"
                if f.get("compare_period"):
                    values[f"{fid}.3"] = nice_dates(str(f["compare_period"]), lang)
                    e["previous_period"] = "{" + f"{fid}.3" + "}"
                rc = f.get("raw_change")
                if isinstance(rc, (int, float)):
                    e["direction"] = "up" if rc > 0 else "down" if rc < 0 else "flat"
            shown.append(e)
    return shown, values


def substitute(text: str, values: dict) -> tuple[str, list[str]]:
    """Replace placeholders by their values. -> (text, unknown placeholders)."""
    unknown = []

    def rep(m):
        key = m.group(1) + (f".{m.group(2)}" if m.group(2) else "")
        if key in values:
            return values[key]
        unknown.append(key)
        return m.group(0)
    return PLACEHOLDER.sub(rep, text), unknown


def stray_numbers(text: str) -> list[str]:
    """Figures typed by the model outside placeholders (counts 0-10 without a unit are fine)."""
    bare = PLACEHOLDER.sub(" ", text)
    out = []
    for m in NUMBER.finditer(bare):
        tok = m.group(0).strip()
        if tok.isdigit() and int(tok) <= 10:
            continue
        out.append(tok)
    return out


def rules_text(parts: list[Part], lang: str) -> str:
    """The answer written without the AI: the tool's own formatted lines, the best few."""
    lines, period = [], ""
    for p in parts:
        if p.kind != "ok":
            continue
        period = period or ((p.result.get("meta") or {}).get("period") or {}).get("text", "")
        for f in p.result["facts"]:
            t = (f.get("text") or "").strip().rstrip(".")
            if t and t not in lines:
                lines.append(t)
    lines = lines[:3]
    text = ". ".join(lines) + ("." if lines else "")
    return f"{text} ({period})" if period and text else text


def caveats_of(parts: list[Part]) -> list[str]:
    out = []
    for p in parts:
        if p.kind == "ok":
            for f in p.result["facts"]:
                n = f.get("note")
                if isinstance(n, str) and n.strip() and n.strip() not in out:
                    out.append(n.strip())
    return out[:2]


def pick_table(parts: list[Part], rows: int = 5) -> dict | None:
    for p in parts:
        if p.kind == "ok" and p.tool in TABLE_TOOLS:
            for t in p.result.get("tables") or []:
                if t.get("rows"):
                    return {"title": t.get("title"), "columns": t.get("columns"), "rows": t["rows"][:rows],
                            "total_rows": t.get("total_rows", len(t["rows"]))}
    return None


def pick_chart(parts: list[Part], reveal: bool = False) -> dict | None:
    """The first chart worth drawing. Plain tables are not charts. A chart that holds real personal names is sent only
    when ``reveal`` is on (the reader may see them); otherwise it is dropped."""
    import json
    for p in parts:
        if p.kind == "ok" and p.tool in CHART_TOOLS:
            real = [str(v) for v in ((p.result.get("meta") or {}).get("aliases") or {}).values()]
            for c in p.result.get("charts") or []:
                if c.get("viz") in ("table", "metric", None):
                    continue
                blob = json.dumps(c.get("result"), ensure_ascii=False, default=str)
                if not reveal and any(r and r in blob for r in real):
                    continue
                return {"kpi": c.get("kpi"), "viz": c.get("viz"), "title": c.get("title"), "result": c.get("result")}
    return None


FULL_MONTHS = {
    "en": "January February March April May June July August September October November December".split(),
    "de": "Januar Februar März April Mai Juni Juli August September Oktober November Dezember".split(),
    "vi": [f"tháng {i}" for i in range(1, 13)]}


def period_name(spec: dict | None, lang: str = "en") -> str:
    """'August' for a period that is exactly one calendar month (words only, no digits); '' otherwise."""
    from datetime import date, timedelta
    try:
        a, b = date.fromisoformat(spec["start"]), date.fromisoformat(spec["end"])
    except (TypeError, KeyError, ValueError):
        return ""
    if a.day == 1 and a.year == b.year and a.month == b.month and (b + timedelta(days=1)).day == 1:
        return FULL_MONTHS.get(lang, FULL_MONTHS["en"])[a.month - 1]
    return ""


def period_names(steps: list[dict], lang: str = "en") -> dict:
    out = {}
    for s in steps or []:
        a = s.get("args") or {}
        for key, label in (("period", "period"), ("period_a", "period"), ("period_b", "other_period")):
            n = period_name(a.get(key), lang)
            if n and label not in out:
                out[label] = n
    return out


def alias_map(parts: list[Part]) -> dict:
    """{alias: real name} of every personal name the tools replaced by 'Customer A' ..."""
    out = {}
    for p in parts:
        if p.kind == "ok":
            out.update({str(a): str(r) for a, r in ((p.result.get("meta") or {}).get("aliases") or {}).items()})
    return out


def reveal_text(text, aliases: dict):
    """Put the real names back (after the AI has written and the checks have passed); longest alias first."""
    if not isinstance(text, str) or not aliases:
        return text
    for a in sorted(aliases, key=len, reverse=True):
        text = re.sub(rf"(?<![\w]){re.escape(a)}(?![\w])", lambda m, r=aliases[a]: r, text)
    return text


def reveal_table(table: dict | None, aliases: dict) -> dict | None:
    if not table or not aliases:
        return table
    rows = []
    for r in table.get("rows") or []:
        if isinstance(r, dict):
            rows.append({k: reveal_text(v, aliases) for k, v in r.items()})
        elif isinstance(r, (list, tuple)):
            rows.append([reveal_text(v, aliases) for v in r])
        else:
            rows.append(r)
    return {**table, "rows": rows}
