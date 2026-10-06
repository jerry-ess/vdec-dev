/* Veridex BI - the person's own settings (v51).
 * One page in the menu for what belongs to the person, not to a business: language, light / dark, how the
 * dashboard opens (period, comparison, time unit), the trend line, and a look at the regular reports of the
 * business they are in. Choices apply at once and are saved to the person's account (vdash/settings_routes.py)
 * so they follow them to other devices; the page itself keeps no data and makes no access decisions.
 * Talks to the page only through window.VdashApp (and window.VdashSchedules for the schedule wording).
 */
(() => {
  "use strict";
  const A = () => window.VdashApp;
  const el = () => document.getElementById("settings-page");
  // v51.2: "Manage regular reports" opens Reports and brings the regular-reports block into view once it is there
  function toSchedules() {
    let n = 0;
    const tick = () => {
      const b = document.getElementById("report-schedules");
      if (b && !b.hidden && b.offsetParent && document.body.classList.contains("in-reports")) { if (window.VdashSchedules) window.VdashSchedules.open(); b.scrollIntoView({ behavior: "smooth", block: "start" }); return; }
      if (++n < 40) setTimeout(tick, 150);
    };
    setTimeout(tick, 150);
  }
  const Q = { seq: 0, sched: null, schedErr: false, status: "" };
  // the same ids the server accepts (vdash/settings_routes.py); every one has a text in each language
  const PRESETS = ["last_7_days", "last_30_days", "last_90_days", "mtd", "qtd", "ytd", "last_12_months", "prev_month", "prev_quarter", "prev_year", "all"];
  const COMPARES = ["previous", "yoy", "none"];
  const GRAINS = ["day", "week", "month", "quarter"];
  const TRENDS = ["ma", "linear", "off"];
  const THEMES = ["light", "dark", "system"];
  const isAdmin = () => { const r = A().S.me && A().S.me.role; return r === "tenant_admin" || r === "platform_admin"; };

  const row = (label, help, control) => `<div class="st-row"><div class="st-label"><b>${label}</b>${help ? `<span class="muted">${help}</span>` : ""}</div><div class="st-ctl">${control}</div></div>`;
  function seg(name, values, cur, labelOf, aria) {
    const { esc } = A();
    return `<div class="st-seg" role="radiogroup" aria-label="${esc(aria)}">${values.map((v) =>
      `<button type="button" role="radio" aria-checked="${v === cur}" data-st-seg="${name}" data-v="${esc(v)}">${esc(labelOf(v))}</button>`).join("")}</div>`;
  }
  function select(name, options, cur, aria) {
    const { esc } = A();
    return `<select data-st-sel="${name}" aria-label="${esc(aria)}">${options.map(([v, l]) => `<option value="${esc(v)}" ${v === cur ? "selected" : ""}>${esc(l)}</option>`).join("")}</select>`;
  }

  function render() {
    const box = el(); if (!box) return;
    const { S, T, esc } = A();
    const p = S.prefs || {};
    const theme = p.theme || document.documentElement.getAttribute("data-theme") || "system";
    const std = (label) => `${T("set_standard")} (${label})`;
    const langs = Object.entries(S.languages || { en: "English" });
    const role = S.me ? T("role_" + S.me.role) : "";
    const account = `<section class="card st-card" aria-labelledby="st-h-account">
      <h2 id="st-h-account">${esc(T("set_account"))}</h2><p class="muted st-sub">${esc(T("set_account_help"))}</p>
      ${row(esc(T("set_name")), "", esc(S.me.display_name))}
      ${row(esc(T("set_username")), "", esc(S.me.username))}
      ${row(esc(T("set_role")), "", esc(role))}
      ${row(esc(T("set_businesses")), "", String(S.tenants.length))}
      <div class="st-actions"><button type="button" class="btn" data-st-act="logout">${esc(T("sign_out"))}</button></div></section>`;
    const look = `<section class="card st-card" aria-labelledby="st-h-look">
      <h2 id="st-h-look">${esc(T("set_appearance"))}</h2>
      ${row(esc(T("language")), esc(T("set_language_help")), select("lang", langs, S.lang, T("language")))}
      ${row(esc(T("set_theme")), "", seg("theme", THEMES, theme, (v) => T("set_theme_" + v), T("set_theme")))}</section>`;
    const view = `<section class="card st-card" aria-labelledby="st-h-view">
      <h2 id="st-h-view">${esc(T("set_default_view"))}</h2><p class="muted st-sub">${esc(T("set_default_view_help"))}</p>
      ${row(esc(T("period")), "", select("preset", [["", std(T("preset_last_30_days"))], ...PRESETS.map((id) => [id, T("preset_" + id)])], p.preset || "", T("period")))}
      ${row(esc(T("compare")), "", select("compare", [["", std(T("cmp_previous"))], ...COMPARES.map((id) => [id, T("cmp_" + id)])], p.compare || "", T("compare")))}
      ${row(esc(T("set_grain")), "", select("grain", [["", std(T("grain_auto"))], ...GRAINS.map((id) => [id, T("grain_" + id)])], p.grain || "", T("set_grain")))}
      ${row(esc(T("set_trend")), "", seg("trend", TRENDS, p.trend || "ma", (v) => T("trend_" + v), T("set_trend")))}
      <div class="st-actions"><button type="button" class="btn" data-st-act="reset">${esc(T("set_reset"))}</button></div></section>`;
    const reports = `<section class="card st-card" aria-labelledby="st-h-rep"><h2 id="st-h-rep">${esc(T("set_reports"))}</h2>
      <p class="muted st-sub">${esc(T("set_reports_help", { business: S.ctx.tenant.display_name }))}</p><div id="st-reports-body"></div></section>`;
    box.innerHTML = `<div class="st-grid"><div class="st-col">${look}${view}</div><div class="st-col">${reports}${account}</div></div>
      <p id="st-status" class="st-status" role="status" aria-live="polite"></p>
      <p class="muted st-note">${esc(S.prefsOk === false ? T("set_saved_here") : T("set_follow"))}</p>`;
    paintReports();
    paintStatus();
  }

  // ------------------------------------------------------------------ regular reports (a look; they are managed in Reports)
  function paintReports() {
    const body = document.getElementById("st-reports-body"); if (!body) return;
    const { T, esc } = A();
    if (!isAdmin()) { body.innerHTML = `<p class="muted">${esc(T("set_reports_admin_only"))}</p>`; return; }
    let html;
    if (Q.schedErr) html = `<p class="muted">${esc(T("set_reports_unavailable"))}</p>`;
    else if (Q.sched === null) html = `<p class="muted">…</p>`;
    else if (!Q.sched.length) html = `<p class="muted">${esc(T("set_reports_none"))}</p>`;
    else {
      const when = window.VdashSchedules && window.VdashSchedules.when;
      html = `<ul class="st-sched">${Q.sched.map((s) => `<li><span><b>${esc(s.name || "")}</b>${when ? `<span class="muted"> · ${esc(when(s))}</span>` : ""}</span>
        <span class="rb-tag">${esc(T(s.enabled ? "rs_on" : "rs_off"))}</span></li>`).join("")}</ul>`;
    }
    body.innerHTML = html + `<div class="st-actions"><button type="button" class="btn btn-primary" data-st-act="reports">${esc(T("set_reports_manage"))}</button></div>`;
  }
  async function loadSchedules() {
    if (!isAdmin()) return;
    const { api, tenantUrl } = A();
    const seq = ++Q.seq;
    try {
      const d = await api(tenantUrl("/report-schedules"));
      if (seq !== Q.seq) return;
      Q.sched = d.schedules || []; Q.schedErr = false;
    } catch (e) {
      if (seq !== Q.seq) return;
      Q.sched = null; Q.schedErr = true;
    }
    paintReports();
  }

  // ------------------------------------------------------------------ saving feedback
  function paintStatus() {
    const n = document.getElementById("st-status"); if (!n) return;
    const { T } = A();
    n.className = "st-status" + (Q.status === "saved" ? " ok" : Q.status === "failed" ? " bad" : "");
    n.textContent = Q.status === "saved" ? T("set_saved") : Q.status === "failed" ? T("set_save_failed") : "";
  }
  const status = (st) => { Q.status = st; paintStatus(); };

  // ------------------------------------------------------------------ events
  const change = (changes) => A().setPref(changes);
  document.addEventListener("click", (e) => {
    const box = el(); if (!box || !box.contains(e.target)) return;
    const sg = e.target.closest("[data-st-seg]");
    if (sg) {
      const name = sg.dataset.stSeg, v = sg.dataset.v;
      box.querySelectorAll(`[data-st-seg="${name}"]`).forEach((b) => b.setAttribute("aria-checked", String(b === sg)));
      change({ [name]: v });
      return;
    }
    const act = e.target.closest("[data-st-act]"); if (!act) return;
    if (act.dataset.stAct === "logout") document.getElementById("logout-btn").click();
    else if (act.dataset.stAct === "reports") { A().goSection("reports"); toSchedules(); }
    else if (act.dataset.stAct === "reset") change({ preset: null, compare: null, grain: null, trend: null }).then(() => { if (!el().hidden) render(); });
  });
  document.addEventListener("change", (e) => {
    const box = el(); if (!box || !box.contains(e.target)) return;
    const s = e.target.closest("[data-st-sel]"); if (!s) return;
    const name = s.dataset.stSel, v = s.value;
    if (name === "lang") { A().changeLanguage(v, true); return; }   // reloads the page text in the new language
    change({ [name]: v === "" ? null : v });
  });

  window.VdashSettings = {
    load() { const b = el(); if (!b) return Promise.resolve(); b.hidden = false; Q.status = ""; render(); return loadSchedules(); },
    hide() { const b = el(); if (b) b.hidden = true; },
    reset() { Q.sched = null; Q.schedErr = false; Q.status = ""; ++Q.seq; },
    status,
  };
})();
