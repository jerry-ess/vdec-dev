# Veridex BI Dashboard

This is a multi-tenant retail/trading KPI dashboard that runs as its own service. It reads the TMPL-RETAIL-TRADE-004 warehouse (SQLite for the demo, ClickHouse or PostgreSQL in production) and draws the charts automatically. Each tenant sees only the KPIs its data supports. The design is in [ARCHITECTURE.md](ARCHITECTURE.md).

## Run the demo (Windows)

```bat
cd D:\work\my_ai_projects\generative_bi\latest_system\dashboard
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python run_dashboard.py
```

Open http://127.0.0.1:8050.

The demo warehouse path is set in `config/datasources.json`. By default it is `dw_tmpl-retail-trade-004_20260923_1220.db`. To use another file, set the `VDASH_DEMO_DB` environment variable.

## Demo accounts

Change these passwords before showing the demo outside your machine. Use `python tools/manage_users.py passwd <user>`.

| User              | Password            | Role           | Sees                                                                   |
| ----------------- | ------------------- | -------------- | ---------------------------------------------------------------------- |
| `admin`           | `VeridexAdmin!2026` | platform_admin | All tenants and the hidden-KPI list                                    |
| `phung.owner`     | `PhungOwner!2026`   | tenant_admin   | Phụng Kitchen: everything, plus the hidden-KPI list                    |
| `phung.manager`   | `PhungManager!2026` | manager        | Phụng Kitchen, including finance, workforce and customer names         |
| `phung.staff`     | `PhungStaff!2026`   | viewer         | Phụng Kitchen, without finance, workforce or customer names            |
| `grocery.manager` | `GroceryMgr!2026`   | manager        | Grocery Store 1 only (no data loaded yet, so it shows the empty state) |

## Suggested demo flow

1. Sign in as `phung.owner`. The Executive Overview opens on the last 30 days of data, compared with the previous 30 days.
2. Change the period, for example to Year to date or a custom range from 1 Jan 2025 to 31 Dec 2025, and compare with the same period last year. Every tile and chart updates.
3. Read the Business evaluation below the tiles, then click the warning strip to open AI Insights → Early warnings. Click a finding to see the customers or products behind it.
4. Walk through Sales (weekday × hour heatmap, festival seasons), Customers (cohort retention, RFM) and AI Insights (forecast, churn, CLV, bought-together). Switch the language to Tiếng Việt for a Vietnamese audience. Click the "Champions" bar to download that segment's customer list to Excel.
5. Open the grey "N more KPIs hidden" panel at the bottom of a section. It lists what this business would unlock by loading more data (costs, inventory, marketing and so on).
6. Sign in as `phung.staff` to show role filtering: Finance and Workforce shrink or disappear, and findings no longer list customer names.
7. Sign in as `grocery.manager` to show tenant isolation. This user cannot open Phụng Kitchen; the API returns 403 and the attempt is audited.

## New in v2

- **Languages:** English, German, Vietnamese and Chinese. Use the picker on the login page or in the sidebar. Numbers and dates follow the chosen language.
- **Download menu on every chart:** chart image (PNG, SVG) or table data (Excel, CSV).
- **Full lists:** customer segments (manager and above) and product segments. To open the download dialog, choose "Full list…" in the menu, or click a bar or slice on a segment chart; that segment is then preselected.
- **Trend line on revenue-type time series with more than 5 points:** a moving average by default, or a linear trend or none. The chip shows the growth per period.
- **Stability:** fixed a crash under parallel load on SQLite (see ARCHITECTURE.md §14).

## New in v3

- **Phone layout** (screens up to 820px wide, or landscape phones):
  - A compact app bar holds the menu, the current section and a Filters button.
  - Section tabs scroll sideways under the app bar.
  - The sidebar becomes a slide-in menu, and the filters open as a bottom sheet.
  - KPI tiles show two per row.
  - Tables become one small card per row.
  - The ⓘ explanations open on tap.
  - The sales heatmap uses 3-hour blocks, and the cohort grid shows months 0–6.
- **Installable app (PWA)** on Android, iPhone and desktop Chrome/Edge:
  - It gets its own icon, runs without the browser bar and has shortcuts to sections.
  - It starts offline, showing an "offline" notice rather than old numbers. **No business data is stored on the device.**
  - When a new version is deployed, a "Reload" banner appears.
  - Downloads go through the Share sheet inside an installed iPhone app.

## New in v4

