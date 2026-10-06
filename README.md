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

The demo warehouse path is set in `config/datasources.json`. By default it is `dw_tmpl-retail-trade-004_20260923_1220.db` in this folder. To use another file, set `VDASH_DEMO_DB`, for example in `config/local.env` (next section).

## Local development and online (Render)

The code and the committed config files are the same in both places. Only environment variables differ.

**Local.** Copy `config/local.env.example` to `config/local.env` and set your values. The file is read at start-up, is listed in `.gitignore`, and is never copied to the deploy repo. A variable that is already set in the real environment wins over the file.

```ini
VDASH_ENV=local
VDASH_DEMO_DB=D:/work/my_ai_projects/generative_bi/latest_system/ui_orchestrator/output/dw_tmpl-retail-trade-004_20260923_1220.db
```

**Online.** Set these in the Render service under **Environment**:

| Variable               | Value                                           | Why                                                                          |
| ---------------------- | ----------------------------------------------- | ---------------------------------------------------------------------------- |
| `VDASH_SESSION_SECRET` | a long random string (Render: "Generate")       | Without it a new key is made on every deploy/restart and everyone is signed out |
| `VDASH_ENV`            | `online`                                        | Optional: it is `online` automatically on Render                             |
| `VDASH_DEMO_DB`        | only if the `.db` is not in the repo root       | Warehouse path                                                               |

Build command on Render (v20): `pip install -r requirements.txt && python -m vdash.refresher --once`. The second part pre-computes the common periods, the evaluation and the warnings into `runtime/` during the build, so every start, restart or wake-up begins with a warm cache instead of making the first visitor wait. The start-up log then says `result cache file result_cache.db: N stored result(s)`.

Start command on Render: `python run_dashboard.py`. With the `online` profile it listens on `0.0.0.0` and on Render's `$PORT`. If the service starts `uvicorn vdash.asgi:app` instead, host and port come from that command (`--host 0.0.0.0 --port $PORT`).

**Profiles.** `VDASH_ENV` selects a profile. For every config file `<name>.json`, an optional `<name>.<profile>.json` is merged on top. `config/settings.online.json` sets host `0.0.0.0` and secure cookies (https only). Put any other online-only setting there, e.g. fewer `refresh.prewarm_presets`.

| Setting        | Local                     | Online                    | Change with                          |
| -------------- | ------------------------- | ------------------------- | ------------------------------------ |
| Warehouse      | `VDASH_DEMO_DB` in local.env | `.db` in the repo root  | `VDASH_DEMO_DB`                      |
| Host           | `127.0.0.1`               | `0.0.0.0`                 | `VDASH_HOST`                         |
| Port           | `8050`                    | `$PORT`                   | `VDASH_PORT`                         |
| Secure cookie  | off (http)                | on (https)                | `VDASH_COOKIE_SECURE=true/false`     |
| Session key    | `runtime/session_secret.key` | `VDASH_SESSION_SECRET` | `VDASH_SESSION_SECRET`               |

The log's first line says which profile is used and which values came from `config/local.env`.

**Publishing to the deploy repo.** `latest_system/dashboard` is the source. Copy it into the GitHub repo with:

```bat
python tools\sync_to_deploy.py            :: dry run: lists new / changed files, writes nothing
python tools\sync_to_deploy.py --apply    :: copy them; then git add -A, commit and push in the repo
```

The target is `C:\Users\jerry\Documents\GitHub\veridex-test` (or `--target`, or `VDASH_DEPLOY_REPO` in local.env). The script never copies `config/local.env`, `runtime/` or caches, and never deletes anything in the repo. The demo `.db` is copied only with `--with-db`; `tests/`, `tools/` and `ARCHITECTURE.md` only with `--include-dev`.

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

## New in v48 (9.27.0): margin and business-customer blocks, only when the data exists (AI plan Step 22)

The report and the dashboard now have margin and business-customer blocks. A business that records no costs or no customer types sees none of them (nothing empty, no zero-filled charts).

- **Margin** (Products, and the margin tiles on the first page as before): *Gross margin over time*, *Products with the lowest margin* and *Share of sales with a known cost*. A margin is now measured on the sales whose cost is recorded, so lines without a cost can no longer make the margin look better than it is. The "known cost" share tells the reader how far to trust it. A column that is only zeros no longer counts as cost data.
- **Business customers** (Sales and Customers): *Revenue from business customers* (share of revenue), *Revenue trend by customer type*, *Average order value by customer type* and *Top business customers* (names visible to managers and above; in the AI fact sheet they are "Customer A, B ...", never real names). Customers count as business when their type reads business, B2B, wholesale, corporate, company, trade, reseller, distributor, enterprise, or the Vietnamese equivalents (list: `B2B_TYPES` in `vdash/catalog/retail_trade_004.py`).
- **Evaluation:** a change in the business-customer share of 3 points or more becomes a finding (`chg_b2b_revenue_share` in `config/rules/retail_trade_004.json`; threshold editable there). Margin changes were already covered by `chg_gross_margin_pct`.
- **Mechanism:** a data requirement can now carry a condition (`Req.where`) - here "at least one business customer exists". The blocks reach the report through the existing block list (every KPI is a block), so the customer can switch them on or off like any other.
- Translations in English, Vietnamese, German and Chinese for every new title and text.
- The demo warehouse has no costs and no customer types, so these blocks are hidden there; `test_margin_and_business_blocks_appear_only_when_the_data_exists` fills them into a copy and checks the figures.

## New in v47 (9.26.0): the report PDF without a browser (AI plan Step 24, third part)

