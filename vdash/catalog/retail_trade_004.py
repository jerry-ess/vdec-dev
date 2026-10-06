"""KPI catalog for TMPL-RETAIL-TRADE-004 (retail / trading warehouses).

Definitions follow the template's canonical semantic layer (metric expressions,
the 'cancelled lines are not revenue' filter, KPI ids in ``template_kpis``),
but the catalog is owned by the dashboard: the dashboard never reads the
Step 1-9 pipeline at runtime.

SQL rules (keep every query portable across SQLite / PostgreSQL / ClickHouse):
* every query binds ``:tenant_id`` (validated at load time);
* dates are compared through ``dim_date.full_date`` with ``:start`` / ``:end``;
  monthly marts use ``:start_month`` / ``:end_month`` ('YYYY-MM');
* time buckets come from ``@BUCKET@`` (dim_date columns - no dialect date functions);
* ratios use ``* 1.0`` / ``100.0 *`` and ``NULLIF(x, 0)`` (no integer division);
* dimension BOOLEAN flags are used as bare predicates, fact ``*_flag`` INTEGERs as ``= 1``;
* select aliases never reuse a physical column name (ClickHouse alias rules).
"""
from __future__ import annotations

from . import (Column, RuleData, DetailColumn, DetailSource, Drill, Export, ExportColumn, ExportFilter, ExportJoin, Kpi, Req,
               Section, Series)

TEMPLATE_ID = "TMPL-RETAIL-TRADE-004"

# v51: the left menu is shown in these blocks (id, title), in this order; every section names its block.
GROUPS = [("overview", "Overview"), ("sales", "Sales"), ("growth", "Customers & Growth"),
          ("operations", "Operations & Finance")]

SECTIONS = [
    Section("overview", "Executive Overview", "home", "Headline performance and business evaluation for the selected period", group="overview"),
    Section("sales", "Sales Performance", "trending", "Revenue, orders, basket and timing patterns", group="sales"),
    Section("products", "Products & Assortment", "box", "What sells, concentration, margin and momentum", group="sales"),
    Section("customers", "Customers & Loyalty", "users", "Acquisition, retention, value and loyalty", group="growth"),
    Section("channels", "Channels & Stores", "store", "Channel, store, traffic and online funnel", group="sales"),
    Section("marketing", "Marketing & Promotions", "megaphone", "Campaign efficiency and promotion impact", group="growth"),
    Section("operations", "Fulfilment & Returns", "truck", "Order status, delivery and returns", group="operations"),
    Section("inventory", "Inventory & Supply", "layers", "Stock health, perishables and suppliers", group="operations"),
    Section("finance", "Finance & Cash", "wallet", "Collections, cash flow, receivables, payables, costs", group="operations"),
    Section("workforce", "Workforce", "badge", "Staff sales, labour cost and productivity", group="operations"),
    Section("experience", "Customer Experience", "star", "NPS, CSAT, ratings and service interactions", group="growth"),
    Section("external", "Market & Events", "globe", "Seasonal events, weather, competitors and economy", group="growth"),
    Section("ai", "AI Insights", "spark", "Forecasts, risks and early warnings for the days ahead", group="overview"),
]

# ------------------------------------------------------------------ helpers
FSL, FSO = "fact_sales_transaction_line", "fact_sales_order_header"
VALID = "COALESCE(f.line_status, 'completed') <> 'cancelled'"
VALID_O = "COALESCE(o.order_status, 'completed') <> 'cancelled'"
DOW = {"1": "Mon", "2": "Tue", "3": "Wed", "4": "Thu", "5": "Fri", "6": "Sat", "7": "Sun"}


def frm(table: str, a: str, date_col: str = "date_sk") -> str:
    return f"{table} {a} JOIN dim_date d ON d.dim_date_sk = {a}.{date_col} AND d.tenant_id = {a}.tenant_id"


def dj(table: str, alias: str, fk: str, a: str = "f") -> str:
    return f" JOIN {table} {alias} ON {alias}.{table}_sk = {a}.{fk} AND {alias}.tenant_id = {a}.tenant_id"


def wh(a: str, extra: str | None = None) -> str:
    w = f"{a}.tenant_id = :tenant_id AND d.full_date BETWEEN :start AND :end"
    return f"{w} AND {extra}" if extra else w


def R(table: str, column: str, min_distinct: int = 1, nonzero: bool = False, where: str = "", label: str = "") -> Req:
    return Req(table, column, min_distinct, nonzero, where, label)


def fact_metric(id, title, expr, table, a, sections, *, extra=None, joins="", fmt="currency", good="up",
                req=None, req_any=None, spark=True, **kw) -> Kpi:
    """Period-filtered metric on a fact table (+ auto sparkline)."""
    src = frm(table, a) + joins
    w = wh(a, extra)
    return Kpi(
        id=id, title=title, sections=sections, viz="metric", fmt=fmt, good=good,
        sql=f"SELECT {expr} AS v FROM {src} WHERE {w}",
        spark_sql=(f"SELECT MIN(d.full_date) AS k, {expr} AS v FROM {src} WHERE {w} GROUP BY @BUCKET@ ORDER BY k"
                   if spark else None),
        requires=req or [], requires_any=req_any or [], **kw)


def sales_metric(id, title, expr, sections, **kw) -> Kpi:
    kw.setdefault("req", [R(FSL, "line_total")])
    return fact_metric(id, title, expr, FSL, "f", sections, extra=kw.pop("extra", VALID), **kw)


DRILL_SOURCE = {FSL: "sales_lines", FSO: "orders"}   # facts whose charts can open their detail rows


def _auto_drill(kw: dict, table: str, extra, **how) -> None:
    if "drill" not in kw and table in DRILL_SOURCE:
        kw["drill"] = Drill(DRILL_SOURCE[table], extra, **how)


def fact_series(id, title, cols, table, a, sections, series, *, extra=None, joins="", **kw) -> Kpi:
    """Bucketed time series on a fact table: cols = 'SUM(x) AS v, ...'."""
    _auto_drill(kw, table, extra, by_bucket=True)
    return Kpi(id=id, title=title, sections=sections, viz="timeseries", series=series,
               sql=(f"SELECT MIN(d.full_date) AS k, {cols} FROM {frm(table, a)}{joins} WHERE {wh(a, extra)} "
                    f"GROUP BY @BUCKET@ ORDER BY k"), **kw)


def fact_pivot(id, title, value, label, table, a, sections, *, extra=None, joins="", fmt="currency", **kw) -> Kpi:
    """Bucketed series split by a category (top N categories become series)."""
    _auto_drill(kw, table, extra, by_bucket=True, key_expr=label)
    return Kpi(id=id, title=title, sections=sections, viz="timeseries", fmt=fmt, post="pivot",
               series=[Series("v", "", "line", fmt)],
               sql=(f"SELECT MIN(d.full_date) AS k, {label} AS label, {value} AS v FROM {frm(table, a)}{joins} "
                    f"WHERE {wh(a, extra)} GROUP BY @BUCKET@, {label} ORDER BY k"), **kw)


def fact_breakdown(id, title, value, label, table, a, sections, *, viz="donut", extra=None, joins="",
                   fmt="currency", limit=None, order="v DESC, label", extra_cols="", **kw) -> Kpi:
    lim = f" LIMIT {limit}" if limit else ""
    _auto_drill(kw, table, extra, key_expr=label)
    return Kpi(id=id, title=title, sections=sections, viz=viz, fmt=fmt,
               sql=(f"SELECT {label} AS label, {value} AS v{extra_cols} FROM {frm(table, a)}{joins} "
                    f"WHERE {wh(a, extra)} GROUP BY {label} ORDER BY {order}{lim}"), **kw)


def mart_metric(id, title, expr, mart, sections, *, extra=None, fmt="currency", good="up", req=None, **kw) -> Kpi:
    w = "m.tenant_id = :tenant_id AND m.year_month BETWEEN :start_month AND :end_month"
    if extra:
        w += f" AND {extra}"
    return Kpi(id=id, title=title, sections=sections, viz="metric", fmt=fmt, good=good, period_mode="monthly",
               sql=f"SELECT {expr} AS v FROM {mart} m WHERE {w}",
               spark_sql=f"SELECT m.year_month AS k, {expr} AS v FROM {mart} m WHERE {w} GROUP BY m.year_month ORDER BY k",
               requires=req or [], **kw)


def mart_series(id, title, cols, mart, sections, series, *, extra=None, joins="", **kw) -> Kpi:
    w = "m.tenant_id = :tenant_id AND m.year_month BETWEEN :start_month AND :end_month"
    if extra:
        w += f" AND {extra}"
    return Kpi(id=id, title=title, sections=sections, viz="timeseries", period_mode="monthly", series=series,
               sql=f"SELECT m.year_month AS k, {cols} FROM {mart} m{joins} WHERE {w} GROUP BY m.year_month ORDER BY k",
               **kw)


def asof(table: str, a: str) -> str:
    """Restrict a snapshot fact to its latest snapshot date on/before the period end."""
    return (f"d.full_date = (SELECT MAX(d2.full_date) FROM {table} x2 JOIN dim_date d2 "
            f"ON d2.dim_date_sk = x2.date_sk AND d2.tenant_id = x2.tenant_id "
            f"WHERE x2.tenant_id = :tenant_id AND d2.full_date <= :end)")


def asof_where(table: str, a: str, extra: str | None = None) -> str:
    w = f"{a}.tenant_id = :tenant_id AND {asof(table, a)}"
    return f"{w} AND {extra}" if extra else w


REV = "SUM(f.line_total)"
ORD = "COUNT(DISTINCT f.order_id)"
GP = "SUM(COALESCE(f.gross_profit, f.line_total - f.line_cost))"
COST_REQ = [R(FSL, "gross_profit", nonzero=True), R(FSL, "line_cost", nonzero=True)]
# Revenue of the lines whose cost is known: margin % is measured on this, so lines without a cost never read as 100% margin.
COSTED = "SUM(CASE WHEN f.gross_profit IS NOT NULL OR f.line_cost IS NOT NULL THEN f.line_total END)"
# Customer types that mean "a business buying for its work" (English and Vietnamese wording, case-insensitive).
B2B_TYPES = ("b2b", "business", "wholesale", "corporate", "company", "trade", "reseller", "distributor", "enterprise",
             "doanh nghiệp", "công ty", "đại lý", "bán buôn", "bán sỉ")
_B2B_IN = ", ".join("'" + t + "'" for t in B2B_TYPES)
B2B_PRED = f"LOWER(customer_type) IN ({_B2B_IN})"               # on dim_customer itself (availability probe)
B2B_C = f"LOWER(c.customer_type) IN ({_B2B_IN})"                 # joined as c
B2B_REQ = R("dim_customer", "customer_type", where=B2B_PRED, label="No business customers in the data")
PROD = dj("dim_product", "p", "product_sk")
CUST = dj("dim_customer", "c", "customer_sk")

KPIS: list[Kpi] = []
add = KPIS.append

# =============================================================== SALES / OVERVIEW
add(sales_metric("net_revenue", "Net revenue", REV, ["overview", "sales"], template_kpis=["KPI-SALES-001"],
                 desc="Sum of line totals after discounts, excluding cancelled lines."))
add(sales_metric("orders", "Orders", ORD, ["overview", "sales"], fmt="int", req=[R(FSL, "order_id")],
                 template_kpis=["KPI-SALES-002"]))
add(sales_metric("aov", "Average order value", f"{REV} * 1.0 / NULLIF({ORD}, 0)", ["overview", "sales"],
                 template_kpis=["KPI-SALES-002"]))
add(sales_metric("units_sold", "Units sold", "SUM(f.qty)", ["overview", "sales"], fmt="number",
                 req=[R(FSL, "qty")]))
add(sales_metric("active_customers", "Active customers", "COUNT(DISTINCT f.customer_sk)",
                 ["overview", "customers"], fmt="int", req=[R(FSL, "customer_sk")], template_kpis=["KPI-CUST-010"]))
add(sales_metric("gross_profit", "Gross profit", GP, ["overview", "products"], req=[], req_any=COST_REQ,
                 template_kpis=["KPI-MARGIN-001"]))
add(sales_metric("gross_margin_pct", "Gross margin", f"100.0 * {GP} / NULLIF({COSTED}, 0)", ["overview", "products"],
                 fmt="pct", req=[], req_any=COST_REQ, template_kpis=["KPI-MARGIN-001"]))
add(sales_metric("gross_sales", "Gross sales (before discount)", "SUM(f.gross_amount)", ["sales"],
                 req=[R(FSL, "gross_amount")]))
add(sales_metric("upt", "Units per transaction", f"SUM(f.qty) * 1.0 / NULLIF({ORD}, 0)", ["sales"], fmt="ratio",
                 req=[R(FSL, "qty")], template_kpis=["KPI-SALES-009"]))
add(sales_metric("avg_selling_price", "Average selling price", "SUM(f.gross_amount) * 1.0 / NULLIF(SUM(f.qty), 0)",
                 ["sales", "products"], req=[R(FSL, "gross_amount"), R(FSL, "qty")]))
add(sales_metric("discount_rate", "Discount rate", "100.0 * SUM(f.discount_amount) / NULLIF(SUM(f.gross_amount), 0)",
                 ["sales", "marketing"], fmt="pct", good="down", req=[R(FSL, "discount_amount", nonzero=True)],
                 template_kpis=["KPI-SALES-010"]))
add(sales_metric("full_price_share", "Full-price revenue share",
                 f"100.0 * SUM(CASE WHEN COALESCE(f.discount_amount, 0) = 0 THEN f.line_total ELSE 0 END) / NULLIF({REV}, 0)",
                 ["sales"], fmt="pct", req=[R(FSL, "discount_amount", nonzero=True)], template_kpis=["KPI-PRICE-003"]))
add(fact_metric("cancelled_value", "Cancelled order value", "SUM(f.line_total)", FSL, "f", ["sales", "operations"],
                extra="f.line_status = 'cancelled'", good="down", req=[R(FSL, "line_status", 2)],
                template_kpis=["KPI-SALES-011"]))
add(fact_metric("cancellation_rate", "Order cancellation rate",
                "100.0 * SUM(CASE WHEN o.order_status = 'cancelled' THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0)",
                FSO, "o", ["sales", "operations"], fmt="pct", good="down", req=[R(FSO, "order_status", 2)],
                template_kpis=["KPI-SALES-011"]))
