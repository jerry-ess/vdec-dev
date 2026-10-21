"""Pure helpers of the answer writer: classify tool results, build the model payload (numbers as placeholders),
substitute the values back, check a written answer, write the rules answer, pick the table / chart."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .messages import msg

PLACEHOLDER = re.compile(r"\{(T\d+)(?:\.(\d+))?\}")
NUMBER = re.compile(r"\d[\d.,]*\s?%?")
MAX_FACTS = 12
RANK_COLUMNS = {"#", "rank", "no", "no.", "stt",
                # the RFM segment score ("555") is a code, not an amount: it must never be told as the customer's value
                "segment score (recency · frequency · spend)", "điểm phân khúc (gần đây · tần suất · chi tiêu)",
                "segmentwert (aktualität · häufigkeit · ausgaben)"}
NAME_COLUMNS = {"name", "label", "customer", "kunde", "khách hàng", "product", "produkt", "sản phẩm"}
TABLE_TOOLS = {"kpi_breakdown", "top_movers", "forecast", "customer_segments", "explain_change", "drill", "custom_query"}
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
        if tool == "custom_query" and code in ("bad_args", "too_big"):
            return Part(tool, "error", code, msg("custom_too_big" if code == "too_big" else "custom_failed", lang), res)
        if code == "no_data":
            if tool == "custom_query":
                return Part(tool, "empty", code, msg("no_rows", lang), res)
            if tool == "customer_segments":
                return Part(tool, "empty", code, msg("empty_segments", lang), res)
            if kpi in set(snapshot_ids):
                return Part(tool, "empty", code, msg("empty_now", lang), res)
            return Part(tool, "empty", code, msg("no_data", lang), res)
        return Part(tool, "error", code, msg("failed", lang), res)
    if not res.get("facts"):
        key = "empty_segments" if tool == "customer_segments" else "no_rows" if tool == "custom_query" else "no_data"
        return Part(tool, "empty", "no_data", msg(key, lang), res)
    return Part(tool, "ok", "", "", res)


def split_cells(cells: dict) -> tuple[list[str], dict, dict]:
    """A ranked row's cells -> (names written as they are, {column index: display text of a figure}, {column index: column name})."""
    names, nums, cols = [], {}, {}
    for i, (col, v) in enumerate((cells or {}).items(), 1):
        if str(col).strip().lower() in RANK_COLUMNS:          # the position in the list is told as "rank", not as a figure
            continue
        s = "" if v is None else str(v)
        if str(col).strip().lower() in NAME_COLUMNS:          # "Sat 19:00" is a name, whatever digits it holds
            if s:
                names.append(s)
            continue
        if re.search(r"\d", s) and not re.match(r"^\s*[A-Za-zÀ-ỹ]", s) and (len(re.sub(r"[^A-Za-zÀ-ỹ]", "", s)) < 4 or re.match(r"^[\s₫$€+\-−–(]*\d", s)):      # a figure; "Dầu gội 500ml", "Thứ 7" and "Thứ 7 19:00" are names that hold digits (a figure starts with a digit, a sign or a currency)
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
    only_dates = not re.sub(r"[\d\-\s–—to]+|\bto\b|\bbis\b|\bđến\b", "", text or "")
    if not only_dates:                                  # a sentence holding dates: change the dates, keep the sentence
        return ISO.sub(lambda m: one(m.groups()), text)
    if len(ds) == 1:
        return one(ds[0])
    same_year = ds[0][0] == ds[-1][0]
    return f"{one(ds[0], not same_year)} – {one(ds[-1])}"


def tidy(text: str) -> str:
    """The period wording of a snapshot already says 'as of 3 Oct 2026'; a sentence that adds its own 'as of' must not repeat it."""
    return re.sub(r"\b(as of|tính đến|tinh den|Stand)\s+\1\b", r"\1", text or "", flags=re.I)