- **Pinned page header on laptop and desktop screens.** The page title, period, comparison, grain and theme controls stay at the top while you scroll, so you can change them from anywhere on the page. Once you scroll, the header shrinks (the description line is hidden) and gets a shadow.
- **Phones are unchanged:** their app bar, with the Filters button, was already pinned in v3.

## New in v5

- **Drill-down from any chart to its detail rows.** Click a bar, slice, point on a trend, or heatmap cell. A panel lists the sales lines or orders behind that value, with the row count and total.
- In the panel, **Download all rows** gives the full list (Excel or CSV).
- **Phones:** tap once for the tooltip, and tap again for the rows.
- **Personal columns** (customer, salesperson) appear only for managers and above.
- Try it in the demo:
  - On the Overview, click a day on the revenue trend.
  - Click the top product.
  - On Sales, click the busiest heatmap cell.

## New in v16 (9.3.4): faster period changes

- **Common periods are ready in advance for every section.** These are: last 7, 30 and 90 days, month, quarter and year to date, last 12 months, and previous month. The period evaluation is ready in advance too.
  - Before, only Overview and Sales were prepared, and only right after a data reload. After every server restart or cache expiry they were computed from scratch, which took about 10 s on the Windows demo machine.
  - Now the refresher fills in whatever is missing every 15 minutes, and right after start-up. On the test copy this takes about 11 s in the background.
- **The SQLite warehouse is kept in memory once read** (`sqlite_cache_mb`, default 64 MB, and `sqlite_mmap_mb`, default 256 MB, can be set per data source in `datasources.json`). This matters most when the data folder is on a slow or synced drive.
- **Slow requests are logged.** Any section or evaluation that takes longer than `query.log_slow_seconds` (default 1 s) is written to the server log, with its 5 slowest KPIs.
- **Warehouse:** from loader v014 (LOADER 1.9), Step 4.1 adds a calendar index on (tenant_id, full_date). It is created as soon as Step 4.1 is opened with the warehouse selected. Period queries then read only that period's rows, which was 6× faster for a 3-month period in the test.

## New in v15 (9.3.3): new versions load automatically

- When a new dashboard version is deployed, the page now switches to it by itself; a short "Updating to the new version…" line appears first.
- It never reloads while you are in the middle of something: a half-typed login, an open drill-down panel, the export dialog, the filter sheet or menu drawer, or a field you are typing in.
  - In that case the banner with **Reload** stays.
  - The update is applied as soon as you finish, or when you switch to another tab.
- The dashboard also checks for a new version whenever you come back to its tab, not only every 30 minutes.

## New in v14 (9.3.2): payment charts grouped by payment type

- **"Receipts by payment method"** and **"Payment method trend"** now group by payment *type*: bank transfer, cash, e-wallet, card, cash on delivery, buy now pay later, voucher, trade credit, other.
  - Before, they grouped by the name the shop wrote, so "BIDV" and "Ngân hàng" were separate slices although both are bank transfers.
  - A name the load could not assign to a type is still shown under its own name.
- Payment types have labels in all four languages.
- Detail tables still show the payment name as the shop wrote it (e.g. BIDV).
- Two tests now read their expected values from the demo warehouse (number of Champions, latest sales date) instead of hard-coded numbers, so they keep passing after a tenant reload.

## New in v13 (9.3.1): order status labels

Order and line status charts now have labels for the template's order statuses `shipped` (shown as "In delivery"), `placed` and `confirmed`, in all four languages. With TMPL-RETAIL-TRADE-004 v1.4 the loader writes these standard values instead of the source's own words, so "Orders by status" and "Order lines by status" show the same statuses. The old raw-word label (`Đang giao`) stays for warehouses loaded before the fix.

## New in v12 (9.3): evaluation and warning rules in a config file

All Business evaluation and Early warning rules now live in **`config/rules/retail_trade_004.json`**. Edit the file and the dashboard uses the new rules within a minute. No code change and no restart are needed.

**What you can change by editing the file**

- **Thresholds:** e.g. warn when revenue is 25% below a normal week instead of 20%.
- **Urgent vs Watch limits.**
- **Ranking weights.**
- **Which rules run:** `"enabled": false` switches a rule off.
- **"See chart" links.**
- **Who sees customer names:** it can be raised, but never lowered for personal data.
- **Titles,** optionally per language, e.g. `"title": {"en": "...", "vi": "..."}`.
- **New KPI-change rules:** add a rule with `"check": "kpi_change"` and any single-figure KPI, e.g. items per order.