add(sales_metric("freight_ratio", "Freight cost ratio", f"100.0 * SUM(f.shipping_amount) / NULLIF({REV}, 0)",
                 ["operations"], fmt="pct", good="down", req=[R(FSL, "shipping_amount", nonzero=True)],
                 template_kpis=["KPI-MARGIN-005"]))

add(fact_series("revenue_trend", "Revenue & orders trend", f"{REV} AS v, {ORD} AS v2", FSL, "f",
                ["overview", "sales"], [Series("v", "Net revenue", "bar", "currency", trend=True),
                                        Series("v2", "Orders", "line", "int", axis=1)],
                extra=VALID, span=2, requires=[R(FSL, "line_total")], template_kpis=["KPI-SALES-001"]))
add(fact_series("aov_trend", "Average order value & units per order",
                f"{REV} * 1.0 / NULLIF({ORD}, 0) AS v, SUM(f.qty) * 1.0 / NULLIF({ORD}, 0) AS v2", FSL, "f",
                ["sales"], [Series("v", "AOV", "line", "currency", trend=True), Series("v2", "Units / order", "line", "ratio", axis=1)],
                extra=VALID, span=2, requires=[R(FSL, "line_total")], template_kpis=["KPI-SALES-002", "KPI-SALES-009"]))
add(Kpi("revenue_by_dow", "Average daily revenue by weekday", ["overview", "sales"], "bar",
        f"SELECT d.day_of_week AS label, {REV} * 1.0 / NULLIF(COUNT(DISTINCT d.full_date), 0) AS v "
        f"FROM {frm(FSL, 'f')} WHERE {wh('f', VALID)} GROUP BY d.day_of_week ORDER BY d.day_of_week",
        label_map=DOW, requires=[R(FSL, "line_total"), R("dim_date", "day_of_week", 2)],
        drill=Drill("sales_lines", VALID, key_expr="d.day_of_week"),
        template_kpis=["KPI-SALES-006"], desc="Revenue divided by the number of trading days of that weekday."))
add(Kpi("hourly_heatmap", "Sales heatmap: weekday x hour", ["sales"], "heatmap",
        f"SELECT t.hour_of_day AS x, d.day_of_week AS y, {REV} AS v FROM {frm(FSL, 'f')}"
        f"{dj('dim_time_of_day', 't', 'time_sk')} WHERE {wh('f', VALID)} GROUP BY t.hour_of_day, d.day_of_week",
        label_map=DOW, span=2, requires=[R(FSL, "time_sk"), R("dim_time_of_day", "hour_of_day", 2)],
        drill=Drill("sales_lines", VALID, x_expr="t.hour_of_day", y_expr="d.day_of_week"),
        template_kpis=["KPI-SALES-008"]))
add(fact_breakdown("revenue_by_daypart", "Revenue by daypart", REV, "t.daypart", FSL, "f", ["sales"],
                   extra=VALID, joins=dj("dim_time_of_day", "t", "time_sk"),
                   requires=[R(FSL, "time_sk"), R("dim_time_of_day", "daypart", 2)], template_kpis=["KPI-SALES-008"]))
add(fact_breakdown("weekend_vs_weekday", "Weekend vs weekday revenue", REV,
                   "CASE WHEN d.is_weekend THEN 'Weekend' ELSE 'Weekday' END", FSL, "f", ["sales"], viz="hbar", extra=VALID,
                   requires=[R(FSL, "line_total"), R("dim_date", "is_weekend", 2)], template_kpis=["KPI-SALES-006"]))
add(fact_breakdown("revenue_by_season", "Avg daily revenue by season / festival",
                   f"{REV} * 1.0 / NULLIF(COUNT(DISTINCT d.full_date), 0)",
                   "COALESCE(d.holiday_name, 'Regular days')", FSL, "f", ["sales", "external"], viz="hbar",
                   extra=VALID, requires=[R(FSL, "line_total"), R("dim_date", "holiday_name", 2)],
                   template_kpis=["KPI-EXT-003", "KPI-EXT-004"],
                   desc="Average revenue per trading day inside each seasonal period vs regular days."))
add(Kpi("basket_size_distribution", "Orders by basket size (items)", ["sales"], "bar",
        "SELECT CASE WHEN o.item_qty_total <= 1 THEN '1' WHEN o.item_qty_total <= 2 THEN '2' "
        "WHEN o.item_qty_total <= 3 THEN '3' WHEN o.item_qty_total <= 5 THEN '4-5' "
        "WHEN o.item_qty_total <= 10 THEN '6-10' ELSE '11+' END AS label, COUNT(*) AS v, "
        "MIN(o.item_qty_total) AS sort_key "
        f"FROM {frm(FSO, 'o')} WHERE {wh('o', VALID_O)} GROUP BY CASE WHEN o.item_qty_total <= 1 THEN '1' "
        "WHEN o.item_qty_total <= 2 THEN '2' WHEN o.item_qty_total <= 3 THEN '3' WHEN o.item_qty_total <= 5 THEN '4-5' "
        "WHEN o.item_qty_total <= 10 THEN '6-10' ELSE '11+' END ORDER BY sort_key",
        fmt="int", requires=[R(FSO, "item_qty_total")], template_kpis=["KPI-SALES-009"],
        drill=Drill("orders", VALID_O, key_expr="CASE WHEN o.item_qty_total <= 1 THEN '1' WHEN o.item_qty_total <= 2 THEN '2' "
                    "WHEN o.item_qty_total <= 3 THEN '3' WHEN o.item_qty_total <= 5 THEN '4-5' "
                    "WHEN o.item_qty_total <= 10 THEN '6-10' ELSE '11+' END")))
add(fact_breakdown("revenue_by_customer_type", "Revenue by customer type", REV, "c.customer_type", FSL, "f",
                   ["sales", "customers"], extra=VALID, joins=CUST,
                   requires=[R("dim_customer", "customer_type", 2)], template_kpis=["KPI-SALES-013"]))

add(sales_metric("b2b_revenue_share", "Revenue from business customers", f"100.0 * SUM(CASE WHEN {B2B_C} THEN f.line_total END) / NULLIF({REV}, 0)",
                 ["sales", "customers"], fmt="pct", joins=CUST, req=[R(FSL, "line_total"), B2B_REQ]))
add(fact_pivot("customer_type_trend", "Revenue trend by customer type", REV, "c.customer_type", FSL, "f",
               ["sales", "customers"], extra=VALID, joins=CUST, span=2,
               requires=[R("dim_customer", "customer_type", 2), B2B_REQ]))
add(fact_breakdown("aov_by_customer_type", "Average order value by customer type", f"{REV} * 1.0 / NULLIF({ORD}, 0)",
                   "c.customer_type", FSL, "f", ["customers"], viz="hbar", extra=VALID, joins=CUST,
                   requires=[R("dim_customer", "customer_type", 2), B2B_REQ]))
add(Kpi("b2b_accounts", "Top business customers", ["customers"], "table",
        f"SELECT c.customer_name AS customer, {ORD} AS n_orders, {REV} AS revenue, "
        f"{REV} * 1.0 / NULLIF({ORD}, 0) AS avg_order, MAX(d.full_date) AS last_date "
        f"FROM {frm(FSL, 'f')}{CUST} WHERE {wh('f', VALID + ' AND ' + B2B_C)} "
        "GROUP BY c.dim_customer_sk, c.customer_name ORDER BY revenue DESC, customer LIMIT 15",
        min_role="manager", span=2, requires=[R("dim_customer", "customer_name"), B2B_REQ],
        columns=[Column("customer", "Customer"), Column("n_orders", "Orders", "int"),
                 Column("revenue", "Revenue", "currency"), Column("avg_order", "Average order value", "currency"),
                 Column("last_date", "Last purchase", "date")],
        note="Customer names are personal data: visible to managers and above."))

# =============================================================== PRODUCTS
add(sales_metric("active_skus", "Active SKUs sold", "COUNT(DISTINCT f.product_sk)", ["products"], fmt="int",
                 req=[R(FSL, "product_sk")], template_kpis=["KPI-ASSORT-002"]))
add(sales_metric("revenue_per_sku", "Revenue per active SKU", f"{REV} * 1.0 / NULLIF(COUNT(DISTINCT f.product_sk), 0)",
                 ["products"], req=[R(FSL, "product_sk")], template_kpis=["KPI-ASSORT-002"]))
add(Kpi("top20_product_share", "Revenue share of top 20% products", ["products"], "metric",
        f"SELECT f.product_sk AS label, {REV} AS v FROM {frm(FSL, 'f')} WHERE {wh('f', VALID)} GROUP BY f.product_sk",
        fmt="pct", good=None, post="top_pct_share:20", requires=[R(FSL, "product_sk", 5)],
        template_kpis=["KPI-SALES-014"], desc="Revenue concentration: share of revenue from the best-selling 20% of SKUs."))
add(fact_breakdown("top_products_revenue", "Top products by revenue", REV, "p.product_name", FSL, "f",
                   ["overview", "products"], viz="hbar", extra=VALID, joins=PROD, limit=10, span=1,
                   requires=[R(FSL, "product_sk"), R("dim_product", "product_name", 2)], template_kpis=["KPI-SALES-014"]))
add(fact_breakdown("top_products_units", "Top products by units", "SUM(f.qty)", "p.product_name", FSL, "f",
                   ["products"], viz="hbar", extra=VALID, joins=PROD, limit=10, fmt="number",
                   requires=[R(FSL, "product_sk"), R("dim_product", "product_name", 2)]))
# ABC class per SKU, ranked in SQL (window functions: SQLite >= 3.25, PostgreSQL, ClickHouse) so the chart and
# its drill-down share one definition. A SKU is A while the revenue of the SKUs ranked above it is < 80% of the
# total, B while < 95%, else C. Ties broken by product_sk so every engine ranks identically. Integer percentages
# (100 * x < 80 * total) keep DECIMAL columns out of float arithmetic.
# Lines without a product (product_sk NULL) are not a SKU, so they are left out of the ranking.
_ABC_REV = (f"SELECT f.product_sk AS sku, {REV} AS sku_rev FROM {frm(FSL, 'f')} "
            f"WHERE {wh('f', VALID + ' AND f.product_sk IS NOT NULL')} "
            f"GROUP BY f.product_sk HAVING {REV} > 0")
_ABC_BEFORE = "COALESCE(SUM(r.sku_rev) OVER (ORDER BY r.sku_rev DESC, r.sku ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING), 0)"
ABC_CLASSES = (f"SELECT r.sku AS sku, r.sku_rev AS sku_rev, CASE WHEN 100 * {_ABC_BEFORE} < 80 * SUM(r.sku_rev) OVER () "
               f"THEN 'A' WHEN 100 * {_ABC_BEFORE} < 95 * SUM(r.sku_rev) OVER () THEN 'B' ELSE 'C' END AS abc_cls "
               f"FROM ({_ABC_REV}) r")
add(Kpi("abc_analysis", "ABC analysis (revenue concentration)", ["products"], "donut",
        f"SELECT x.abc_cls AS label, COUNT(*) AS v, SUM(x.sku_rev) AS rev_v FROM ({ABC_CLASSES}) x "
        f"GROUP BY x.abc_cls ORDER BY x.abc_cls",
        fmt="int", post="abc", requires=[R(FSL, "product_sk", 5)], template_kpis=["KPI-SALES-014"],
        desc="A = SKUs making the first 80% of revenue, B = next 15%, C = last 5%. Values = SKU counts.",
        drill=Drill("sales_lines", VALID, note="drill_abc_note",
                    key_member=("f.product_sk", f"SELECT x.sku FROM ({ABC_CLASSES}) x WHERE x.abc_cls = :dv"))))
add(fact_breakdown("revenue_by_category", "Revenue by category", REV, "p.category", FSL, "f",
                   ["overview", "products"], extra=VALID, joins=PROD, requires=[R("dim_product", "category", 2)],
                   template_kpis=["KPI-SALES-001"]))
add(fact_pivot("category_trend", "Category revenue trend", REV, "p.category", FSL, "f", ["products"], extra=VALID,
               joins=PROD, span=2, requires=[R("dim_product", "category", 2)], template_kpis=["KPI-SALES-001"]))
add(fact_breakdown("margin_by_category", "Gross margin % by category", f"100.0 * {GP} / NULLIF({COSTED}, 0)",
                   "p.category", FSL, "f", ["products"], viz="hbar", extra=VALID, joins=PROD, fmt="pct",
                   requires=[R("dim_product", "category", 2)], requires_any=COST_REQ, template_kpis=["KPI-MARGIN-001"]))
add(fact_breakdown("revenue_by_brand", "Top brands by revenue", REV, "p.brand", FSL, "f", ["products"], viz="hbar",
                   extra=VALID, joins=PROD, limit=10, requires=[R("dim_product", "brand", 2)]))
add(fact_breakdown("private_label_mix", "Private label vs national brand", REV, "p.brand_type", FSL, "f",
                   ["products"], extra=VALID, joins=PROD, requires=[R("dim_product", "brand_type", 2)],
                   template_kpis=["KPI-ASSORT-001"]))
add(Kpi("product_margin_table", "Product margin leaderboard", ["products"], "table",
        f"SELECT p.product_name AS product, {REV} AS revenue, {GP} AS gp, 100.0 * {GP} / NULLIF({COSTED}, 0) AS gm "
        f"FROM {frm(FSL, 'f')}{PROD} WHERE {wh('f', VALID)} GROUP BY p.product_name ORDER BY revenue DESC, product LIMIT 15",
        span=2, requires=[R("dim_product", "product_name")], requires_any=COST_REQ, template_kpis=["KPI-MARGIN-001"],
        columns=[Column("product", "Product"), Column("revenue", "Revenue", "currency"),
                 Column("gp", "Gross profit", "currency"), Column("gm", "Margin", "pct")]))
add(sales_metric("cost_coverage", "Share of sales with a known cost", f"100.0 * {COSTED} / NULLIF({REV}, 0)",
                 ["products"], fmt="pct", req=[], req_any=COST_REQ, template_kpis=["KPI-MARGIN-001"],
                 desc="How much of your revenue comes from items whose cost is recorded. Margin figures are measured on these sales only."))