def unsigned(s: str) -> str:
    """The direction is told in words, so a change is shown without its sign ('-31.3%' -> '31.3%')."""
    return re.sub(r"^[\s+\-−–]+", "", str(s))


def ratio_text(r: float, lang: str) -> str:
    s = f"{r:.1f}"
    return (s.replace(".", ",") if lang in ("vi", "de") else s) + "×"


def payload_facts(parts: list[Part], lang: str = "en", keep: set | None = None, period_of: dict | None = None) -> tuple[list[dict], dict]:
    """(facts shown to the model, {placeholder: text}). Every figure is a placeholder; names are plain text.
    ``keep``: only these fact ids are shown (the brief chose them); None shows the first MAX_FACTS."""
    shown, values = [], {}
    for p in parts:
        if p.kind != "ok":
            continue
        for f in p.result["facts"]:
            if len(shown) >= MAX_FACTS:
                break
            fid = f["id"]
            if keep is not None and fid not in keep:
                continue
            if f.get("unusual"):                              # a finished sentence about unusual days: shown alone
                t = str(f.get("text") or "").strip()
                values[fid] = t if t[-1:] in ".!?" else t + "."
                shown.append({"id": fid, "tool": p.tool, "label": "", "show": "{" + fid + "}", "statement": True})
                continue
            if f.get("cells"):
                names, nums, cols = split_cells(f["cells"])
                for i, s in nums.items():
                    values[f"{fid}.{i}"] = s
                e = {"id": fid, "tool": p.tool, "rank": f.get("rank"), "label": f.get("label"), "names": names,
                     "figures": {cols[i]: "{" + f"{fid}.{i}" + "}" for i in nums}}
                if p.tool in ("kpi_breakdown", "customer_segments") and f.get("rank") is not None:
                    ranked = [x for x in p.result["facts"] if x.get("cells") and x.get("rank") is not None]
                    if len(ranked) > 1:                      # the model cannot see the numbers: say who is first and last
                        e["position"] = "largest" if f["rank"] == min(x["rank"] for x in ranked) else \
                            "smallest" if f["rank"] == max(x["rank"] for x in ranked) else "middle"
                shown.append(e)
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
            if f.get("name") and f.get("rank") is not None and not f.get("now_text"):
                e["name"] = str(f["name"])                      # a ranked item: its name is copied as it is (it may hold digits: "Sat 19:00")
                e["rank"] = f["rank"]
                if p.tool in ("kpi_breakdown", "customer_segments"):        # a ranked list shown as plain facts (the weekday x hour heatmap): the answer must name who is first
                    ranked = [x for x in p.result["facts"] if x.get("name") and x.get("rank") is not None and not x.get("cells")]
                    if len(ranked) > 1:
                        e["names"] = [str(f["name"])]
                        e["position"] = "largest" if f["rank"] == min(x["rank"] for x in ranked) else \
                            "smallest" if f["rank"] == max(x["rank"] for x in ranked) else "middle"
            if f.get("highest") and f.get("lowest"):            # a trend: the extremes with their own dates (never mixed across series)
                e["meaning"] = ("the most recent value; highest and lowest are the extremes of this one series, each with the date it happened; "
                                "never pair an extreme of one series with a figure of another; the main value is the most recent point only: "
                                "name it with latest_at, never with the whole period")
                for key, tag, ph in (("highest", "highest", 11), ("highest_at", "highest_at", 12), ("lowest", "lowest", 13),
                                     ("lowest_at", "lowest_at", 14), ("average", "average", 15), ("latest_at", "latest_at", 18)):
                    if f.get(key):
                        values[f"{fid}.{ph}"] = str(f[key])
                        e[tag] = "{" + f"{fid}.{ph}" + "}"
                if isinstance(f.get("raw_highest"), (int, float)) and f.get("raw_average"):
                    values[f"{fid}.17"] = ratio_text(f["raw_highest"] / f["raw_average"], lang)
                    e["highest_vs_average"] = "{" + f"{fid}.17" + "}"
                if f.get("second_half_vs_first_half"):
                    values[f"{fid}.16"] = unsigned(f["second_half_vs_first_half"])
                    e["second_half_vs_first_half"] = "{" + f"{fid}.16" + "}"
                    if isinstance(f.get("raw_half_change"), (int, float)):      # the direction belongs to the half-vs-half figure only,
                        e["half_direction"] = "up" if f["raw_half_change"] > 0 else "down" if f["raw_half_change"] < 0 else "flat"   # not to the extremes
            if f.get("value") in (None, "") and not f.get("value_text") and f.get("text") and re.search(r"\d", str(f["text"])):
                e["label"] = label if not re.search(r"\d", label) else ""
                values[fid] = str(values[fid]).rstrip() if str(values[fid]).rstrip()[-1:] in ".!?" else str(values[fid]).rstrip() + "."
                e["statement"] = True        # a finished sentence with its own figures: show it alone, never build a sentence around it
            if signed and isinstance(f.get("raw"), (int, float)):
                e["direction"] = "up" if f["raw"] > 0 else "down" if f["raw"] < 0 else "flat"
            rn, rb = f.get("raw_now"), f.get("raw_before")
            if f.get("now_text") and f.get("before_text"):       # a mover: "X rose by {change}, from {before} to {now}"
                values[f"{fid}.5"], values[f"{fid}.6"] = str(f["before_text"]), str(f["now_text"])
                e["name"] = f.get("name")
                e["before"], e["now"], e["change"] = "{" + f"{fid}.5" + "}", "{" + f"{fid}.6" + "}", "{" + fid + "}"
                e["direction"] = "up" if f.get("direction") == "rose" else "down" if f.get("direction") == "fell" else "flat"
                values[fid] = unsigned(values[fid])
                if f.get("pct_text"):
                    values[f"{fid}.7"] = unsigned(str(f["pct_text"]))
                    e["change_pct"] = "{" + f"{fid}.7" + "}"
            elif isinstance(rn, (int, float)) and isinstance(rb, (int, float)) and not f.get("raw_effect") and not f.get("breakdown"):
                d = rn - rb                                   # explain_change's total change: the direction is a fact, not the question's premise
                e["direction"] = "up" if d > 0 else "down" if d < 0 else "flat"
                e["meaning"] = "the total change between the periods, in money; its direction is given - it may differ from what the question assumes"
                values[fid] = unsigned(values[fid])
            if f.get("raw_effect") is not None or f.get("bridge"):
                e["meaning"] = "how much of the revenue change this factor accounts for (money); direction up = it added to revenue, down = it took away from revenue"
                if isinstance(f.get("raw_effect"), (int, float)):
                    e["direction"] = "up" if f["raw_effect"] > 0 else "down" if f["raw_effect"] < 0 else "flat"
                    values[fid] = unsigned(values[fid])
            elif f.get("breakdown"):
                e["meaning"] = "how much this item's own value changed between the periods"
            if per or (period_of and fid in period_of):
                values[f"{fid}.9"] = period_of[fid] if period_of and fid in period_of else nice_dates(per, lang)
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
    stm = [e for e in shown if e.get("statement")]
    if stm:                                                # a finding that only repeats a figure already shown adds nothing
        other = [str(values[e["id"]]) for e in shown if not e.get("statement") and e.get("id") in values and len(str(values[e["id"]])) >= 5]
        drop = {e["id"] for e in stm if any(o in str(values.get(e["id"], "")) for o in other)}
        shown = [e for e in shown if e.get("id") not in drop]
        for k in drop:
            values.pop(k, None)
    for k, v in list(values.items()):                      # ISO dates inside text facts ("starts 2026-10-04") read as dates
        if isinstance(v, str) and ISO.search(v):
            values[k] = nice_dates(v, lang)
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
    out = PLACEHOLDER.sub(rep, text)
    return re.sub(r"([.!?])\.(?!\.)", r"\1", out), unknown      # a finished sentence already ends with its full stop


