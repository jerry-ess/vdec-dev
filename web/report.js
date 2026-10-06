/* Veridex BI - report builder (v30).
 * Shown as the "Reports" entry of the section navigation. Uses the dashboard's period / comparison filters and
 * language. Lists every report block (all KPIs + summary blocks) with its state for the business, user and period;
 * blocks without data, above the user's role or not valid for a past period cannot be ticked and say why.
 * Profiles pre-select blocks. v34: "Create report" makes the report once on the server - stored exactly as made,
 * named automatically, counted against the business's monthly plan limit - and shows it in a frame (new tab on
 * phones), where "Save as PDF" opens the browser's print dialog for the report alone. "My reports" lists the
 * person's own created reports (newest first, the plan keeps the newest N); opening one is free and unchanged.
 * v41: the report is composed and counted at once and its story written in the background (1-3 minutes with AI):
 * the builder shows the progress (step, elapsed time) and opens the report when it is done; the person can keep
 * working meanwhile - "My reports" shows it as being written, and a failed one as "not created - not counted".
 * Talks to app.js only through window.VdashApp.
 */
(() => {
  "use strict";
  const A = () => window.VdashApp;
  const GROUPS = ["summary", "figures", "charts", "lists"];
  const R = { tenant: null, data: null, sel: new Set(), profile: null, open: new Set(), seq: 0, modal: null,
    mine: null, usage: null, storeError: "", msg: "", busy: false, mineOpen: true, cfgOpen: false,
    jobs: [], job: null, tick: null };

  const el = () => document.getElementById("report-builder");
  const isPhone = () => window.matchMedia("(max-width: 820px)").matches;
  const lockSvg = '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/></svg>';

  function allBlocks() {
    return R.data ? R.data.layers.flatMap((ly) => ly.chapters.flatMap((c) => c.blocks.map((b) => ({ ...b, chapter: c.id })))) : [];
  }
  const availableIds = () => new Set(allBlocks().filter((b) => b.state === "available").map((b) => b.id));
  const sameSet = (a, b) => a.size === b.size && [...a].every((x) => b.has(x));

  function profileMatch() {
    if (!R.data) return "custom";
    const p = (R.data.profiles || []).find((x) => sameSet(new Set(x.blocks), R.sel));
    return p ? p.id : "custom";
  }
  function applyProfile(id) {
    const p = (R.data.profiles || []).find((x) => x.id === id);
    if (!p) return;
    R.sel = new Set(p.blocks); R.profile = id;
    R.open = new Set(R.data.layers.flatMap((ly) => ly.chapters.filter((c) => c.blocks.some((b) => R.sel.has(b.id))).map((c) => c.id)));
  }

  async function load() {
    const { S, api, tenantUrl, T, esc } = A();
    const box = el(); box.hidden = false;
    if (!R.data) box.innerHTML = `<div class="empty">${esc(T("loading"))}</div>`;
    const seq = ++R.seq;
    let d;
    try { d = await api(tenantUrl("/reports/blocks", { start: S.start, end: S.end, compare: S.compare })); }
    catch (e) { if (seq === R.seq) box.innerHTML = `<div class="err">${esc(e.message)}</div>`; return; }
    if (seq !== R.seq) return;
    const firstTime = R.tenant !== S.tenant || !R.data;
    const prevProfile = R.profile;
    R.data = d; R.tenant = S.tenant;
    const avail = availableIds();
    if (firstTime) applyProfile(d.default_profile || (d.profiles[0] && d.profiles[0].id));
    else if (prevProfile && prevProfile !== "custom") applyProfile(prevProfile);   // profile follows the new period's data
    else { R.sel = new Set([...R.sel].filter((x) => avail.has(x))); R.profile = "custom"; }
    render();
    if (firstTime || R.mine === null) loadMine();
    if (window.VdashSchedules && (firstTime || window.VdashSchedules._state.data === null)) window.VdashSchedules.load();   // v49
    else if (window.VdashSchedules) window.VdashSchedules.show();   // v51.2: it was hidden when the page was left
  }
  function openSelected() {
    R.open = new Set(R.data.layers.flatMap((ly) => ly.chapters.filter((c) => c.blocks.some((b) => R.sel.has(b.id))).map((c) => c.id)));
  }

  // ---------------------------------------------------------------- v34: created reports ("My reports")
  async function loadMine() {
    const { api, tenantUrl } = A();
    try {
      const d = await api(tenantUrl("/reports/mine"));
      R.mine = d.reports; R.usage = d.usage; R.jobs = d.jobs || []; R.storeError = "";
    } catch (e) { R.mine = R.mine || []; R.storeError = e.message; }
    render();
    // v41: reports still being written (started on another device or before a reload) are watched too
    R.jobs.filter((j) => j.status === "writing").forEach((j) => watch(j, A().S.tenant, null, false));
  }
  const limitReached = () => !!(R.usage && R.usage.counted && R.usage.limit !== null && R.usage.remaining <= 0);
  function usageHtml() {
    const { T, esc, fmtDate } = A();
    if (R.storeError) return `<p class="rb-hint">${esc(T("rb_store_down"))}</p>`;
    const u = R.usage; if (!u) return "";
    const line = u.limit === null ? T("rb_usage_nolimit", { used: u.used, plan: u.plan })
      : T("rb_usage", { used: u.used, limit: u.limit, plan: u.plan });
    let ai = "";
    if (u.ai && u.counted) {
      ai = u.ai_limit === null ? "" : `<span class="muted rb-usage">${esc(T("rb_usage_ai", { used: u.ai_used, limit: u.ai_limit }))}</span>`;
      if (u.ai_limit !== null && u.ai_remaining <= 0) ai += `<p class="rb-hint">${esc(T("rb_ai_used_up", { date: fmtDate(u.resets_on) }))}</p>`;
    }
    return `<span class="muted rb-usage">${esc(line)}</span>` + ai
      + (limitReached() ? `<p class="rb-hint">${esc(T("rb_limit_reached", { date: fmtDate(u.resets_on) }))}</p>`
        : `<span class="muted rb-usage">${esc(T("rb_count_hint"))}</span>`);
  }
  function mineHtml() {
    const { S, T, esc } = A();
    if (!R.mine) return "";
    const df = new Intl.DateTimeFormat(S.locale, { dateStyle: "medium", timeStyle: "short" });
    const rows = R.mine.map((h) => `<li><div class="rb-h-main"><b>${esc(h.name)}</b>
        <span class="muted">${esc(T("rb_parts", { n: h.parts }))} · ${esc((S.languages || {})[h.language] || h.language)} · ${esc(df.format(new Date(h.created_at * 1000)))}</span>
        ${h.data_changed ? `<span class="rb-tag">${esc(T("rb_data_changed"))}</span>` : ""}</div>
        <div class="rb-h-side"><a class="btn btn-sm" href="${esc(excelUrl(h.id))}" download title="${esc(T("rb_excel_tip"))}">${esc(T("rb_excel"))}</a>
        <button type="button" class="btn btn-sm" data-share="${esc(h.id)}" data-share-name="${esc(h.name)}">${esc(T("sh_btn"))}</button>
        <button type="button" class="btn btn-sm" data-mine="${esc(h.id)}">${esc(T("rb_open"))}</button></div></li>`).join("");
    const jobRows = (R.jobs || []).map((j) => j.status === "writing"
      ? `<li class="rb-job"><div class="rb-h-main"><b>${esc(j.name || T("rb_writing"))}</b>
          <span class="muted"><span class="rb-spin" aria-hidden="true"></span>${esc(T("rb_job_writing"))} · ${esc(df.format(new Date(j.created_at * 1000)))}</span></div></li>`
      : `<li class="rb-job failed"><div class="rb-h-main"><b>${esc(j.name || "")}</b>
          <span class="rb-err">${esc(j.error || T("rb_job_failed"))}</span></div></li>`).join("");
    const keep = R.usage ? `<p class="muted rb-keep">${esc(T("rb_mine_keep", { n: R.usage.keep }))}</p>` : "";
    return `<details class="rb-ch card rb-mine" ${R.mineOpen ? "open" : ""}><summary><span class="rb-ch-title">${esc(T("rb_mine"))}</span>
      <span class="rb-count-chip">${R.mine.length}</span></summary><div class="rb-ch-body">
      ${R.mine.length || jobRows ? `<ul class="rb-hist">${jobRows}${rows}</ul>${keep}` : `<p class="muted">${esc(T("rb_mine_none"))}</p>`}</div></details>`;
  }
  async function createReport() {
    const { api, tenantUrl, T, esc } = A();
    if (R.busy || !R.sel.size) return;   // at the limit the same report still opens the kept copy (server decides)
    // phones show the report in a new tab: open it now, while this is still the user's click (pop-up rules)
    let tab = null;
    if (isPhone()) {
      try {
        tab = window.open("", "_blank");
        if (tab) { tab.opener = null; tab.document.title = T("rb_writing");
          tab.document.body.innerHTML = `<p style="font:16px system-ui;margin:32px 20px;color:#52514e">${esc(T("rb_writing_tab"))}</p>`; }
      } catch (e) { tab = null; }
    }
    R.busy = true; R.msg = ""; render();
    try {
      const d = await api(tenantUrl("/reports/create"), { method: "POST", body: JSON.stringify(spec()) });
      R.usage = d.usage;
      if (d.report) {                                       // the same report already exists (or was written at once)
        R.msg = d.reused ? T("rb_reused") : T("rb_created", { name: d.report.name });
        R.busy = false; show(d.report.id, tab); loadMine(); return;
      }
      if (d.usage && d.usage.ai) askNotify();               // v41.1: a finished report can also notify the system
      follow(d.job, tab);                                   // v41: being written - show the progress, open when done
      loadMine();
    } catch (e) {
      if (tab) tab.close();
      R.busy = false; R.msg = e.message; render();
      await loadMine();                                      // the limit may have been reached meanwhile
      if (limitReached() || R.storeError) { R.msg = ""; render(); }   // shown translated from the usage instead
    }
  }

  // v41.1: every report being written is watched until it is done - wherever the person is in the app (another
  // section, another business, after a page reload: the list is kept in this browser). When it is done a
  // notice pops up with "Open" (and a system notification / tab title mark when the tab is in the background);
  // only a report the person is waiting for in the builder opens by itself.
  const WKEY = "vdash-report-watch";
  const W = { list: [], timer: null, ready: false, notified: new Set() };
  try { W.list = JSON.parse(localStorage.getItem(WKEY) || "[]").filter((w) => w && w.id && w.tenant); } catch (e) { W.list = []; }
  function saveW() {
    try { localStorage.setItem(WKEY, JSON.stringify(W.list.map(({ id, tenant, t0, name }) => ({ id, tenant, t0, name })))); } catch (e) { /* private mode */ }
  }
  function watch(job, tenant, tab, live) {
    let w = W.list.find((x) => x.id === job.id);
    if (!w) { w = { id: job.id, tenant, t0: Date.now() - (job.elapsed || 0) * 1000, name: job.name || "" }; W.list.push(w); saveW(); }
    w.step = job.step; if (tab) w.tab = tab;
    if (live) {
      R.job = w; R.busy = true; clearInterval(R.tick);
      R.tick = setInterval(() => { const n = document.getElementById("rb-elapsed"); if (n) n.textContent = elapsed(); }, 1000);
    }
    schedule(live ? 1500 : 3000);
  }
  function follow(job, tab) { watch(job, A().S.tenant, tab, true); render(); }
  function elapsed() {
    const s = Math.max(0, Math.round((Date.now() - (R.job ? R.job.t0 : Date.now())) / 1000));
    return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
  }
  function schedule(ms) {
    clearTimeout(W.timer);
    if (W.ready && W.list.length) W.timer = setTimeout(pollAll, ms);
  }
  const jobUrl = (w) => `/api/tenants/${encodeURIComponent(w.tenant)}/reports/jobs/${encodeURIComponent(w.id)}?lang=${encodeURIComponent(A().S.lang || "en")}`;
  async function pollAll() {
    for (const w of [...W.list]) {
      let res, d;
      try { res = await fetch(jobUrl(w), { credentials: "same-origin", cache: "no-store" }); }
      catch (e) { continue; }                                   // offline / server waking: try again later
      if (res.status === 401) { W.ready = false; return; }       // signed out: resumes after the next sign-in
      if (res.status === 404 || res.status === 403) { drop(w); continue; }   // not this person's (any more)
      if (!res.ok) continue;
      try { d = await res.json(); } catch (e) { continue; }
      const j = d.job;
      if (j.status === "writing") {
        if (w.step !== j.step) { w.step = j.step; if (R.job === w) render(); }
        continue;
      }
      if (w.tenant === A().S.tenant) R.usage = d.usage;
      finish(w, j);
    }
    schedule(R.job ? 2000 : 4000);
  }
  function drop(w) {
    W.list = W.list.filter((x) => x !== w); saveW();
    if (R.job === w) { clearInterval(R.tick); R.job = null; R.busy = false; }
  }
  function finish(w, j) {
    const { T } = A();
    const live = R.job === w, here = w.tenant === A().S.tenant, inBuilder = here && el() && !el().hidden;
    drop(w);
    if (W.notified.has(w.id)) return;
    W.notified.add(w.id);
    const tab = w.tab && !w.tab.closed ? w.tab : null;
    if (j.status === "done" && j.report) {
      if (live) R.msg = T("rb_created", { name: j.report.name });
      const opensNow = live && inBuilder && !document.hidden && !tab;
      if (tab) tab.location.href = `/api/tenants/${encodeURIComponent(w.tenant)}/reports/mine/${encodeURIComponent(j.report.id)}`;
      else if (opensNow) show(j.report.id);               // the person is waiting for it in the builder: open it
      if (!opensNow) toast(w, j, "done");
    } else {
      if (tab) tab.close();
      if (live) R.msg = j.error || T("rb_job_failed");
      toast(w, j, "failed");
    }
    if (document.hidden) background(j);
    if (inBuilder) { render(); loadMine(); }
  }

  // ---- the pop-up notice (bottom right; stays until closed or opened)
  function toastBox() {
    let box = document.getElementById("rb-toasts");
    if (!box) {
      box = document.createElement("div"); box.id = "rb-toasts"; box.className = "rb-toasts";
      box.setAttribute("aria-live", "polite"); document.body.appendChild(box);
    }
    return box;
  }
  function toast(w, j, kind) {
    const { T, esc, S } = A();
    const name = (j.report && j.report.name) || j.name || w.name || "";
    const biz = w.tenant !== S.tenant ? (((S.tenants || []).find((t) => t.id === w.tenant) || {}).display_name || w.tenant) : "";
    const n = document.createElement("div");
    n.className = `rb-toast ${kind}`; n.setAttribute("role", kind === "failed" ? "alert" : "status");
    n.innerHTML = `<div class="rb-toast-icon" aria-hidden="true">${kind === "done" ? "✓" : "!"}</div>
      <div class="rb-toast-body"><b>${esc(T(kind === "done" ? "rb_toast_ready" : "rb_toast_failed"))}</b>
        <div class="rb-toast-name">${esc(name)}${biz ? ` · ${esc(biz)}` : ""}</div>
        ${kind === "failed" ? `<div class="rb-toast-err">${esc(j.error || T("rb_job_failed"))}</div>` : ""}</div>
      <div class="rb-toast-actions">${kind === "done" ? `<button type="button" class="btn btn-primary btn-sm" data-t-open>${esc(T("rb_open"))}</button>` : ""}
        <button type="button" class="rb-toast-x" data-t-close aria-label="${esc(T("rb_close"))}">×</button></div>`;
    n.addEventListener("click", (e) => {
      if (e.target.closest("[data-t-close]")) { n.remove(); return; }
      if (e.target.closest("[data-t-open]")) {
        n.remove();
        const url = `/api/tenants/${encodeURIComponent(w.tenant)}/reports/mine/${encodeURIComponent(j.report.id)}`;
        if (w.tenant === A().S.tenant && !isPhone()) show(j.report.id); else window.open(url, "_blank", "noopener");
      }
    });
    const box = toastBox(); box.appendChild(n);
    while (box.children.length > 4) box.firstElementChild.remove();       // at most 4 notices at once
  }
  // ---- the tab is in the background: mark its title and, if the person allowed it, a system notification
  let baseTitle = null;
  function background(j) {
    const { T } = A();
    const key = j.status === "done" ? "rb_toast_ready" : "rb_toast_failed";
    if (baseTitle === null) baseTitle = document.title;
    document.title = `${j.status === "done" ? "✓" : "!"} ${T(key)} · ${baseTitle}`;
    try {
      if ("Notification" in window && Notification.permission === "granted") {
        const nt = new Notification(T(key), { body: (j.report && j.report.name) || j.name || "", tag: j.id });
        nt.onclick = () => { window.focus(); nt.close(); };
      }
    } catch (e) { /* not available here */ }
  }
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && baseTitle !== null) { document.title = baseTitle; baseTitle = null; }
  });
  function askNotify() {   // once, from the person's click on "Create report", for the long (AI-written) reports
    try { if ("Notification" in window && Notification.permission === "default") Notification.requestPermission().catch(() => {}); } catch (e) { /* ignore */ }
  }

  function progressHtml() {
    const { T, esc } = A();
    if (!R.job) return "";
    const step = T("rb_step_" + (R.job.step || "queued"));
    const ai = R.usage && R.usage.ai;
    return `<div class="rb-progress" role="status" aria-live="polite"><div class="rb-bar"><span></span></div>
      <div><b>${esc(step)}</b> · <span id="rb-elapsed">${elapsed()}</span></div>
      <p class="muted">${esc(T(ai ? "rb_writing_hint_ai" : "rb_writing_hint"))}</p></div>`;
  }

  function itemHtml(b) {
    const { T, esc } = A();
    const snap = b.snapshot ? ` <span class="rb-tag" title="${esc(T("badge_snapshot_tip"))}">${esc(T("rb_now_only"))}</span>` : "";
    return `<label class="rb-item" title="${esc(b.desc || "")}"><input type="checkbox" data-id="${esc(b.id)}" ${R.sel.has(b.id) ? "checked" : ""}>`
      + `<span>${esc(b.title)}${snap}</span></label>`;
  }

  function chapterHtml(c) {
    const { T, esc } = A();
    const on = c.blocks.filter((b) => b.state === "available"), off = c.blocks.filter((b) => b.state !== "available");
    const nSel = on.filter((b) => R.sel.has(b.id)).length;
    const open = R.open.has(c.id) ? " open" : "";
    const tools = on.length ? `<span class="rb-tools"><button type="button" class="btn btn-sm" data-all="${esc(c.id)}">${esc(T("rb_all"))}</button>`
      + `<button type="button" class="btn btn-sm" data-none="${esc(c.id)}">${esc(T("rb_none"))}</button></span>` : "";
    const count = on.length ? `<span class="rb-count-chip${nSel ? " on" : ""}">${nSel} / ${on.length}</span>`
      : `<span class="rb-count-chip muted">${esc(T("rb_no_data_chapter"))}</span>`;
    const groups = GROUPS.map((g) => {
      const items = on.filter((b) => b.group === g);
      return items.length ? `<div class="rb-group"><div class="eyebrow">${esc(T("rb_group_" + g))}</div><div class="rb-items">${items.map(itemHtml).join("")}</div></div>` : "";
    }).join("");
    const offHtml = off.length ? `<details class="rb-off"><summary>${esc(T("rb_unavailable", { n: off.length }))}</summary><ul>`
      + off.map((b) => `<li>${b.state === "locked" ? lockSvg + " " : ""}<span>${esc(b.title)}</span> <span class="muted">— ${esc(b.reason)}</span></li>`).join("")
      + "</ul></details>" : "";
    return `<details class="rb-ch card${on.length ? "" : " rb-empty"}" data-ch="${esc(c.id)}"${open}>`
      + `<summary><span class="rb-ch-title">${esc(c.title)}</span>${count}${tools}</summary>`
      + `<div class="rb-ch-body">${c.desc ? `<p class="muted rb-ch-desc">${esc(c.desc)}</p>` : ""}${groups}${offHtml}</div></details>`;
  }

  function render() {
    const { S, T, esc } = A();
    const d = R.data, box = el();
    if (!d) return;
    R.profile = profileMatch();
    const chips = (d.profiles || []).map((p) => `<button type="button" class="chip-btn" data-profile="${esc(p.id)}" aria-pressed="${R.profile === p.id}" title="${esc(p.desc)}">${esc(p.title)}</button>`).join("")
      + `<span class="chip-btn chip-static" aria-pressed="${R.profile === "custom"}">${esc(T("rb_custom"))}</span>`;
    const pdesc = (d.profiles.find((p) => p.id === R.profile) || {}).desc || "";
    const n = R.sel.size, avail = d.counts.available, total = d.counts.total;
    const lang = (S.languages && S.languages[S.lang]) || S.lang;
    const head = `<div class="rb-head card">
      <div class="rb-profiles"><span class="eyebrow">${esc(T("rb_profile"))}</span><div class="chips">${chips}</div>
        <p class="muted rb-pdesc">${esc(pdesc)}</p></div>
      <div class="rb-actions">
        <div class="rb-summary"><b>${esc(T("rb_selected", { n }))}</b>
          <span class="muted">${esc(T("rb_available", { a: avail, t: total }))}</span>
          <span class="muted">${esc(T("rb_language", { lang }))}</span></div>
        <div class="rb-buttons"><button type="button" class="btn btn-primary" id="rb-create" ${n && !R.busy && !R.storeError ? "" : "disabled"}>${esc(T(R.job ? "rb_writing" : R.busy ? "rb_creating" : "rb_create"))}</button></div>
        ${n ? "" : `<p class="rb-hint">${esc(T("rb_pick_one"))}</p>`}
        ${progressHtml()}
        ${usageHtml()}
        ${R.msg && !R.job ? `<div class="rb-msg" role="status">${esc(R.msg)}</div>` : ""}
      </div>
      ${d.period.note ? `<div class="banner rb-note">${esc(d.period.note)}</div>` : ""}
    </div>`;
    const layers = d.layers.map((ly, i) => `<section class="rb-layer"><h2 class="rb-layer-title"><span class="n">${i + 1}</span>${esc(ly.title)}</h2>`
      + (ly.note ? `<p class="muted rb-layer-note">${esc(ly.note)}</p>` : "")
      + ly.chapters.map(chapterHtml).join("") + "</section>").join("");
    const y = window.scrollY;
    // v55: the long list of chapters sits in its own collapsed section, so the page opens short
    const steps = d.layers.map((ly) => ly.title).join(" \u2192 ");
    const config = `<details class="rb-ch rb-config card"${R.cfgOpen ? " open" : ""}><summary><span class="rb-ch-title">${esc(T("rb_cfg_title"))}</span>`
      + `<span class="rb-count-chip on">${esc(T("rb_selected", { n }))}</span></summary>`
      + `<div class="rb-ch-body"><p class="muted rb-ch-desc">${esc(T("rb_cfg_hint", { n: d.layers.length, steps }))}</p>${layers}</div></details>`;
    box.innerHTML = head + config + mineHtml();
    window.scrollTo({ top: y });
    if (window.VdashSchedules) window.VdashSchedules.selectionChanged();
  }

  function spec() {
    const { S } = A();
    const order = allBlocks().map((b) => b.id).filter((id) => R.sel.has(id));
    return { blocks: order, start: S.start, end: S.end, compare: S.compare, lang: S.lang,
      profile: R.profile || "custom", period_preset: S.preset };
  }
  const reportUrl = (id) => A().tenantUrl(`/reports/mine/${encodeURIComponent(id)}`);
  const excelUrl = (id) => A().tenantUrl(`/reports/mine/${encodeURIComponent(id)}/xlsx`);   // v35

  function closeModal() { if (R.modal) { R.modal.remove(); R.modal = null; document.body.classList.remove("rb-modal-open"); } }
  function savePdf() {
    // the browser's own print dialog for the report frame only; "Save as PDF" gives a sharp, searchable PDF.
    // The page title (business, period) becomes the suggested file name while the dialog is open.
    const fr = R.modal && R.modal.querySelector("iframe");
    if (!fr || !fr.contentWindow) return;
    const old = document.title;
    try { document.title = fr.contentDocument.title || old; } catch (e) { /* cross-origin never happens here */ }
    const restore = () => { document.title = old; };
    fr.contentWindow.addEventListener("afterprint", restore, { once: true });
    fr.contentWindow.focus(); fr.contentWindow.print();
    setTimeout(restore, 60000);
  }
  function show(id, tab) {
    const { T, esc } = A();
    const url = reportUrl(id);
    if (tab && !tab.closed) { tab.location.href = url; render(); return; }
    if (isPhone()) { window.open(url, "_blank", "noopener"); render(); return; }
    render();
    closeModal();
    const m = document.createElement("div");
    m.className = "rb-modal"; m.setAttribute("role", "dialog"); m.setAttribute("aria-modal", "true");
    m.innerHTML = `<div class="rb-modal-box"><div class="rb-modal-bar"><b>${esc(T("rb_preview"))}</b><span class="rb-modal-status muted">${esc(T("rb_preparing"))}</span>
      <button type="button" class="btn btn-primary btn-sm" data-pdf disabled>${esc(T("rb_save_pdf"))}</button>
      <button type="button" class="btn btn-sm" data-share-now>${esc(T("sh_btn"))}</button>
      <a class="btn btn-sm" href="${esc(excelUrl(id))}" download title="${esc(T("rb_excel_tip"))}">${esc(T("rpt_excel"))}</a>
      <a class="btn btn-sm" href="${esc(url)}" target="_blank" rel="noopener">${esc(T("rb_open_tab"))}</a>
      <button type="button" class="btn btn-sm" data-close>${esc(T("rb_close"))}</button></div>
      <iframe title="${esc(T("rb_preview"))}" src="${esc(url)}"></iframe></div>`;
    m.addEventListener("click", (e) => {
      if (e.target === m || e.target.closest("[data-close]")) closeModal();
      else if (e.target.closest("[data-pdf]")) savePdf();
      else if (e.target.closest("[data-share-now]") && window.VdashShare) window.VdashShare.open(id, "");
    });
    m.querySelector("iframe").addEventListener("load", () => {
      const s = m.querySelector(".rb-modal-status"); if (s) s.textContent = T("rb_pdf_hint");
      const b = m.querySelector("[data-pdf]"); if (b) b.disabled = false;
    });
    document.body.appendChild(m); document.body.classList.add("rb-modal-open");
    R.modal = m; m.querySelector("[data-close]").focus();
  }

  document.addEventListener("click", (e) => {
    const box = el(); if (!box || box.hidden || !box.contains(e.target)) return;
    const prof = e.target.closest("[data-profile]");
    if (prof) { applyProfile(prof.dataset.profile); R.msg = ""; render(); return; }
    const all = e.target.closest("[data-all],[data-none]");
    if (all) {
      e.preventDefault(); e.stopPropagation();
      const cid = all.dataset.all || all.dataset.none;
      const c = R.data.layers.flatMap((ly) => ly.chapters).find((x) => x.id === cid);
      c.blocks.filter((b) => b.state === "available").forEach((b) => (all.dataset.all ? R.sel.add(b.id) : R.sel.delete(b.id)));
      R.open.add(cid); render(); return;
    }
    if (e.target.id === "rb-create") { createReport(); return; }
    const sb = e.target.closest("[data-share]");
    if (sb && window.VdashShare) { window.VdashShare.open(sb.dataset.share, sb.dataset.shareName); return; }
    const mb = e.target.closest("[data-mine]");
    if (mb) { R.msg = ""; show(mb.dataset.mine); }
  });
  document.addEventListener("change", (e) => {
    const cb = e.target.closest && e.target.closest(".rb-item input[data-id]");
    if (!cb) return;
    cb.checked ? R.sel.add(cb.dataset.id) : R.sel.delete(cb.dataset.id);
    R.msg = ""; render();
  });
  document.addEventListener("toggle", (e) => {
    const d = e.target; if (!(d instanceof HTMLDetailsElement)) return;
    if (d.classList.contains("rb-mine")) { R.mineOpen = d.open; return; }
    if (d.classList.contains("rb-config")) { R.cfgOpen = d.open; return; }
    if (!d.dataset.ch) return;
    d.open ? R.open.add(d.dataset.ch) : R.open.delete(d.dataset.ch);
  }, true);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && R.modal) closeModal(); });

  window.VdashReports = {
    load,
    hide() { closeModal(); const b = el(); if (b) b.hidden = true; if (window.VdashSchedules) window.VdashSchedules.hide(); },
    // v49: what the regular-report form (schedule.js) takes: the ticked figures, in report order, and the profile
    selection() { return R.data ? { blocks: spec().blocks, profile: R.profile || "custom" } : { blocks: [], profile: "custom" }; },
    openReport(id) { show(id); },
    reset() { if (window.VdashSchedules) window.VdashSchedules.reset();
      R.tenant = null; R.data = null; R.sel = new Set(); R.profile = null; R.open = new Set();
      clearInterval(R.tick);                                  // reports being written stay watched (W)
      R.mine = null; R.usage = null; R.msg = ""; R.storeError = ""; R.busy = false; R.jobs = []; R.job = null; },
    // v41.1: called by app.js once a business is loaded (signed in): resume watching, pick up reports being written
    async watchTenant() {
      W.ready = true; schedule(500);
      try {
        const d = await (await fetch(A().tenantUrl("/reports/mine"), { credentials: "same-origin", cache: "no-store" })).json();
        (d.jobs || []).filter((j) => j.status === "writing").forEach((j) => watch(j, A().S.tenant, null, false));
      } catch (e) { /* the report database may be down: nothing to watch */ }
    },
    _state: R,
  };
})();