add(fact_series("margin_trend", "Gross margin over time", f"100.0 * {GP} / NULLIF({COSTED}, 0) AS v", FSL, "f",
                ["products"], [Series("v", "Gross margin", "line", "pct")], extra=VALID, span=2,
                requires_any=COST_REQ, template_kpis=["KPI-MARGIN-001"], fmt="pct"))
add(Kpi("margin_watchlist", "Products with the lowest margin", ["products"], "table",
        f"SELECT p.product_name AS product, {REV} AS revenue, {GP} AS gp, 100.0 * {GP} / NULLIF({COSTED}, 0) AS gm "
        f"FROM {frm(FSL, 'f')}{PROD} WHERE {wh('f', VALID)} GROUP BY p.product_name "
        f"HAVING {COSTED} > 0 ORDER BY gm, revenue DESC, product LIMIT 10",
        span=2, requires=[R("dim_product", "product_name")], requires_any=COST_REQ, template_kpis=["KPI-MARGIN-001"],
        columns=[Column("product", "Product"), Column("revenue", "Revenue", "currency"),
                 Column("gp", "Gross profit", "currency"), Column("gm", "Margin", "pct")]))
add(Kpi("product_momentum", "Product momentum (last 6 months)", ["products"], "donut",
        "SELECT m.trend_status AS label, COUNT(*) AS v FROM mart_product_trend_momentum m "
        "WHERE m.tenant_id = :tenant_id GROUP BY m.trend_status ORDER BY v DESC, label",
        fmt="int", period_mode="snapshot", requires=[R("mart_product_trend_momentum", "trend_status", 2)],
        desc="SKU counts by volume/growth quadrant, from the latest model run."))
add(Kpi("emerging_products", "Emerging & core-hero products", ["products"], "table",
        "SELECT p.product_name AS product, m.trend_status AS status, m.total_qty_6m AS qty6, m.momentum_pct AS mom "
        "FROM mart_product_trend_momentum m JOIN dim_product p ON p.dim_product_sk = m.product_key "
        "AND p.tenant_id = m.tenant_id WHERE m.tenant_id = :tenant_id AND (m.trend_status LIKE 'Emerging%' "
        "OR m.trend_status LIKE 'Core%') ORDER BY m.momentum_pct DESC, p.product_name LIMIT 12",
        period_mode="snapshot", span=2, requires=[R("mart_product_trend_momentum", "momentum_pct")],
        columns=[Column("product", "Product"), Column("status", "Status"), Column("qty6", "Units (6m)", "number"),
                 Column("mom", "Momentum", "pct")]))

# =============================================================== CUSTOMERS
FIRST_PURCHASE = (f"(SELECT f2.customer_sk AS csk, MIN(d2.full_date) AS first_date FROM {FSL} f2 JOIN dim_date d2 "
                  f"ON d2.dim_date_sk = f2.date_sk AND d2.tenant_id = f2.tenant_id WHERE f2.tenant_id = :tenant_id "
                  f"AND COALESCE(f2.line_status, 'completed') <> 'cancelled' AND f2.customer_sk IS NOT NULL "
                  f"GROUP BY f2.customer_sk) fp")
add(Kpi("new_customers", "New customers", ["overview", "customers"], "metric",
        f"SELECT COUNT(*) AS v FROM {FIRST_PURCHASE} WHERE fp.first_date BETWEEN :start AND :end",
        fmt="int", requires=[R(FSL, "customer_sk")], template_kpis=["KPI-CUST-006"],
        spark_sql=(f"SELECT MIN(d.full_date) AS k, COUNT(DISTINCT f.customer_sk) AS v FROM {frm(FSL, 'f')} "
                   f"JOIN {FIRST_PURCHASE} ON fp.csk = f.customer_sk WHERE {wh('f', VALID)} "
                   f"AND d.full_date = fp.first_date GROUP BY @BUCKET@ ORDER BY k"),
        desc="Customers whose first ever purchase falls in the period."))
add(Kpi("repeat_rate", "Repeat purchase rate", ["customers"], "metric",
        "SELECT 100.0 * SUM(CASE WHEN x.n >= 2 THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0) AS v FROM "
        f"(SELECT f.customer_sk AS csk, COUNT(DISTINCT f.order_id) AS n FROM {frm(FSL, 'f')} "
        f"WHERE {wh('f', VALID)} AND f.customer_sk IS NOT NULL GROUP BY f.customer_sk) x",
        fmt="pct", requires=[R(FSL, "customer_sk")], template_kpis=["KPI-CUST-009"],
        desc="Share of the period's customers who ordered at least twice in the period."))
add(sales_metric("orders_per_customer", "Orders per customer", f"{ORD} * 1.0 / NULLIF(COUNT(DISTINCT f.customer_sk), 0)",
                 ["customers"], fmt="ratio", req=[R(FSL, "customer_sk")], template_kpis=["KPI-CUST-010"]))
add(sales_metric("revenue_per_customer", "Revenue per customer", f"{REV} * 1.0 / NULLIF(COUNT(DISTINCT f.customer_sk), 0)",
                 ["customers"], req=[R(FSL, "customer_sk")]))
add(Kpi("top20_customer_share", "Revenue share of top 20% customers", ["customers"], "metric",
        f"SELECT f.customer_sk AS label, {REV} AS v FROM {frm(FSL, 'f')} WHERE {wh('f', VALID)} "
        "AND f.customer_sk IS NOT NULL GROUP BY f.customer_sk",
        fmt="pct", good=None, post="top_pct_share:20", requires=[R(FSL, "customer_sk", 5)],
        template_kpis=["KPI-CUST-001"]))
add(fact_series("active_customers_trend", "Active customers trend", "COUNT(DISTINCT f.customer_sk) AS v", FSL, "f",
                ["customers"], [Series("v", "Active customers", "line", "int", trend=True)], extra=VALID,
                requires=[R(FSL, "customer_sk")], template_kpis=["KPI-CUST-010"]))
add(Kpi("revenue_new_vs_returning", "Revenue: new vs returning customers", ["overview", "customers"], "donut",
        f"SELECT CASE WHEN fp.first_date >= :start THEN 'New' ELSE 'Returning' END AS label, {REV} AS v "
        f"FROM {frm(FSL, 'f')} JOIN {FIRST_PURCHASE} ON fp.csk = f.customer_sk WHERE {wh('f', VALID)} "
        "GROUP BY label ORDER BY v DESC, label",
        requires=[R(FSL, "customer_sk")], template_kpis=["KPI-CUST-006"],
        desc="'New' = first ever purchase inside the selected period."))
add(Kpi("new_vs_returning_trend", "New vs returning customers (monthly)", ["customers"], "timeseries",
        "SELECT m.year_month AS k, m.customer_status AS label, SUM(m.customer_count) AS v "
        "FROM mart_new_vs_returning_customer_monthly m WHERE m.tenant_id = :tenant_id "
        "AND m.year_month BETWEEN :start_month AND :end_month GROUP BY m.year_month, m.customer_status ORDER BY k",
        fmt="int", period_mode="monthly", post="pivot", series=[Series("v", "", "bar", "int", stack="s")],
        requires=[R("mart_new_vs_returning_customer_monthly", "customer_count")], template_kpis=["KPI-CUST-006"]))
add(Kpi("cohort_retention", "Cohort retention (% of cohort active, months since first purchase)", ["customers"],
        "heatmap",
        "SELECT m.months_since_first_purchase AS x, m.cohort_month AS y, m.retention_rate_pct AS v "
        "FROM mart_customer_cohort_retention m WHERE m.tenant_id = :tenant_id "
        "AND m.cohort_month BETWEEN :start_month AND :end_month AND m.months_since_first_purchase <= 12",
        fmt="pct", period_mode="monthly", span=2, requires=[R("mart_customer_cohort_retention", "retention_rate_pct")],
        template_kpis=["KPI-CUST-003"], desc="Rows = cohorts that made their first purchase in the period."))
add(Kpi("rfm_segments", "RFM segments", ["customers"], "donut",
        "SELECT m.rfm_segment AS label, COUNT(*) AS v FROM mart_customer_rfm_summary m "
        "WHERE m.tenant_id = :tenant_id GROUP BY m.rfm_segment ORDER BY v DESC, label",
        fmt="int", period_mode="snapshot", requires=[R("mart_customer_rfm_summary", "rfm_segment", 2)],
        template_kpis=["KPI-CUST-001"], desc="Customers per RFM segment (rolling 12-month window, latest build)."))
add(Kpi("rfm_segment_value", "Revenue by RFM segment", ["customers"], "donut",
        "SELECT m.rfm_segment AS label, SUM(m.monetary_total) AS v FROM mart_customer_rfm_summary m "
        "WHERE m.tenant_id = :tenant_id GROUP BY m.rfm_segment ORDER BY v DESC, label",
        period_mode="snapshot", requires=[R("mart_customer_rfm_summary", "monetary_total", nonzero=True),
                                          R("mart_customer_rfm_summary", "rfm_segment", 2)],
        template_kpis=["KPI-CUST-001"]))
add(Kpi("top_customers", "Top customers in period", ["customers"], "table",
        f"SELECT c.customer_name AS customer, {ORD} AS n_orders, {REV} AS revenue, MAX(d.full_date) AS last_date "
        f"FROM {frm(FSL, 'f')}{CUST} WHERE {wh('f', VALID)} GROUP BY c.dim_customer_sk, c.customer_name "
        "ORDER BY revenue DESC, customer LIMIT 15",
        min_role="manager", span=2, requires=[R("dim_customer", "customer_name")], template_kpis=["KPI-CUST-001"],
        columns=[Column("customer", "Customer"), Column("n_orders", "Orders", "int"),
                 Column("revenue", "Revenue", "currency"), Column("last_date", "Last purchase", "date")],
        note="Customer names are personal data: visible to managers and above."))
add(fact_breakdown("customers_by_city", "Revenue by customer city", REV, "c.city", FSL, "f", ["customers"],
                   viz="hbar", extra=VALID, joins=CUST, limit=10, requires=[R("dim_customer", "city", 2)],
                   template_kpis=["KPI-CUST-011"]))
add(fact_breakdown("customers_by_region", "Revenue by customer region", REV, "c.region", FSL, "f", ["customers"],
                   viz="hbar", extra=VALID, joins=CUST, limit=10, requires=[R("dim_customer", "region", 2)],
                   template_kpis=["KPI-CUST-011"]))
add(fact_breakdown("customers_by_gender", "Customers by gender", "COUNT(DISTINCT f.customer_sk)", "c.gender", FSL, "f",
                   ["customers"], extra=VALID, joins=CUST, fmt="int", requires=[R("dim_customer", "gender", 2)]))
add(fact_breakdown("customers_by_age", "Customers by age band", "COUNT(DISTINCT f.customer_sk)", "c.age_band", FSL,
                   "f", ["customers"], viz="bar", extra=VALID, joins=CUST, fmt="int", order="label",
                   requires=[R("dim_customer", "age_band", 2)]))
add(fact_breakdown("revenue_by_loyalty_tier", "Revenue by loyalty tier", REV, "c.loyalty_tier", FSL, "f",
                   ["customers"], extra=VALID, joins=CUST, requires=[R("dim_customer", "loyalty_tier", 2)],
                   template_kpis=["KPI-LOY-001"]))
add(fact_breakdown("revenue_by_segment", "Revenue by customer segment", REV, "c.segment", FSL, "f", ["customers"],
                   extra=VALID, joins=CUST, requires=[R("dim_customer", "segment", 2)], template_kpis=["KPI-SALES-013"]))
add(fact_breakdown("acquisition_channel_mix", "Customers by acquisition channel", "COUNT(DISTINCT f.customer_sk)",
                   "c.acquisition_channel", FSL, "f", ["customers", "marketing"], extra=VALID, joins=CUST, fmt="int",
                   requires=[R("dim_customer", "acquisition_channel", 2)]))
LOY = "fact_loyalty_points"
add(fact_metric("loyalty_points_earned", "Loyalty points earned", "SUM(ly.points_earned)", LOY, "ly", ["customers"],
                fmt="number", req=[R(LOY, "points_earned")], template_kpis=["KPI-LOY-001"]))
add(fact_metric("loyalty_redemption_rate", "Points redemption rate",
                "100.0 * SUM(ly.points_redeemed) / NULLIF(SUM(ly.points_earned), 0)", LOY, "ly", ["customers"],
                fmt="pct", req=[R(LOY, "points_redeemed")], template_kpis=["KPI-LOY-001"]))
add(fact_metric("loyalty_members", "Active loyalty members", "COUNT(DISTINCT ly.customer_sk)", LOY, "ly",
                ["customers"], fmt="int", req=[R(LOY, "customer_sk")], template_kpis=["KPI-LOY-001"]))

# =============================================================== CHANNELS & STORES
CH = dj("dim_channel", "ch", "channel_sk")
LOC = dj("dim_location", "lo", "location_sk")
add(fact_breakdown("revenue_by_channel", "Revenue by channel", REV, "ch.channel_name", FSL, "f",
                   ["overview", "channels"], extra=VALID, joins=CH, requires=[R(FSL, "channel_sk", 2)],
                   template_kpis=["KPI-SALES-003"]))
add(fact_pivot("channel_trend", "Channel revenue trend", REV, "ch.channel_name", FSL, "f", ["channels"], extra=VALID,
               joins=CH, span=2, requires=[R(FSL, "channel_sk", 2)], template_kpis=["KPI-SALES-003"]))
add(fact_breakdown("revenue_by_location", "Revenue by store / location", REV, "lo.location_name", FSL, "f",
                   ["overview", "channels"], viz="hbar", extra=VALID, joins=LOC, limit=15,
                   requires=[R(FSL, "location_sk", 2)], template_kpis=["KPI-SALES-004"]))
add(fact_pivot("location_trend", "Store revenue trend", REV, "lo.location_name", FSL, "f", ["channels"], extra=VALID,
               joins=LOC, span=2, requires=[R(FSL, "location_sk", 2)], template_kpis=["KPI-SALES-004"]))
add(fact_breakdown("revenue_by_store_region", "Revenue by store region", REV, "lo.region", FSL, "f", ["channels"],
                   viz="hbar", extra=VALID, joins=LOC, requires=[R("dim_location", "region", 2)],
                   template_kpis=["KPI-SALES-005"]))