def stray_numbers(text: str) -> list[str]:
    """Figures typed by the model outside placeholders (counts 0-10 without a unit are fine)."""
    bare = PLACEHOLDER.sub(" ", text)
    out = []
    for m in NUMBER.finditer(bare):
        tok = m.group(0).strip()
        if tok.rstrip(".,;:").isdigit() and int(tok.rstrip(".,;:")) <= 10:     # "tháng 8." - a month number at the end of a sentence
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
    if period and text and period.lower() not in text.lower():
        return f"{text} ({period})"
    return text


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
                return {"kpi": c.get("kpi"), "viz": c.get("viz"), "title": c.get("title"), "def": c.get("def"), "result": c.get("result")}
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


HIGH_WORDS = {"en": r"highest|best|strongest|most|top|largest|biggest|leads?|first",
              "vi": r"cao nhất|tốt nhất|mạnh nhất|nhiều nhất|đứng đầu|lớn nhất",
              "de": r"höchste[nrs]?|beste[nrs]?|stärkste[nrs]?|meiste[nrs]?|größte[nrs]?|führt|erste[nrs]?"}
LOW_WORDS = {"en": r"lowest|worst|weakest|least|smallest|bottom|last|fewest",
             "vi": r"thấp nhất|yếu nhất|kém nhất|ít nhất|nhỏ nhất|cuối",
             "de": r"niedrigste[nrs]?|schlechteste[nrs]?|schwächste[nrs]?|wenigste[nrs]?|kleinste[nrs]?|letzte[nrs]?"}