Mails now carry the PDF on any server. When Chrome, Edge or Chromium exists (your computer) its print layout is used as before; where there is none (Render's free server) the report is drawn directly with ReportLab, so a scheduled mail there has the PDF too.

- **What is in it:** the same content in the same order as the report page - cover, "At a glance" (headline, key messages, since your last report, key figures with trend lines, top actions), contents, the story chapters with their figures, the root causes and the action plan, then the appendix with every figure and table. Charts are drawn as vector graphics: bars and lines (second measure in its own panel, dashed linear trend, numbered unusual points), bar charts with the highlighted items, rings, heat maps. A4, text selectable, 70-190 KB.
- **Languages:** English, Vietnamese, German and Chinese. Fonts are bundled and embedded (`vdash/reports/fonts`: DejaVu Sans for Latin and Vietnamese, Droid Sans Fallback for Chinese characters; licences included).
- **Choosing the engine:** `VDASH_PDF_ENGINE` = `auto` (default: browser if found, built-in layout when there is no browser or printing fails), `browser` or `native`. `send_report_email.py` prints which layout made the PDF.
- Needs `reportlab` (in `requirements.txt`; on Render it is installed with the next deploy).
- Differences from the browser layout: no clickable chart tooltips (paper), the contents list has no page numbers, and Chinese text is regular weight only (no bold font for it).

## New in v46 (9.25.0): scheduled reports are mailed to confirmed addresses (AI plan Step 24, second part)

When a schedule makes its report, the scheduler now mails it (summary in the body, full report as a PDF) to the addresses of that schedule - **but only to addresses that confirmed by link**. Anyone else on the list is skipped (`not_confirmed`) and receives nothing.

- **Confirming:** a business administrator (role `tenant_admin` or higher) adds an address (`POST /api/tenants/{tid}/report-recipients`). The address gets one mail with a link; the link opens a page with a button, and only pressing the button confirms (a mail scanner that opens links does not). The link works once and expires after 7 days. Every report mail has a "Stop receiving these reports" link (and the one-click `List-Unsubscribe` header that mail programs show as an Unsubscribe button).
- **Limits** (`config/report_mail.json`, `mails_per_month` per plan in `config/report_plans.json`): addresses per business, confirmation mails per address and day, addresses per schedule, mails per month. A temporary Resend problem is retried after 1, 5 and 30 minutes (same idempotency key, so never a double mail); a refused mail is not retried. A mail that did not go out is not counted.
- **Settings in the environment:** `RESEND_API_KEY`, `RESEND_FROM` and **`VDASH_PUBLIC_URL`** (the address of this dashboard, for the links in mails). Without the address no mail with a link is sent.
- **Result on the schedule:** `last_status` = `sent` (at least one mail went out, none failed) or `failed` / `mail_failed`; each mail has its own row (`report_deliveries`: sent, skipped with a reason, failed).
- **Try it with a real mail to your mailbox** (a throw-away database; your real one is only read):
  `python tools/try_mail_schedule.py --to you@example.com` - it sends the confirmation mail, waits for your click (the link opens a page of this script on your computer), then runs a schedule as if it were the 1st of September and mails the August report with the PDF.
- Render's free server has no Chrome: since v47 the PDF is drawn with ReportLab there.

## New in v45 (9.24.0): report e-mail through Resend (AI plan Step 24, first part)

A stored report can be mailed through Resend's HTTP API (Render blocks SMTP, so HTTPS is used): the mail body repeats page 1 of the report - the headline, three key messages and the "Since your last report" lines - and the full report is attached as a PDF. This first part is a test tool; the scheduler will use the same code to send scheduled reports next.

- **Try it** (a real mail to your own mailbox):
  1. In Resend, verify a domain and create an API key. Put `RESEND_API_KEY=re_...` and `RESEND_FROM=Veridex <reports@your-domain.com>` in `config/local.env` (never committed or synced). Without a verified domain, `onboarding@resend.dev` delivers only to the e-mail address of your own Resend account.
  2. Create a report in the dashboard, then run: `python tools/send_report_email.py --user <report owner> --to you@example.com`
  3. `--dry-run` builds the mail and the PDF into `./mail_preview/` and sends nothing; `--report-id` picks another report (default: the owner's newest); `--no-pdf` leaves the PDF out.
- **PDF:** made by printing the report's own page with Chrome, Edge or Chromium (found automatically; `VDASH_PDF_BROWSER` = full path to override). If none is found (for example on a small web server) the mail is still sent, without the PDF, and says so.
- **Safety:** the key is only read from the environment and never appears in files, logs or error messages; a retry after a network error or a server error uses the same idempotency key, so Resend never delivers a mail twice; subjects and names in the mail are escaped (no header or HTML injection); the mail has no scripts, images or links.
- The reading of the report store is read-only. Nothing is sent unless you run the tool.

## New in v44 (9.23.0): the report scheduler (AI plan Step 23)

Schedules now make reports by themselves. This step is the engine; the schedule screen (Step 25) and sending by e-mail / Telegram (Steps 24, 26) come next, so for now a schedule is created in the report store and its reports appear in the owner's "My reports".

- **Which period.** Always the last *complete* week, month or quarter before the run date, in the business's time zone (weekly: Monday to Sunday). Never a rolling window, so two regular reports never overlap and "Since your last report" compares them.
- **Who runs it.** The schedule's owner, with the role the owner has at that moment. The report is made like a manual one: same limits, same AI story, counted in the plan, kept in the owner's list. If the owner or the business is gone, the schedule is switched off.
- **Late data.** The run waits up to 3 days for the data to reach the end of the period, then is skipped (`data_not_ready`). A report with a half period is never made.
- **After downtime** (free servers sleep): only the latest missed run is made, and only if it is less than 4 days (weekly), 7 (monthly) or 10 (quarterly) late; otherwise it is skipped (`missed`).
- **No duplicates.** A run is claimed with one database update before anything is made, so several servers never make the same report twice. If the server dies exactly between the claim and the report, that run is lost, never doubled.
- **Outcome** is kept on the schedule: `last_status` (created / skipped / failed), `last_error` (a short reason: `data_not_ready`, `missed`, `quota`, `nothing_to_show`, `owner_gone`, `business_off`, `settings_gone`, `report_failed`) and `last_report_id`; also in the audit log. Platform admins see the scheduler under `/api/admin/status` -> `report_scheduler`.
- **Settings:** `config/report_schedule.json` (check: `python tools/check_report_blocks.py`); switch off with `"report_scheduler": {"enabled": false}` in `settings.json`. The scheduler looks for due schedules every minute, which also keeps a free Aiven database active.
- **Database:** two nullable columns (`lang`, `last_report_id`) are added to `report_schedules` automatically on first use; an existing database keeps working, and older dashboard versions ignore them.

## New in v43 (9.22.0): "Since your last report" (AI plan Step 21)

A new report is compared with your previous report of the same kind, but only when the comparison is safe and clear. Otherwise nothing is shown, never a confusing number.

- **When it compares.** Same report blocks, periods that do not overlap more than half, at most one period of gap, and lengths within 3 days (or 12%). It always uses the closest earlier report. Reports made before v43 carry no saved figures and are never compared. Rolling windows (for example "last 30 days" every week) overlap too much and get no comparison, by design.
- **What it says.** Only clear changes (at least 10%, or 2 points for percentages, and more than normal noise for counts). When month lengths differ, revenue, orders, units and new customers are compared per day. Early warnings are reported as new, still open or resolved only when both reports checked the rule.
- **Where it shows.** Three lines on page 1, the details in the chapter they belong to. If nothing changed clearly, one steady line.
- Settings: `config/report_changes.json` (checked by `tools/check_report_blocks.py`). Rule-written, no AI; warnings and actions are not sent to the AI writer.

## New in v42 (9.21.0): charts made for reading (AI plan Step 20)

In a report with an AI-written story, the charts now point at what matters, and the text and the charts refer to each other. A report without a story looks exactly as before.

- **Unusual days are marked.** On a trend chart, a day that is far from the usual level gets a numbered dark badge (above a high point, below a low one). Under the chart, one line per badge says what it is: "(1) Unusually high: <date> at <value>, <n>% above the typical level of <value>." At most three badges; a chart with nothing unusual shows none.
  - "Unusual" is plain statistics, not AI: the median and the median absolute deviation (robust against one huge day), a score of at least 3.5, and a difference of at least 35% from the typical level. Charts with fewer than 14 points, stacked charts and second-axis lines are not marked.
- **The item the text talks about is highlighted.** On a bar or donut chart, the bars/slices named in the chapter's paragraphs or in the chart's caption stay strong (outlined bar, slice pulled out) and the others are drawn lighter. A line under the chart names them ("Highlighted: …"). Names are matched ignoring capitals and accents. Short labels ("A", "1") and labels that carry their own numbers are never matched. If every item is named, nothing is highlighted.
- **Figures are numbered and linked.** "Figure 2.1 · What to see: …". The paragraph that talks about the highlighted item ends with a link "(see Figure 2.1)"; otherwise figure *j* is linked from paragraph *j* of its chapter. These links and notes are added when the page is drawn; the stored text is not changed, so the number guard and the stored report are untouched.
- The appendix charts get the unusual-day badges too.
- Four languages (vi, en, de, zh). New texts: `story_fig`, `story_see_fig`, `story_highlighted`, `story_unusual_up`, `story_unusual_down`.
- Not changed: the dashboard's own charts, the Excel file, the story writer and its prompts (no new AI call, no new cost).

## New in v41.1 (9.20.1): "Your report is ready" notice

Before, a report finished silently when the person had left the Reports page; it only showed up in "My reports". Now every report being written is watched until it is done, wherever the person is in the app:

- **A pop-up notice** in the bottom-right corner (full width on a phone): "✓ Your report is ready", with the report's name and an **Open** button. It stays until it is opened or closed.
  - A failed report gets a red notice with the reason; it was not counted.
  - It also works from another section and after switching business (the notice then names the business).
  - It also works after a page reload: the reports being watched are kept in this browser, and reports still being written on the server are picked up after sign-in.
- **Waiting in the builder:** the report opens by itself, as before, with no extra notice.
- **The tab is in the background:** the tab title gets "✓ Your report is ready · …". If the person allowed notifications, a system notification appears too. The browser asks once, when the first AI report is created.
- Checked in Chromium: notice after leaving the builder, notice after a reload, Open shows the report, and no notice when waiting in the builder.

## New in v41 (9.20.0): reports written in the background, AI quota per plan (AI plan Step 17)

**Creating a report no longer blocks the screen for 1–3 minutes.**

- "Create report" composes the report at once, checks the limits and counts it. The numbers are those of that moment. The AI analysis is then written in the background.
- The builder shows the progress: step (getting ready / writing the analysis / saving) and elapsed time. It opens the report when it is done. On a phone, the new tab says "Writing your report…" and turns into the report.
- The person can keep working meanwhile:
  - "My reports" shows the report as *Being written…* until it is done.
  - A report that could not be made shows *Could not be created – not counted* for a day.
  - Clicking the same report again follows the one being written; it is not made or counted twice.
- **Restarts are safe** (Render redeploys, sleeps).
  - While a job is being written, its process renews a heartbeat. After a restart, any running process takes over a job whose heartbeat is older than `stale_after_seconds` and writes it again from the stored document. The AI answer usually comes from the AI cache.
  - The report is stored in one step under the job's id, so it is never half stored and never stored twice.
  - After `max_attempts` the job fails cleanly, and the report and AI counts are given back.
  - Tested by killing the server in the middle of writing a report: after the restart the job was resumed, one report was stored, and the counts were unchanged.
- **AI quota per plan:** `ai_reports_per_month` in `config/report_plans.json`. Trial 3, Starter 10, Growth 40, Pro 150, Enterprise unlimited; adjust as you like.
  - Only AI-written reports count against it.
  - When the quota is used up, the report is still created with the standard texts and a note on the cover: used up, x of y, available again from …
  - When the AI fails, the report gets the standard texts and a note, and it is not counted as an AI report.
  - The builder shows "x of y AI-written reports used this month" and says when they are used up.
- **Switch AI off per business:** `"ai_reports": false` in `tenants.json`. That business gets the standard texts with no note.
- Platform admins (support) are never counted.
- Settings: `report_jobs` in `settings.json` (workers 2, stale_after_seconds 180, beat_seconds 20, max_attempts 3). `/api/admin/status` shows the running jobs.
- New tables `report_jobs` and `report_ai_usage` are created automatically in the report database (Aiven).
- Tests: 98 passed. New tests cover background progress, 5 parallel creates against a limit of 3, resume / already stored / give up after a restart, the AI quota, AI failure, AI switched off, and platform admins.

## New in v40 (9.19.0): the story layout (AI plan Step 16)

A report with an AI-written story (`story.source` ai or mixed) now reads like a consultant's report. The page and the PDF are in this order:

1. **Cover.**
2. **At a glance:**
   - the headline and key messages;
   - six key figures;
   - the top 3 priorities (who · when), with a link to the full plan.
3. **Contents:** the four chapters and the appendix.
4. **The four chapters** (What happened / Why it happened / What could come next / What to do about it):
   - the AI's paragraphs;
   - the charts the AI chose. They can be any printed part, or an explanatory part the report did not print. Each chart has "What to see:" and the AI's caption under it.
   - "Why" also has the diagnosis: root causes with how sure they are, hidden connections, and what is worth checking.
   - "What to do" has the **action plan**. Each action is a card with why, steps, who, when and the expected effect.
5. **Appendix: all figures:** every part the user ticked, as before (A1, A2 …). Its page and Excel file are smaller and are there for checking details.
6. "Not included", then the **AI footer** with the date: "The story in this report was written with AI on … from the report's own figures. Every number was checked; causes marked likely or possible are not proven."

Other details:
- **Phone:** chapters and plan cards stack. Wide tables now scroll inside their box instead of widening the whole page. This was also a phone bug in the old layout: some finding tables made the page 893 px wide on a 390 px screen.
- **Excel:** a **Story sheet** comes first, before Summary. It has:
  - the headline and messages;
  - the four chapters, each chart with what to see and a ↗ link to its data sheet;
  - the diagnosis and the action plan;
  - the AI note.

  The Summary's contents link to it. The rest is as in v35.
- **Unchanged:** a report without an AI story (AI off, no key, quota, or an older stored report) renders exactly as before, apart from the table scroll box. This was checked by comparing the HTML with v39.7's in German and Chinese.
- Checked:
  - real AI stories (from `story_eval_20261002_1835`) printed to A4 in Chromium, in Vietnamese and English;
  - German and Chinese in the old layout;
  - a 390 px phone with no page-wide scrolling in any of the 4 languages;
  - the Story sheet converted with LibreOffice.
- Tests: 94 passed.

## New in v39.7 (9.18.7): fairer limits for Vietnamese, captions repaired

In the run of 18:35 (v39.6), no report blamed the drop on the salesperson switch; all four call it a recording question to check. Three reports needed one attempt ($0.073–0.085). Vietnamese standard needed two ($0.158, 244 s) because of:
- chart captions with rounded numbers ("75%" for 75,4%);
- two questions "too long" at 31–32 words.

- **Word limits per language.** Vietnamese writes every syllable as a separate word, so the same sentence counts about 1.4× more "words" than in English. Before, Vietnamese texts were cut for length unfairly. The checks now multiply the limits by `word_factor` in `story.json` (vi 1.4).
- **A faulty chart caption is replaced by the chart's own title.** The chart stays in the story, so this no longer counts toward the 2 items that may be left out without a second attempt.
- Tests: 93 passed.

## New in v39.6 (9.18.6): the salesperson switch is a data question, not a cause

In the run of 18:09 (v39.5, `--fresh`), all 4 reports needed one attempt: 94–157 s and $0.057–0.094 each ($0.32 in total). Small faults were repaired without a second call. Vietnamese is now as reliable as English.

Fixes from reading those reports:
- **"Everything now under one item."** When one item now carries ≥95% of a breakdown and another that had ≥20% went to zero, the driver fact gets a note: this is usually a change in how sales are recorded (or a staff change), and it does not explain the total change. Example: "Staff •3400 → 0, Staff •2340 → 100%".
  - Prompt `story-5`: such a driver is a data check, never the cause in the headline.
  - The rule-written root causes skip it.
  - Before, two reports blamed the drop "mainly" on Staff •3400.
- **"₫ ₫" was caused by the label repair of v39.2.** It treated formatted numbers ("-19,91 Tr ₫") as labels and put the ₫ back a second time. Formatted numbers are no longer treated as labels.
- **Id remnants:** "F12: Night fell…; F13: …" no longer leaves ": Night…;:". The prompt also says never to refer to a fact by its id ("F3 shows …").
- Tests: 93 passed.

## New in v39.5 (9.18.5): `--fresh` for test runs

Stored AI answers are reused when the data, the prompt and the model are unchanged. So running `tools/eval_story.py` again re-checks the stored answers with the current checks and repairs, for free and within seconds; that is what the "$0" runs were. Add `--fresh` to ask the AI for new answers; this is charged at normal AI prices. Stored answers are now marked "(stored answer, re-checked)". `write_story(..., fresh="<any value>")` adds that value to the cache key.

## New in v39.4 (9.18.4): repairs inside chapters

In the v39.3 run, both English reports needed one attempt each: about 130 s and $0.07–0.08, with small faults repaired. Both Vietnamese reports still needed a second attempt. The cause was not the language: none of the language checks failed. The Vietnamese answers hit faults that could not yet be repaired:
- one chart caption had a made-up "72,1%";
- the 'do' chapter had the wrong number of paragraphs;
- on the low-thinking retry, fact ids were written inside the texts, which cost the root causes and hidden connections.

- **Chapters are repaired too.**
  - A paragraph or chart caption with a problem is left out when at least one paragraph remains.
  - Extra paragraphs or charts beyond the limit are cut.
- **Ids inside texts** ("… [F12]", "(F3, F4)") are removed when the answer is cleaned. They no longer count as a problem.
- **Number check:** a currency symbol written before a number ("Sep 4 ₫83.01M") belongs to the next number. Before, "4₫" was reported as a made-up number.
- Tests: 92 passed.

## New in v39.3 (9.18.3): fewer second attempts, small faults repaired

In the v39.2 run, the v39.2 fixes worked:
- top-N shares were correct (10.3%);
- "who" and "when" were in Vietnamese;
- "Staff •3400" was kept exactly;
- the forecast showed whole units.

But 3 of 4 reports needed a second attempt. Each one then took about 4 minutes and $0.15, and one report lost its whole action list because of a single word ("churn").

- **Repair instead of fall back.**
  - A list item with a problem (one action, one root cause, one hidden connection, one message or one question) is left out when enough good items remain.
  - A wrong "who" or "when" is left out of its action.
  - Only when too little is left does the rule-written part take over.
- **No second call for small faults.** When the first answer's only problems can be repaired that way, leaving out at most 2 items (`repair_without_retry` in `story.json`), the second paid call is skipped.
- **Cheaper retry.** A needed second attempt runs with thinking `low` (`retry_thinking`), because it only fixes the listed problems.
- **Better report.**
  - Averages have no "share of the total": "average daily revenue by weekday" no longer says "Saturday … 28.2% of the total". This applies to `revenue_by_dow`, `revenue_by_season` and `clv_by_segment` (`analysis.non_additive`).
  - Doubled currency symbols ("12,53 Tr ₫ ₫") are cleaned.
- **Prompt `story-4`.**
  - Root causes are decided first; a headline built on an unconfirmed cause must say so in words.
  - Revenue moving from one salesperson or payment method to another is not by itself the cause of a drop.
  - A currency symbol is written only once.
- `tools/eval_story.py` shows the first attempt's problems, whether the retry was skipped, and what was repaired.
- Tests: 91 passed.

## New in v39.2 (9.18.2): fixes from the first real AI reports

The first real DeepSeek reports passed every check and found real causes. Reading them closely showed five weak spots, fixed here:

- **Top-N lists are not the whole business.** KPIs with `LIMIT n` in their SQL (top products, brands, cities, regions, locations, promotions, suppliers, salespeople) list only the best n items.
  - Before, a "share" was computed over the listed items only. The AI then wrote that one shampoo was "36.7% of revenue"; the real figure is about 10%.
  - Now a cut-off list has no such share. Its chart text says "Highest: …" instead of "… of the total". The fact sheet marks it "top 10 only - not the whole total".
  - Where the list adds up the same money as revenue, each item gets `share_of_period_revenue`, a real share of the period's revenue.
- **Drivers from top-N lists** compare only the items listed in both periods. They have no totals, no shares, and no "new" or "gone": a product that dropped out of the top 10 has not disappeared.
- **Prompt `story-3`:**
  - The headline must not state a "likely" or "possible" cause as a fact.
  - Labels are copied character for character, so "Staff •3400" is never written as "Staff 3400".
  - "who" and "when" are written in the report language, with example words per language.
- **Checks and repairs:**
  - English "who" or "when" in a Vietnamese or Chinese report is a problem sent back to the AI. If it is still English after the retry, only that field is dropped; the actions are kept (`meta.repaired`).
  - Data labels with a dropped symbol are put back automatically.
- The forecast table shows whole units (40, not 39,81).
- Tests: 90 passed.

**Cost and time** with `deepseek-v4-pro` and thinking `high` (first real run):

- About $0.05–0.08 and 80–140 s per report.
- `VDASH_LLM_MODEL=deepseek-v4-flash` costs about a quarter per token. `VDASH_LLM_THINKING=low` spends fewer thinking tokens and finishes faster. Both may give less depth; Step 18 compares them on the same reports.

## New in v39.1 (9.18.1): room for DeepSeek's thinking

The first real run failed with "the answer was cut off (output limit)". DeepSeek V4 **thinks before it answers**: this is on by default, with effort "high". Its reasoning counts as output tokens, so the old limit of 7,000 tokens was used up before the answer was finished, twice.

- **More room:** the output limit is now 24,000 tokens (the visible answer is about 3–5k). An answer that is still cut off is retried once with **twice the limit**, up to `llm.max_tokens_ceiling` (64,000), instead of repeating the same request. The error is now called `too_long` and says how many tokens went to thinking.
- **Thinking is a setting:** `llm.thinking` = `off | low | high | max` (environment variable `VDASH_LLM_THINKING`, default `high`).
  - More thinking gives deeper analysis but is slower and costs more output tokens.
  - `off` is fastest and cheapest.
  - In thinking mode no temperature is sent, because DeepSeek does not accept one.
- **Time:** the timeout is now 300 s per request. With `high` thinking a report takes about 1–3 minutes to write. Step 17 will run it in the background with a progress bar.
- `tools/eval_story.py` prints the thinking setting and limits, and the thinking tokens of each report.
- Tests: 88 passed (doubling the limit, the ceiling, the thinking parameters for DeepSeek only, thinking tokens counted).

## New in v39 (9.18.0): the report as a senior analyst would write it

The AI now writes as a **senior business analyst and management consultant**. It finds the root causes behind the results, spots hidden clues and connections, says how sure each cause is, and recommends actions that address causes, not symptoms. For that it gets **prepared analysis** that the server calculates, so the AI never computes a number:

- **Revenue bridges.** How the change of revenue splits into parts that add up exactly, in money:
  - orders × spend per order (and spend per order = items per order × value per item);
  - active customers × spend per customer;
  - plus the discount rate.
  On the demo data: -₫112.6M = -₫71.2M from fewer orders and -₫41.4M from lower spend per order. Inside that, -₫235M from fewer items per order is partly offset by +₫194M from pricier items. That is a product-mix clue a dashboard does not show.
- **Drivers.** Every revenue breakdown the user may see is compared with the comparison period: time of day, weekday, payment method, salesperson, new vs returning, customer groups, channel, location, category … For each it gives the biggest drops and gains with their share of the total change, the biggest shifts in mix (points), and items that are new or gone. Examples:
  - night-time revenue -63.7%, explaining more than the whole drop;
  - Thursday and Sunday down 85–92% per day;
  - e-wallet down 62% while cash nearly tripled;
  - one salesperson's revenue gone entirely (a staff or a data question).
- **Unusual days:** days far from the period's average, and the last 7 days against the average.
- Settings: `config/report_story.json` → `analysis` (which KPIs form the bridges, which breakdowns are compared, which are averages, how unusual a day must be).

**The AI writer** (`config/report_prompts/story.json`, version `story-2`) works in five moves: diagnose, locate, connect, judge the evidence, prioritise. Besides the headline, messages, chapters and actions, it now delivers:

- **Root causes (2–4)**, each marked *confirmed / likely / possible*, with the evidence;
- **Hidden connections (1–3)**, each linking at least two facts from different areas;
- **Worth checking (0–3):** what to verify to confirm a cause, including possible data problems;
- for each action, the **expected effect**.

These new parts go through the same checks: exact numbers, cited facts, language, no technical words. A clue must cite at least two different facts. If the root causes fail the checks, they become rule-written ones from the bridge and the biggest driver. Failing clues or questions are left out.

**In the report:** the "Why it happened" section opens with the analysis and a **diagnosis box** (root causes with their confidence label, hidden connections, worth checking). The section appears even when no "why" parts were ticked, e.g. in the executive summary. Actions show their expected effect.

`tools/eval_story.py` now also prints root causes, connections and checks. Tests: 88 passed (3 new: bridge and driver arithmetic, unusual days, analysis facts in the report, diagnosis checks and page).

## New in v38 (9.17.0): the AI story writer with checks (AI plan, Step 15)

When a report is created, the AI writes the **story**: a headline, 3–5 key messages, short chapters for *what happened / why / what's coming / what to do*, with up to 2 chosen charts each and a caption, and an action plan of 3–6 actions with why, steps, who and when. It writes from the report's fact sheet (Step 13) via DeepSeek (Step 14).

**Checks** run on every answer, whatever the prompt says:

- **Number guard:** every number in the text must appear in the fact sheet exactly as written, with the same kind of unit. "23%" is not excused by a date "23 Sep". Counting numbers 0–10 ("3 things", "within 7 days") are fine. Invented, rounded or calculated numbers are caught.
- Every message, paragraph and action cites existing facts. Chart ids exist. Limits on counts and length are kept.
- No technical words (KPI, churn, SKU, …). No fact or chart ids inside the text.
- **Language:** each text is in the report's language. Product and customer names from the data are ignored for this check.
- **On a problem:** one retry, with the list of problems sent back to the AI. If it is still wrong, only the failing parts (headline, messages, a chapter, the action plan) are replaced by rule-written text; good parts are kept. If nothing usable is left, or the AI is off, has no key, gives an error or the quota is used, the whole story is rule-written. **The report is always created.**
- **Names:** "Customer A" placeholders are turned back into the real names in the stored report. Real names are never sent.

**What you see now:** the first page uses the AI headline, messages and action plan (with steps, who and when), and each of the four parts starts with the AI paragraphs. A short note says the texts were AI-written and every number was checked. The full new layout, with charts placed by the AI, comes in Step 16. Rule-written stories change nothing on the page.

**Prompt:** `config/report_prompts/story.json` holds the instructions, writing style per language, limits and the list of banned words. You can edit it without code; raise its `version` after a change.

**Stored with each report:** `story.meta`, with provider, model, prompt version, tokens, cost, time, attempts, the problems found and the fallbacks used.

**Try it on real reports** (where DeepSeek can be reached):
```
python tools/eval_story.py                       # 20 reports: 5 profiles x 4 languages
python tools/eval_story.py --langs vi,en --profiles standard,executive
```
It writes `runtime/story_eval/story_eval_<time>.md` (every story, readable, with checks, cost and time) and a `.json` summary. Answers are cached, so a second run is free.

Tests: 85 passed (6 new: number guard and language check, all 5 profiles × 4 languages through the writer, retry with feedback, fallback per part, wrong language, broken answer, AI unavailable / no key / quota / refused key, names never sent but restored, the story stored with the report and shown, cache and usage, prompt file validation).

## New in v37.1 (9.16.1): AI check from the browser

- `/api/admin/llm/test` now also answers GET, so the AI check opens directly in the browser's address bar (signed in as platform admin). Before, only POST worked and the browser showed "Method Not Allowed".

## New in v37 (9.16.0): AI connection (AI plan, Step 14)

The dashboard can now talk to an AI provider. Reports do not use it yet; the story writer comes in Step 15.

- **One adapter** (`vdash/llm.py`) for **DeepSeek** (default, model `deepseek-v4-pro`), Gemini, OpenAI and Anthropic. It uses plain HTTPS: no SDKs and nothing new to install on Render.
- It always asks for a JSON answer and checks it strictly.
- **One automatic retry** on a timeout, a provider error (HTTP 5xx), unreadable JSON or an answer cut off at the length limit. A wrong key (401/403) or an exhausted balance (429) is not retried.
- Every failure has a plain reason: no key, switched off, timeout, key refused, limit, unreadable answer, declined.
- **The API key comes only from the environment:** `config/local.env` locally, the Render "Environment" settings online. It never appears in files that are synced, in logs or in error messages, even when the provider echoes it back.
- **Cost:** tokens from the provider × prices in `settings.json` → `llm.prices` (USD per 1M tokens; DeepSeek V4 Pro peak price 1.32 in / 3.96 out). Keep them up to date.
- **Cache:** an answer is stored in the report database. The same request is never paid twice, also after a restart: same business, data version, report, language, prompt version and model. Entries are kept 120 days.
- **Usage per business and month:** calls, failures, tokens and cost (table `report_llm_usage`). It is the basis for the AI quota per plan in Step 17.
- **Settings** (`settings.json` → `llm`): `enabled` (`VDASH_LLM_ENABLED`), `provider` (`VDASH_LLM_PROVIDER`), `model` (`VDASH_LLM_MODEL`), `timeout_seconds` 120, `max_output_tokens` 6000, `temperature` 0.3, `retries` 1.
- **Checks:**
  - `python tools/check_llm.py` sends a tiny request.
  - `python tools/check_llm.py --sheet phung-kitchen --lang vi` sends a real report's fact sheet and shows time, tokens and cost.
  - Online: `POST /api/admin/llm/test` (platform admin). `/api/admin/status` shows provider, model, whether the key is set, the last result and this month's usage.
- **Deploy:** set `DEEPSEEK_API_KEY` in Render's Environment. Without it the dashboard works as before, and the admin status says "key missing".
- Tests: 79 passed (5 new: request shape for all 4 providers, retries and failure kinds, the key never in messages, cost, cache and usage, admin status and test endpoint, settings).

## New in v36 (9.15.0): first page "At a glance" and the fact sheet (AI plan, Step 13)

Every report now opens with **At a glance**, a page that answers "how are we doing?" before any detail. It is written by rules, without AI, and becomes the fallback of the AI-written report:

- **Headline:** one verdict on net revenue in five bands (clearly better / better / steady / weaker / clearly weaker), with the main reason. Revenue = orders × average order value, so the bigger of the two changes is named: *"A clearly weaker period: Net revenue fell 16.1% to ₫586.7M, mainly because of fewer orders (-10.5%)"*.
- **Key messages (up to 5):** the best and the worst finding of the business evaluation, the most urgent early warning and the outlook, never repeating the headline.
- **Key figures (6)** in a fixed business order, and **top priorities (3)** from the quick wins.
- The page appears in every report, whatever parts were ticked, using only what this user may see for this period (past periods: no warnings or priorities). Reports stored before v36 open unchanged. Settings: `config/report_story.json` (headline figure, bands, figure order, limits).

**Fact sheet** (`vdash/reports/facts.py`): the material the AI writer will get in Steps 14–15. Nothing is sent anywhere yet.

- Every figure, finding, warning, action, chart and list of the report becomes a numbered fact (F1, F2 …) with the numbers **already written in the report's language** ("586,7 Tr ₫", "-16,1%"), whether it is good or bad news, an importance score, and the chapter: what happened / why / what next / what to do.
- Charts are summarised (highest, lowest, average, second half vs first half, top items with shares). Charts get neutral ids (C1 …) for the writer to pick from.
- **Evidence parts:** up to 10 explanatory parts (top products, product momentum, weekday, time of day, new vs returning, repeat rate, customer segments …) are always added to the fact sheet, even if not ticked, so the writer can explain *why*. They are not printed unless the writer uses them (Step 16).
- **Privacy:** customer and salesperson names are replaced by "Customer A", "Salesperson B" … in lists, labels and every sentence. The map back stays on the server. Raw values and sources stay on the server too.
- Size: about 4–5k tokens for the standard report, about 9k for a full report.
- `python tools/show_fact_sheet.py phung-kitchen --profile standard --lang vi --out sheet.json` shows what would be sent.

Also: the 7-day demand forecast is shown in whole units (1,200 instead of 1,200.07). Tests: 74 passed (5 new: first page and fact sheet for 5 profiles × 4 languages × 2 periods, every figure traced to its dashboard number, names hidden, headline bands and main reason, page rendering, settings validation).

## New in v35 (9.14.0): reports as Excel

Every created report can also be downloaded as an Excel file - **Excel** next to each report in *My reports*, **Download Excel** in the preview window, and in the report page's toolbar on phones. It is made from the stored report, so it has exactly the report's numbers, and it is free (not counted, like opening).

- **Summary** sheet: the cover (report name, business, period, comparison, data through, created), **Key figures** (every figure of the report with value, comparison value, change % or change in points, and the sentence), then a contents list linking to every sheet.
- **Findings** (what's going well / needs attention / early warnings, each with its rows), **Actions** (priority, when, action, why), **one sheet per chart or table** with the data behind it, **Not included** (with reasons).
- All labels in the report's language (en / de / vi / zh); sheet names follow Excel's rules (31 characters, cut at a word).
- Numbers are real numbers with Excel formats: amounts with the currency's decimals and the currency in the column header ("Net revenue (VND)"), percentages as Excel percentages (sum/average work), changes with a sign, dates as dates. Columns have filters and frozen headers; every sheet prints on A4 landscape, one page wide.
- API: `GET /api/tenants/{tid}/reports/mine/{id}/xlsx` (only the person who created the report; audit `report_xlsx`).
- Checked: opened in LibreOffice (English and Vietnamese, all 38 sheets of a full report); downloads from the preview, the list and the phone tab in Chromium; tests: 69 passed (1 new).

## New in v34 (9.13.0): created reports are kept and counted per plan

This replaces the saved report settings and the shared history of v32/v33.

- **Create report** (was "Preview & save as PDF") makes the report once and keeps it exactly as made. It gets an automatic name - `#7 · Executive summary · Aug 28, 2026 – Sep 26, 2026` (the person's running number, the profile or "Custom", the period) in the report's language - and opens in the preview window with **Save as PDF** (new tab on phones).
- **My reports** (below the profiles) lists the person's own created reports, newest first, with **Open**. Nobody else sees them; they cannot be renamed, changed or deleted. Opening shows the stored copy unchanged; if the data has been updated since, the list says "Data updated since" and the cover says this is the report as it was created.
- **Kept per person: the newest 20** (`keep_per_user`). Creating the 21st silently removes the oldest (report and its record). Numbers are never reused.
- **Monthly limit per business by payment plan** - `config/report_plans.json`: trial 5, starter 20 (default), growth 60, pro 200, enterprise no limit (example numbers - set your own). A business's plan: `"plan"` in `tenants.json`. The builder shows "3 of 20 reports created this month (Starter plan)"; at the limit it says when new reports are possible again.

**Counting rules** (enforced on the server):

1. One report is counted when it is **created and stored successfully**. Failures (database down, nothing to show) are not counted; a reserved count is given back if storing fails.
2. **The same report again is free:** same person, same parts, period, comparison and language, made from the same data -> the kept copy opens, not counted. When the data has changed, it is a new report.
3. **Opening a report from My reports is free**, also at the limit; so is Save as PDF (any number of times).
4. The limit is **per business and calendar month in the business's time zone** (resets on the 1st, 00:00 local). All users of the business share it. Removing old reports (the 20 rule) never gives counts back.
5. **Platform admins (Veridex staff)** are never counted or limited.
6. The count is atomic in the database: two people pressing Create at the same moment cannot exceed the limit (checked with 30 parallel requests on PostgreSQL).
7. Building a report without storing it (`/reports/render`) is now for platform admins only, so there is no uncounted way to get a report.

- If the report database cannot be reached, reports cannot be created or opened ("please try again in a few minutes"); the dashboard itself is unaffected. Each stored report takes about 6-20 KB (compressed).
- API: `POST …/reports/create`, `GET …/reports/mine`, `GET …/reports/mine/{id}?download=0|1`. Removed: `…/reports/configs`, `…/reports/history`. New database tables `report_documents`, `report_usage` (created automatically, also on Aiven). Validation: `python tools/check_report_blocks.py` also checks the plans and each tenant's plan.
- Tests: 68 passed (5 new: naming, privacy, same-report rule, kept copy after data change, plan limit and keep-20, local-month and atomic counting, database down).

## New in v33 (9.12.1): opening a saved report shows it

- Choosing a saved report in the builder now opens its preview straight away (period, comparison and parts restored first), with **Save as PDF** ready - no extra click on Preview. On phones the report opens in a new tab.
- If none of its parts have data for the saved period, the builder says so instead of showing an empty report.
- Opening a saved report counts as making it, so it appears under **Recent reports** (linked to the saved report).
- Checked in Chromium: desktop (preview window, correct period and comparison, charts drawn) and phone (new tab, no pop-up block). Tests: 66 passed.

## New in v32 (9.12.0): saved reports and report history

- **Saved reports** (top of the report builder): give the current choice a name and **Save as new**; pick it later from the list to restore the parts, the period (preset such as "last 30 days", or fixed dates) and the comparison. **Save changes** / **Delete** (click twice to confirm) are offered to the person who saved it and to business administrators; everyone in the business can open the business's saved reports. Names are unique per business (any case); at most 100 per business.
- **Recent reports** (bottom of the builder): every previewed report is recorded (who, when, period, language, number of parts, data version). Staff see their own; managers and above see the whole business. **Open** shows the same report again (same period and parts); if the data has been updated since, the list marks it "Data updated since" and the report's cover says the figures may differ from the original copy. The same report by the same person within 15 minutes is recorded once; re-opening from the list is not recorded again.
- Both live in the report database (`report_configs`, `report_history`): Aiven Postgres when `VDASH_REPORT_DB_URL` is set, else `runtime/reports.db`. **On Render set `VDASH_REPORT_DB_URL` and `VDASH_REPORT_DB_CA_PEM`**, otherwise saved reports are lost on every restart (Render's disk is temporary).
- If the report database cannot be reached, reports can still be previewed and saved as PDF; the builder says "Saved reports are not available right now" and the history is skipped (logged).
- New API: `GET/POST /api/tenants/{tid}/reports/configs`, `PUT/DELETE …/reports/configs/{id}`, `GET …/reports/history?limit=`; `/reports/render` takes `profile`, `period_preset`, `config_id` (kept in the history), `as_of_version` and `record` (false = not recorded). Changes are written to the audit log (`report_config_create/update/delete`).
- The Reports page subtitle now says "preview it and save it as PDF" (it still mentioned a download).
- Tests: 66 passed (3 new: rights, duplicates, validation, tenant isolation; history once per report, by role, data-changed note; report database down).

## New in v31 (9.11.1): reports are saved as PDF from the preview

Business owners keep PDFs, not HTML files, so the report builder has one button: **Preview & save as PDF**.

- The preview window has a **Save as PDF** button (enabled once the report has loaded) with the hint "In the print window, choose 'Save as PDF' as the printer". It opens the browser's print dialog for the report only; the suggested file name is the report title (business and period). The PDF is sharp vector output with selectable, searchable text, in every language - nothing is installed on the server.
- On phones the preview opens in a new tab with the report's own "Print / Save as PDF" button (the phone's print / save sheet).
- Inside the preview window the report's own toolbar is hidden (one button only).
- The HTML download is no longer offered in the builder (the API `…/reports/render?download=1` still exists, for later use such as e-mail attachments).
- Checked in Chromium: one button in the builder; Save as PDF prints the report frame with all charts drawn and the report title; phone opens the new tab; no script errors. Tests: 63 passed.

## New in v30 (9.11.0): Reports - the report builder in the dashboard

A new **Reports** entry at the end of the section menu (sidebar and phone tabs), for every business that has data:

- **Period and comparison** come from the dashboard's own filters (presets or custom range, previous period / last year / none). Changing them refreshes which parts have data.
- **Start from** a profile: *Executive summary* (scorecard, evaluation, warnings, actions + 3 key charts), *Standard review* (default), *Full business review* (everything with data), *Sales & products*, *Customer health*. Changing any tick shows *Custom*. Profiles: `config/report_profiles.json`.
- **All parts of the report, by layer and chapter**, grouped as summary / key figures / charts / lists, with *All* / *None* per chapter. Only parts with data for this business, user and period can be ticked; the others are listed under "n not available" with the reason ("Needs: Product cost prices", "Available from role manager", "Shows the current state only" for past periods). Parts that always show today's picture carry a "current state" tag.
- **Preview** opens the report in a window over the dashboard (a new tab on phones), with "Open in a new tab" and the report's own "Print / Save as PDF". **Download report** saves the self-contained HTML file.
- The report is written in the dashboard's language; switching business resets the selection; the deep link remembers the Reports page.
- Checked in Chromium: open Reports, every profile, manual changes, All / None, preview (charts drawn), download, past period (profile re-applied, forward-looking parts off), back to the dashboard - desktop and phone, English and Vietnamese, owner and staff - no script errors; all other dashboard pages identical to v29 (486 items compared).
- New API: `GET /api/tenants/{tid}/reports/render?spec=<request JSON>&download=0|1` (used by the preview frame and the download link); `/reports/blocks` now also returns the profiles with the blocks each one selects.
- Tests: 63 passed (4 new).

## New in v29 (9.10.0): the report as an HTML page (with charts)

New API `POST /api/tenants/{tid}/reports/render?download=0|1` (same body as `/reports/compose`):

- **Download (`download=1`):** one self-contained `.html` file, e.g. `report_phung-kitchen_2026-08-28_2026-09-26_vi.html` (~1.1 MB, almost all of it the chart library). Charts, styles and data are inside: it opens offline, can be e-mailed, and prints to PDF from any browser ("Print / Save as PDF" button). No outside resources at all.
- **Preview (`download=0`):** the same page, small (~20 KB), loading the two chart scripts from the server - for the report builder (next steps).
- **Content:** cover (business, period, comparison, data through, generated on; a note for past periods), contents, one section per layer (What happened / Why / What next / What to do), chapter by chapter; scorecard tiles with small trend lines; figures with change and sentence; **charts drawn by the dashboard's own chart code** (same look as the dashboard); tables; findings and early warnings with their rows; quick-win actions with priority, time frame and reason; a final "Not included in this report" list grouped by reason.
- **Print / PDF:** A4, each layer starts on a new page, blocks are never split, a chapter heading never stays alone at the bottom of a page, page numbers "n / total"; charts are redrawn at A4 width when printing (sharp vector graphics).
- **Security:** each report page carries its own strict policy (scripts only with a one-time nonce, no outside resources, may be shown only inside this site's pages); every data value is escaped - a product or customer name can never inject markup or script. The dashboard's own pages keep their policy unchanged.
- **Languages:** all four; the page is marked with its language. Phone-friendly on screen.
- Checked in Chromium: every chart drawn (28 / 28 for the full demo report) on desktop and phone, offline, no script errors. Default report = 13 A4 pages. Samples: `claude_output/veridex_report_sample_vi_*.pdf`, `..._en_*.pdf`.
- Tests: 59 passed (2 new).

## New in v28 (9.9.0): Quick wins - concrete actions in the report

The report's "What to do about it" layer now lists up to 6 actions for the next 1-30 days, e.g. for the demo business:

- *High priority · within 7 days:* Reach out to the 31 regular customers who are overdue for their next order (they spent ₫549.86M in the last 12 months)
- *High priority · within 7 days:* Contact the 22 valuable customers who are likely to leave: about ₫481.5M of yearly revenue is at stake
- *Medium · 7 days:* Offer Bột chiên khoai môn (KM) and combo 5 bánh giò as a combo: customers buy them together 2.63 times as often as expected - *because 195 baskets contained both; 21.5% of baskets with the first also had the second*
- *Medium · 30 days:* review slowing best sellers, refresh new-customer acquisition, find out why three products lost ₫53.7M

How it works:
- **No invented advice.** Each action comes from a trigger in `config/report_quick_wins.json` (14 today): an active early warning, a finding of the period's business evaluation, or a KPI table row meeting typed conditions (the product-pair combo). Each action shows **because** = the exact finding sentence from the same report, or the numbers of the chosen row.
- **Editable without code:** thresholds (`where` / `when` with `>= > <= < == !=` and a number), priority (high / medium / low), time frame in days, `max_actions`, and `unless` (skip an action when another one already fired). Texts in en / de / vi / zh (`rpt_qw_*`).
- **Same rules as everything else:** role filtering, current periods only (forward-looking), evaluation-based actions only with a comparison period. An invalid file never breaks a report (no actions, errors in the log); `python tools/check_report_blocks.py` validates it.
- Tests: 57 passed (3 new).

## New in v27 (9.8.0): report composer (the report's content, as data)

New API `POST /api/tenants/{tid}/reports/compose` with `{"blocks": [...], "start", "end", "compare", "lang"}` returns the complete report document (JSON) that the HTML / PDF / Excel outputs of the next steps are drawn from:

- **Cover:** title, business, period, comparison period, "data through", generation date - in the chosen language.
- **Layers → chapters → blocks**, always in the catalog order. Per block: single figures with value, comparison and a sentence ("Net revenue fell 16.1% to ₫586.7M (was ₫699.32M)"); charts with the chart data and one factual sentence (highest / lowest day, top item and its share of the total, busiest hour); tables (first 20 rows); the scorecard tiles; findings and early warnings with their sentences and rows.
- **No invented content:** every number is the dashboard's own result for the same user, business and period (role rules and personal data included); every sentence is a translated template - KPI changes reuse the business-evaluation sentences. No AI involved.
- **Blocks that cannot be used** (no data, role, past period, no comparison) are listed under `skipped` with their reason instead of breaking the report.
- Fast: all blocks of the demo business in about 0.06 s when the period is pre-computed; each composition is written to the audit log.
- Sample: `claude_output/report_document_sample_vi_*.json` (default blocks, Vietnamese).
- Tests: 54 passed (3 new): every block, 4 languages × current and past period; values and sentences identical to the dashboard's; same report whatever the request order; roles, bad requests, other tenants. Chart time labels in sentences ("Sep 4", "Wk Oct 20") match the page (140 more golden cases).

## New in v26 (9.7.0): report blocks - every KPI, switched by the business's real data

New API `GET /api/tenants/{tid}/reports/blocks?start&end&compare&lang` (used by the report builder in a later step) lists everything a report can contain:

- **Every KPI of the catalog is a report block** (185 today), in the chapter of its dashboard section, plus 5 summary blocks: executive scorecard, what's going well, what needs attention, early warnings, quick wins. A KPI added to the catalog appears in reports automatically.
- **Four layers:** What happened (summary, overview, sales) · Why it happened (products, customers, channels, marketing, fulfilment, inventory, finance, workforce, experience, market) · What could come next (AI insights, early warnings) · What to do about it (recommended actions).
- **Switched on and off by the business's real data**, with the same live check as the dashboard (refreshed whenever the warehouse changes). Each block says why it is off, in plain words and the user's language:
  - *Needs: Product cost prices* - the data that would switch it on;
  - *Available from role manager* - for blocks above the user's role (customer names etc.);
  - *Forward-looking analysis is not available for past periods* - forecasts, warnings, actions when the period ended more than 7 days before the latest data;
  - *Shows the current state only* - blocks that always show today's picture (e.g. customer segments) are off for past periods;
  - *Needs a comparison period* - the findings, when "compare" is none.
- Demo business, last 30 days: 67 of 190 blocks available; a staff user sees 34 locked; Q4 2025: forecasts, warnings, actions and 18 current-state blocks off.
- Layers, chapter order and summary blocks: `config/report_blocks.json`; check with `python tools/check_report_blocks.py --list`.
- Tests: 51 passed (5 new), including "blocks switch on/off when the warehouse gains or loses data".

## New in v25 (9.6.0): report database (Aiven PostgreSQL)

Saved report settings, schedules and report history (used from the next steps on) are kept in a database that survives restarts and redeploys - Render's free disk is wiped on every deploy.

- **Where:** `report_store` in `config/settings.json`. Without `VDASH_REPORT_DB_URL` a local SQLite file `runtime/reports.db` is used. With it, PostgreSQL - here the free Aiven service (`aiven-postgre-info.txt`).
- **Security:** with the Aiven CA certificate the connection is encrypted and the server's certificate and host name are checked (`verify-full`). The password lives only in `config/local.env` (this laptop) and in Render's Environment - never in the repository (`.gitignore` now also excludes `*.pem` and `aiven-*.txt`).
- **Never blocks the dashboard:** the database is contacted only when a report feature needs it. If it is unreachable the dashboard works as usual and `/api/admin/status` → `report_store` shows the error.
- **Check the connection:** `python tools/check_report_store.py` (shows TLS, server version, writes/reads/deletes a test row; exit code 1 on a problem).
- **New package:** `pip install -r requirements.txt` (adds `psycopg[binary]`).

**This laptop** (already done in `config/local.env`):
```
VDASH_REPORT_DB_URL=postgres://avnadmin:<password>@veridex-bi-usr-db-jerry-d51d.d.aivencloud.com:19739/defaultdb?sslmode=require
VDASH_REPORT_DB_CA=ca.pem
```

**Render** → service → Environment → add:

| Key | Value |
|---|---|
| `VDASH_REPORT_DB_URL` | the Service URI from `aiven-postgre-info.txt` (with the password) |
| `VDASH_REPORT_DB_CA_PEM` | the whole content of `ca.pem`, from `-----BEGIN CERTIFICATE-----` to `-----END CERTIFICATE-----` |

Aiven's free plan allows 20 connections (the dashboard uses at most 5) and 1 GB. Aiven may power off a free service that has no activity for a long time (it e-mails first); power it on again in the Aiven console. Once scheduled reports exist, the scheduler's regular check keeps it active.

## New in v24 (9.5.4): chart code shared with reports (no visible change)

- Number formatting and the ECharts chart options moved from `web/app.js` into the new `web/chart_options.js`, loaded by the page just before `app.js` (and kept by the installed app). The same file runs in Node, so reports can draw their charts with exactly the code the page uses.
- Nothing changes on screen: for 420 real chart results (every chart of the demo business, 4 languages, 3 periods, phone and desktop widths, all 3 trend-line modes) the old and new code give identical chart settings (5,640 comparisons; a deliberate change is caught). In a real browser (desktop and phone, English / Vietnamese / German, every section) the old and new page drew identical charts (same SVG) and text, with no script errors.
- `tools/make_format_golden.js` now runs the page's real formatting code from `web/chart_options.js` instead of a copy; `tests/format_golden.json` came out byte-identical.

## New in v23 (9.5.3): downloads write numbers the way each language does

- Sentences in downloaded findings (evaluation and early-warning CSV / Excel) now use the same number, money and date format as the page, in the chosen language: `Nettoumsatz sank um 16,1% auf 586,7 Mio. ₫ (vorher 699,32 Mio. ₫)` instead of `... 586,700,000 VND ...`; Vietnamese `481,5 Tr ₫`, Chinese `₫4.81亿`, "pts" translated (`Pkt.`, `điểm %`, `个百分点`).
- New module `vdash/fmt.py` (based on Babel, the same Unicode locale data browsers use). The coming reports use it for all their text.
- **New package:** run `pip install -r requirements.txt` once (adds `babel`). Render installs it automatically on the next deploy.
- Tests: 42 passed (3 new). The server output is compared with what the browser prints for 1,768 cases (`tests/format_golden.json`, made by `node tools/make_format_golden.js`); all identical.

## New in v22 (9.5.2): service layer for reports (no visible change)

- The dashboard's business logic (which KPIs a user sees, period checks, KPI results, business evaluation, early warnings, role filtering and translation) moved from inside `vdash/api.py` into the new `vdash/services.py` (`Services`, `DashboardService`, `ServiceError`). The API routes now only read the request and call it.
- Why: the coming report feature and its scheduler need exactly the same rules without going through HTTP. Nothing changes for users: all 704 recorded API answers (5 users, 4 languages, valid and invalid periods, downloads, errors) are identical before and after.
- Tests: 39 passed (2 new: the service layer has no web dependency; it gives the same values and errors as the API).

## New in v21 (9.5.1): clearer waiting note

- When figures take longer than 4 seconds, the page checks whether the server answers at all. If it does, the note says "Still calculating these figures…"; only if it does not (the server is starting up) does it say "Connecting to the server…". "Preparing the data for this business…" is unchanged.

## New in v20 (9.5.0): fast first page after a quiet period

- Results are also kept in `runtime/result_cache.db` (setting `cache.persist_path`), so a restarted server starts warm. On Render the build command fills it (see "Local development and online").
- Cached results no longer expire after 2 hours (`cache.ttl_minutes: 0`); a new data load replaces them anyway. The page's default period is pre-computed first.
- A request never runs the full data refresh itself any more. If a business has no prepared data yet, the page says "Preparing the data for this business…" and retries.
- The installed app opens at once from the device, even while the server is starting; a note "Connecting to the server…" appears after 4 seconds and the figures load when the server answers. Only the app itself and its texts are kept on the device, never business data.

## New in v19 (9.4.0): local and online configuration

- Same code and config for your laptop and Render; the differences are environment variables, set in `config/local.env` locally (see "Local development and online").
- Profile overlays (`config/settings.online.json`), nested defaults such as `${VDASH_PORT:-${PORT:-8050}}`, and true/false config values read correctly from variables.
- `tools/sync_to_deploy.py` copies the dashboard into the deploy repo.
- The "Reload" button in the update banner works again.

## New in v17 (9.3.5): app icons from `web/icons/`

- The browser tab, the desktop shortcut and the installed app (PWA) now all use the PNG icons in `web/icons/`: `favicon-32.png` (tab), `apple-touch-icon.png` (iPhone/iPad home screen), `icon-192.png` / `icon-512.png` / `icon-maskable-512.png` (installed app, Android). The logo at the top of the sidebar and the login page uses `icon-192.png` too. The old built-in blue chart-line icon is gone.
- To change the icons, replace the PNG files (keep the names and sizes) and restart the server. Icon links carry a version number, so browsers and installed apps load the new icons instead of old cached ones. An installed desktop/phone app can take up to a day to show a new icon; reinstalling shows it at once.

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