add(Kpi("sales_per_sqft", "Sales per square foot", ["channels"], "metric",
        f"SELECT {REV} * 1.0 / NULLIF((SELECT SUM(lo.square_footage) FROM dim_location lo WHERE lo.tenant_id = :tenant_id "
        f"AND lo._is_current), 0) AS v FROM {frm(FSL, 'f')} WHERE {wh('f', VALID)}",
        requires=[R("dim_location", "square_footage", nonzero=True)], template_kpis=["KPI-OPS-005"]))
TRF = "fact_store_traffic"
add(fact_metric("store_visits", "Store visits", "SUM(vt.visit_count)", TRF, "vt", ["channels"], fmt="int",
                req=[R(TRF, "visit_count")], template_kpis=["KPI-OPS-001"]))
add(Kpi("traffic_conversion", "Visit-to-order conversion", ["channels"], "metric",
        f"SELECT 100.0 * (SELECT {ORD} FROM {frm(FSL, 'f')} WHERE {wh('f', VALID)}) / NULLIF(SUM(vt.visit_count), 0) AS v "
        f"FROM {frm(TRF, 'vt')} WHERE {wh('vt')}",
        fmt="pct", requires=[R(TRF, "visit_count")], template_kpis=["KPI-OPS-001"]))
CART = "fact_cart_session"
add(fact_metric("online_sessions", "Online sessions", "COUNT(*)", CART, "cr", ["channels"], fmt="int",
                req=[R(CART, "session_id")], template_kpis=["KPI-DIGI-001"]))
add(fact_metric("online_conversion", "Online conversion rate",
                "100.0 * SUM(CASE WHEN cr.is_converted_flag = 1 THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0)", CART, "cr",
                ["channels"], fmt="pct", req=[R(CART, "is_converted_flag")], template_kpis=["KPI-DIGI-001"]))
add(fact_metric("cart_abandonment", "Cart abandonment rate",
                "100.0 * SUM(CASE WHEN cr.is_converted_flag = 1 THEN 0 ELSE 1 END) / NULLIF(COUNT(*), 0)", CART, "cr",
                ["channels"], fmt="pct", good="down", req=[R(CART, "is_converted_flag")], template_kpis=["KPI-DIGI-001"]))
add(fact_metric("avg_cart_value", "Average cart value", "AVG(cr.cart_value)", CART, "cr", ["channels"],
                req=[R(CART, "cart_value")], template_kpis=["KPI-DIGI-002"]))
add(fact_breakdown("revenue_by_traffic_source", "Revenue by traffic source", "SUM(o.order_total)", "o.traffic_source",
                   FSO, "o", ["channels", "marketing"], extra=VALID_O, requires=[R(FSO, "traffic_source", 2)],
                   template_kpis=["KPI-MKT-004"]))
add(fact_breakdown("revenue_by_device", "Revenue by device", "SUM(o.order_total)", "o.device_type", FSO, "o",
                   ["channels"], extra=VALID_O, requires=[R(FSO, "device_type", 2)], template_kpis=["KPI-MKT-004"]))

# =============================================================== MARKETING & PROMOTIONS
CMP = "fact_campaign_performance"
add(fact_metric("campaign_spend", "Campaign spend", "SUM(cp.spend)", CMP, "cp", ["marketing"], good="down",
                req=[R(CMP, "spend")], template_kpis=["KPI-MKT-001"]))
add(fact_metric("roas", "Return on ad spend", "SUM(cp.attributed_revenue) * 1.0 / NULLIF(SUM(cp.spend), 0)", CMP, "cp",
                ["marketing"], fmt="ratio", req=[R(CMP, "spend"), R(CMP, "attributed_revenue")],
                template_kpis=["KPI-MKT-003"]))
add(fact_metric("ctr", "Click-through rate", "100.0 * SUM(cp.clicks) / NULLIF(SUM(cp.impressions), 0)", CMP, "cp",
                ["marketing"], fmt="pct", req=[R(CMP, "impressions")], template_kpis=["KPI-MKT-002"]))
add(fact_metric("campaign_cvr", "Campaign conversion rate", "100.0 * SUM(cp.conversions) / NULLIF(SUM(cp.clicks), 0)",
                CMP, "cp", ["marketing"], fmt="pct", req=[R(CMP, "clicks"), R(CMP, "conversions")],
                template_kpis=["KPI-MKT-002"]))
add(Kpi("cac", "Customer acquisition cost", ["marketing"], "metric",
        f"SELECT SUM(cp.spend) * 1.0 / NULLIF((SELECT COUNT(*) FROM {FIRST_PURCHASE} WHERE fp.first_date BETWEEN :start "
        f"AND :end), 0) AS v FROM {frm(CMP, 'cp')} WHERE {wh('cp')}",
        good="down", requires=[R(CMP, "spend"), R(FSL, "customer_sk")], template_kpis=["KPI-MKT-001"]))
add(fact_series("spend_vs_revenue", "Campaign spend vs attributed revenue",
                "SUM(cp.spend) AS v, SUM(cp.attributed_revenue) AS v2", CMP, "cp", ["marketing"],
                [Series("v", "Spend", "bar"), Series("v2", "Attributed revenue", "line", trend=True)], span=2,
                requires=[R(CMP, "spend")], template_kpis=["KPI-MKT-003"]))
add(Kpi("campaign_table", "Campaign performance", ["marketing"], "table",
        "SELECT mc.campaign_name AS campaign, SUM(cp.spend) AS spend_v, SUM(cp.clicks) AS clicks_v, "
        "SUM(cp.conversions) AS conv, SUM(cp.attributed_revenue) AS rev, "
        "SUM(cp.attributed_revenue) * 1.0 / NULLIF(SUM(cp.spend), 0) AS roas_v "
        f"FROM {frm(CMP, 'cp')}{dj('dim_marketing_campaign', 'mc', 'campaign_sk', 'cp')} WHERE {wh('cp')} "
        "GROUP BY mc.campaign_name ORDER BY spend_v DESC, campaign LIMIT 15",
        span=2, requires=[R(CMP, "spend"), R("dim_marketing_campaign", "campaign_name")],
        template_kpis=["KPI-MKT-002", "KPI-MKT-003"],
        columns=[Column("campaign", "Campaign"), Column("spend_v", "Spend", "currency"), Column("clicks_v", "Clicks", "int"),
                 Column("conv", "Conversions", "int"), Column("rev", "Revenue", "currency"),
                 Column("roas_v", "ROAS", "ratio")]))
ATT = "fact_attribution"
add(fact_breakdown("attribution_by_touchpoint", "Attributed revenue by touchpoint", "SUM(att.attributed_revenue)",
                   "att.touchpoint_type", ATT, "att", ["marketing"], requires=[R(ATT, "touchpoint_type", 2)],
                   template_kpis=["KPI-PROMO-003"]))
PRM = "fact_promotion_redemption"
add(fact_metric("promo_revenue", "Promotion revenue", "SUM(pr.redeemed_revenue)", PRM, "pr", ["marketing"],
                req=[R(PRM, "redeemed_revenue")], template_kpis=["KPI-PROMO-001"]))
add(fact_metric("promo_discount", "Promotion discount cost", "SUM(pr.discount_applied)", PRM, "pr", ["marketing"],
                good="down", req=[R(PRM, "discount_applied")], template_kpis=["KPI-PROMO-001"]))
add(fact_metric("promo_discount_ratio", "Discount-to-promo-revenue",
                "100.0 * SUM(pr.discount_applied) / NULLIF(SUM(pr.redeemed_revenue), 0)", PRM, "pr", ["marketing"],
                fmt="pct", good="down", req=[R(PRM, "discount_applied")], template_kpis=["KPI-PROMO-001"]))
add(fact_breakdown("top_promotions", "Top promotions by revenue", "SUM(pr.redeemed_revenue)", "pm.promotion_name", PRM,
                   "pr", ["marketing"], viz="hbar", joins=dj("dim_promotion", "pm", "promotion_sk", "pr"), limit=10,
                   requires=[R(PRM, "redeemed_revenue"), R("dim_promotion", "promotion_name")],
                   template_kpis=["KPI-PROMO-001"]))
add(Kpi("promo_lift", "Promotion incremental lift", ["marketing"], "table",
        "SELECT pm.promotion_name AS promo, p.product_name AS product, m.promo_days AS days_n, "
        "m.promo_daily_units AS promo_u, m.baseline_daily_units AS base_u, m.lift_pct AS lift "
        "FROM mart_promotion_lift_analysis m JOIN dim_promotion pm ON pm.dim_promotion_sk = m.promotion_sk "
        "AND pm.tenant_id = m.tenant_id JOIN dim_product p ON p.dim_product_sk = m.product_sk AND p.tenant_id = m.tenant_id "
        "WHERE m.tenant_id = :tenant_id ORDER BY m.lift_pct DESC, promo, product LIMIT 15",
        period_mode="snapshot", span=2, requires=[R("mart_promotion_lift_analysis", "lift_pct")],
        template_kpis=["KPI-PROMO-002"],
        columns=[Column("promo", "Promotion"), Column("product", "Product"), Column("days_n", "Days", "int"),
                 Column("promo_u", "Units/day (promo)", "ratio"), Column("base_u", "Units/day (base)", "ratio"),
                 Column("lift", "Lift", "pct")]))

# =============================================================== FULFILMENT & RETURNS
add(fact_breakdown("order_status_mix", "Orders by status", "COUNT(*)", "o.order_status", FSO, "o",
                   ["operations"], fmt="int", requires=[R(FSO, "order_status", 2)], template_kpis=["KPI-SALES-011"]))
add(fact_breakdown("line_status_mix", "Order lines by status (value)", REV, "f.line_status", FSL, "f", ["operations"],
                   requires=[R(FSL, "line_status", 2)]))
FUL = "fact_fulfillment"
add(fact_metric("on_time_delivery", "On-time delivery rate",
                "100.0 * SUM(CASE WHEN fu.is_on_time_flag = 1 THEN 1 ELSE 0 END) / NULLIF(COUNT(fu.is_on_time_flag), 0)",
                FUL, "fu", ["operations"], fmt="pct", req=[R(FUL, "is_on_time_flag")], template_kpis=["KPI-OPS-011"]))
add(fact_metric("avg_delivery_days", "Average delivery days", "AVG(fu.delivery_days)", FUL, "fu", ["operations"],
                fmt="days", good="down", req=[R(FUL, "delivery_days")], template_kpis=["KPI-OPS-011"]))
add(fact_metric("time_to_fulfil", "Average time to ship", "AVG(fu.time_to_fulfillment_hours)", FUL, "fu",
                ["operations"], fmt="hours", good="down", req=[R(FUL, "time_to_fulfillment_hours")],
                template_kpis=["KPI-OPS-002"]))
add(fact_metric("fulfilment_completion", "Fulfilment completion",
                "100.0 * SUM(CASE WHEN fu.fulfillment_status = 'delivered' THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0)",
                FUL, "fu", ["operations"], fmt="pct", req=[R(FUL, "fulfillment_status")], template_kpis=["KPI-OPS-002"]))
add(fact_metric("shipping_cost_per_shipment", "Shipping cost per shipment", "AVG(fu.shipping_cost)", FUL, "fu",
                ["operations"], good="down", req=[R(FUL, "shipping_cost")], template_kpis=["KPI-MARGIN-005"]))
add(fact_breakdown("fulfilment_method_mix", "Shipments by fulfilment method", "COUNT(*)", "fu.fulfillment_method", FUL,
                   "fu", ["operations"], fmt="int", requires=[R(FUL, "fulfillment_method", 2)]))
add(Kpi("carrier_performance", "Carrier on-time performance", ["operations"], "table",
        "SELECT sm.carrier_name AS carrier, sm.service_level AS svc, SUM(m.delivered_order_count) AS delivered, "
        "100.0 * SUM(m.on_time_order_count) / NULLIF(SUM(m.delivered_order_count), 0) AS ontime, "
        "SUM(m.total_delivery_days) * 1.0 / NULLIF(SUM(m.delivered_order_count), 0) AS avg_days "
        "FROM mart_delivery_performance_monthly m JOIN dim_shipping_method sm ON sm.dim_shipping_method_sk = m.ship_method_sk "
        "AND sm.tenant_id = m.tenant_id WHERE m.tenant_id = :tenant_id AND m.year_month BETWEEN :start_month AND :end_month "
        "GROUP BY sm.carrier_name, sm.service_level ORDER BY delivered DESC, carrier, svc",
        period_mode="monthly", span=2, requires=[R("mart_delivery_performance_monthly", "delivered_order_count")],
        template_kpis=["KPI-OPS-011"],
        columns=[Column("carrier", "Carrier"), Column("svc", "Service"), Column("delivered", "Delivered", "int"),
                 Column("ontime", "On time", "pct"), Column("avg_days", "Avg days", "days")]))
RET = "fact_returns"
add(Kpi("return_rate", "Return rate (units)", ["operations"], "metric",
        f"SELECT 100.0 * SUM(r.qty_returned) / NULLIF((SELECT SUM(f.qty) FROM {frm(FSL, 'f')} WHERE {wh('f', VALID)}), 0) AS v "
        f"FROM {frm(RET, 'r')} WHERE {wh('r')}",
        fmt="pct", good="down", requires=[R(RET, "qty_returned")], template_kpis=["KPI-OPS-004"]))
add(fact_metric("refund_amount", "Refunds", "SUM(r.refund_amount)", RET, "r", ["operations", "finance"], good="down",
                req=[R(RET, "refund_amount")], template_kpis=["KPI-OPS-004"]))
add(fact_breakdown("returns_by_reason", "Returns by reason", "SUM(r.qty_returned)", "r.return_reason", RET, "r",
                   ["operations"], viz="hbar", fmt="number", requires=[R(RET, "return_reason", 2)],
                   template_kpis=["KPI-OPS-004"]))
add(fact_breakdown("returns_by_category", "Refunds by product category", "SUM(r.refund_amount)", "p.category", RET, "r",
                   ["operations"], viz="hbar", joins=dj("dim_product", "p", "product_sk", "r"),
                   requires=[R(RET, "refund_amount"), R("dim_product", "category", 2)]))
add(fact_series("returns_trend", "Returns trend", "SUM(r.qty_returned) AS v, SUM(r.refund_amount) AS v2", RET, "r",
                ["operations"], [Series("v", "Units returned", "bar", "number", trend=True),
                                 Series("v2", "Refunds", "line", "currency", axis=1)],
                span=2, requires=[R(RET, "qty_returned")], template_kpis=["KPI-OPS-004"]))

