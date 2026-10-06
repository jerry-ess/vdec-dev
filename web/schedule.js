/* Veridex BI - regular reports (v49, AI plan Step 25).
 * A card above the report builder (business administrators only): the regular reports of this business - when
 * each one is made, who receives it, what happened last time - and a form to set one up from the figures ticked
 * in the builder below. Everything is stored on the server (vdash/schedule_routes.py); the scheduler makes the
 * report of the last complete week / month / quarter and mails it to the addresses that confirmed by link.
 * Talks to the page only through window.VdashApp and window.VdashReports.
 */
(() => {
  "use strict";
  const A = () => window.VdashApp;
  const Q = { tenant: null, data: null, form: null, msg: "", err: "", busy: false, open: false, seq: 0,
    del: null, preview: null, pvTimer: null, pvSeq: 0, noteList: [] };
  const el = () => document.getElementById("report-schedules");
  const isAdmin = () => { const r = A().S.me && A().S.me.role; return r === "tenant_admin" || r === "platform_admin"; };
  const su = (suffix) => A().tenantUrl("/report-schedules" + (suffix || ""));   // tenantUrl adds ?lang=..., so the path is complete first
  const sel = () => (window.VdashReports && window.VdashReports.selection) ? window.VdashReports.selection() : { blocks: [], profile: "custom" };
  const defDay = (f) => (Q.data && Q.data.defaults && Q.data.defaults[f]) ?? (f === "weekly" ? 0 : 1);

  function weekday(i) {
    try { return new Intl.DateTimeFormat(A().S.locale, { weekday: "long", timeZone: "UTC" }).format(new Date(Date.UTC(2026, 9, 5 + i))); }
    catch (e) { return ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][i]; }
  }
  function when(s) {
    const { T } = A();
    const day = s.frequency === "weekly" ? (s.day_of_week ?? defDay("weekly")) : (s.day_of_month ?? defDay(s.frequency));
    if (s.frequency === "weekly") return T("rs_every_week", { day: weekday(day), time: s.send_time });
    return T(s.frequency === "monthly" ? "rs_every_month" : "rs_every_quarter", { day, time: s.send_time });
  }
  function stamp(ts) {
    const { S } = A();
    try { return new Intl.DateTimeFormat(S.locale, { dateStyle: "medium", timeStyle: "short", timeZone: (Q.data && Q.data.timezone) || undefined }).format(new Date(ts * 1000)); }
    catch (e) { return new Date(ts * 1000).toLocaleString(); }
  }
  function runLine(r) {
    const { T, fmtDate } = A();
    return T("rs_run_line", { when: stamp(r.run_at), from: fmtDate(r.period_start), to: fmtDate(r.period_end) });
  }
  const langName = (code) => ((A().S.languages || {})[code]) || code;

  // ------------------------------------------------------------------ loading
  async function load() {
    const { S, api, esc, T } = A();
    const box = el(); if (!box) return;
    if (!isAdmin()) { box.hidden = true; return; }
    box.hidden = false;
    const seq = ++Q.seq;
    try {
      const d = await api(su());
      if (seq !== Q.seq) return;
      Q.data = d; Q.tenant = S.tenant; Q.err = "";
    } catch (e) {
      if (seq !== Q.seq) return;
      Q.err = e.message; Q.data = Q.data || null;
    }
    render();
  }

  // ------------------------------------------------------------------ the list
  function statusHtml(s) {
    const { T, esc } = A();
    if (!s.last_status) return `<span class="muted">${esc(T("rs_never"))}</span>`;
    const label = (A().S.ui && A().S.ui["rs_status_" + s.last_status] !== undefined) ? T("rs_status_" + s.last_status) : s.last_status;
    const known = (k) => A().S.ui && A().S.ui[k] !== undefined;
    const why = s.last_error ? ` – ${esc(known("rs_why_" + s.last_error) ? T("rs_why_" + s.last_error) : T("rs_why_other"))}` : "";
    const bad = s.last_status === "failed" || s.last_status === "skipped";
    return `<span class="${bad ? "rs-bad" : ""}">${esc(label)}${why}</span>${s.last_run ? ` <span class="muted">· ${esc(stamp(s.last_run))}</span>` : ""}`;
  }
  function recipientHtml(r) {
    const { T, esc } = A();
    const again = r.state === "pending" || r.state === "unknown" || r.state === "revoked"
      ? ` <button type="button" class="rs-link" data-rs-again="${esc(r.address)}">${esc(T(r.state === "revoked" ? "rs_ask_again" : "rs_send_again"))}</button>` : "";
    return `<li class="rs-rcpt rs-${esc(r.state)}"><span class="rs-addr">${esc(r.address)}</span>
      <span class="rs-state">${esc(T("rs_rstate_" + r.state))}</span>${again}</li>`;
  }
  function itemHtml(s) {
    const { T, esc } = A();
    const nr = s.next_runs || [];
    const next = s.enabled && nr.length
      ? `<div><span class="muted">${esc(T("rs_next"))}</span> ${esc(runLine(nr[0]))}</div>${nr.length > 1 ? `<div class="muted rs-more">${esc(T("rs_then"))} ${nr.slice(1).map((r) => esc(stamp(r.run_at))).join(" · ")}</div>` : ""}`
      : `<div class="muted">${esc(T("rs_paused_note"))}</div>`;
    const rec = (s.recipients || []).length
      ? `<ul class="rs-rcpts">${s.recipients.map(recipientHtml).join("")}</ul>` : `<p class="muted">${esc(T("rs_no_recipients"))}</p>`;
    const confirming = Q.del === s.id;
    const acts = confirming
      ? `<span class="rs-confirm">${esc(T("rs_delete_q"))}</span>
         <button type="button" class="btn btn-sm rs-danger" data-rs-del-yes="${esc(s.id)}">${esc(T("rs_delete_yes"))}</button>
         <button type="button" class="btn btn-sm" data-rs-del-no>${esc(T("rs_cancel"))}</button>`
      : `<button type="button" class="btn btn-sm" data-rs-run="${esc(s.id)}" ${s.can_run && !Q.busy ? "" : "disabled"} title="${esc(T("rs_run_tip"))}">${esc(T("rs_run"))}</button>
         <button type="button" class="btn btn-sm" data-rs-toggle="${esc(s.id)}">${esc(T(s.enabled ? "rs_pause" : "rs_resume"))}</button>
         <button type="button" class="btn btn-sm" data-rs-edit="${esc(s.id)}">${esc(T("rs_edit"))}</button>
         <button type="button" class="btn btn-sm" data-rs-del="${esc(s.id)}">${esc(T("rs_delete"))}</button>`;
    const open = s.last_report_id && ["created", "sent"].includes(s.last_status)
      ? ` <button type="button" class="rs-link" data-rs-open="${esc(s.last_report_id)}">${esc(T("rs_open_last"))}</button>` : "";
    return `<li class="rs-item${s.enabled ? "" : " off"}"><div class="rs-main">
        <div class="rs-title"><b>${esc(s.name)}</b><span class="rb-tag">${esc(T(s.enabled ? "rs_on" : "rs_off"))}</span></div>
        <div class="muted">${esc(when(s))} · ${esc(langName(s.lang))} · ${esc(T("rs_figures", { n: s.blocks }))}</div>
        ${next}
        <div class="rs-last"><span class="muted">${esc(T("rs_last"))}</span> ${statusHtml(s)}${open}</div>
        ${rec}</div><div class="rs-acts">${acts}</div></li>`;
  }

  // ------------------------------------------------------------------ the form
  function newForm() {
    const lang = (Q.data && Q.data.business_lang) || "en";
    return { id: null, name: "", frequency: "weekly", day_of_week: defDay("weekly"), day_of_month: defDay("monthly"),
      send_time: "08:00", lang, recipients: [], useTicked: true, hadBlocks: 0, input: "", enabled: true };
  }
  function editForm(s) {
    return { id: s.id, name: s.name, frequency: s.frequency, day_of_week: s.day_of_week ?? defDay("weekly"),
      day_of_month: s.day_of_month ?? defDay(s.frequency === "weekly" ? "monthly" : s.frequency), send_time: s.send_time,
      lang: s.lang, recipients: s.recipients.map((r) => r.address), useTicked: false, hadBlocks: s.blocks, input: "", enabled: s.enabled };
  }
  function selText() {
    const { T } = A();
    return T("rs_ticked", { n: sel().blocks.length });
  }
  function formHtml() {
    const f = Q.form; if (!f) return "";
    const { S, T, esc } = A();
    const opt = (v, label, cur) => `<option value="${esc(v)}" ${String(cur) === String(v) ? "selected" : ""}>${esc(label)}</option>`;
    const dayField = f.frequency === "weekly"
      ? `<label><span>${esc(T("rs_day"))}</span><select data-rs-f="day_of_week">${[0, 1, 2, 3, 4, 5, 6].map((i) => opt(i, weekday(i), f.day_of_week)).join("")}</select></label>`
      : `<label><span>${esc(T("rs_day_of_month"))}</span><select data-rs-f="day_of_month">${Array.from({ length: 28 }, (_, i) => i + 1).map((i) => opt(i, T("rs_day_n", { n: i }), f.day_of_month)).join("")}</select></label>`;
    const langs = Object.entries(S.languages || { en: "English" }).map(([c, n]) => opt(c, n, f.lang)).join("");
    const contents = f.id
      ? `<div class="rs-contents"><label class="rs-radio"><input type="radio" name="rs-keep" data-rs-keep="1" ${f.useTicked ? "" : "checked"}> ${esc(T("rs_keep_figures", { n: f.hadBlocks }))}</label>
         <label class="rs-radio"><input type="radio" name="rs-keep" data-rs-keep="0" ${f.useTicked ? "checked" : ""}> <span data-rs-sel>${esc(selText())}</span></label></div>`
      : `<div class="rs-contents"><span data-rs-sel>${esc(selText())}</span><p class="muted rs-note">${esc(T("rs_contents_hint"))}</p></div>`;
    const chips = f.recipients.map((a, i) => `<li class="rs-chip"><span>${esc(a)}</span><button type="button" data-rs-rm="${i}" aria-label="${esc(T("rs_remove"))}">×</button></li>`).join("");
    const pv = Q.preview && Q.preview.runs
      ? `<ul class="rs-preview">${Q.preview.runs.map((r) => `<li>${esc(runLine(r))}</li>`).join("")}</ul>` : "";
    const noSel = !f.id && !sel().blocks.length ? `<p class="rb-hint" data-rs-nosel>${esc(T("rs_pick_figures"))}</p>` : "";
    return `<form class="rs-form card" data-rs-form novalidate>
      <h3>${esc(T(f.id ? "rs_edit_title" : "rs_new_title"))}</h3>
      <label class="rs-wide"><span>${esc(T("rs_name"))}</span><input data-rs-f="name" maxlength="80" value="${esc(f.name)}" placeholder="${esc(T("rs_name_ph"))}"></label>
      ${contents}${noSel}
      <div class="rs-grid">
        <label><span>${esc(T("rs_frequency"))}</span><select data-rs-f="frequency">${["weekly", "monthly", "quarterly"].map((v) => opt(v, T("rs_freq_" + v), f.frequency)).join("")}</select></label>
        ${dayField}
        <label><span>${esc(T("rs_time"))}</span><input type="time" data-rs-f="send_time" value="${esc(f.send_time)}"></label>
        <label><span>${esc(T("rs_language"))}</span><select data-rs-f="lang">${langs}</select></label>
      </div>
      <p class="muted rs-note">${esc(T("rs_tz", { tz: (Q.data && Q.data.timezone) || "UTC" }))} ${esc(T("rs_period_hint_" + f.frequency))}</p>
      <div data-rs-preview>${pv}</div>
      <div class="rs-wide"><span class="rs-label">${esc(T("rs_recipients"))}</span>
        <ul class="rs-chips">${chips}</ul>
        <div class="rs-add"><input type="email" data-rs-f="input" autocomplete="off" inputmode="email" value="${esc(f.input)}" placeholder="${esc(T("rs_add_ph"))}" aria-label="${esc(T("rs_recipients"))}">
          <button type="button" class="btn btn-sm" data-rs-addaddr>${esc(T("rs_add"))}</button></div>
        <p class="muted rs-note">${esc(T("rs_confirm_hint", { n: (Q.data && Q.data.limits.recipients_per_schedule) || 10 }))}</p></div>
      ${Q.err ? `<div class="rb-msg rs-bad" role="alert" data-rs-err>${esc(Q.err)}</div>` : ""}
      <div class="rs-buttons"><button type="submit" class="btn btn-primary" ${Q.busy ? "disabled" : ""}>${esc(T(Q.busy ? "rs_saving" : "rs_save"))}</button>
        <button type="button" class="btn" data-rs-cancel>${esc(T("rs_cancel"))}</button></div></form>`;
  }

  // ------------------------------------------------------------------ the card
  function render() {
    const box = el(); if (!box || !isAdmin()) return;
    const { T, esc } = A();
    const d = Q.data;
    const n = d ? d.schedules.length : 0;
    let body;
    if (!d) body = Q.err ? `<p class="rb-hint">${esc(Q.err)}</p>` : `<div class="empty">${esc(T("loading"))}</div>`;
    else {
      const warn = !d.mail.configured || !d.mail.public_url ? `<div class="banner rb-note">${esc(T("rs_mail_off"))}</div>` : "";
      const full = n >= d.limits.schedules;
      body = `<p class="muted rs-intro">${esc(T("rs_intro"))}</p>${warn}
        ${n ? `<ul class="rs-list">${d.schedules.map(itemHtml).join("")}</ul>` : `<p class="muted">${esc(T("rs_none"))}</p>`}
        ${Q.msg ? `<div class="rb-msg" role="status">${esc(Q.msg)}</div>` : ""}
        ${Q.noteList.map((x) => `<div class="rb-msg rs-bad" role="status">${esc(x.address)}: ${esc((A().S.ui && A().S.ui["rs_note_" + x.code] !== undefined) ? T("rs_note_" + x.code) : T("rs_note_other"))}</div>`).join("")}
        ${Q.form ? formHtml() : `<div class="rs-newrow"><button type="button" class="btn btn-primary" data-rs-new ${full ? "disabled" : ""}>${esc(T("rs_new"))}</button>${full ? `<span class="rb-hint">${esc(T("rs_full", { n: d.limits.schedules }))}</span>` : ""}</div>`}`;
    }
    const keep = document.activeElement && document.activeElement.dataset ? document.activeElement.dataset.rsF : null;
    const pos = keep && document.activeElement.selectionStart != null ? document.activeElement.selectionStart : null;
    box.innerHTML = `<details class="rb-ch card rs-card" ${Q.open ? "open" : ""}><summary><span class="rb-ch-title">${esc(T("rs_title"))}</span>
      <span class="rb-count-chip${n ? " on" : ""}">${n}</span></summary><div class="rb-ch-body">${body}</div></details>`;
    if (keep) {
      const nx = box.querySelector(`[data-rs-f="${keep}"]`);
      if (nx) { nx.focus(); try { if (pos != null) nx.setSelectionRange(pos, pos); } catch (e) { /* time / select */ } }
    }
  }

  // ------------------------------------------------------------------ talking to the server
  function payload(f) {
    const p = { name: f.name.trim() || null, frequency: f.frequency, send_time: f.send_time, lang: f.lang, recipients: f.recipients };
    if (f.frequency === "weekly") p.day_of_week = Number(f.day_of_week); else p.day_of_month = Number(f.day_of_month);
    if (!f.id || f.useTicked) { const s = sel(); p.blocks = s.blocks; p.profile = s.profile || "custom"; }
    return p;
  }
  function takeResult(d, okMsg) {
    Q.data = d; Q.err = ""; Q.msg = okMsg || ""; Q.noteList = d.notes || []; Q.form = null; Q.preview = null; Q.del = null;
  }
  async function call(path, opts, okMsg) {
    const { api } = A();
    Q.busy = true; Q.err = ""; render();
    try { const d = await api(path, opts); Q.busy = false; takeResult(d, okMsg); render(); return d; }
    catch (e) { Q.busy = false; if (Q.form) Q.err = e.message; else Q.msg = e.message; render(); return null; }
  }
  async function save() {
    const { T } = A();
    const f = Q.form; if (!f) return;
    if (f.input.trim()) addAddress();                       // an address typed but not added yet is not lost
    if (!f.id && !sel().blocks.length) { Q.err = T("rs_pick_figures"); render(); return; }
    if (f.id && f.useTicked && !sel().blocks.length) { Q.err = T("rs_pick_figures"); render(); return; }
    const d = f.id ? await call(su("/" + encodeURIComponent(f.id)), { method: "PATCH", body: JSON.stringify(payload(f)) }, T("rs_saved"))
      : await call(su(), { method: "POST", body: JSON.stringify(payload(f)) }, T("rs_created"));
    if (d && (d.notes || []).length === 0 && f.recipients.length) Q.msg += " " + T("rs_asked");
    if (d) render();
  }
  function addAddress() {
    const f = Q.form; if (!f) return;
    const parts = f.input.split(/[\s,;]+/).map((x) => x.trim().toLowerCase()).filter(Boolean);
    parts.forEach((a) => { if (!f.recipients.includes(a)) f.recipients.push(a); });
    f.input = "";
  }
  function previewSoon() {
    clearTimeout(Q.pvTimer);
    Q.pvTimer = setTimeout(async () => {
      const f = Q.form; if (!f) return;
      const seq = ++Q.pvSeq;
      try {
        const d = await A().api(su("/preview"), { method: "POST", body: JSON.stringify({ frequency: f.frequency, send_time: f.send_time,
          ...(f.frequency === "weekly" ? { day_of_week: Number(f.day_of_week) } : { day_of_month: Number(f.day_of_month) }) }) });
        if (seq !== Q.pvSeq || !Q.form) return;
        Q.preview = d;
        const box = el() && el().querySelector("[data-rs-preview]");
        if (box) box.innerHTML = `<ul class="rs-preview">${d.runs.map((r) => `<li>${A().esc(runLine(r))}</li>`).join("")}</ul>`;
      } catch (e) { /* the form still works without the preview */ }
    }, 250);
  }
  const byId = (id) => Q.data && Q.data.schedules.find((s) => s.id === id);

  // ------------------------------------------------------------------ events
  document.addEventListener("click", (e) => {
    const box = el(); if (!box || box.hidden || !box.contains(e.target)) return;
    const g = (a) => e.target.closest(`[${a}]`);
    let x;
    if (g("data-rs-new")) { Q.form = newForm(); Q.msg = ""; Q.noteList = []; Q.err = ""; Q.open = true; render(); previewSoon(); return; }
    if ((x = g("data-rs-edit"))) { const s = byId(x.dataset.rsEdit); if (s) { Q.form = editForm(s); Q.msg = ""; Q.noteList = []; Q.err = ""; render(); previewSoon(); } return; }
    if (g("data-rs-cancel")) { Q.form = null; Q.err = ""; Q.preview = null; render(); return; }
    if (g("data-rs-addaddr")) { addAddress(); render(); const i = box.querySelector('[data-rs-f="input"]'); if (i) i.focus(); return; }
    if ((x = g("data-rs-rm"))) { Q.form.recipients.splice(Number(x.dataset.rsRm), 1); render(); return; }
    if ((x = g("data-rs-toggle"))) { const s = byId(x.dataset.rsToggle); if (s) call(su("/" + encodeURIComponent(s.id)), { method: "PATCH", body: JSON.stringify({ enabled: !s.enabled }) }, ""); return; }
    if ((x = g("data-rs-run"))) { call(su("/" + encodeURIComponent(x.dataset.rsRun) + "/run"), { method: "POST" }, A().T("rs_run_started")); return; }
    if ((x = g("data-rs-del"))) { Q.del = x.dataset.rsDel; render(); return; }
    if (g("data-rs-del-no")) { Q.del = null; render(); return; }
    if ((x = g("data-rs-del-yes"))) { call(su("/" + encodeURIComponent(x.dataset.rsDelYes)), { method: "DELETE" }, A().T("rs_deleted")); return; }
    if ((x = g("data-rs-again"))) {
      const s = A();
      const a = x.dataset.rsAgain;
      s.api(s.tenantUrl("/report-recipients"), { method: "POST", body: JSON.stringify({ address: a, lang: s.S.lang || "en" }) })
        .then(() => { Q.msg = s.T("rs_asked_one", { address: a }); return load(); })
        .catch((er) => { Q.msg = er.message; render(); });
      return;
    }
    if ((x = g("data-rs-open"))) { if (window.VdashReports && window.VdashReports.openReport) window.VdashReports.openReport(x.dataset.rsOpen); return; }
  });
  document.addEventListener("input", (e) => {
    const t = e.target.closest && e.target.closest("[data-rs-f]"); if (!t || !Q.form) return;
    const k = t.dataset.rsF;
    Q.form[k] = t.value;
    if (k === "send_time") previewSoon();
  });
  document.addEventListener("change", (e) => {
    const k = e.target.closest && e.target.closest("[data-rs-keep]");
    if (k && Q.form) { Q.form.useTicked = k.dataset.rsKeep === "0"; return; }
    const t = e.target.closest && e.target.closest("[data-rs-f]"); if (!t || !Q.form) return;
    Q.form[t.dataset.rsF] = t.value;
    if (t.dataset.rsF === "frequency") {
      if (Q.form.frequency !== "weekly" && !Q.form.day_of_month) Q.form.day_of_month = defDay(Q.form.frequency);
      render();
    }
    if (["frequency", "day_of_week", "day_of_month", "send_time"].includes(t.dataset.rsF)) previewSoon();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && e.target.matches && e.target.matches('[data-rs-f="input"]')) { e.preventDefault(); addAddress(); render(); const i = el().querySelector('[data-rs-f="input"]'); if (i) i.focus(); }
  });
  document.addEventListener("submit", (e) => {
    if (!e.target.matches || !e.target.matches("[data-rs-form]")) return;
    e.preventDefault(); save();
  });
  document.addEventListener("toggle", (e) => {
    const d = e.target; if (d instanceof HTMLDetailsElement && d.classList.contains("rs-card")) Q.open = d.open;
  }, true);

  window.VdashSchedules = {
    load,
    when,                                   // v51: "every Monday at 08:00" - the Settings page lists the schedules with it
    hide() { const b = el(); if (b) b.hidden = true; },
    open() { Q.open = true; render(); },                       // v51.2: "Manage regular reports" arrives with the block expanded
    show() { const b = el(); if (b) b.hidden = !isAdmin(); },   // v51.2: coming back to Reports shows the block again
    reset() { Q.tenant = null; Q.data = null; Q.form = null; Q.msg = ""; Q.err = ""; Q.del = null; Q.noteList = []; Q.preview = null; Q.open = false; ++Q.seq; },
    // the ticks in the builder changed: update the "N figures" text of an open form without redrawing it
    selectionChanged() {
      const b = el(); if (!b || !Q.form) return;
      b.querySelectorAll("[data-rs-sel]").forEach((n) => { n.textContent = selText(); });
      const hint = b.querySelector("[data-rs-nosel]"); if (hint) hint.hidden = sel().blocks.length > 0;
    },
    _state: Q,
  };
})();
