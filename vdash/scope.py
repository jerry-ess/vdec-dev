"""Store-level data scope (v99): a user with ``locations`` in users.json sees only those stores' data.

How it works - one place, no per-KPI changes. ``ScopedSource`` wraps a DataSource; every query is rewritten so each
fact_/mart_ table that has a ``location_sk`` column is replaced by a filtered sub-select, and ``dim_location`` is reduced
to the user's stores. A fact_/mart_ table WITHOUT ``location_sk`` cannot be split by store: a query that reads one raises
``ScopeUnavailable`` (the KPI is then hidden from that user - never shown as chain totals).
Dimension tables (product, customer, date ...) are left as they are: they hold no store figures on their own.
"""
from __future__ import annotations

import re

_ID = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
_KEYWORDS = {"on", "where", "join", "left", "right", "inner", "outer", "cross", "full", "group", "order", "having", "limit",
             "union", "using", "natural", "window", "offset", "fetch", "except", "intersect", "select", "set", "values"}
_LOC = "location_sk"


class ScopeUnavailable(Exception):
    """The query reads a table that cannot be restricted to the user's stores."""


def _pub(name: str) -> str:
    return name.lower().split(".")[-1]


class ScopedSource:
    """A DataSource restricted to some stores. Everything not overridden is forwarded to the real source."""

    def __init__(self, ds, tenant_id: str, location_ids: list[str]):
        ids = [str(i) for i in location_ids]
        if not ids or not all(_ID.match(i) for i in ids):
            raise ValueError("invalid store id in users.json 'locations'")
        self._ds, self.tenant_id, self.location_ids = ds, tenant_id, sorted(ids)
        self.scope_key = "@loc:" + ",".join(self.location_ids)
        self._in = ", ".join("'" + i + "'" for i in self.location_ids)

    def __getattr__(self, name):
        return getattr(self._ds, name)

    def _tables(self) -> dict[str, set[str]]:
        return {_pub(t): {c.lower() for c in cols} for t, cols in self._ds.columns().items()}

    def rewrite(self, sql: str, need_store: bool = False) -> str:
        cols = self._tables()
        scoped = {t for t, c in cols.items() if _LOC in c and t != "dim_location"}
        blocked = {t for t, c in cols.items() if t.startswith(("fact_", "mart_")) and _LOC not in c}
        loc_sub = (f"SELECT dim_location_sk FROM dim_location WHERE tenant_id = :tenant_id "
                   f"AND dim_location_id IN ({self._in})")
        done: dict[str, int] = {}

        def repl(m: re.Match) -> str:
            kw, table, sp, alias = m.groups()
            low = _pub(table)
            real_alias = alias if alias and alias.lower() not in _KEYWORDS else None
            rest = "" if real_alias else (sp or "") + (alias or "")      # give back text that was not an alias
            if low == "dim_location":
                sub = f"(SELECT * FROM {table} WHERE dim_location_id IN ({self._in}))"
            elif low in scoped:
                sub = f"(SELECT * FROM {table} WHERE {_LOC} IN ({loc_sub}))"
            elif low in blocked:
                raise ScopeUnavailable(f"{table} cannot be split by store")
            else:
                return m.group(0)
            done[low] = done.get(low, 0) + 1
            return f"{kw} {sub} {real_alias or table}{rest}"

        out = re.sub(r"\b(FROM|JOIN)\s+([A-Za-z_][\w.]*)(\s+(?:AS\s+)?)?([A-Za-z_]\w*)?", repl, sql, flags=re.I)
        # safety net: a table named anywhere else (comma join, odd syntax) is refused rather than shown unfiltered
        for t in scoped | blocked | {"dim_location"}:
            n = len(re.findall(r"\b" + re.escape(t) + r"\b", sql, flags=re.I))
            if n != done.get(t, 0):
                raise ScopeUnavailable(f"{t} is used in a form that cannot be split by store")
        if need_store and not (set(done) & (scoped | {"dim_location"})):
            raise ScopeUnavailable("reads no store-level table (would show chain totals)")
        return out

    def query(self, sql: str, params: dict | None = None) -> list[dict]:
        p = dict(params or {})
        p.setdefault("tenant_id", self.tenant_id)
        return self._ds.query(self.rewrite(sql), p)

    def allows(self, *sqls: str | None) -> bool:
        try:
            for s in sqls:
                if s:
                    self.rewrite(s, need_store=True)
        except ScopeUnavailable:
            return False
        return True