# =============================================================== INVENTORY & SUPPLY
INV = "fact_inventory_snapshot"
add(Kpi("inventory_value", "Inventory value on hand", ["inventory"], "metric",
        f"SELECT SUM(i.on_hand_value) AS v FROM {frm(INV, 'i')} WHERE {asof_where(INV, 'i')}",
        good=None, requires=[R(INV, "on_hand_value")], template_kpis=["KPI-INV-005"], note="As of period end."))
add(Kpi("on_hand_units", "Units on hand", ["inventory"], "metric",
        f"SELECT SUM(i.on_hand_qty) AS v FROM {frm(INV, 'i')} WHERE {asof_where(INV, 'i')}",
        fmt="number", good=None, requires=[R(INV, "on_hand_qty")], note="As of period end."))
add(Kpi("below_reorder", "SKU-locations below reorder level", ["inventory"], "metric",
        "SELECT SUM(CASE WHEN i.reorder_level IS NOT NULL AND i.on_hand_qty <= i.reorder_level THEN 1 ELSE 0 END) AS v "
        f"FROM {frm(INV, 'i')} WHERE {asof_where(INV, 'i')}",
        fmt="int", good="down", requires=[R(INV, "reorder_level")], template_kpis=["KPI-INV-001"], note="As of period end."))
add(Kpi("out_of_stock_rate", "Out-of-stock rate", ["inventory"], "metric",
        f"SELECT 100.0 * SUM(CASE WHEN i.on_hand_qty <= 0 THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0) AS v "
        f"FROM {frm(INV, 'i')} WHERE {asof_where(INV, 'i')}",
        fmt="pct", good="down", requires=[R(INV, "on_hand_qty")], template_kpis=["KPI-INV-009"], note="As of period end."))
IHD = "mart_inventory_health_daily"
add(Kpi("days_of_supply", "Average days of supply", ["inventory"], "metric",
        f"SELECT AVG(m.days_of_supply) AS v FROM {frm(IHD, 'm')} WHERE {asof_where(IHD, 'm')}",
        fmt="days", good=None, requires=[R(IHD, "days_of_supply")], template_kpis=["KPI-INV-010"], note="As of period end."))
add(Kpi("reorder_list", "Reorder now", ["inventory"], "table",
        "SELECT p.product_name AS product, lo.location_name AS loc, m.on_hand_qty AS on_hand, m.reorder_level AS rop, "
        "m.days_of_supply AS dos, m.deficit_units AS deficit "
        f"FROM {frm(IHD, 'm')} JOIN dim_product p ON p.dim_product_sk = m.product_sk AND p.tenant_id = m.tenant_id "
        "JOIN dim_location lo ON lo.dim_location_sk = m.location_sk AND lo.tenant_id = m.tenant_id "
        f"WHERE {asof_where(IHD, 'm', 'm.reorder_flag = 1')} ORDER BY m.deficit_units DESC, product, loc LIMIT 20",
        span=2, requires=[R(IHD, "reorder_flag", nonzero=True)], template_kpis=["KPI-INV-001", "KPI-INV-003"],
        columns=[Column("product", "Product"), Column("loc", "Location"), Column("on_hand", "On hand", "number"),
                 Column("rop", "Reorder level", "number"), Column("dos", "Days of supply", "days"),
                 Column("deficit", "Deficit", "number")]))
TURN = "mart_inventory_turnover_monthly"
add(mart_metric("inventory_turnover", "Inventory turnover", "SUM(m.cogs) * 1.0 / NULLIF(AVG(m.avg_on_hand_value), 0)",
                TURN, ["inventory"], fmt="ratio", req=[R(TURN, "inventory_turnover_ratio")], template_kpis=["KPI-INV-007"]))
add(mart_metric("gmroi", "GMROI", "SUM(m.gross_profit) * 1.0 / NULLIF(AVG(m.avg_on_hand_value), 0)", TURN,
                ["inventory"], fmt="ratio", req=[R(TURN, "gmroi")], template_kpis=["KPI-INV-007"]))
MOV = "fact_inventory_movement"
add(fact_breakdown("movements_by_type", "Inventory movements by type", "SUM(mv.movement_qty)", "mv.movement_type", MOV,
                   "mv", ["inventory"], viz="hbar", fmt="number", requires=[R(MOV, "movement_type", 2)],
                   template_kpis=["KPI-INV-005"]))
add(fact_metric("shrinkage_rate", "Shrinkage rate",
                "100.0 * -SUM(CASE WHEN mv.movement_type IN ('shrinkage', 'write_off') THEN mv.movement_qty ELSE 0 END) "
                "/ NULLIF(-SUM(CASE WHEN mv.movement_qty < 0 THEN mv.movement_qty ELSE 0 END), 0)",
                MOV, "mv", ["inventory"], fmt="pct", good="down", req=[R(MOV, "movement_qty")],
                template_kpis=["KPI-INV-005"]))
add(fact_metric("transfer_qty", "Inter-location transfers (units)", "SUM(tr.transfer_qty)", "fact_stock_transfer", "tr",
                ["inventory"], fmt="number", good=None, req=[R("fact_stock_transfer", "transfer_qty")],
                template_kpis=["KPI-INV-006"]))
WST = "fact_spoilage_waste_event"
add(fact_metric("waste_cost", "Waste cost", "SUM(w.waste_cost)", WST, "w", ["inventory"], good="down",
                req=[R(WST, "waste_cost")], template_kpis=["KPI-PERISH-002"]))
add(fact_metric("waste_rate", "Waste rate", "100.0 * SUM(w.units_wasted) / NULLIF(SUM(w.units_available), 0)", WST, "w",
                ["inventory"], fmt="pct", good="down", req=[R(WST, "units_wasted")], template_kpis=["KPI-PERISH-001"]))
add(fact_breakdown("waste_by_reason", "Waste cost by reason", "SUM(w.waste_cost)", "w.waste_reason", WST, "w",
                   ["inventory"], viz="hbar", requires=[R(WST, "waste_reason", 2)], template_kpis=["KPI-PERISH-001"]))
PER = "fact_perishable_inventory_snapshot"
add(Kpi("lots_at_risk", "Perishable lots at risk", ["inventory"], "metric",
        "SELECT COUNT(DISTINCT ps.product_lot_batch_sk) AS v FROM "
        f"{frm(PER, 'ps')} WHERE {asof_where(PER, 'ps', '(ps.days_remaining <= 2 OR ps.temp_abuse_events > 0 OR ps.spoilage_risk_score >= 0.5)')}",
        fmt="int", good="down", requires=[R(PER, "spoilage_risk_score")], template_kpis=["KPI-PERISH-003"],
        note="As of period end."))
MKD = "fact_markdown_pricing_event"
add(fact_metric("markdown_revenue", "Post-markdown revenue", "SUM(md.revenue_post_markdown)", MKD, "md", ["inventory"],
                req=[R(MKD, "revenue_post_markdown")], template_kpis=["KPI-PERISH-004"]))
add(fact_metric("markdown_depth", "Average markdown depth", "AVG(md.discount_pct)", MKD, "md", ["inventory"],
                fmt="pct", good="down", req=[R(MKD, "discount_pct")], template_kpis=["KPI-PERISH-004"]))
POL = "fact_purchase_order_line"
add(fact_metric("purchase_spend", "Purchase spend", "SUM(l.line_subtotal)", POL, "l", ["inventory", "finance"],
                good=None, req=[R(POL, "line_subtotal")], template_kpis=["KPI-PROC-001"]))
add(fact_metric("po_count", "Purchase orders", "COUNT(DISTINCT l.po_id)", POL, "l", ["inventory"], fmt="int", good=None,
                req=[R(POL, "po_id")]))
GR = "fact_goods_receipt"
add(fact_metric("supplier_on_time", "Supplier on-time receipts",
                "100.0 * SUM(CASE WHEN COALESCE(g.days_late, 0) <= 0 THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0)", GR, "g",
                ["inventory"], fmt="pct", req=[R(GR, "receipt_id")], template_kpis=["KPI-PROC-002"]))
add(fact_metric("avg_lead_time", "Average supplier lead time", "AVG(g.lead_time_days)", GR, "g", ["inventory"],
                fmt="days", good="down", req=[R(GR, "lead_time_days")], template_kpis=["KPI-PROC-005"]))
add(fact_metric("rejection_rate", "Inbound rejection rate", "100.0 * SUM(g.qty_rejected) / NULLIF(SUM(g.qty_received), 0)",
                GR, "g", ["inventory"], fmt="pct", good="down", req=[R(GR, "qty_rejected")], template_kpis=["KPI-PROC-002"]))
add(fact_breakdown("top_suppliers", "Top suppliers by spend", "SUM(l.line_subtotal)", "s.supplier_name", POL, "l",
                   ["inventory"], viz="hbar", joins=dj("dim_supplier", "s", "supplier_sk", "l"), limit=10,
                   requires=[R(POL, "line_subtotal"), R("dim_supplier", "supplier_name")], template_kpis=["KPI-PROC-001"]))

# =============================================================== FINANCE & CASH (manager+)
PAY = "fact_customer_payment"
FIN = dict(min_role="manager")
add(fact_metric("cash_received", "Customer payments received", "SUM(p.amount_received)", PAY, "p", ["finance"],
                extra="COALESCE(p.payment_status, 'received') NOT IN ('failed', 'pending')", req=[R(PAY, "amount_received")],
                template_kpis=["KPI-AR-001", "KPI-FIN-001"], **FIN))
# v14 (2026-09-30): grouped by payment TYPE (template method_type: bank_transfer, cash, e_wallet, …), not by the name the
# shop wrote (BIDV and "Ngân hàng" are both bank transfers). A name the load could not type falls back to itself.
PAY_TYPE = "COALESCE(pm.method_type, pm.method_name)"
add(fact_breakdown("payment_method_mix", "Receipts by payment method", "SUM(p.amount_received)", PAY_TYPE, PAY,
                   "p", ["finance", "sales"], joins=dj("dim_payment_method", "pm", "payment_method_sk", "p"),
                   requires=[R(PAY, "payment_method_sk", 2)], template_kpis=["KPI-FIN-001"]))
add(fact_pivot("payment_method_trend", "Payment method trend", "SUM(p.amount_received)", PAY_TYPE, PAY, "p",
               ["finance"], joins=dj("dim_payment_method", "pm", "payment_method_sk", "p"), span=2,
               requires=[R(PAY, "payment_method_sk", 2)], template_kpis=["KPI-FIN-001"], **FIN))
add(fact_metric("avg_installments", "Average installments per payment", "AVG(p.installment_count)", PAY, "p",
                ["finance"], fmt="ratio", good=None, req=[R(PAY, "installment_count")], template_kpis=["KPI-FIN-001"], **FIN))
add(fact_metric("collection_rate", "Collection rate", "100.0 * SUM(o.amount_paid) / NULLIF(SUM(o.order_total), 0)", FSO,
                "o", ["finance"], extra=VALID_O, fmt="pct", req=[R(FSO, "amount_paid")], template_kpis=["KPI-FIN-002"], **FIN))
add(fact_metric("balance_due", "Unpaid order balance", "SUM(o.balance_due)", FSO, "o", ["finance"], extra=VALID_O,
                good="down", req=[R(FSO, "balance_due", nonzero=True)], template_kpis=["KPI-FIN-002"], **FIN))
CF = "mart_cash_flow_monthly"
add(mart_series("cash_flow", "Monthly cash flow",
                "SUM(m.cash_in) AS v, -SUM(m.cash_out_suppliers) AS v2, -SUM(m.cash_out_opex) AS v3, SUM(m.net_cash_flow) AS v4",
                CF, ["finance"], [Series("v", "Cash in", "bar", stack="c"), Series("v2", "Paid to suppliers", "bar", stack="c"),
                                  Series("v3", "Operating expenses", "bar", stack="c"), Series("v4", "Net cash flow", "line")],
                span=2, requires=[R(CF, "cash_in")], template_kpis=["KPI-AP-002"], **FIN))
add(mart_metric("net_cash_flow", "Net cash flow", "SUM(m.net_cash_flow)", CF, ["finance"], req=[R(CF, "net_cash_flow")],
                template_kpis=["KPI-AP-002"], **FIN))
AR, AP = "fact_accounts_receivable", "fact_accounts_payable"
add(Kpi("ar_outstanding", "Receivables outstanding", ["finance"], "metric",
        f"SELECT SUM(a.outstanding_balance) AS v FROM {frm(AR, 'a')} WHERE {asof_where(AR, 'a')}", good="down",
        requires=[R(AR, "outstanding_balance")], template_kpis=["KPI-AR-001"], note="As of period end.", **FIN))
add(Kpi("ar_overdue_share", "Receivables overdue > 60 days", ["finance"], "metric",
        "SELECT 100.0 * SUM(CASE WHEN a.days_outstanding > 60 THEN a.outstanding_balance ELSE 0 END) "
        f"/ NULLIF(SUM(a.outstanding_balance), 0) AS v FROM {frm(AR, 'a')} WHERE {asof_where(AR, 'a')}",
        fmt="pct", good="down", requires=[R(AR, "days_outstanding")], template_kpis=["KPI-AR-002"], **FIN))
add(Kpi("dso", "Days sales outstanding", ["finance"], "metric",
        f"SELECT SUM(a.outstanding_balance) * 90.0 / NULLIF((SELECT SUM(o.order_total) FROM {frm(FSO, 'o')} "
        f"WHERE o.tenant_id = :tenant_id AND {VALID_O} AND d.full_date > :end_minus_90 AND d.full_date <= :end), 0) AS v "
        f"FROM {frm(AR, 'a')} WHERE {asof_where(AR, 'a')}",
        fmt="days", good="down", requires=[R(AR, "outstanding_balance")], template_kpis=["KPI-AR-004"], **FIN))
ARA = "mart_accounts_receivable_aging"
add(Kpi("ar_aging", "Receivables aging", ["finance"], "bar",
        f"SELECT m.aging_bucket AS label, SUM(m.outstanding_balance) AS v FROM {frm(ARA, 'm')} "
        f"WHERE {asof_where(ARA, 'm')} GROUP BY m.aging_bucket ORDER BY label",
        requires=[R(ARA, "outstanding_balance")], template_kpis=["KPI-AR-002"], **FIN))
