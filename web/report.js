/* Veridex BI - report builder (v30).
 * Shown as the "Reports" entry of the section navigation. Uses the dashboard's period / comparison filters and
 * language. Lists every report block (all KPIs + summary blocks) with its state for the business, user and period;
 * blocks without data, above the user's role or not valid for a past period cannot be ticked and say why.
 * Profiles pre-select blocks; Preview shows the report in a frame (new tab on phones), where "Save as PDF" opens
 * the browser's print dialog for the report alone (v31: no separate download - business owners keep PDFs).
 * Talks to app.js only through window.VdashApp.
 */
(() => {
  "use strict";
  const A = () => window.VdashApp;
  const GROUPS = ["summary", "figures", "charts", "lists"];
  const R = { tenant: null, data: null, sel: new Set(), profile: null, open: new Set(), seq: 0, modal: null,
    configs: null, history: null, storeError: "", current: null, pending: null, pendingTab: null, msg: "", delArm: false, histOpen: false };

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
    catch (e) {
      if (seq === R.seq) { box.innerHTML = `<div class="err">${esc(e.message)}</div>`; if (R.pendingTab) { R.pendingTab.close(); R.pendingTab = null; } }
      return;
    }
    if (seq !== R.seq) return;
    const firstTime = R.tenant !== S.tenant || !R.data;
    const prevProfile = R.profile;
    R.data = d; R.tenant = S.tenant;
    const avail = availableIds();
    if (R.pending) {                                     // v32: a saved report was opened
      const c = R.pending; R.pending = null;
      if (c.profile && c.profile !== "custom" && d.profiles.some((p) => p.id === c.profile)) applyProfile(c.profile);
      else { R.sel = new Set((c.blocks || []).filter((x) => avail.has(x))); R.profile = "custom"; openSelected(); }
      render();
      const tab = R.pendingTab; R.pendingTab = null;
      if (R.sel.size) { preview(undefined, tab); }                // v33: show the saved report straight away
      else { if (tab) tab.close(); R.msg = A().T("rb_saved_empty", { name: c.name }); render(); }
      if (R.configs === null) loadSaved();
      return;
    } else if (firstTime) applyProfile(d.default_profile || (d.profiles[0] && d.profiles[0].id));
    else if (prevProfile && prevProfile !== "custom") applyProfile(prevProfile);   // profile follows the new period's data
    else { R.sel = new Set([...R.sel].filter((x) => avail.has(x))); R.profile = "custom"; }
    render();
    if (firstTime || R.configs === null) loadSaved();
  }
  function openSelected() {
    R.open = new Set(R.data.layers.flatMap((ly) => ly.chapters.filter((c) => c.blocks.some((b) => R.sel.has(b.id))).map((c) => c.id)));
  }

  // ---------------------------------------------------------------- v32: saved reports + history
  async function loadSaved() {
    const { api, tenantUrl } = A();
    try {
      const [c, h] = await Promise.all([api(tenantUrl("/reports/configs")), api(tenantUrl("/reports/history", { limit: 30 }))]);
      R.configs = c.configs; R.history = h.history; R.storeError = "";
    } catch (e) { R.configs = R.configs || []; R.history = R.history || []; R.storeError = e.message; }
    render();
  }
  function currentConfig() { return (R.configs || []).find((c) => c.id === R.current) || null; }
  function openConfig(id) {
    const c = (R.configs || []).find((x) => x.id === id); if (!c) return;
    R.current = c.id; R.pending = c; R.msg = A().T("rb_opened", { name: c.name }); R.delArm = false;
    // v33: opening a saved report shows it. Phones open the report in a new tab - reserve that tab now, while the
    // choice is still a user action (a tab opened after the data has loaded would be blocked as a pop-up).
    R.pendingTab = null;
    if (isPhone()) { try { R.pendingTab = window.open("", "_blank"); if (R.pendingTab) R.pendingTab.opener = null; } catch (e) { R.pendingTab = null; } }
    A().setPeriod(c.period_preset, c.period_start, c.period_end, c.compare);   // reloads the builder for that period
  }
  function configBody(name) {
    const { S } = A();
    return { name, profile: R.profile === "custom" ? "custom" : R.profile, period_preset: S.preset,
      period_start: S.preset === "custom" ? S.start : null, period_end: S.preset === "custom" ? S.end : null,
      compare: S.compare, blocks: allBlocks().map((b) => b.id).filter((x) => R.sel.has(x)) };
  }
  async function saveConfig(asNew) {
    const { api, tenantUrl, T } = A();
    const input = document.getElementById("rb-name"), name = (input && input.value || "").trim();
    if (!name) { R.msg = T("rb_name_needed"); render(); if (input) input.focus(); return; }
    try {
      const cur = currentConfig();
      const row = asNew || !cur
        ? await api(tenantUrl("/reports/configs"), { method: "POST", body: JSON.stringify(configBody(name)) })
        : await api(tenantUrl(`/reports/configs/${encodeURIComponent(cur.id)}`), { method: "PUT", body: JSON.stringify(configBody(name)) });
      R.current = row.id; R.msg = T("rb_saved_ok", { name: row.name }); R.delArm = false;
      await loadSaved();
    } catch (e) { R.msg = e.message; render(); }
  }
  async function deleteConfig() {
    const { api, tenantUrl, T } = A();
    const cur = currentConfig(); if (!cur) return;
    if (!R.delArm) { R.delArm = true; R.msg = T("rb_delete_sure"); render(); return; }
    try {
      await api(tenantUrl(`/reports/configs/${encodeURIComponent(cur.id)}`), { method: "DELETE" });
      R.msg = T("rb_deleted", { name: cur.name }); R.current = null; R.delArm = false;
      await loadSaved();
    } catch (e) { R.msg = e.message; R.delArm = false; render(); }
  }
  function savedHtml() {
    const { T, esc } = A();
    if (R.configs === null) return `<div class="rb-saved muted">${esc(T("loading"))}</div>`;
    const cur = currentConfig();
    const opts = `<option value="">${esc(T(R.configs.length ? "rb_choose" : "rb_saved_none"))}</option>`
      + R.configs.map((c) => `<option value="${esc(c.id)}" ${c.id === R.current ? "selected" : ""}>${esc(c.name)}</option>`).join("");
    const nameVal = cur ? cur.name : "";
    const btns = `<button type="button" class="btn btn-sm" id="rb-save-new">${esc(T("rb_save_as"))}</button>`
      + (cur && cur.can_change ? `<button type="button" class="btn btn-sm" id="rb-update">${esc(T("rb_update"))}</button>`
        + `<button type="button" class="btn btn-sm${R.delArm ? " btn-danger" : ""}" id="rb-delete">${esc(T(R.delArm ? "rb_delete_confirm" : "rb_delete"))}</button>` : "");
    const who = cur ? `<span class="muted">${esc(T("rb_saved_by", { who: cur.created_by }))}</span>` : "";
    return `<div class="rb-saved"><span class="eyebrow">${esc(T("rb_saved"))}</span>
      <div class="rb-saved-row"><select id="rb-config" aria-label="${esc(T("rb_saved"))}" ${R.storeError ? "disabled" : ""}>${opts}</select>
        <input id="rb-name" type="text" maxlength="80" placeholder="${esc(T("rb_name_ph"))}" value="${esc(nameVal)}" ${R.storeError ? "disabled" : ""}>
        ${R.storeError ? "" : btns}</div>
      <div class="rb-msg" role="status">${esc(R.storeError ? T("rb_store_down") : R.msg)} ${R.storeError ? "" : who}</div></div>`;
  }
  function historyHtml() {
    const { S, T, esc, fmtDate } = A();
    if (!R.history) return "";
    const df = new Intl.DateTimeFormat(S.locale, { dateStyle: "medium", timeStyle: "short" });
    const rows = R.history.map((h) => `<li><div class="rb-h-main"><b>${esc(fmtDate(h.period_start))} – ${esc(fmtDate(h.period_end))}</b>
        <span class="muted">${esc(T("rb_parts", { n: h.parts }))} · ${esc((S.languages || {})[h.language] || h.language)}${h.mine ? "" : " · " + esc(h.generated_by)}</span>
        ${h.data_changed ? `<span class="rb-tag">${esc(T("rb_data_changed"))}</span>` : ""}</div>
        <div class="rb-h-side"><span class="muted">${esc(df.format(new Date(h.generated_at * 1000)))}</span>
        <button type="button" class="btn btn-sm" data-hist="${esc(h.id)}">${esc(T("rb_open"))}</button></div></li>`).join("");
    return `<details class="rb-ch card rb-history" ${R.histOpen ? "open" : ""}><summary><span class="rb-ch-title">${esc(T("rb_history"))}</span>
      <span class="rb-count-chip">${R.history.length}</span></summary><div class="rb-ch-body">
      ${R.history.length ? `<ul class="rb-hist">${rows}</ul>` : `<p class="muted">${esc(T("rb_history_none"))}</p>`}</div></details>`;
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
        <div class="rb-buttons"><button type="button" class="btn btn-primary" id="rb-preview" ${n ? "" : "disabled"}>${esc(T("rb_preview_pdf"))}</button></div>
        ${n ? "" : `<p class="rb-hint">${esc(T("rb_pick_one"))}</p>`}
      </div>
      ${savedHtml()}
      ${d.period.note ? `<div class="banner rb-note">${esc(d.period.note)}</div>` : ""}
    </div>`;
    const layers = d.layers.map((ly, i) => `<section class="rb-layer"><h2 class="rb-layer-title"><span class="n">${i + 1}</span>${esc(ly.title)}</h2>`
      + (ly.note ? `<p class="muted rb-layer-note">${esc(ly.note)}</p>` : "")
      + ly.chapters.map(chapterHtml).join("") + "</section>").join("");
    const y = window.scrollY;
    box.innerHTML = head + layers + historyHtml();
    window.scrollTo({ top: y });
  }

  function spec() {
    const { S } = A();
    const order = allBlocks().map((b) => b.id).filter((id) => R.sel.has(id));
    return { blocks: order, start: S.start, end: S.end, compare: S.compare, lang: S.lang,
      profile: R.profile || "custom", period_preset: S.preset, config_id: R.current || null };
  }
  const renderUrl = (download, sp) => A().tenantUrl("/reports/render", { spec: JSON.stringify(sp || spec()), download: download ? "1" : "0" });

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
  function preview(sp, tab) {
    const { T, esc } = A();
    const url = renderUrl(false, sp);
    if (tab && !tab.closed) { tab.location.href = url; return; }      // v33: tab reserved when a saved report was opened
    if (isPhone()) { window.open(url, "_blank", "noopener"); return; }
    closeModal();
    const m = document.createElement("div");
    m.className = "rb-modal"; m.setAttribute("role", "dialog"); m.setAttribute("aria-modal", "true");
    m.innerHTML = `<div class="rb-modal-box"><div class="rb-modal-bar"><b>${esc(T("rb_preview"))}</b><span class="rb-modal-status muted">${esc(T("rb_preparing"))}</span>
      <button type="button" class="btn btn-primary btn-sm" data-pdf disabled>${esc(T("rb_save_pdf"))}</button>
      <a class="btn btn-sm" href="${esc(url)}" target="_blank" rel="noopener">${esc(T("rb_open_tab"))}</a>
      <button type="button" class="btn btn-sm" data-close>${esc(T("rb_close"))}</button></div>
      <iframe title="${esc(T("rb_preview"))}" src="${esc(url)}"></iframe></div>`;
    m.addEventListener("click", (e) => {
      if (e.target === m || e.target.closest("[data-close]")) closeModal();
      else if (e.target.closest("[data-pdf]")) savePdf();
    });
    m.querySelector("iframe").addEventListener("load", () => {
      const s = m.querySelector(".rb-modal-status"); if (s) s.textContent = T("rb_pdf_hint");
      const b = m.querySelector("[data-pdf]"); if (b) b.disabled = false;
      if (!sp) loadSaved();                                 // the new report is now in the history
    });
    document.body.appendChild(m); document.body.classList.add("rb-modal-open");
    R.modal = m; m.querySelector("[data-close]").focus();
  }

  document.addEventListener("click", (e) => {
    const box = el(); if (!box || box.hidden || !box.contains(e.target)) return;
    const prof = e.target.closest("[data-profile]");
    if (prof) { applyProfile(prof.dataset.profile); render(); return; }
    const all = e.target.closest("[data-all],[data-none]");
    if (all) {
      e.preventDefault(); e.stopPropagation();
      const cid = all.dataset.all || all.dataset.none;
      const c = R.data.layers.flatMap((ly) => ly.chapters).find((x) => x.id === cid);
      c.blocks.filter((b) => b.state === "available").forEach((b) => (all.dataset.all ? R.sel.add(b.id) : R.sel.delete(b.id)));
      R.open.add(cid); render(); return;
    }
    if (e.target.id === "rb-preview") { preview(); return; }
    if (e.target.id === "rb-save-new") { saveConfig(true); return; }
    if (e.target.id === "rb-update") { saveConfig(false); return; }
    if (e.target.id === "rb-delete") { deleteConfig(); return; }
    const hb = e.target.closest("[data-hist]");
    if (hb) {
      const h = (R.history || []).find((x) => x.id === hb.dataset.hist);
      if (h) preview({ ...h.spec, as_of_version: h.data_version, record: false });
    }
  });
  document.addEventListener("change", (e) => {
    if (e.target.id === "rb-config") { if (e.target.value) openConfig(e.target.value); else { R.current = null; R.msg = ""; render(); } return; }
    const cb = e.target.closest && e.target.closest(".rb-item input[data-id]");
    if (!cb) return;
    cb.checked ? R.sel.add(cb.dataset.id) : R.sel.delete(cb.dataset.id);
    render();
  });
  document.addEventListener("toggle", (e) => {
    const d = e.target; if (!(d instanceof HTMLDetailsElement)) return;
    if (d.classList.contains("rb-history")) { R.histOpen = d.open; return; }
    if (!d.dataset.ch) return;
    d.open ? R.open.add(d.dataset.ch) : R.open.delete(d.dataset.ch);
  }, true);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && R.modal) closeModal(); });

  window.VdashReports = {
    load,
    hide() { closeModal(); const b = el(); if (b) b.hidden = true; },
    reset() { R.tenant = null; R.data = null; R.sel = new Set(); R.profile = null; R.open = new Set();
      R.configs = null; R.history = null; R.current = null; R.pending = null; R.msg = ""; R.storeError = ""; },
    _state: R,
  };
})();