def ordering_errors(text: str, positions: dict, lang: str = "en") -> list[tuple[str, str]]:
    """The writer never sees the figures, so a sentence that calls the smallest item the highest (or the largest the
    lowest) is wrong. -> [(name, 'smallest' | 'largest')]."""
    out = []
    if not positions or not isinstance(text, str):
        return out
    hi = re.compile(rf"\b(?:{HIGH_WORDS.get(lang, HIGH_WORDS['en'])})\b", re.I)
    lo = re.compile(rf"\b(?:{LOW_WORDS.get(lang, LOW_WORDS['en'])})\b", re.I)
    for sent in re.split(r"(?<=[.!?])\s+", PLACEHOLDER.sub("", text)):
        named = [n for n in positions if n and n in sent]
        if len(named) != 1:                      # a sentence about several items is too ambiguous to judge
            continue
        n = named[0]
        if positions[n] == "smallest" and hi.search(sent):
            out.append((n, "smallest"))
        elif positions[n] == "largest" and lo.search(sent):
            out.append((n, "largest"))
    return out


def previous_brief(previous: dict | None, lang: str = "en") -> dict | None:
    """What the writer may know of the turn before (the conversation state's brief): the question without any long
    figures or years, which tools it used and which periods it named. Never its answer, figures or personal names."""
    if not isinstance(previous, dict) or not previous.get("question"):
        return None
    q = re.sub(r"\b\d{3,}\b", "", str(previous["question"])).strip()[:160]
    out = {"question": q}
    if previous.get("tools"):
        out["tools"] = [str(x) for x in previous["tools"]][:3]
    names = [n for n in (period_name(p, lang) for p in previous.get("periods") or []) if n]
    if names:
        out["period"] = names[0]
    return out


UP_WORDS = {"en": r"rose|rise|rises|risen|rising|up|higher|increased?|increase|grew|grown|growth|gained?|climbed|improved|added|adds|boosted|more",
            "vi": r"tăng|cao hơn|tốt lên|cải thiện|thêm",
            "de": r"gestiegen|stieg|steigt|höher|anstieg|zugenommen|gewachsen|gewonnen|verbessert|brachte|erhöht|stiegen|stieg"}
