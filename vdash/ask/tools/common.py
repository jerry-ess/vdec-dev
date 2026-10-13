"""Shared pieces of the Ask tool layer (talk-to-data Step 2).

Every tool gets a ``ToolContext`` built by the SERVER from the signed-in user and the tenant in the URL; the tenant
and the user never come from tool arguments (or from the model). A tool returns ONE shape:

    {"ok": True,  "tool": name,
     "facts":  [{"id": "T1", "label": ..., "value": "₫1.2B", "unit": "currency", "period": "...", "text": "...", "raw": 1.2e9}],
     "tables": [...], "charts": [{"kpi", "viz", "title", "result"}],
     "meta":   {"period": {...}, "kpi": {...}, "role_applied": ..., "data_version": ..., "aliases": {alias: real}}}
    {"ok": False, "tool": name, "error": "not_available" | "forbidden" | "unknown_kpi" | "bad_args" | "no_data" | "failed",
     "message": "...", "meta": {...}}

``text`` is already written in the user's language (numbers, currency and dates formatted by ``vdash.fmt``), so the
answer writer copies it instead of formatting numbers itself.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from ...catalog import Kpi, load_catalog
from ...engine import PeriodRequest, presets
from ...fmt import fmt_date, fmt_value
from ...i18n import norm_lang, t, ui
from ...reports.story_rules import load_story_config
from ...services import ServiceError

MAX_TOP_N = 20
MAX_SERIES = 3
DEFAULT_PERIOD = "last_12_months"
COMPARE_MODES = ("none", "previous", "yoy")


class ToolError(Exception):
    """A typed failure a tool wants to report (becomes ``{"ok": False, ...}``)."""

    def __init__(self, code: str, message: str, **extra):
        super().__init__(message)
        self.code, self.message, self.extra = code, message, extra


def _num(v: Any) -> float | None:
    try:
        x = None if v is None else float(v)
        return None if x is None or x != x or x in (float("inf"), float("-inf")) else x
    except (TypeError, ValueError):
        return None


@dataclass
class ToolContext:
    dash: Any                      # DashboardService
    user: Any                      # security.User (from the session)
    tc: Any                        # TenantConfig (from the URL, checked against the session)
    st: dict                       # tenant state: availability, max_date, data_version ...
    lang: str = "en"
    catalog: Any = None
    aliases: dict = field(default_factory=dict)       # alias -> real name, filled while a turn runs
    _alias_of: dict = field(default_factory=dict)
    _counts: dict = field(default_factory=dict)
    _fact_n: int = 0

    @classmethod
    def build(cls, dash, user, tc, lang: str = "en") -> "ToolContext":
        if not user.can_access_tenant(tc.tenant_id):  # the tool layer never trusts the caller to have checked
            raise ServiceError(404, "Not found")
        _, _, _, st = dash.visible(user, tc)          # raises 503 "preparing" until the tenant's data is checked
        return cls(dash=dash, user=user, tc=tc, st=st, lang=norm_lang(lang), catalog=load_catalog(tc.catalog))

    # ---------------------------------------------------------------- facts
    @property
    def currency(self) -> str:
        return self.tc.currency or ""

    def tx(self, key: str, **kw) -> str:
        """A short phrase from the UI bundle (all four languages): ``{value}`` / ``{n}`` are filled in."""
        out = ui(self.lang, key)
        for k, v in kw.items():
            out = out.replace("{" + k + "}", str(v))
        return out

    def fact_id(self) -> str:
        self._fact_n += 1
        return f"T{self._fact_n}"

    def money(self, v, fmt: str | None, compact: bool = True) -> str | None:
        if _num(v) is None:
            return None
        return fmt_value(v, fmt or "number", self.lang, self.currency, compact=compact)

    def fact(self, label: str, value, fmt: str | None, period: str = "", text: str | None = None, **extra) -> dict:
        shown = self.money(value, fmt)
        f = {"id": self.fact_id(), "label": label, "value": shown, "unit": fmt or "number", "period": period,
             "text": text or (f"{label}: {shown}" if shown is not None else label), "raw": _num(value)}
        f.update({k: v for k, v in extra.items() if v not in (None, "", [], {})})
        return f

    # ---------------------------------------------------------------- personal names
    def personal_kind(self, kpi: Kpi) -> str | None:
        """'customer' / 'staff' when the KPI reads a column that holds people's names (same rule as reports)."""
        cols = (load_story_config().get("personal_data") or {}).get("columns") or {}
        for r in kpi.requires:
            if r.column in cols:
                return cols[r.column]
        return None

    def alias(self, kind: str, real) -> str:
        real = str(real)
        key = (kind, real)
        if key not in self._alias_of:
            n = self._counts.get(kind, 0)
            self._counts[kind] = n + 1
            a = ui(self.lang, f"story_alias_{kind}").replace("{n}", _letters(n))
            self._alias_of[key] = a
            self.aliases[a] = real
        return self._alias_of[key]

    # ---------------------------------------------------------------- periods
    def anchor(self) -> date:
        return date.fromisoformat(self.st["max_date"]) if self.st.get("max_date") else date.today()

    def period(self, spec, compare: str = "none", grain: str = "auto") -> PeriodRequest:
        """``spec``: preset id ('last_30_days', 'prev_month', ...) or {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"} or
        {"preset": id}. Presets are relative to the latest loaded date of the tenant, never to the wall clock."""
        if compare not in COMPARE_MODES:
            raise ToolError("bad_args", f"compare must be one of {', '.join(COMPARE_MODES)}")
        spec = spec or DEFAULT_PERIOD
        if isinstance(spec, str):
            spec = {"preset": spec}
        if not isinstance(spec, dict):
            raise ToolError("bad_args", "period must be a preset id or {start, end}")
        if spec.get("preset"):
            min_d = date.fromisoformat(self.st["min_date"]) if self.st.get("min_date") else self.anchor()
            by_id = {p["id"]: p for p in presets(self.anchor(), min_d)}
            p = by_id.get("all" if spec["preset"] == "all_time" else spec["preset"])
            if p is None:
                raise ToolError("bad_args", f"unknown period '{spec['preset']}'", allowed=sorted(by_id))
            start, end = p["start"], p["end"]
        else:
            start, end = spec.get("start"), spec.get("end")
            if not start or not end:
                raise ToolError("bad_args", "period needs 'preset' or both 'start' and 'end'")
        try:
            return self.dash.period(start, end, grain, compare, self.st)
        except ServiceError as exc:
            raise ToolError("bad_args", exc.detail)

    def period_text(self, req: PeriodRequest) -> str:
        return f"{fmt_date(req.start, self.lang)} – {fmt_date(req.end, self.lang)}"

    def period_meta(self, req: PeriodRequest) -> dict:
        return {"start": req.start.isoformat(), "end": req.end.isoformat(), "text": self.period_text(req),
                "compare": req.compare}

    # ---------------------------------------------------------------- KPI access
    def reasons_text(self, reasons: list[str]) -> str:
        """Business wording for 'what data is missing' (never table or column names)."""
        areas = []
        for r in reasons:
            a = t(self.lang, self.catalog.data_area(r))
            if a not in areas:
                areas.append(a)
        return ", ".join(areas)

    def check_kpi(self, kpi: Kpi) -> None:
        """Role gate + availability for this tenant. Raises ToolError (never returns partial data)."""
        if not self.user.has_role(kpi.min_role):
            raise ToolError("forbidden", ui(self.lang, "ask_tool_forbidden") if _has_ui(self.lang, "ask_tool_forbidden")
                            else "Your role does not allow this question.", kpi=kpi.id)
        a = (self.st.get("availability") or {}).get(kpi.id, {"available": False, "reasons": ["not probed yet"]})
        if not a.get("available"):
            raise ToolError("not_available", "This business has no data for that yet.", kpi=kpi.id,
                            reasons=a.get("reasons") or [], missing=self.reasons_text(a.get("reasons") or []))

    def title(self, kpi: Kpi) -> str:
        return t(self.lang, kpi.title)

    def run(self, kpi: Kpi, req: PeriodRequest) -> dict:
        res = self.dash.run_many(self.user, self.tc, [kpi], req, self.st)[kpi.id]
        res = self.dash.translate(kpi, res, self.lang, self.user)
        if res.get("type") == "error":
            raise ToolError("failed", "The data could not be read right now.", kpi=kpi.id)
        return res

    def meta(self, req: PeriodRequest | None = None, kpi: Kpi | None = None, **extra) -> dict:
        m: dict = {"role_applied": self.user.role, "data_version": self.st.get("data_version"),
                   "data_through": self.st.get("max_date"), "language": self.lang}
        if req is not None:
            m["period"] = self.period_meta(req)
        if kpi is not None:
            m["kpi"] = {"id": kpi.id, "title": self.title(kpi), "fmt": kpi.fmt, "viz": kpi.viz,
                        "template_kpis": list(kpi.template_kpis), "description": t(self.lang, kpi.desc) if kpi.desc else ""}
        m["aliases"] = dict(self.aliases)
        m.update(extra)
        return m


def _has_ui(lang: str, key: str) -> bool:
    return ui(lang, key) != key


def _letters(n: int) -> str:
    s = ""
    n += 1
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def change_text(ctx: ToolContext, kpi: Kpi, value, previous) -> tuple[str | None, float | None]:
    """(text, raw) of the change: percentage points for % KPIs, percent otherwise (same rule as the report)."""
    from ...fmt import fmt_arg
    v, p = _num(value), _num(previous)
    if v is None or p is None:
        return None, None
    if kpi.fmt == "pct":
        d = v - p
        return fmt_arg({"v": d, "fmt": "signed_pts"}, ctx.lang, ctx.currency), d
    if p == 0:
        return None, None
    d = 100.0 * (v - p) / abs(p)
    return fmt_arg({"v": d, "fmt": "signed_pct"}, ctx.lang, ctx.currency), d