add(Kpi("ap_outstanding", "Payables outstanding", ["finance"], "metric",
        f"SELECT SUM(ap.outstanding_balance) AS v FROM {frm(AP, 'ap')} WHERE {asof_where(AP, 'ap')}", good=None,
        requires=[R(AP, "outstanding_balance")], template_kpis=["KPI-AP-001"], note="As of period end.", **FIN))
add(Kpi("dpo", "Days payable outstanding", ["finance"], "metric",
        f"SELECT SUM(ap.outstanding_balance) * 90.0 / NULLIF((SELECT SUM(l.line_subtotal) FROM {frm(POL, 'l')} "
        "WHERE l.tenant_id = :tenant_id AND d.full_date > :end_minus_90 AND d.full_date <= :end), 0) AS v "
        f"FROM {frm(AP, 'ap')} WHERE {asof_where(AP, 'ap')}",
        fmt="days", good=None, requires=[R(AP, "outstanding_balance"), R(POL, "line_subtotal")],
        template_kpis=["KPI-AP-003"], **FIN))
APA = "mart_accounts_payable_aging"
add(Kpi("ap_aging", "Payables aging", ["finance"], "bar",
        f"SELECT m.aging_bucket AS label, SUM(m.outstanding_balance) AS v FROM {frm(APA, 'm')} "
        f"WHERE {asof_where(APA, 'm')} GROUP BY m.aging_bucket ORDER BY label",
        requires=[R(APA, "outstanding_balance")], template_kpis=["KPI-AP-001"], **FIN))
OPX = "fact_operating_expense"
add(fact_metric("opex", "Operating expenses", "SUM(x.expense_amount)", OPX, "x", ["finance"], good="down",
                req=[R(OPX, "expense_amount")], template_kpis=["KPI-FIN-003"], **FIN))
add(Kpi("opex_ratio", "Operating expense ratio", ["finance"], "metric",
        f"SELECT 100.0 * SUM(x.expense_amount) / NULLIF((SELECT {REV} FROM {frm(FSL, 'f')} WHERE {wh('f', VALID)}), 0) AS v "
        f"FROM {frm(OPX, 'x')} WHERE {wh('x')}",
        fmt="pct", good="down", requires=[R(OPX, "expense_amount")], template_kpis=["KPI-FIN-003"], **FIN))
add(fact_breakdown("opex_by_category", "Operating expenses by category", "SUM(x.expense_amount)", "x.expense_category",
                   OPX, "x", ["finance"], requires=[R(OPX, "expense_category", 2)], template_kpis=["KPI-FIN-003"], **FIN))
PNL = "mart_location_pnl_monthly"
add(mart_series("pnl_trend", "Store P&L (monthly)",
                "SUM(m.net_revenue) AS v, SUM(m.gross_profit) AS v2, SUM(m.labor_cost) AS v3, "
                "SUM(m.operating_expense) AS v4, SUM(m.contribution_margin) AS v5", PNL, ["finance"],
                [Series("v", "Net revenue", "bar"), Series("v2", "Gross profit", "bar"), Series("v3", "Labour cost", "bar"),
                 Series("v4", "Operating expense", "bar"), Series("v5", "Contribution margin", "line")],
                span=2, requires=[R(PNL, "contribution_margin")], template_kpis=["KPI-MARGIN-004"], **FIN))
add(mart_metric("contribution_margin_pct", "Contribution margin", "100.0 * SUM(m.contribution_margin) / NULLIF(SUM(m.net_revenue), 0)",
                PNL, ["finance"], fmt="pct", req=[R(PNL, "contribution_margin")], template_kpis=["KPI-MARGIN-004"], **FIN))
TAX = "fact_tax_transaction"
add(fact_metric("net_tax", "Net tax liability",
                "SUM(CASE WHEN tx.tax_direction = 'output' THEN tx.tax_amount ELSE 0 END) - "
                "SUM(CASE WHEN tx.tax_direction = 'input' THEN tx.tax_amount ELSE 0 END)", TAX, "tx", ["finance"],
                good=None, req=[R(TAX, "tax_amount")], template_kpis=["KPI-TAX-001"], **FIN))
add(fact_metric("effective_tax_rate", "Effective tax rate", "100.0 * SUM(tx.tax_amount) / NULLIF(SUM(tx.taxable_amount), 0)",
                TAX, "tx", ["finance"], fmt="pct", good=None, req=[R(TAX, "taxable_amount")],
                template_kpis=["KPI-TAX-001"], **FIN))

# =============================================================== WORKFORCE (manager+)
EMP = dj("dim_employee", "e", "employee_sk")
SH = "fact_employee_shift"
add(fact_breakdown("revenue_by_employee", "Revenue by salesperson", REV, "e.employee_name", FSL, "f",
                   ["workforce", "sales"], viz="hbar", extra=VALID, joins=EMP, limit=15,
                   requires=[R(FSL, "employee_sk", 2)], template_kpis=["KPI-SALES-012"], **FIN))
add(sales_metric("revenue_per_salesperson", "Revenue per salesperson",
                 f"{REV} * 1.0 / NULLIF(COUNT(DISTINCT f.employee_sk), 0)", ["workforce"],
                 req=[R(FSL, "employee_sk")], template_kpis=["KPI-SALES-012"], **FIN))
add(Kpi("headcount", "Active headcount", ["workforce"], "metric",
        "SELECT COUNT(*) AS v FROM dim_employee e WHERE e.tenant_id = :tenant_id AND e._is_current "
        "AND e.termination_date IS NULL",
        fmt="int", good=None, period_mode="snapshot", compare=False, requires=[R("dim_employee", "employee_name")],
        **FIN))
add(fact_pivot("employee_trend", "Salesperson revenue trend", REV, "e.employee_name", FSL, "f", ["workforce"],
               extra=VALID, joins=EMP, span=2, requires=[R(FSL, "employee_sk", 2)], template_kpis=["KPI-SALES-012"], **FIN))
add(fact_metric("labor_cost", "Labour cost", "SUM(sh.labor_cost)", SH, "sh", ["workforce"], good="down",
                req=[R(SH, "labor_cost")], template_kpis=["KPI-OPS-006"], **FIN))
add(fact_metric("worked_hours", "Hours worked", "SUM(sh.worked_hours)", SH, "sh", ["workforce"], fmt="number", good=None,
                req=[R(SH, "worked_hours")], template_kpis=["KPI-OPS-003"], **FIN))
add(Kpi("revenue_per_labor_hour", "Revenue per labour hour", ["workforce"], "metric",
        f"SELECT (SELECT {REV} FROM {frm(FSL, 'f')} WHERE {wh('f', VALID)}) * 1.0 / NULLIF(SUM(sh.worked_hours), 0) AS v "
        f"FROM {frm(SH, 'sh')} WHERE {wh('sh')}",
        requires=[R(SH, "worked_hours")], template_kpis=["KPI-OPS-003"], **FIN))
add(Kpi("labor_cost_pct", "Labour cost % of sales", ["workforce"], "metric",
        f"SELECT 100.0 * SUM(sh.labor_cost) / NULLIF((SELECT {REV} FROM {frm(FSL, 'f')} WHERE {wh('f', VALID)}), 0) AS v "
        f"FROM {frm(SH, 'sh')} WHERE {wh('sh')}",
        fmt="pct", good="down", requires=[R(SH, "labor_cost")], template_kpis=["KPI-OPS-006"], **FIN))
add(fact_metric("schedule_adherence", "Schedule adherence", "100.0 * SUM(sh.worked_hours) / NULLIF(SUM(sh.scheduled_hours), 0)",
                SH, "sh", ["workforce"], fmt="pct", good=None, req=[R(SH, "scheduled_hours")],
                template_kpis=["KPI-OPS-007"], **FIN))
add(fact_metric("overtime_share", "Overtime share of hours", "100.0 * SUM(sh.overtime_hours) / NULLIF(SUM(sh.worked_hours), 0)",
                SH, "sh", ["workforce"], fmt="pct", good="down", req=[R(SH, "overtime_hours", nonzero=True)], **FIN))
add(Kpi("employee_turnover", "Employee turnover rate", ["workforce"], "metric",
        "SELECT 100.0 * SUM(CASE WHEN e.termination_date IS NOT NULL THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0) AS v "
        "FROM dim_employee e WHERE e.tenant_id = :tenant_id AND e._is_current",
        fmt="pct", good="down", period_mode="snapshot", compare=False, requires=[R("dim_employee", "termination_date")],
        template_kpis=["KPI-OPS-008"], **FIN))

# =============================================================== CUSTOMER EXPERIENCE
SRV = "fact_customer_survey"
add(fact_metric("nps", "Net Promoter Score",
                "100.0 * (SUM(CASE WHEN cs.nps_score >= 9 THEN 1 ELSE 0 END) - SUM(CASE WHEN cs.nps_score <= 6 THEN 1 ELSE 0 END)) "
                "/ NULLIF(COUNT(cs.nps_score), 0)", SRV, "cs", ["overview", "experience"], fmt="score",
                req=[R(SRV, "nps_score")], template_kpis=["KPI-CSAT-001"]))
add(fact_metric("csat", "Average CSAT", "AVG(cs.csat_score)", SRV, "cs", ["experience"], fmt="ratio",
                req=[R(SRV, "csat_score")], template_kpis=["KPI-CSAT-001"]))
add(fact_metric("survey_responses", "Survey responses", "COUNT(*)", SRV, "cs", ["experience"], fmt="int", good=None,
                req=[R(SRV, "survey_id")]))
REV_T = "fact_product_review"
add(fact_metric("avg_rating", "Average product rating", "AVG(rv.rating_value)", REV_T, "rv", ["experience", "products"],
                fmt="ratio", req=[R(REV_T, "rating_value")], template_kpis=["KPI-CUST-008"]))
add(fact_metric("review_count", "Reviews", "SUM(COALESCE(rv.review_count, 1))", REV_T, "rv", ["experience"], fmt="int",
                req=[R(REV_T, "rating_value")], template_kpis=["KPI-CUST-008"]))
add(fact_series("rating_trend", "Rating trend", "AVG(rv.rating_value) AS v, COUNT(*) AS v2", REV_T, "rv", ["experience"],
                [Series("v", "Avg rating", "line", "ratio"), Series("v2", "Reviews", "bar", "int", axis=1)], span=2,
                requires=[R(REV_T, "rating_value")], template_kpis=["KPI-CUST-008"]))
add(Kpi("lowest_rated_products", "Lowest-rated products (min. 3 reviews)", ["experience"], "table",
        f"SELECT p.product_name AS product, AVG(rv.rating_value) AS rating, COUNT(*) AS n_reviews FROM {frm(REV_T, 'rv')}"
        f"{dj('dim_product', 'p', 'product_sk', 'rv')} WHERE {wh('rv')} GROUP BY p.product_name HAVING COUNT(*) >= 3 "
        "ORDER BY rating ASC, product LIMIT 10",
        requires=[R(REV_T, "rating_value")], columns=[Column("product", "Product"), Column("rating", "Rating", "ratio"),
                                                     Column("n_reviews", "Reviews", "int")]))
INT = "fact_customer_interaction"
add(fact_metric("interactions", "Service interactions", "COUNT(*)", INT, "ci", ["experience"], fmt="int", good=None,
                req=[R(INT, "interaction_type")], template_kpis=["KPI-CUST-007"]))
add(fact_metric("avg_handle_time", "Average handle time", "AVG(ci.interaction_duration_minutes)", INT, "ci",
                ["experience"], fmt="number", good="down", req=[R(INT, "interaction_duration_minutes")]))
add(fact_breakdown("interactions_by_type", "Interactions by type", "COUNT(*)", "ci.interaction_type", INT, "ci",
                   ["experience"], fmt="int", requires=[R(INT, "interaction_type", 2)], template_kpis=["KPI-CUST-007"]))

# =============================================================== MARKET & EVENTS
add(Kpi("event_uplift", "Seasonal event uplift vs baseline", ["external", "sales"], "table",
        "SELECT ev.event_name AS event, ev.start_date AS start_d, ev.end_date AS end_d, m.event_days_with_sales AS days_n, "
        "m.event_revenue AS rev, m.uplift_pct AS uplift FROM mart_event_period_performance m "
        "JOIN dim_local_event_calendar ev ON ev.dim_local_event_calendar_sk = m.event_sk AND ev.tenant_id = m.tenant_id "
        "WHERE m.tenant_id = :tenant_id AND ev.start_date <= :end AND ev.end_date >= :start ORDER BY m.uplift_pct DESC, event",
        span=2, requires=[R("mart_event_period_performance", "uplift_pct")], template_kpis=["KPI-EXT-004"],
        columns=[Column("event", "Event"), Column("start_d", "From", "date"), Column("end_d", "To", "date"),
                 Column("days_n", "Trading days", "int"), Column("rev", "Revenue", "currency"),
                 Column("uplift", "Uplift vs baseline", "pct")],
        desc="Events overlapping the selected period; uplift = avg daily revenue in event vs non-event baseline."))
EXT = "mart_external_factor_daily"
add(Kpi("weather_vs_sales", "Daily revenue vs temperature", ["external"], "timeseries",
        f"SELECT MIN(d.full_date) AS k, SUM(m.daily_revenue) AS v, AVG(m.temp_high) AS v2 FROM {frm(EXT, 'm')} "
        f"WHERE {wh('m')} GROUP BY @BUCKET@ ORDER BY k",
        span=2, series=[Series("v", "Revenue", "bar", trend=True), Series("v2", "Max temp", "line", "number", axis=1)],
        requires=[R(EXT, "temp_high")], template_kpis=["KPI-EXT-001"]))
COMP = "mart_competitive_landscape_monthly"
add(mart_metric("competitor_price_gap", "Price gap vs competitors", "AVG(m.price_gap_pct)", COMP, ["external"], fmt="pct",
                good=None, req=[R(COMP, "price_gap_pct")], template_kpis=["KPI-EXT-002"]))