DOWN_WORDS = {"en": r"fell|fall|falls|fallen|falling|down|lower|decreased?|decrease|dropped?|declined?|decline|lost|shrank|dipped|took away|takes away|reduced|reduces|fewer|cut",
              "vi": r"giảm|thấp hơn|sụt|xấu đi|mất|lấy đi",
              "de": r"gesunken|sank|sinkt|niedriger|rückgang|abgenommen|gefallen|verloren|geschrumpft|kostete|verringert|fiel|fielen|zurückgegangen|rückläufig"}


NEGATION = re.compile(r"\b(?:no|none|not|nothing|kein|keine|keiner|nicht|không|chẳng)\b", re.I)


def direction_errors(text: str, directions: dict, lang: str = "en") -> list[tuple[str, str]]:
    """The question may assume the wrong direction ("why did revenue fall?" when it rose). A sentence whose figures all
    went one way (per the facts) must not use only words for the other way. -> [(placeholder, 'up' | 'down')]."""
    out = []
    if not directions or not isinstance(text, str):
        return out
    up = re.compile(rf"\b(?:{UP_WORDS.get(lang, UP_WORDS['en'])})\b", re.I)
    dn = re.compile(rf"\b(?:{DOWN_WORDS.get(lang, DOWN_WORDS['en'])})\b", re.I)
    for sent in re.split(r"(?<=[.!?])\s+", text):
        keys = [m.group(1) + (f".{m.group(2)}" if m.group(2) else "") for m in PLACEHOLDER.finditer(sent)]
        ds = {directions.get(k) or directions.get(k.split(".")[0]) for k in keys}
        ds.discard(None)
        if ds == {"flat"}:                         # "flat at 0.0%, even though it rose 0.0 pts": no direction word for a figure that did not move
            if not NEGATION.search(sent) and (up.search(PLACEHOLDER.sub("", sent)) or dn.search(PLACEHOLDER.sub("", sent))):
                out.append((keys[0], "flat"))
            continue
        ds.discard("flat")
        if len(ds) != 1:
            continue
        d = ds.pop()
        if NEGATION.search(sent):                  # "no product gained revenue ..." - the sentence denies the direction
            continue
        has_up, has_dn = bool(up.search(PLACEHOLDER.sub("", sent))), bool(dn.search(PLACEHOLDER.sub("", sent)))
        if d == "up" and has_dn and not has_up:
            out.append((keys[0], "up"))
        elif d == "down" and has_up and not has_dn:
            out.append((keys[0], "down"))
    return out


def statement_errors(text: str, statements) -> list[str]:
    """A finished sentence from a finding ({T3}) must stand alone: no words built around it. -> [placeholder]."""
    out = []
    for sent in re.split(r"(?<=[.!?])\s+", text or ""):
        keys = [m.group(1) + (f".{m.group(2)}" if m.group(2) else "") for m in PLACEHOLDER.finditer(sent)]
        for k in keys:
            if k in statements and len(re.findall(r"[^\W\d_]+", PLACEHOLDER.sub(" ", sent))) > 3:
                out.append(k)
    return out


def lead_expected(ranked_names: dict, question: str, lang: str = "en") -> str | None:
    """For a ranking question, the name the answer must open with: the first-ranked item ("which sells the most?") or,
    when the question asks for the lowest / worst, the last-ranked one among those shown. None = no rule."""
    if len(ranked_names) < 2:
        return None
    q = (question or "").lower()
    lo = re.search(rf"\b(?:{LOW_WORDS.get(lang, LOW_WORDS['en'])})\b", q, re.I)
    hi = re.search(rf"\b(?:{HIGH_WORDS.get(lang, HIGH_WORDS['en'])})\b", q, re.I)
    if lo and hi:
        return None                                  # "best and worst": both are named, in either order
    pick = max if lo else min
    return pick(ranked_names.items(), key=lambda kv: kv[1])[0]


def first_ranked_name(text: str, ranked_names: dict) -> str | None:
    """The ranked item named first in the text."""
    hits = [(text.find(n), n) for n in ranked_names if n and text.find(n) >= 0]
    return min(hits)[1] if hits else None