**Check your edit first**

```bat
python tools\check_rules.py              & rem validates the file and every business's overrides
python tools\check_rules.py --describe   & rem check types, parameters, allowed ranges and defaults
```

**Safeguards**

- An invalid file is rejected as a whole and the last valid rules stay in use. The error is written to the log and shown in `/api/admin/status`.
- Every value is range-checked, and pairs are order-checked (e.g. the watch limit must not exceed the urgent limit).
- Unknown fields, rules, KPIs or data sets are reported. The tool lists all problems at once.

**Per-business changes** stay in `config/tenants.json` → `alerts`:

- `disabled`: switch a rule off for this business;
- `enabled`: switch on a rule that is off in the rule file;
- `params`: change thresholds for this business only.

**Still needs code:** a new *type* of check (new logic) or a new data set (new query). Queries stay in code on purpose: a config edit can never bypass the per-business data separation.

## New in v11 (9.2)

- **No internal codes on screen.** Charts no longer show KPI codes (such as KPI-SALES-014), and the page footer no longer shows the template name or KPI counts.
- **Every chart and figure has an "About" text written for business owners:** what it shows and how to use it. All 185 texts are in English, German, Vietnamese and Chinese.
- **Plain titles instead of analyst jargon.** For example "RFM segments" became "Customer segments by buying behaviour", "ABC analysis" became "Products by revenue importance (A / B / C)" and "Annual revenue at risk (churn >= 50%)" became "Yearly revenue at risk from customers likely to leave".
- **Implementation details stay internal:**
  - model names (the forecast table's "Model" column is removed);
  - technical model notes stored with predictions (method names, accuracy scores);
  - technical error messages;
  - table and column names.
    Platform admins still see them.
- **"More charts can be unlocked" panel (business admins).** It now says in plain words which data each chart needs, e.g. "Needs: Product cost prices".
- Demand patterns read "Irregular, big swings" and "Occasional, some days only", instead of "Lumpy" and "Intermittent".

## New in v10 (9.1)

- The Executive Overview header now matches the other sections: the period, compare and grain controls sit on the same row as the title (the longer section description wraps instead of pushing them down).
- Business evaluation: *What's going well* is light green, *What needs attention* is light pink.
- Early warnings: urgent warnings are light pink, watch warnings are light yellow. Dark mode uses darker versions of the same colours.

## New in v9

- **Business evaluation on the Executive Overview.** Below the headline tiles, two columns: *What's going well* and *What needs attention*, for the selected period against the chosen comparison. Each line is one plain sentence with its numbers (e.g. "Repeat purchase rate fell 13.9 pts to 21.9%"). Click a line to see its detail rows or jump to the chart it comes from.
- **Early warnings in AI Insights.** A panel at the top lists short-term risks found in the latest data (7–30 days ahead), each tagged **Urgent** or **Watch** and with its basis: **Forecast**, **Model**, **Recent trend** or **Data feed**. The panel ignores the period filter.
- **Warning strip and bell.** The Overview shows a strip ("3 early warnings · 1 urgent"), and a bell with the count sits in the page header (on phones, in the app bar). Both open the Early warnings panel. The AI Insights menu item shows the count too.
- **Fixed rules, no AI text generation.** The same data always gives the same verdict. A KPI-based finding uses exactly the KPI tile's number.
- **Roles and tenants as everywhere else.** Customer names in a finding's list are for managers and above. Viewers still see the finding and its count.
- **Downloads.** The whole panel, or one finding's rows, as Excel or CSV.
- **Per-business settings** in `config/tenants.json` → `alerts`: switch a rule off (`disabled`) or change its thresholds (`params`). Phụng Kitchen has the "No new data" warning off, because the demo data ends in Feb 2026.
- On Phụng Kitchen's data (last 30 days): 7 findings going well, 4 needing attention, and 3 warnings (1 urgent).

## New in v8

- **ABC analysis can now drill down.** Click the A, B or C slice (Products & Assortment) to see the sales lines of every SKU in that class. The total in the panel is that class's revenue.
- The A/B/C class is now worked out in the warehouse (SQL), so the chart and its rows always use the same SKUs.
- Sales lines without a product are no longer counted as an extra "SKU" in the ABC chart. In the demo data this moves one "SKU" out of class B for some periods.

## New in v7

- **AI Insights now shows only forward-looking numbers**, 6 charts:
  - demand forecast for the next 7 days, in total and by product;
  - customers by churn risk, annual revenue at risk, and high-value customers at churn risk;
  - predicted future customer value.
- **Removed "Forecast back-test".** It checks forecast accuracy for development, not for business owners. The data stays in the warehouse.
- **Descriptive charts moved** and now appear once, in their home section:
  - Products: product momentum, emerging & core-hero products, product importance clusters, frequently bought together, and price-demand correlation.
  - Customers: RFM segments, average customer lifetime value, average CLV by RFM segment, and segment x category affinity.
- Overview still shows the next-7-day forecast and annual revenue at risk.

## New in v6

- **Removed the "Behavioural customer clusters" donut** (AI Insights) and the "Behavioural cluster" column and filter in the Customer segment list download.
  - Its groups were raw k-means IDs (SEGMENT_0 to SEGMENT_5). They had no business meaning, and a rerun could renumber them.
  - Template tenants get customer segments from the RFM mart, which gives the same result on every run. K-means is no longer run for them, so the chart was showing an old one-off snapshot.
  - The RFM segment charts (customers, revenue and average CLV by segment) still cover how customers are split.

## Testing the PWA

A phone only installs the app over **HTTPS**. `http://localhost` counts as secure, which is why the laptop test below works without it.

1. **Laptop (Chrome or Edge):**
   - Open http://localhost:8050. Click the install icon in the address bar, or use **Install app** in the sidebar.
   - Press F12 → **Application** → **Manifest** / **Service workers** / **Cache storage**. The cache should hold only app files and `/api/i18n/*`; business data from `/api/tenants/...` should never appear.
   - Network tab → **Offline** → reload: the app opens with the offline notice.
2. **Android over USB:**
   - Enable USB debugging on the phone and connect it.
   - On the laptop, open `chrome://inspect/#devices` → **Port forwarding** `8050 → localhost:8050`.
   - On the phone, open http://localhost:8050 in Chrome → menu → **Install app**.
3. **Any phone, including iPhone, through a temporary HTTPS link:**
   - Run `cloudflared tunnel --url http://localhost:8050` and open the printed `https://….trycloudflare.com` link on the phone.
   - Android Chrome: **Install app**. iPhone Safari: **Share → Add to Home Screen**.
   - **Change the demo passwords first**, because the link is public while the tunnel runs. Also set `"cookie_secure": true` in `config/settings.json`.
4. **Check:**
   - icon and name on the home screen, and no browser bar
   - login still valid after closing and reopening the app
   - language and dark mode remembered
   - offline notice in airplane mode
   - Excel/PNG downloads (on iPhone they open the Share sheet)
   - rotation to landscape
5. **Update test:** edit any file in `web/` and restart the server, then reopen the app. The "A new version is available — Reload" banner should appear.
6. **Reset:**
   - Desktop: `chrome://apps` → Remove, then DevTools → Application → Storage → **Clear site data**.
   - Phone: uninstall the app, then clear the site data in the browser.

## Layout

```
run_dashboard.py         start the web server and the background refresher
vdash/                   backend: config, datasources, security, catalog/, engine, evaluation, refresher, state_store, api, exports, drill, i18n/
web/                     frontend: index.html, app.js, app.css, sw.js (service worker), manifest.json, icons/, vendor/echarts.min.js (Apache-2.0)
config/                  settings.json, datasources.json, tenants.json, users.json, rules/<catalog>.json (evaluation + warning rules)
tools/                   check_catalog.py, check_rules.py, portability_check.py, manage_users.py
tests/                   pytest: isolation, roles, availability, periods, catalog, translations, downloads, parallel load, PWA, drill-down, evaluation, warnings
runtime/                 created at runtime: dashboard_state.db, session_secret.key
```

## Checks

```bat
set VDASH_DEMO_DB=D:\work\my_ai_projects\generative_bi\latest_system\ui_orchestrator\output\dw_tmpl-retail-trade-004_20260923_1220.db
python -m pytest tests -q
python tools\check_catalog.py --tenant phung-kitchen --all-grains
python tools\portability_check.py --sqlite %VDASH_DEMO_DB% --ddl <TMPL-RETAIL-TRADE-004_unified_warehouse_ddl.sql> --target chdb
```

The `portability_check.py` command needs `pip install chdb`. To check against a PostgreSQL server instead, use `--target postgres --pg-url ...`.