add(Kpi("competitor_price_table", "Largest competitor price gaps", ["external"], "table",
        "SELECT p.product_name AS product, AVG(m.avg_own_price) AS own_p, AVG(m.avg_competitor_price) AS comp_p, "
        "AVG(m.price_gap_pct) AS gap FROM mart_competitive_landscape_monthly m JOIN dim_product p "
        "ON p.dim_product_sk = m.product_sk AND p.tenant_id = m.tenant_id WHERE m.tenant_id = :tenant_id "
        "AND m.year_month BETWEEN :start_month AND :end_month GROUP BY p.product_name ORDER BY gap DESC, product LIMIT 15",
        period_mode="monthly", span=2, requires=[R(COMP, "price_gap_pct")], template_kpis=["KPI-EXT-002"],
        columns=[Column("product", "Product"), Column("own_p", "Our price", "currency"),
                 Column("comp_p", "Competitor price", "currency"), Column("gap", "Gap", "pct")]))
add(fact_metric("competitor_openings", "Competitor openings nearby", "SUM(pp.presence_event_count)",
                "fact_competitor_presence", "pp", ["external"], extra="pp.event_type = 'open'", fmt="int", good="down",
                req=[R("fact_competitor_presence", "presence_event_count")]))
ECO = "fact_economic_indicator"
add(fact_series("economy_trend", "Consumer confidence & inflation",
                "AVG(ec.consumer_confidence_index) AS v, AVG(ec.inflation_rate) AS v2", ECO, "ec", ["external"],
                [Series("v", "Consumer confidence", "line", "number"), Series("v2", "Inflation %", "line", "pct", axis=1)],
                span=2, requires=[R(ECO, "consumer_confidence_index")], template_kpis=["KPI-EXT-003"]))

# =============================================================== AI INSIGHTS (latest model runs)
LATEST_FC = ("m.forecast_generated_date = (SELECT MAX(m2.forecast_generated_date) FROM fact_pred_demand_forecast m2 "
             "WHERE m2.tenant_id = :tenant_id)")
add(Kpi("forecast_next_week", "Forecast demand, next 7 days (units)", ["ai", "overview"], "metric",
        f"SELECT SUM(m.week_forecast_value) AS v, MIN(m.forecast_start_date) AS caveat_from "
        f"FROM fact_pred_demand_forecast m WHERE m.tenant_id = :tenant_id AND {LATEST_FC}",
        fmt="int", good=None, period_mode="snapshot", compare=False,
        requires=[R("fact_pred_demand_forecast", "week_forecast_value")], template_kpis=["KPI-FCST-001"],
        desc="Sum of 7-day forecasts across SKUs from the latest forecasting run, starting the day after the last sale."))
add(Kpi("forecast_top_products", "Forecast demand by product (next 7 days)", ["ai"], "table",
        "SELECT p.product_name AS product, m.week_forecast_value AS wk, m.demand_class AS demand, "
        f"m.product_sk AS psk FROM fact_pred_demand_forecast m JOIN dim_product p ON p.dim_product_sk = m.product_sk "
        f"AND p.tenant_id = m.tenant_id WHERE m.tenant_id = :tenant_id AND {LATEST_FC} ORDER BY m.week_forecast_value DESC, p.product_name LIMIT 15",
        period_mode="snapshot", span=2, requires=[R("fact_pred_demand_forecast", "week_forecast_value")],
        template_kpis=["KPI-FCST-001"],
        columns=[Column("product", "Product"), Column("wk", "Expected units, next 7 days", "int"), Column("demand", "Demand pattern")]))   # v11: model name removed (internal); v39.2: whole units
# v7 (2026-09-29): "Forecast back-test" removed from the dashboard. It checks algorithm accuracy for development,
# not for business owners; fact_pred_demand_forecast_accuracy stays in the warehouse for Step 5 and developers.
CHURN = "fact_pred_customer_churn_risk"
add(Kpi("churn_risk_distribution", "Customers by churn risk", ["ai"], "bar",
        "SELECT CASE WHEN m.churn_risk_score >= 0.5 THEN 'High (>= 50%)' WHEN m.churn_risk_score >= 0.3 "
        "THEN 'Medium (30-50%)' ELSE 'Low (< 30%)' END AS label, COUNT(*) AS v, MIN(m.churn_risk_score) AS sort_key "
        f"FROM {CHURN} m WHERE m.tenant_id = :tenant_id GROUP BY CASE WHEN m.churn_risk_score >= 0.5 THEN 'High (>= 50%)' "
        "WHEN m.churn_risk_score >= 0.3 THEN 'Medium (30-50%)' ELSE 'Low (< 30%)' END ORDER BY sort_key",
        fmt="int", period_mode="snapshot", requires=[R(CHURN, "churn_risk_score")], template_kpis=["KPI-CUST-004"]))
add(Kpi("revenue_at_risk", "Annual revenue at risk (churn >= 50%)", ["ai", "overview"], "metric",
        "SELECT SUM(c.avg_monthly_spend) * 12 AS v, MIN(m.reliability_caveat) AS caveat FROM fact_pred_customer_churn_risk m "
        "JOIN mart_customer_clv c ON c.customer_sk = m.customer_sk AND c.tenant_id = m.tenant_id "
        "WHERE m.tenant_id = :tenant_id AND m.churn_risk_score >= 0.5",
        good="down", period_mode="snapshot", compare=False,
        requires=[R(CHURN, "churn_risk_score"), R("mart_customer_clv", "avg_monthly_spend")],
        template_kpis=["KPI-CUST-004"], desc="12 x average monthly spend of customers the churn model scores >= 50%."))
add(Kpi("high_risk_customers", "High-value customers at churn risk", ["ai"], "table",
        "SELECT dc.customer_name AS customer, m.churn_risk_score * 100 AS risk, c.historical_spend AS spend, "
        "c.recency_days AS recency, c.segment AS seg FROM fact_pred_customer_churn_risk m "
        "JOIN mart_customer_clv c ON c.customer_sk = m.customer_sk AND c.tenant_id = m.tenant_id "
        "JOIN dim_customer dc ON dc.dim_customer_sk = m.customer_sk AND dc.tenant_id = m.tenant_id "
        "WHERE m.tenant_id = :tenant_id AND m.churn_risk_score >= 0.5 ORDER BY c.historical_spend DESC, dc.customer_name LIMIT 15",
        period_mode="snapshot", span=2, min_role="manager",
        requires=[R(CHURN, "churn_risk_score"), R("dim_customer", "customer_name")], template_kpis=["KPI-CUST-004", "KPI-CUST-005"],
        columns=[Column("customer", "Customer"), Column("risk", "Risk of leaving", "pct"), Column("spend", "Lifetime spend", "currency"),
                 Column("recency", "Days since last order", "int"), Column("seg", "Customer segment")],
        note="Win-back candidates. Customer names visible to managers and above."))
CLV = "mart_customer_clv"
add(Kpi("avg_clv", "Average customer lifetime value", ["customers"], "metric",
        f"SELECT AVG(m.total_clv) AS v, MIN(m.reliability_caveat) AS caveat FROM {CLV} m WHERE m.tenant_id = :tenant_id",
        period_mode="snapshot", compare=False, requires=[R(CLV, "total_clv")], template_kpis=["KPI-CUST-002"]))
add(Kpi("predicted_future_value", "Predicted future customer value", ["ai"], "metric",
        f"SELECT SUM(m.future_predicted_value) AS v, MIN(m.reliability_caveat) AS caveat FROM {CLV} m WHERE m.tenant_id = :tenant_id",
        period_mode="snapshot", compare=False, requires=[R(CLV, "future_predicted_value")], template_kpis=["KPI-CUST-002"]))
add(Kpi("clv_by_segment", "Average CLV by RFM segment", ["customers"], "hbar",
        f"SELECT m.segment AS label, AVG(m.total_clv) AS v FROM {CLV} m WHERE m.tenant_id = :tenant_id "
        "GROUP BY m.segment ORDER BY v DESC, label",
        period_mode="snapshot", requires=[R(CLV, "segment", 2)], template_kpis=["KPI-CUST-002"]))
# v6 (2026-09-29): "Behavioural customer clusters" (k-means SEGMENT_0..n from fact_pred_customer_segment) removed.
# Template tenants take customer segments from the RFM mart (consistent on every run); k-means is not run for them,
# so that table was a stale one-off snapshot with unnamed, run-dependent labels.
add(Kpi("product_clusters", "Product importance clusters", ["products"], "donut",
        "SELECT m.cluster_label AS label, COUNT(*) AS v FROM fact_pred_product_cluster m WHERE m.tenant_id = :tenant_id "
        "GROUP BY m.cluster_label ORDER BY v DESC, label",
        fmt="int", period_mode="snapshot", requires=[R("fact_pred_product_cluster", "cluster_label", 2)]))
add(Kpi("affinity_pairs", "Frequently bought together", ["products"], "table",
        "SELECT pa.product_name AS item_a, pb.product_name AS item_b, m.co_occurrence_count AS together, "
        "m.confidence_a_to_b_pct AS conf, m.lift AS lift_v FROM mart_product_affinity m "
        "JOIN dim_product pa ON pa.dim_product_sk = m.product_a_sk AND pa.tenant_id = m.tenant_id "
        "JOIN dim_product pb ON pb.dim_product_sk = m.product_b_sk AND pb.tenant_id = m.tenant_id "
        "WHERE m.tenant_id = :tenant_id AND m.lift > 1 ORDER BY m.co_occurrence_count DESC, m.lift DESC, pa.product_name, pb.product_name LIMIT 15",
        period_mode="snapshot", span=2, requires=[R("mart_product_affinity", "lift")], template_kpis=["KPI-XSELL-001"],
        columns=[Column("item_a", "If customer buys"), Column("item_b", "They also buy"),
                 Column("together", "Baskets together", "int"), Column("conf", "Confidence", "pct"),
                 Column("lift_v", "Lift", "ratio")]))
add(Kpi("segment_category_affinity", "Segment x category affinity", ["customers"], "table",
        "SELECT m.segment AS seg, m.category AS cat, m.net_revenue AS rev, m.affinity_index AS idx "
        "FROM mart_segment_category_affinity m WHERE m.tenant_id = :tenant_id ORDER BY m.affinity_index DESC, seg, cat LIMIT 15",
        period_mode="snapshot", requires=[R("mart_segment_category_affinity", "segment", 2),
                                          R("mart_segment_category_affinity", "category", 2)],
        template_kpis=["KPI-XSELL-002"],
        columns=[Column("seg", "Segment"), Column("cat", "Category"), Column("rev", "Revenue", "currency"),
                 Column("idx", "Affinity index", "ratio")]))
add(Kpi("price_elasticity", "Price-demand correlation (r)", ["products"], "metric",
        "SELECT m.corr_coefficient AS v, m.reliability_caveat AS caveat FROM fact_diag_price_elasticity m "
        "WHERE m.tenant_id = :tenant_id AND m.computed_date = (SELECT MAX(m2.computed_date) FROM fact_diag_price_elasticity m2 "
        "WHERE m2.tenant_id = :tenant_id)",
        fmt="ratio", good=None, period_mode="snapshot", compare=False,
        requires=[R("fact_diag_price_elasticity", "corr_coefficient")], template_kpis=["KPI-PRICE-001"],
        desc="Correlation between price and units across products; near 0 = price changes barely move demand."))


# =============================================================== FULL-LIST DOWNLOADS
# Complete lists (no LIMIT) behind the segment charts. Optional joins are used only when the table exists,
# so a tenant without model outputs still gets the list with the columns it has.
EXPORTS: list[Export] = [
    Export(
        id="customer_segments", title="Customer segment list",
        desc="Every customer with buying-behaviour segment, purchase history, risk of leaving and lifetime value.",
        base_from="dim_customer c", base_where="c.tenant_id = :tenant_id AND c._is_current",
        columns=[ExportColumn("customer", "Customer", "c.customer_name")],
        joins=[
            ExportJoin("mart_customer_rfm_summary", "r", "r.customer_sk = c.dim_customer_sk AND r.tenant_id = c.tenant_id", [
                ExportColumn("rfm_segment_v", "Customer segment", "r.rfm_segment"),
                ExportColumn("rfm_code_v", "Segment score (recency · frequency · spend)", "r.rfm_code"),
                ExportColumn("first_purchase", "First purchase", "r.first_purchase_date", "date"),
                ExportColumn("last_purchase", "Last purchase", "r.last_purchase_date", "date"),
                ExportColumn("recency", "Days since last order", "r.recency_days", "int"),
                ExportColumn("n_orders", "Orders", "r.frequency_count", "int"),
                ExportColumn("spend", "Total spend", "r.monetary_total", "currency")]),
            ExportJoin("fact_pred_customer_churn_risk", "ch",
                       "ch.customer_sk = c.dim_customer_sk AND ch.tenant_id = c.tenant_id", [
                ExportColumn("churn_pct", "Risk of leaving", "ch.churn_risk_score * 100", "pct")]),
            ExportJoin("mart_customer_clv", "v", "v.customer_sk = c.dim_customer_sk AND v.tenant_id = c.tenant_id", [
                ExportColumn("clv", "Customer lifetime value", "v.total_clv", "currency"),
                ExportColumn("future_value", "Expected future spend", "v.future_predicted_value", "currency")]),
        ],
        filters=[
            ExportFilter("segment", "Customer segment", "r.rfm_segment", needs="r"),
            ExportFilter("churn", "Risk of leaving (band)",
                         "CASE WHEN ch.churn_risk_score >= 0.5 THEN 'High (>= 50%)' WHEN ch.churn_risk_score >= 0.3 "
                         "THEN 'Medium (30-50%)' WHEN ch.churn_risk_score IS NOT NULL THEN 'Low (< 30%)' END", needs="ch"),
        ],
        order_by="COALESCE(r.monetary_total, 0) DESC, c.customer_name",
        requires=[R("dim_customer", "customer_name"), R("mart_customer_rfm_summary", "rfm_segment")],
        min_role="manager",
        kpis={"rfm_segments": "segment", "rfm_segment_value": "segment", "churn_risk_distribution": "churn",
              "clv_by_segment": "segment", "high_risk_customers": "churn"},
    ),
    Export(
        id="product_segments", title="Product segment list",
        desc="Every product with sales momentum, importance group, category and lifetime sales.",
        base_from="dim_product p", base_where="p.tenant_id = :tenant_id AND p._is_current",
        columns=[ExportColumn("product", "Product", "p.product_name"), ExportColumn("sku_v", "Product code", "p.sku"),
                 ExportColumn("category_v", "Category", "p.category"), ExportColumn("brand_v", "Brand", "p.brand")],
        joins=[
            ExportJoin("mart_product_trend_momentum", "m", "m.product_key = p.dim_product_sk AND m.tenant_id = p.tenant_id", [
                ExportColumn("status", "Momentum status", "m.trend_status"),
                ExportColumn("qty6", "Units (6m)", "m.total_qty_6m", "number"),
                ExportColumn("avg_m", "Avg units / month (6m)", "m.avg_monthly_qty", "number"),
                ExportColumn("mom", "Momentum", "m.momentum_pct", "pct")]),
            ExportJoin("fact_pred_product_cluster", "pc", "pc.product_sk = p.dim_product_sk AND pc.tenant_id = p.tenant_id", [
                ExportColumn("cluster", "Importance group", "pc.cluster_label"),
                ExportColumn("importance", "Importance score", "pc.importance_score", "ratio")]),
            ExportJoin("mart_product_performance", "pp", "pp.product_sk = p.dim_product_sk AND pp.tenant_id = p.tenant_id", [
                ExportColumn("revenue", "Revenue (all time)", "pp.net_revenue", "currency"),
                ExportColumn("units", "Units (all time)", "pp.units_sold", "number"),
                ExportColumn("n_orders", "Orders", "pp.order_count", "int"),
                ExportColumn("rank_v", "Revenue rank", "pp.revenue_rank_overall", "int"),
                ExportColumn("first_sale", "First sale", "pp.first_sale_date", "date"),
                ExportColumn("last_sale", "Last sale", "pp.last_sale_date", "date")]),
        ],
        filters=[
            ExportFilter("status", "Momentum status", "m.trend_status", needs="m"),
            ExportFilter("cluster", "Importance group", "pc.cluster_label", needs="pc"),
            ExportFilter("category", "Category", "p.category"),
        ],
        order_by="COALESCE(pp.net_revenue, 0) DESC, p.product_name",
        requires=[R("dim_product", "product_name")],
        kpis={"product_momentum": "status", "emerging_products": "status", "product_clusters": "cluster",
              "top_products_revenue": "status", "revenue_by_category": "category"},
    ),
]


# =============================================================== DRILL-DOWN DETAIL ROWS
def _lj(table: str, alias: str, fk: str, a: str) -> str:
    return f" LEFT JOIN {table} {alias} ON {alias}.{table}_sk = {a}.{fk} AND {alias}.tenant_id = {a}.tenant_id"


_SHARED_DIMS = lambda a: (_lj("dim_time_of_day", "t", "time_sk", a) + _lj("dim_customer", "c", "customer_sk", a)  # noqa: E731
                          + _lj("dim_channel", "ch", "channel_sk", a) + _lj("dim_location", "lo", "location_sk", a)
                          + _lj("dim_employee", "e", "employee_sk", a) + _lj("dim_payment_method", "pmx", "payment_method_sk", a))

DETAILS: dict[str, DetailSource] = {
    "sales_lines": DetailSource(
        id="sales_lines", alias="f",
        from_sql=frm(FSL, "f") + _SHARED_DIMS("f") + _lj("dim_product", "p", "product_sk", "f"),
        columns=[
            DetailColumn("dt", "Date", "d.full_date", "date"),
            DetailColumn("tm", "Time", "t.time_of_day"),
            DetailColumn("order_no", "Order", "f.order_id"),
            DetailColumn("product", "Product", "p.product_name"),
            DetailColumn("category_v", "Category", "p.category"),
            DetailColumn("customer", "Customer", "c.customer_name", pii=True),
            DetailColumn("channel_v", "Channel", "ch.channel_name"),
            DetailColumn("location_v", "Location", "lo.location_name"),
            DetailColumn("staff", "Salesperson", "e.employee_name", pii=True),
            DetailColumn("pay", "Payment method", "pmx.method_name"),
            DetailColumn("qty_v", "Quantity", "f.qty", "number"),
            DetailColumn("price_v", "Unit price", "f.unit_price", "currency"),
            DetailColumn("disc_v", "Discount", "f.discount_amount", "currency"),
            DetailColumn("total_v", "Line total", "f.line_total", "currency"),
            DetailColumn("gp_v", "Gross profit", "COALESCE(f.gross_profit, f.line_total - f.line_cost)", "currency"),
            DetailColumn("status_v", "Status", "f.line_status"),
        ],
        order_by="d.full_date DESC, f.order_id, f.line_number", total_expr="f.line_total"),
    "orders": DetailSource(
        id="orders", alias="o",
        from_sql=frm(FSO, "o") + _SHARED_DIMS("o"),
        columns=[
            DetailColumn("dt", "Date", "d.full_date", "date"),
            DetailColumn("tm", "Time", "t.time_of_day"),
            DetailColumn("order_no", "Order", "o.order_id"),
            DetailColumn("customer", "Customer", "c.customer_name", pii=True),
            DetailColumn("channel_v", "Channel", "ch.channel_name"),
            DetailColumn("location_v", "Location", "lo.location_name"),
            DetailColumn("staff", "Salesperson", "e.employee_name", pii=True),
            DetailColumn("pay", "Payment method", "pmx.method_name"),
            DetailColumn("items", "Items", "o.item_qty_total", "number"),
            DetailColumn("disc_v", "Discount", "o.discount_total", "currency"),
            DetailColumn("total_v", "Order total", "o.order_total", "currency"),
            DetailColumn("status_v", "Status", "o.order_status"),
            DetailColumn("src", "Traffic source", "o.traffic_source"),
            DetailColumn("dev", "Device", "o.device_type"),
        ],
        order_by="d.full_date DESC, o.order_id", total_expr="o.order_total"),
}


# v11: business-facing titles and "About this chart" texts (no internal codes or implementation details)
from .retail_trade_004_info import DATA_AREAS, apply as _apply_business_text  # noqa: E402,F401
_apply_business_text(KPIS)

# =============================================================== BUSINESS EVALUATION + EARLY WARNINGS
# v12: WHICH rules run, their thresholds, weights, severities and links live in config/rules/retail_trade_004.json
# (edit that file, no code change; validated by vdash/rules_config.py and tools/check_rules.py). This module only
# defines the DATA SETS the checks read: portable SQL (tenant filter mandatory), data requirements and item columns.
# Binds set by the evaluators: :start/:end/:c_start/:c_end (evaluation), :asof and window dates (warnings).
_FSL_D = frm(FSL, "f")
_ROWS_BOTH = "((d.full_date BETWEEN :start AND :end) OR (d.full_date BETWEEN :c_start AND :c_end))"
_LATEST_FC_IN = ("f.product_sk IN (SELECT m.product_sk FROM fact_pred_demand_forecast m WHERE m.tenant_id = :tenant_id "
                 f"AND {LATEST_FC})")

RULE_DATA: dict[str, RuleData] = {
    "product_sales_vs_comparison": RuleData(
        "movers",
        {"rows": f"SELECT f.product_sk AS k, p.product_name AS label, "
                 f"SUM(CASE WHEN d.full_date BETWEEN :start AND :end THEN f.line_total ELSE 0 END) AS cur, "
                 f"SUM(CASE WHEN d.full_date BETWEEN :c_start AND :c_end THEN f.line_total ELSE 0 END) AS prev "
                 f"FROM {_FSL_D}{dj('dim_product', 'p', 'product_sk')} "
                 f"WHERE f.tenant_id = :tenant_id AND {VALID} AND {_ROWS_BOTH} "
                 f"GROUP BY f.product_sk, p.product_name"},
        [R(FSL, "product_sk", 5), R("dim_product", "product_name")],
        [Column("label", "Product"), Column("cur", "This period", "currency"),
         Column("prev", "Comparison period", "currency"), Column("chg", "Change", "currency"),
         Column("chg_pct", "Change %", "pct")]),
    "customer_revenue": RuleData(
        "concentration",
        {"rows": f"SELECT f.customer_sk AS k, c.customer_name AS label, SUM(f.line_total) AS v "
                 f"FROM {_FSL_D} LEFT JOIN dim_customer c ON c.dim_customer_sk = f.customer_sk AND c.tenant_id = f.tenant_id "
                 f"WHERE f.tenant_id = :tenant_id AND {VALID} AND d.full_date BETWEEN :start AND :end "
                 f"GROUP BY f.customer_sk, c.customer_name"},
        [R(FSL, "customer_sk", 20)],
        [Column("label", "Customer"), Column("v", "Revenue", "currency"), Column("share", "Share of revenue", "pct")],
        items_min_role="manager"),
    "demand_forecast": RuleData(
        "forecast_drop",
        {"forecast": "SELECT SUM(m.week_forecast_value) AS fc, MIN(m.forecast_start_date) AS fc_from "
                     f"FROM fact_pred_demand_forecast m WHERE m.tenant_id = :tenant_id AND {LATEST_FC}",
         "actual": f"SELECT SUM(f.qty) AS v FROM {_FSL_D} WHERE f.tenant_id = :tenant_id AND {VALID} "
                   f"AND d.full_date BETWEEN :w_from AND :w_to AND {_LATEST_FC_IN}"},
        [R("fact_pred_demand_forecast", "week_forecast_value"), R(FSL, "qty")]),
    "customer_churn_value": RuleData(
        "high_value_churn",
        {"customers": "SELECT m.customer_sk AS k, dc.customer_name AS label, m.churn_risk_score AS risk, "
                      "c.historical_spend AS spend, c.avg_monthly_spend AS monthly, c.recency_days AS recency, "
                      "c.segment AS seg FROM fact_pred_customer_churn_risk m "
                      "JOIN mart_customer_clv c ON c.customer_sk = m.customer_sk AND c.tenant_id = m.tenant_id "
                      "LEFT JOIN dim_customer dc ON dc.dim_customer_sk = m.customer_sk AND dc.tenant_id = m.tenant_id "
                      "WHERE m.tenant_id = :tenant_id",
         "rev12": f"SELECT SUM(f.line_total) AS v FROM {_FSL_D} WHERE f.tenant_id = :tenant_id AND {VALID} "
                  f"AND d.full_date BETWEEN :y_from AND :asof"},
        [R("fact_pred_customer_churn_risk", "churn_risk_score"), R("mart_customer_clv", "historical_spend")],
        [Column("label", "Customer"), Column("risk", "Risk of leaving", "pct"),
         Column("spend", "Lifetime spend", "currency"), Column("recency", "Days since last order", "int"),
         Column("seg", "Customer segment")],
        items_min_role="manager"),
    "customer_order_rhythm": RuleData(
        "overdue_regulars",
        {"customers": f"SELECT f.customer_sk AS k, c.customer_name AS label, COUNT(DISTINCT f.order_id) AS n_orders, "
                      f"MIN(d.full_date) AS first_d, MAX(d.full_date) AS last_d, "
                      f"SUM(CASE WHEN d.full_date >= :y_from THEN f.line_total ELSE 0 END) AS spend12 "
                      f"FROM {_FSL_D} LEFT JOIN dim_customer c ON c.dim_customer_sk = f.customer_sk "
                      f"AND c.tenant_id = f.tenant_id WHERE f.tenant_id = :tenant_id AND {VALID} "
                      f"AND f.customer_sk IS NOT NULL AND d.full_date <= :asof GROUP BY f.customer_sk, c.customer_name"},
        [R(FSL, "customer_sk", 20), R(FSL, "order_id")],
        [Column("label", "Customer"), Column("orders", "Orders", "int"), Column("last", "Last order", "date"),
         Column("gap", "Usual days between orders", "number"), Column("since", "Days since last order", "int"),
         Column("spend12", "Spend, last 12 months", "currency")],
        items_min_role="manager"),
    "product_momentum": RuleData(
        "bestseller_decline",
        {"revenue": f"SELECT f.product_sk AS k, p.product_name AS label, SUM(f.line_total) AS rev "
                    f"FROM {_FSL_D}{dj('dim_product', 'p', 'product_sk')} WHERE f.tenant_id = :tenant_id AND {VALID} "
                    f"AND d.full_date BETWEEN :q_from AND :asof GROUP BY f.product_sk, p.product_name",
         "momentum": "SELECT m.product_key AS k, MAX(m.trend_status) AS status, MAX(m.momentum_pct) AS mom "
                     "FROM mart_product_trend_momentum m WHERE m.tenant_id = :tenant_id "
                     "AND m.trend_status LIKE 'Declining%' GROUP BY m.product_key"},
        [R("mart_product_trend_momentum", "trend_status", 2), R(FSL, "product_sk", 5)],
        [Column("label", "Product"), Column("rev", "Revenue, last 90 days", "currency"),
         Column("share", "Share of revenue", "pct"), Column("mom", "Momentum", "pct"),
         Column("status", "Momentum status")]),
    "daily_revenue": RuleData(
        "recent_drop",
        {"daily": f"SELECT d.full_date AS dt, SUM(f.line_total) AS v FROM {_FSL_D} WHERE f.tenant_id = :tenant_id "
                  f"AND {VALID} AND d.full_date BETWEEN :w_from AND :asof GROUP BY d.full_date"},
        [R(FSL, "line_total")]),
    "monthly_revenue": RuleData(
        "mtd_pace",
        {"months": f"SELECT d.year_month AS ym, SUM(f.line_total) AS v FROM {_FSL_D} WHERE f.tenant_id = :tenant_id "
                   f"AND {VALID} AND d.year_month IN (:ym, :ym_prev, :ym_ly) AND d.full_date <= :asof "
                   f"GROUP BY d.year_month"},
        [R(FSL, "line_total")]),
    "first_purchases": RuleData(
        "new_customers_drop",
        {"firsts": f"SELECT f.customer_sk AS k, MIN(d.full_date) AS first_d FROM {_FSL_D} "
                   f"WHERE f.tenant_id = :tenant_id AND {VALID} AND f.customer_sk IS NOT NULL AND d.full_date <= :asof "
                   f"GROUP BY f.customer_sk"},
        [R(FSL, "customer_sk", 20)]),
    "data_freshness": RuleData("stale_data", {}, [R(FSL, "line_total")]),
}


# Drill notes: say so when the chart value is not simply the sum of the rows' totals
for _k in KPIS:
    if _k.drill is None or _k.drill.note:
        continue
    if _k.id in ("revenue_by_dow", "revenue_by_season"):
        _k.drill.note = "drill_avg_note"          # chart = average per day
    elif _k.id in ("aov_trend", "active_customers_trend") or (_k.fmt != "currency" and not (_k.drill.source == "orders" and _k.fmt == "int")):
        _k.drill.note = "drill_lines_note"        # chart = a count of customers / a ratio / units
