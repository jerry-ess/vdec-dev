/* VDec BI dashboard - frontend.
 * Plain JS + ECharts (vendored). Everything the page shows comes from the API; the page holds no tenant data
 * and makes no access decisions. UI text comes from /api/i18n/<lang>; KPI/catalog text arrives translated.
 */
(() => {
  "use strict";

  // ---------------------------------------------------------------- state
  const S = {
    me: null, tenants: [], tenant: null, ctx: null, section: "overview",
    preset: "last_30_days", start: null, end: null, grain: "auto", compare: "previous",
    charts: new Map(), lastData: null, reqSeq: 0, tableView: new Set(),
    lang: "en", locale: "en-US", ui: {}, languages: {}, trendMode: "ma",
    warn: null, warnReq: null, evalOpen: { positive: false, negative: false }, warnOpen: false,
    prefs: {}, prefsOk: null,   // v51: the person's own settings (server copy when it can be read) and whether saving works
  };
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const T = (key, args) => { let s = S.ui[key] ?? key; if (args) for (const [k, v] of Object.entries(args)) s = s.split(`{${k}}`).join(v); return s; };
  const store = { get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }, set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* ignore */ } } };

  const ICONS = {
    home: "M3 11l9-7 9 7v9a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z",
    trending: "M3 17l6-6 4 4 8-8M15 7h6v6",
    box: "M21 8l-9-5-9 5 9 5 9-5zM3 8v8l9 5 9-5V8M12 13v8",
    users: "M16 19v-1a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v1M9 10a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM22 19v-1a4 4 0 0 0-3-3.9M16 4.1a3 3 0 0 1 0 5.8",
    store: "M3 9l1.5-5h15L21 9M3 9h18v2a3 3 0 0 1-6 0 3 3 0 0 1-6 0 3 3 0 0 1-6 0zM5 13v7h14v-7",
    megaphone: "M3 11v2a1 1 0 0 0 1 1h2l5 4V6L6 10H4a1 1 0 0 0-1 1zM15 9a3 3 0 0 1 0 6M18 6a7 7 0 0 1 0 12",
    truck: "M3 6h11v9H3zM14 9h4l3 3v3h-7M7 18a2 2 0 1 0 0-.01M17 18a2 2 0 1 0 0-.01",
    layers: "M12 3l9 5-9 5-9-5 9-5zM3 13l9 5 9-5M3 17l9 5 9-5",
    wallet: "M3 7a2 2 0 0 1 2-2h13v4M3 7v11a2 2 0 0 0 2 2h15V9H5a2 2 0 0 1-2-2zM16 14h.01",
    badge: "M12 3a4 4 0 1 1 0 8 4 4 0 0 1 0-8zM6 21v-2a6 6 0 0 1 12 0v2",
    star: "M12 3l2.8 5.7 6.2.9-4.5 4.4 1.1 6.2L12 17.3 6.4 20.2l1.1-6.2L3 9.6l6.2-.9z",
    globe: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zM3 12h18M12 3c2.5 2.7 3.8 5.7 3.8 9s-1.3 6.3-3.8 9c-2.5-2.7-3.8-5.7-3.8-9S9.5 5.7 12 3z",
    spark: "M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8zM19 16l.8 2.2L22 19l-2.2.8L19 22l-.8-2.2L16 19l2.2-.8z",
    logout: "M15 17l5-5-5-5M20 12H9M11 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h6",
    settings: "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.9.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.9 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.9l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.9.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.9-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.9V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z",   // v51
    chat: "M21 11.5a8.5 8.5 0 0 1-12.3 7.6L3 21l1.9-5.7A8.5 8.5 0 1 1 21 11.5zM8 10h8M8 14h5",   // v51: Talk to Data
    external: "M14 4h6v6M10 14L20 4M20 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1h5",   // v51: opens elsewhere
    moon: "M20 14.5A8 8 0 0 1 9.5 4 8 8 0 1 0 20 14.5z",
    sun: "M12 7a5 5 0 1 0 0 10 5 5 0 0 0 0-10zM12 1v2M12 21v2M4.2 4.2l1.4 1.4M18.4 18.4l1.4 1.4M1 12h2M21 12h2M4.2 19.8l1.4-1.4M18.4 5.6l1.4-1.4",
    table: "M3 5h18v14H3zM3 10h18M3 15h18M9 5v14",
    chart: "M4 20V10M10 20V4M16 20v-8M22 20H2",
    info: "M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20zM12 16v-4M12 8h.01",
    download: "M12 3v12M7 10l5 5 5-5M4 21h16",
    list: "M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01",
    image: "M3 5h18v14H3zM3 16l5-5 4 4 3-3 6 6M15.5 9.5a1.5 1.5 0 1 0 0-.01",
    menu: "M4 6h16M4 12h16M4 18h16",
    close: "M6 6l12 12M18 6L6 18",
    sliders: "M4 6h10M18 6h2M4 12h4M12 12h8M4 18h12M20 18h0M14 4v4M8 10v4M16 16v4",
    install: "M12 3v10M8 9l4 4 4-4M5 15v3a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-3",
    bell: "M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9M10.3 21a1.94 1.94 0 0 0 3.4 0",
    alert: "M10.3 3.9L1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0zM12 9v4M12 17h.01",
    up: "M12 19V5M5 12l7-7 7 7",
    down: "M12 5v14M19 12l-7 7-7-7",
    chevron: "M9 6l6 6-6 6",
  };
  ICONS.report = "M7 3h7l5 5v13H7zM14 3v5h5M10 13h6M10 17h6"; // v30: Reports
  const icon = (name) => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${ICONS[name] || ICONS.chart}"/></svg>`;

  // ---------------------------------------------------------------- api
  class OfflineError extends Error {}
  // v20: after a quiet period the server may be starting up (or still preparing a tenant's data). A note appears
  // when a request takes longer than WAIT_NOTE_MS, and reads (GET) are retried while the server answers
  // "preparing" (503) or the host's start-up page (502/503/504, or a non-JSON reply), for up to WAIT_MAX_MS.
  const WAIT_NOTE_MS = 4000, WAIT_MAX_MS = 180000;
  const waiting = new Map(); let waitSeq = 0, waitTimer = null;
  const WAIT_FALLBACK = {
    preparing_data: "Preparing the data for this business…",
    still_calculating: "Still calculating these figures…",
    server_waking: "Connecting to the server… After a quiet period this can take up to a minute.",
  };
  const waitText = (k) => (S.ui && S.ui[k]) || WAIT_FALLBACK[k];
  // v21: a request can be slow because the server is starting up OR because it is still calculating. While a
  // request waits, a tiny /api/health check (every 5 s, 3 s timeout) tells which: it answers -> "still
  // calculating"; no answer -> "connecting to the server". The note waits for the first check's result.
  const probe = { state: "unknown", at: 0, busy: false };
  async function probeServer() {
    if (probe.busy || Date.now() - probe.at < 5000) return;
    probe.busy = true;
    const ctl = new AbortController(), timer = setTimeout(() => ctl.abort(), 3000);
    let up = false;
    try {
      const r = await fetch("/api/health", { cache: "no-store", credentials: "same-origin", signal: ctl.signal });
      up = r.ok && (r.headers.get("content-type") || "").includes("json") && !!(await r.json()).ok;
    } catch (e) { up = false; }
    clearTimeout(timer);
    probe.state = up ? "up" : "down"; probe.at = Date.now(); probe.busy = false;
    renderWait();
  }
  function renderWait() {
    const el = $("wait-toast"); if (!el) return;
    const now = Date.now(), all = [...waiting.values()];
    const preparing = all.some((w) => w.kind === "preparing_data");
    const longWait = all.some((w) => now - w.t0 >= WAIT_NOTE_MS);
    const retrying = all.some((w) => w.retrying);            // the server already failed to answer this request
    if (longWait && !preparing && !retrying) probeServer();
    let kind = null;
    if (preparing) kind = "preparing_data";
    else if (longWait && retrying) kind = "server_waking";
    else if (longWait && probe.at >= Math.min(...all.map((w) => w.t0)) + WAIT_NOTE_MS - 100) kind = probe.state === "up" ? "still_calculating" : "server_waking";
    el.hidden = !kind;
    if (kind) el.textContent = waitText(kind);
    if (!waiting.size) { probe.state = "unknown"; probe.at = 0; }
    if (waiting.size && !waitTimer) waitTimer = setInterval(renderWait, 1000);
    if (!waiting.size && waitTimer) { clearInterval(waitTimer); waitTimer = null; }
  }
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  async function api(path, opts = {}) {
    const id = ++waitSeq, t0 = Date.now(), isGet = !opts.method || opts.method === "GET";
    waiting.set(id, { t0, kind: "server_waking" }); renderWait();
    try {
      for (;;) {
        let res;
        try {
          res = await fetch(path, { credentials: "same-origin", headers: { "Content-Type": "application/json" }, ...opts });
        } catch (e) {
          // the device is online but the server does not answer (yet): it is starting up - keep trying (v20)
          if (isGet && navigator.onLine !== false && Date.now() - t0 < WAIT_MAX_MS) {
            waiting.set(id, { t0, kind: "server_waking", retrying: true }); renderWait();
            await sleep(3000); continue;
          }
          setOffline(true); throw new OfflineError(T("offline_msg")); // network down (fetch rejects)
        }
        setOffline(false);
        if (res.status === 401 && !path.endsWith("/login")) { showLogin(); throw new Error("signed out"); }
        const isJson = (res.headers.get("content-type") || "").includes("json");
        const body = isJson ? await res.json().catch(() => ({})) : {};
        const preparing = res.status === 503 && body.error === "preparing";
        const hostWaking = [502, 503, 504].includes(res.status) && !preparing && !isJson;
        if (isGet && (preparing || hostWaking || (res.ok && !isJson && path.startsWith("/api/"))) && Date.now() - t0 < WAIT_MAX_MS) {
          waiting.set(id, { t0, kind: preparing ? "preparing_data" : "server_waking", retrying: !preparing }); renderWait();
          await sleep(Math.max(1000, 1000 * Number(res.headers.get("Retry-After") || 3)));
          continue;
        }
        if (!res.ok) throw new Error(preparing ? waitText("preparing_data") : (body.error || `HTTP ${res.status}`));
        return body;
      }
    } finally { waiting.delete(id); renderWait(); }
  }
  const tenantUrl = (path, params = {}) => `/api/tenants/${encodeURIComponent(S.tenant)}${path}?${new URLSearchParams({ lang: S.lang, ...params })}`;

  // ---------------------------------------------------------------- i18n
  async function loadLanguage(lang) {
    const b = await api(`/api/i18n/${encodeURIComponent(lang)}`);
    S.lang = b.lang; S.locale = b.locale; S.ui = b.ui; S.languages = b.languages;
    document.documentElement.lang = S.lang === "zh" ? "zh-CN" : S.lang;
    store.set("vdash-lang", S.lang);
    document.querySelectorAll("[data-i18n]").forEach((el) => { el.textContent = T(el.dataset.i18n); });
    document.querySelectorAll("[data-i18n-title]").forEach((el) => { el.title = T(el.dataset.i18nTitle); el.setAttribute("aria-label", el.title); });
    const opts = Object.entries(S.languages).map(([k, v]) => `<option value="${esc(k)}" ${k === S.lang ? "selected" : ""}>${esc(v)}</option>`).join("");
    $("login-lang").innerHTML = opts;   // v51: the dashboard's own language choice lives in Settings
  }
  // v51: called from the login page (not saved) and from Settings (saved to the person's account)
  async function changeLanguage(lang, save) {
    await loadLanguage(lang);
    if (save) savePrefs({ lang: S.lang });
    if (!$("app-view").hidden && S.tenant) await loadTenant(true);
  }
  $("login-lang").addEventListener("change", (e) => changeLanguage(e.target.value, false));

  // ---------------------------------------------------------------- formatting
  const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
  const cur = () => (S.ctx && S.ctx.tenant.currency) || "USD";
  // v24: number formatting and chart options live in chart_options.js (shared with server-side report rendering)
  const CH = VdashCharts.create({ css, locale: () => S.locale, currency: cur, T, trendMode: () => S.trendMode, scope: () => S.tenant });
  const { fmt, fmtDate, fmtBucket, fmtArg, buildOption, phoneHeat } = CH;

  const ago = (ts) => { if (!ts) return T("never"); const m = Math.round((Date.now() / 1000 - ts) / 60); return m < 1 ? T("just_now") : m < 60 ? T("min_ago", { n: m }) : T("h_ago", { n: Math.round(m / 60) }); };

  // ---------------------------------------------------------------- theme
  function currentTheme() {
    const t = document.documentElement.getAttribute("data-theme");
    if (t) return t;
    return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }
  function setTheme(t) {   // "light" | "dark" | "system" (v51: system = follow the device)
    if (t === "system") document.documentElement.removeAttribute("data-theme");
    else document.documentElement.setAttribute("data-theme", t);
    store.set("vdash-theme", t);
    $("theme-btn").innerHTML = icon(currentTheme() === "dark" ? "sun" : "moon");
    if (S.lastData && !isPage(S.section)) renderSection(S.lastData, false);
  }

  // ---------------------------------------------------------------- personal settings (v51)
  // The person's own choices (language, theme, how the dashboard opens, trend line) are kept in their account on
  // the server so they follow them to other devices; this device also remembers them, so a slow or unreachable
  // database never blocks the page. The server copy wins when it has one.
  const PREF_KEYS = ["lang", "theme", "preset", "compare", "grain", "trend"];
  const localPrefs = () => {
    let o = {};
    try { o = JSON.parse(store.get("vdash-prefs") || "{}") || {}; } catch (e) { o = {}; }
    const th = store.get("vdash-theme"), tr = store.get("vdash-trend");   // set by earlier versions / the quick toggles
    if (["light", "dark"].includes(th)) o.theme = th;
    if (["ma", "linear", "off"].includes(tr)) o.trend = tr;
    return o;
  };
  async function loadPrefs() {
    const local = localPrefs();
    let server = null;
    try { server = await Promise.race([api("/api/me/settings"), sleep(2500).then(() => null)]); } catch (e) { server = null; }
    S.prefsOk = server ? !!server.available : null;
    const saved = server && server.available ? server.prefs || {} : null;
    S.prefs = saved && Object.keys(saved).length ? saved : local;
    if (saved && !Object.keys(saved).length && (local.theme || local.trend || local.preset || local.compare || local.grain)) {
      const { lang, ...carry } = local;   // first time on a new device: bring up what this device already had (never the guessed language)
      savePrefs(carry, true);
    }
    if (S.prefs.theme && S.prefs.theme !== store.get("vdash-theme")) setTheme(S.prefs.theme);
    if (S.prefs.trend) S.trendMode = S.prefs.trend;
    if (S.prefs.lang && S.prefs.lang !== S.lang) await loadLanguage(S.prefs.lang).catch(() => {});
  }
  const mirrorPrefs = () => store.set("vdash-prefs", JSON.stringify(Object.fromEntries(["preset", "compare", "grain"].filter((k) => S.prefs[k]).map((k) => [k, S.prefs[k]]))));
  const setPrefStatus = (st) => { if (window.VdashSettings) window.VdashSettings.status(st); };
  let saveSeq = 0;
  // changes: {key: value | null}; null puts the setting back to standard. Returns true when the account copy was saved.
  function savePrefs(changes, quiet) {
    for (const [k, v] of Object.entries(changes)) { if (v === null) delete S.prefs[k]; else S.prefs[k] = v; }
    mirrorPrefs();
    const seq = ++saveSeq;
    if (!quiet) setPrefStatus("saving");
    return api("/api/me/settings", { method: "PUT", body: JSON.stringify(changes) })
      .then(() => { S.prefsOk = true; if (!quiet && seq === saveSeq) setPrefStatus("saved"); return true; })
      .catch((e) => { if (e.message === "signed out") return false; S.prefsOk = false; if (seq === saveSeq) setPrefStatus("failed"); return false; });
  }
  // the Settings page and the quick toggles: apply the change now, then save it
  function setPref(changes) {
    for (const [k, v] of Object.entries(changes)) {
      if (k === "theme") setTheme(v === null ? "system" : v);
      if (k === "trend") { S.trendMode = v || "ma"; store.set("vdash-trend", S.trendMode); if (v === null) store.set("vdash-trend", ""); }
    }
    return savePrefs(changes);
  }
  const defaultPreset = (c) => (S.prefs.preset && c.presets.some((p) => p.id === S.prefs.preset)) ? S.prefs.preset : "last_30_days";

  // ---------------------------------------------------------------- auth views
  function showLogin() {
    $("app-view").hidden = true; $("login-view").hidden = false; $("login-user").focus();
  }
  $("login-form").addEventListener("submit", async (e) => {
    e.preventDefault(); $("login-error").textContent = "";
    try {
      await api("/api/login", { method: "POST", body: JSON.stringify({ username: $("login-user").value, password: $("login-pass").value }) });
      $("login-pass").value = ""; await boot();
    } catch (err) { $("login-error").textContent = err.message; }
  });
  $("logout-btn").addEventListener("click", async () => { await api("/api/logout", { method: "POST" }).catch(() => {}); history.replaceState(null, "", "/"); showLogin(); });

  // ---------------------------------------------------------------- URL state (deep links)
  function readHash() {
    const p = new URLSearchParams(location.hash.slice(1));
    return { t: p.get("t"), s: p.get("s"), p: p.get("p"), from: p.get("from"), to: p.get("to"), g: p.get("g"), c: p.get("c") };
  }
  function writeHash() {
    const p = new URLSearchParams({ t: S.tenant, s: S.section, g: S.grain, c: S.compare });
    if (S.preset === "custom") { p.set("from", S.start); p.set("to", S.end); } else p.set("p", S.preset);
    history.replaceState(null, "", "#" + p.toString());
  }

  // ---------------------------------------------------------------- boot
  async function boot() {
    let me;
    try { me = await api("/api/me"); } catch (e) {
      showLogin();
      if (e instanceof OfflineError) { $("login-error").textContent = T("offline_login"); window.addEventListener("online", boot, { once: true }); }
      return;
    }
    S.me = me.user; S.tenants = me.tenants;
    $("login-view").hidden = true; $("app-view").hidden = false;
    $("user-name").textContent = me.user.display_name;
    $("logout-btn").innerHTML = icon("logout");
    const sel = $("tenant-select");
    sel.innerHTML = S.tenants.map((t) => `<option value="${esc(t.id)}">${esc(t.display_name)}</option>`).join("");
    sel.disabled = S.tenants.length < 2;
    if (!S.tenants.length) { $("status-banner").hidden = false; $("status-banner").textContent = T("banner_no_access"); return; }
    await loadPrefs();   // v51: language, theme and how the dashboard opens, from the person's account
    const h = readHash();
    if (S.prefs.grain) S.grain = S.prefs.grain;
    if (S.prefs.compare) S.compare = S.prefs.compare;
    if (S.prefs.preset) S.preset = S.prefs.preset;     // a link's own period (below) still wins
    S.tenant = S.tenants.some((t) => t.id === h.t) ? h.t : S.tenants[0].id;
    if (h.s) S.section = h.s;
    if (h.g) S.grain = h.g;
    if (h.c) S.compare = h.c;
    if (h.from && h.to) { S.preset = "custom"; S.start = h.from; S.end = h.to; } else if (h.p) S.preset = h.p;
    sel.value = S.tenant;
    await loadTenant(true);
  }

  async function loadTenant(keepPeriod) {
    disposeCharts();
    if (window.VdashReports) window.VdashReports.reset();
    if (window.VdashSettings) window.VdashSettings.reset();
    if (window.VdashCredits) window.VdashCredits.reset();
    S.cr = null;
    $("tiles").innerHTML = ""; $("charts").innerHTML = ""; $("hidden-panel").hidden = true; clearPanels();
    $("user-role").textContent = T("role_" + S.me.role);
    S.warn = null; S.warnReq = null; renderBell();
    S.ctx = await api(tenantUrl("/context"));
    if (window.VdashReports && window.VdashReports.watchTenant) window.VdashReports.watchTenant();   // v41.1
    const c = S.ctx;
    $("tenant-line").textContent = c.tenant.industry ? `${c.tenant.display_name} · ${c.tenant.industry}` : c.tenant.display_name;
    renderFreshness();
    if (!c.data.max_date) {
      $("section-nav").innerHTML = ""; $("section-title").textContent = T("no_data_title");
      $("section-desc").textContent = "";
      $("status-banner").hidden = false;
      $("status-banner").textContent = c.data.status === "error" ? T("banner_error") : T("banner_no_sales");
      return;
    }
    $("status-banner").hidden = true;
    if (!keepPeriod || !(S.preset === "custom" || c.presets.some((p) => p.id === S.preset))) S.preset = defaultPreset(c);
    applyPreset();
    if (!validSection(S.section)) S.section = c.sections.length ? c.sections[0].id : null;
    renderNav(); renderPresetList(); syncControls(); refreshCredits();
    if (c.panels && c.panels.warnings) fetchWarnings(); // not awaited: the bell and the Overview strip fill in when ready
    await loadSection();
  }

  function renderFreshness() {
    const d = S.ctx.data;
    $("freshness").innerHTML = d.max_date
      ? `<span>${esc(T("data_through", { date: "\u0000" })).replace("\u0000", `<b>${esc(fmtDate(d.max_date))}</b>`)}</span><span>${esc(T("synced", { ago: ago(d.refreshed_at) }))}</span>`
      : `<span>${esc(T("no_data_loaded"))}</span>`;
  }

  function applyPreset() {
    if (S.preset === "custom") return;
    const p = S.ctx.presets.find((x) => x.id === S.preset) || S.ctx.presets[1];
    S.preset = p.id; S.start = p.start; S.end = p.end;
  }

  // v51: screens that are not dashboard sections. Reports and Talk to Data sit in the "Tools" block of the menu;
  // Settings has its own entry at the bottom of the sidebar.
  const PAGES = new Set(["reports", "settings", "talk", "credits"]);   // NB: "ai" is the AI Insights section - never reuse that id
  const isPage = (id) => PAGES.has(id);
  const talkFeature = () => (S.ctx && S.ctx.features && S.ctx.features.talk_to_data) || { enabled: false, url: null };
  const toolPages = () => {
    const out = [];
    // v30: the report builder is one more entry of the navigation (only when the business has data)
    if (S.ctx.data && S.ctx.data.max_date) out.push({ id: "reports", title: T("nav_reports"), desc: T("nav_reports_desc"), icon: "report", kpis: [], panel: null, report: true, group: "tools" });
    // v51: the access point of Talk to Data - a separate application on the same data; the entry opens it (or says it is coming)
    if (talkFeature().enabled) out.push({ id: "talk", title: T("nav_talk"), desc: T("nav_talk_desc"), icon: "chat", kpis: [], panel: null, page: true, external: !!talkFeature().url, group: "tools" });
    return out;
  };
  const navSections = () => S.ctx.sections.concat(toolPages());
  const validSection = (id) => S.ctx.sections.some((s) => s.id === id) || id === "settings" || id === "credits" || toolPages().some((p) => p.id === id);
  // the menu in blocks: the catalog's groups in order, then Tools; a catalog without groups stays one plain list
  function navBlocks() {
    const groups = S.ctx.groups || [], known = new Set(groups.map((g) => g.id)), all = navSections();
    const blocks = groups.map((g) => ({ id: g.id, title: g.title, items: all.filter((s) => s.group === g.id) })).filter((b) => b.items.length);
    const loose = all.filter((s) => s.group !== "tools" && !known.has(s.group));
    if (loose.length) blocks.unshift({ id: "other", title: null, items: loose });
    const tools = all.filter((s) => s.group === "tools");
    if (tools.length) blocks.push({ id: "tools", title: blocks.length ? T("nav_group_tools") : null, items: tools });
    return blocks;
  }
  function navItemHtml(s) {
    const wn = s.panel === "warnings" ? warnCount() : 0;
    const badge = wn ? `<span class="warn-badge${S.warn.counts.urgent ? " urgent" : ""}" title="${esc(T("warn_strip", { n: wn }))}">${wn}</span>` : (s.report || s.page ? "" : `<span class="count">${s.kpis.length}</span>`);
    const ext = s.external ? `<span class="nav-ext" title="${esc(T("talk_new_tab"))}" aria-hidden="true">${icon("external")}</span>` : "";
    return `<button class="nav-item" data-sec="${esc(s.id)}" ${s.id === S.section ? 'aria-current="page"' : ""}>${icon(s.icon)}<span>${esc(s.title)}</span>${badge}${ext}</button>`;
  }
  // v84: the remaining AI credits as a small bar in the menu - green / yellow / red, refreshed while the page is open
  const creditsShown = () => !!(S.cr && S.cr.mode === "enforce" && !S.cr.unlimited && !S.cr.exempt && S.cr.level && S.cr.level !== "none");
  function creditsChip() {
    if (!creditsShown()) return "";
    const c = S.cr, tip = T("ai_chip_tip", { n: Number(c.available).toLocaleString(S.lang || "en"), p: c.percent });
    return `<span class="cr-mini lv-${esc(c.level)}" role="img" title="${esc(tip)}" aria-label="${esc(tip)}"><i style="width:${Math.max(0, Math.min(100, c.percent))}%"></i></span>`;
  }
  function setCredits(c) {
    const was = JSON.stringify(S.cr || null);
    S.cr = c || null;
    if (JSON.stringify(S.cr) !== was && S.ctx) renderUserbar();
  }
  // v85: account menu at the top right - name, language, AI credits (with the traffic-light bar), settings, sign out
  let userbarLang = null;
  function renderUserbar() {
    if (!S.me || !S.ctx) return;
    const name = S.me.display_name || S.me.username, role = T("role_" + S.me.role), biz = S.ctx.tenant.display_name;
    document.querySelectorAll(".avatar").forEach((a) => { a.textContent = (name.trim()[0] || "?").toUpperCase(); });
    $("user-btn-name").textContent = name; $("user-btn-role").textContent = role;
    $("up-name").textContent = name; $("up-role").textContent = role; $("up-biz").textContent = biz;
    if (userbarLang !== S.lang) {                  // the language lists are rebuilt only when the language changes (an open list must not close)
      userbarLang = S.lang;
      const opts = Object.entries(S.languages || {}).map(([k, v]) => `<option value="${esc(k)}" ${k === S.lang ? "selected" : ""}>${esc(v)}</option>`).join("");
      $("top-lang").innerHTML = opts; $("pop-lang").innerHTML = opts;
    }
    const on = creditsShown(), tip = on ? T("ai_chip_tip", { n: Number(S.cr.available).toLocaleString(S.lang || "en"), p: S.cr.percent }) : "";
    document.querySelectorAll(".user-btn .cr-dot, .avatar-btn .cr-dot").forEach((d) => {
      d.hidden = !on; d.className = "cr-dot" + (on ? ` lv-${S.cr.level}` : ""); d.title = tip;
    });
    $("up-credits-bar").innerHTML = on ? `<span class="up-num">${esc(Number(S.cr.available).toLocaleString(S.lang || "en"))}</span>${creditsChip()}` : "";
  }
  function userPop(open, anchor) {
    const pop = $("user-pop");
    document.querySelectorAll("#user-btn, #user-btn-m").forEach((b) => b.setAttribute("aria-expanded", String(!!open)));
    if (!open) { pop.hidden = true; return; }
    renderUserbar();
    pop.hidden = false;
    const r = anchor.getBoundingClientRect(), w = pop.offsetWidth;
    pop.style.top = `${Math.round(r.bottom + 6)}px`;
    pop.style.left = `${Math.round(Math.max(8, Math.min(window.innerWidth - w - 8, r.right - w)))}px`;
  }
  const onUserBtn = (e) => { const b = e.currentTarget; userPop($("user-pop").hidden, b); e.stopPropagation(); };
  $("user-btn").addEventListener("click", onUserBtn);
  $("user-btn-m").addEventListener("click", onUserBtn);
  document.addEventListener("click", (e) => { if (!$("user-pop").hidden && !e.target.closest("#user-pop")) userPop(false); });
  window.addEventListener("resize", () => userPop(false));
  const onLang = (e) => { userPop(false); changeLanguage(e.target.value, true); };
  $("top-lang").addEventListener("change", onLang);
  $("pop-lang").addEventListener("change", onLang);
  $("up-credits").addEventListener("click", () => { userPop(false); setPanel(null); goSection("credits"); });
  $("up-settings").addEventListener("click", () => { userPop(false); setPanel(null); goSection("settings"); });
  $("up-logout").addEventListener("click", () => { userPop(false); $("logout-btn").click(); });
  async function refreshCredits() {
    if (!S.ctx || !S.tenant || document.hidden) return;
    const t = S.tenant;
    try { const d = await api(tenantUrl("/ai-credits/summary")); if (t === S.tenant) setCredits(d); } catch (e) { /* the bar is optional */ }
  }
  function tabHtml(s) {
    const wn = s.panel === "warnings" ? warnCount() : 0;
    return `<button data-sec="${esc(s.id)}" ${s.id === S.section ? 'aria-current="page"' : ""}>${esc(s.title)}${wn ? ` <span class="warn-badge${S.warn.counts.urgent ? " urgent" : ""}">${wn}</span>` : ""}</button>`;
  }
  function renderNav() {
    const blocks = navBlocks();
    $("section-nav").innerHTML = blocks.map((b) => `<div class="nav-block" role="group"${b.title ? ` aria-label="${esc(b.title)}"` : ""}>${b.title ? `<div class="nav-block-title eyebrow">${esc(b.title)}</div>` : ""}${b.items.map(navItemHtml).join("")}</div>`).join("");
    $("tabstrip").innerHTML = blocks.map((b, i) => (i ? '<span class="tab-sep" aria-hidden="true"></span>' : "") + b.items.map(tabHtml).join("")).join("");
    const cur = $("tabstrip").querySelector('[aria-current="page"]');
    if (cur) cur.scrollIntoView({ block: "nearest", inline: "center" });
    const sn = $("settings-nav");
    if (S.section === "settings") sn.setAttribute("aria-current", "page"); else sn.removeAttribute("aria-current");
    const sec = navSections().find((x) => x.id === S.section);
    $("appbar-tenant").textContent = S.ctx.tenant.display_name;
    $("appbar-section").textContent = sec ? sec.title : S.section === "settings" ? T("nav_settings") : S.section === "credits" ? T("nav_ai") : "";
    renderUserbar();
  }
  const onSection = (e) => {
    const b = e.target.closest("[data-sec]"); if (!b) return;
    setPanel(null);
    if (b.dataset.sec === "talk" && talkFeature().url) {   // v51: a separate application: open it beside this page
      window.open(talkFeature().url, "_blank", "noopener");
      return;
    }
    goSection(b.dataset.sec);
  };
  function goSection(sid, then) {
    S.section = sid; renderNav(); disposeCharts(); $("tiles").innerHTML = ""; $("charts").innerHTML = ""; clearPanels();
    writeHash(); // v9: the deep link now follows section changes too
    window.scrollTo({ top: 0 });
    return loadSection().then(() => { if (then) then(); });
  }
  $("section-nav").addEventListener("click", onSection);
  $("tabstrip").addEventListener("click", onSection);
  $("settings-nav").addEventListener("click", onSection);
  $("tenant-select").addEventListener("change", (e) => { S.tenant = e.target.value; loadTenant(false); });

  // ---------------------------------------------------------------- filters
  function renderPresetList() {
    $("preset-list").innerHTML = S.ctx.presets.map((p) => `<li role="option" data-p="${esc(p.id)}" aria-selected="${p.id === S.preset}"><span>${esc(T("preset_" + p.id))}<br><small>${esc(fmtDate(p.start))} – ${esc(fmtDate(p.end))}</small></span></li>`).join("");
    $("custom-start").min = $("custom-end").min = S.ctx.data.min_date;
    $("custom-start").max = $("custom-end").max = S.ctx.data.max_date;
  }
  function syncControls() {
    const p = S.ctx.presets.find((x) => x.id === S.preset);
    $("period-text").textContent = p ? `${T("preset_" + p.id)} · ${fmtDate(S.start)} – ${fmtDate(S.end)}` : `${fmtDate(S.start)} – ${fmtDate(S.end)}`;
    $("filter-btn-text").textContent = p ? T("preset_" + p.id) : `${fmtDate(S.start)} – ${fmtDate(S.end)}`;
    $("custom-start").value = S.start; $("custom-end").value = S.end;
    $("compare-select").value = S.compare;
    document.querySelectorAll("#grain-group button").forEach((b) => b.setAttribute("aria-checked", String(b.dataset.grain === S.grain)));
    writeHash();
  }
  const pop = $("period-pop");
  $("period-btn").addEventListener("click", (e) => { e.stopPropagation(); pop.hidden = !pop.hidden; $("period-btn").setAttribute("aria-expanded", String(!pop.hidden)); });
  document.addEventListener("click", (e) => {
    if (!pop.hidden && !pop.contains(e.target)) { pop.hidden = true; $("period-btn").setAttribute("aria-expanded", "false"); }
    if (!e.target.closest(".dl-wrap")) closeMenus();
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") { if (closeDrill()) return; userPop(false); closeMenus(); closeDialog(); setPanel(null); } });
  $("preset-list").addEventListener("click", (e) => {
    const li = e.target.closest("[data-p]"); if (!li) return;
    S.preset = li.dataset.p; applyPreset(); renderPresetList(); pop.hidden = true; syncControls(); loadSection(); setPanel(null);
  });
  $("custom-apply").addEventListener("click", () => {
    const a = $("custom-start").value, b = $("custom-end").value;
    if (!a || !b || a > b) { $("custom-start").focus(); return; }
    S.preset = "custom"; S.start = a; S.end = b; renderPresetList(); pop.hidden = true; syncControls(); loadSection(); setPanel(null);
  });
  $("compare-select").addEventListener("change", (e) => { S.compare = e.target.value; syncControls(); loadSection(); });
  $("grain-group").addEventListener("click", (e) => { const b = e.target.closest("[data-grain]"); if (!b) return; S.grain = b.dataset.grain; syncControls(); loadSection(); });
  $("theme-btn").addEventListener("click", () => setPref({ theme: currentTheme() === "dark" ? "light" : "dark" }));
  window.addEventListener("resize", () => S.charts.forEach((c) => c.resize()));
  // pinned page header gets a shadow and drops the description once the page scrolls
  let scrolledState = false;
  window.addEventListener("scroll", () => {
    const v = window.scrollY > 8;
    if (v !== scrolledState) { scrolledState = v; document.body.classList.toggle("scrolled", v); }
  }, { passive: true });

  // ---------------------------------------------------------------- section load
  async function loadSection() {
    if (!S.section) return;
    pageMode(S.section);
    if (S.section === "reports") return showReports();
    if (S.section === "settings") return showSettings();
    if (S.section === "talk") return showTalk();
    if (S.section === "credits") return showAi();
    const sec = S.ctx.sections.find((s) => s.id === S.section);
    $("section-title").textContent = sec.title; $("section-desc").textContent = sec.desc;
    document.title = `${sec.title} · ${S.ctx.tenant.display_name} · VDec BI`;
    const seq = ++S.reqSeq;
    document.querySelectorAll(".tile, .card").forEach((el) => el.classList.add("stale")); // hold previous render
    try {
      const per = { start: S.start, end: S.end, grain: S.grain, compare: S.compare };
      const evalP = sec.panel === "evaluation"
        ? api(tenantUrl("/evaluation", per)).catch((e) => ({ error: e.message })) : Promise.resolve(null);
      const warnP = sec.panel === "warnings" ? fetchWarnings() : Promise.resolve(null);
      const scP = api(tenantUrl("/scorecard", per)).catch(() => null);   // v69: the story; the page still works without it
      const [data, evalData, , sc] = await Promise.all([api(tenantUrl(`/sections/${encodeURIComponent(S.section)}`, per)), evalP, warnP, scP]);
      if (seq !== S.reqSeq) return; // a newer request superseded this one
      S.lastData = { sec, data, evalData, sc };
      renderSection(S.lastData, true);
    } catch (e) {
      if (seq !== S.reqSeq) return;
      $("status-banner").hidden = false; $("status-banner").textContent = T("load_failed", { msg: e.message });
    }
  }

  // v51: page-type screens hide the dashboard's own controls (period, comparison, charts) and the other pages
  function pageMode(id) {
    document.body.classList.toggle("in-reports", id === "reports");
    document.body.classList.toggle("in-settings", id === "settings" || id === "talk" || id === "credits");
    if (id !== "credits" && window.VdashCredits) window.VdashCredits.hide();
    if (id !== "reports" && window.VdashReports) window.VdashReports.hide();
    if (id !== "settings" && window.VdashSettings) window.VdashSettings.hide();
    $("talk-page").hidden = id !== "talk";
  }
  function pageShell(titleKey, descKey) {
    disposeCharts(); clearPanels();
    ++S.reqSeq;          // a slower answer for the section we just left must not paint over this page
    S.lastData = null;   // nothing from the previous section may be drawn again into a page screen (e.g. warnings arriving late)
    $("section-title").textContent = T(titleKey); $("section-desc").textContent = T(descKey);
    document.title = `${T(titleKey)} · VDec BI`;
    $("tiles").innerHTML = ""; $("charts").innerHTML = ""; $("page-foot").textContent = ""; $("hidden-panel").hidden = true;
    $("status-banner").hidden = true;
  }
  function showSettings() {
    pageShell("nav_settings", "nav_settings_desc");
    return window.VdashSettings ? window.VdashSettings.load() : Promise.resolve();
  }
  function showAi() {   // v83: web/credits.js
    pageShell("nav_ai", "nav_ai_desc");
    return window.VdashCredits ? window.VdashCredits.load() : Promise.resolve();
  }
  function showTalk() {
    pageShell("nav_talk", "nav_talk_desc");
    const url = talkFeature().url;
    $("talk-page").innerHTML = `<div class="card st-card st-talk"><div class="st-talk-ico" aria-hidden="true">${icon("chat")}</div>
      <h2>${esc(url ? T("nav_talk") : T("talk_soon_title"))}</h2>
      <p class="muted">${esc(url ? T("nav_talk_desc") : T("talk_soon_text"))}</p>
      ${url ? `<a class="btn btn-primary" href="${esc(url)}" target="_blank" rel="noopener">${esc(T("talk_open"))}</a><p class="muted st-note">${esc(T("talk_new_tab"))}</p>` : ""}</div>`;
  }

  function showReports() { // v30: report builder (web/report.js) in place of tiles and charts
    disposeCharts(); clearPanels();
    $("section-title").textContent = T("nav_reports"); $("section-desc").textContent = T("nav_reports_desc");
    document.title = `${T("nav_reports")} · ${S.ctx.tenant.display_name} · VDec BI`;
    $("tiles").innerHTML = ""; $("charts").innerHTML = ""; $("page-foot").textContent = ""; $("hidden-panel").hidden = true;
    document.body.classList.add("in-reports");
    $("status-banner").hidden = true;
    return window.VdashReports ? window.VdashReports.load() : Promise.resolve();
  }

  function compareLabel(r) {
    if (S.compare === "yoy") return T("cmp_yoy_short");
    if (!r.compare_start) return "";
    const n = Math.round((new Date(r.compare_end) - new Date(r.compare_start)) / 864e5) + 1;
    return T("cmp_prev_n", { n });
  }
  const modeBadge = (k) => k.period_mode === "snapshot" ? `<span class="badge" title="${esc(T("badge_snapshot_tip"))}">${esc(T("badge_snapshot"))}</span>`
    : k.period_mode === "monthly" ? `<span class="badge" title="${esc(T("badge_monthly_tip"))}">${esc(T("badge_monthly"))}</span>` : "";

  function renderSection({ sec, data, evalData, sc }, fresh) {
    disposeCharts();
    renderPanels(sec, evalData, sc);
    renderStory(sec, sc);
    const metrics = sec.kpis.filter((k) => k.viz === "metric");
    const charts = sec.kpis.filter((k) => k.viz !== "metric");
    if (sec.bands && sec.bands.length) {
      // v64/v75: a banded page: jump links, then per band a collapsible group (heading + summary + status dot,
      // key figures and charts). Charts of a closed group are drawn when it is opened.
      const bandIds = new Set(sec.bands.map((b) => b.id));
      const groups = sec.bands.map((b) => ({ ...b, ks: sec.kpis.filter((k) => k.band === b.id) }));
      const rest = sec.kpis.filter((k) => !bandIds.has(k.band));
      if (rest.length) groups.push({ id: "", title: "", desc: "", ks: rest });
      const ms = new Map(); const scs = sc && sc.available ? sc.sections.find((x) => x.id === sec.id) : null;
      if (scs) scs.measures.forEach((m) => ms.set(m.kpi, m));
      const prefs = bandPrefs(sec.id);
      groups.forEach((g) => {
        const st = g.ks.map((k) => ms.get(k.id)).filter(Boolean).map((m) => m.status);
        g.attn = st.filter((x) => x === "red" || x === "amber").length;
        g.status = st.includes("red") ? "red" : st.includes("amber") ? "amber" : st.length ? "green" : "none";
      });
      const anyAttn = groups.some((g) => g.id && g.attn);
      groups.forEach((g, i) => { g.open = !g.id || (g.id in prefs ? prefs[g.id] : (anyAttn ? g.attn > 0 : i === 0)); });
      S.bandDrawn = new Set();
      const navHtml = `<nav class="band-nav" id="band-nav" aria-label="${esc(T("band_nav"))}">
        <div class="bn-pills">${groups.filter((g) => g.id).map((g) => `<button type="button" class="bn-pill bd-${g.status}" data-band="${esc(g.id)}" id="bnp-${esc(g.id)}"><span class="sc-dot"></span>${esc(g.title)}</button>`).join("")}</div>
        <div class="bn-tools"><button type="button" class="bn-btn${bandSingle() ? " on" : ""}" data-bandsingle="1" aria-pressed="${bandSingle()}">${esc(T("band_single"))}</button>
          <button type="button" class="bn-btn" data-bandall="1">${esc(T("band_open_all"))}</button><button type="button" class="bn-btn" data-bandall="0">${esc(T("band_close_all"))}</button></div></nav>`;
      $("tiles").innerHTML = ""; $("tiles").hidden = true;
      $("charts").innerHTML = navHtml + groups.map((g) => bandHtml(g, data)).join("");
      updateStick(); spyBands();
      groups.forEach((g) => { if (g.open) drawBand(sec, g.id, data); });
      renderHidden(sec);
      return renderSectionFoot(data, fresh);
    }
    $("tiles").innerHTML = metrics.map((k, i) => tileHtml(k, data.results[k.id], i === 0 && sec.id === "overview")).join("");
    $("tiles").hidden = !metrics.length;
    $("charts").innerHTML = charts.map((k) => cardHtml(k, data.results[k.id])).join("");
    charts.forEach((k) => drawCard(k, data.results[k.id]));
    renderHidden(sec);
    renderSectionFoot(data, fresh);
  }

  // ---------------------------------------------------------------- v69: the story (scorecard, rule based)
  // Each sector page opens with a verdict and its key measures against default targets; the Overview shows a board of
  // all sectors plus what needs attention first. Wording = ui keys + typed args; numbers are formatted here.
  const scNum = (v, d = 1, sign = false) => new Intl.NumberFormat(S.locale, { maximumFractionDigits: d, minimumFractionDigits: 0, signDisplay: sign ? "always" : "auto" }).format(v);
  const scTarget = (m) => {
    if (m.target) return T(m.dir === "down" ? "sc_target_down" : "sc_target_up", { v: fmt(m.target.green, m.fmt) });
    const c = m.change_rule; if (!c) return "";
    return c.green >= 0 ? T(m.dir === "down" ? "sc_hold_down" : "sc_hold_up")
      : T(m.dir === "down" ? "sc_dip_down" : "sc_dip_up", { v: scNum(Math.abs(c.green)) + (m.fmt === "pct" ? " " + T("pts") : "%") });
  };
  const scDelta = (m) => {
    if (m.delta === null || m.delta === undefined) return "";
    const flat = Math.abs(m.delta) < 0.05;
    const txt = `${scNum(m.delta, 1, true)}${m.delta_fmt === "pts" ? " " + T("pts") : "%"}`;
    return `<span class="delta ${flat ? "neutral" : m.trend === "good" ? "good" : m.trend === "bad" ? "bad" : "neutral"}">${flat ? "●" : m.delta > 0 ? "▲" : "▼"} ${esc(txt)}</span>`;
  };
  const scWhy = (m) => (m.why || []).map((w) =>
    `<div class="sc-why"><span class="sc-why-t">${esc(T("sc_why", { what: w.title }))}</span> ${w.items.map((i) => `<b>${esc(i.label)}</b> ${esc(scNum(i.share, 0))}%`).join(" · ")}</div>`).join("");
  function scRow(m, withSection) {
    return `<div class="sc-row st-${m.status}" data-sckpi="${esc(m.kpi)}" data-scsec="${esc(m.section)}">
      <span class="sc-dot" title="${esc(T("sc_status_" + m.status))}"></span>
      <div class="sc-main"><div class="sc-name">${withSection ? `<span class="sc-sec">${esc(m.section_title)}</span> ` : ""}${esc(m.title)}</div>
        <div class="sc-sub">${esc(scTarget(m))}</div>${m.status === "green" ? "" : scWhy(m)}</div>
      <div class="sc-val"><div class="sc-v">${esc(fmt(m.value, m.fmt))}</div><div class="sc-d">${scDelta(m)}${m.prev !== null && m.prev !== undefined ? `<span class="sc-was">${esc(T("sc_was", { v: fmt(m.prev, m.fmt) }))}</span>` : ""}</div></div>
    </div>`;
  }
  function renderStory(sec, sc) {
    const el = $("story");
    if (!sc || !sc.available || !sc.sections.length) { el.innerHTML = ""; el.hidden = true; return; }
    let h = "";
    if (sec.id === "overview") { el.innerHTML = ""; el.hidden = true; return; }
    {
      const s = sc.sections.find((x) => x.id === sec.id);
      if (s) h = `<div class="story st-${s.status}"><header class="st-head"><span class="sc-dot big"></span><div><h2>${esc(T("sc_status_" + s.status))}</h2><p>${renderMsg(s.headline)}</p></div></header>
        <div class="sc-list">${s.measures.map((m) => scRow(m, false)).join("")}</div></div>`;
    }
    el.innerHTML = h; el.hidden = !h;
  }
  const onStoryClick = (e) => {
    const go = e.target.closest("[data-scgo]"); if (go) return goSection(go.dataset.scgo);
    const r = e.target.closest("[data-sckpi]"); if (!r) return;
    let tile = $(`tile-${r.dataset.sckpi}`);
    const closed = tile && tile.closest(".band-body[hidden]");   // v75: it sits in a closed group: open that first
    if (closed) { setBand(closed.id.replace("bandbody-", ""), true); }
    if (tile && S.section === r.dataset.scsec) { tile.scrollIntoView({ block: "center", behavior: "smooth" }); flash(tile); }
    else goSection(r.dataset.scsec, () => { const t2 = $(`tile-${r.dataset.sckpi}`); const c2 = t2 && t2.closest(".band-body[hidden]"); if (c2) setBand(c2.id.replace("bandbody-", ""), true); if (t2) { t2.scrollIntoView({ block: "center", behavior: "smooth" }); flash(t2); } });
  };
  $("story").addEventListener("click", onStoryClick);
  $("post-panel").addEventListener("click", onStoryClick);
  // v75: collapsible groups
  const bandKey = (sid) => `vdash.bands.${S.tenant}.${sid}`;
  function bandPrefs(sid) { try { return JSON.parse(localStorage.getItem(bandKey(sid)) || "{}") || {}; } catch (e) { return {}; } }
  function saveBandPref(sid, bid, open) {
    try { const p = bandPrefs(sid); p[bid] = open; localStorage.setItem(bandKey(sid), JSON.stringify(p)); } catch (e) { /* private mode: not remembered */ }
  }
  function bandHtml(g, data) {
    const m = g.ks.filter((k) => k.viz === "metric"), c = g.ks.filter((k) => k.viz !== "metric");
    const inner = (m.length ? `<div class="tiles band-tiles">${m.map((k) => tileHtml(k, data.results[k.id], false)).join("")}</div>` : "")
      + c.map((k) => cardHtml(k, data.results[k.id])).join("");
    if (!g.id) return `<div class="band-body">${inner}</div>`;
    const heads = m.slice(0, 3).filter((k) => data.results[k.id] && data.results[k.id].type === "metric" && !data.results[k.id].empty)
      .map((k) => `<span class="bh-fig">${esc(k.title)} <b>${esc(fmt(data.results[k.id].value, k.fmt))}</b></span>`).join("");
    const counts = [m.length ? T("band_figs", { n: m.length }) : "", c.length ? T("band_charts", { n: c.length }) : "",
      g.attn ? T("band_attn", { n: g.attn }) : ""].filter(Boolean).join(" · ");
    return `<section class="band bd-${g.status}${g.open ? " open" : ""}" id="band-${esc(g.id)}">
      <button type="button" class="band-bar" data-bandtoggle="${esc(g.id)}" aria-expanded="${g.open}" aria-controls="bandbody-${esc(g.id)}" title="${esc(T("band_toggle"))}">
        <span class="sc-dot"></span><span class="bb-title">${esc(g.title)}</span><span class="bb-sum">${esc(counts)}</span>
        <span class="bb-figs">${heads}</span><span class="bb-act"><span class="bb-act-t bb-show">${esc(T("band_show"))}</span><span class="bb-act-t bb-hide">${esc(T("band_hide"))}</span><span class="bb-chev" aria-hidden="true">${icon("down")}</span></span></button>
      <div class="band-body" id="bandbody-${esc(g.id)}"${g.open ? "" : " hidden"}>${inner}</div></section>`;
  }
  function drawBand(sec, bid, data) {
    if (S.bandDrawn.has(bid)) return;
    S.bandDrawn.add(bid);
    sec.kpis.filter((k) => (bid ? k.band === bid : !sec.bands.some((b) => b.id === k.band)) && k.viz !== "metric")
      .forEach((k) => drawCard(k, data.results[k.id]));
  }
  // v76: the group bar and the jump bar stay pinned; the jump bar shows where you are and switches group at once
  const bandSingle = () => { try { return localStorage.getItem("vdash.bandsingle") === "1"; } catch (e) { return false; } };
  function stickTop() {   // height of whatever is already pinned above the content (page header, or app bar + tabs)
    let h = 0;
    document.querySelectorAll(".topbar, .appbar, .tabstrip").forEach((el) => {
      if (getComputedStyle(el).position === "sticky" && el.offsetParent !== null) h += el.getBoundingClientRect().height;
    });
    return h;
  }
  function updateStick() {
    const root = document.documentElement.style, nav = $("band-nav");
    root.setProperty("--stick-top", `${Math.round(stickTop())}px`);
    root.setProperty("--bandnav-h", `${nav ? Math.round(nav.getBoundingClientRect().height) : 0}px`);
  }
  let spyQueued = false;
  function spyBands() {
    if (spyQueued) return; spyQueued = true;
    requestAnimationFrame(() => {
      spyQueued = false;
      const nav = $("band-nav"); if (!nav) return;
      const line = stickTop() + nav.getBoundingClientRect().height + 12;
      let cur = null;
      document.querySelectorAll("#charts > .band").forEach((b) => { if (b.getBoundingClientRect().top <= line) cur = b.id.replace("band-", ""); });
      if (!cur) { const f = document.querySelector("#charts > .band"); cur = f ? f.id.replace("band-", "") : null; }
      nav.querySelectorAll(".bn-pill").forEach((p) => p.classList.toggle("active", p.id === `bnp-${cur}`));
      const act = cur && $(`bnp-${cur}`); if (act && nav.querySelector(".bn-pills").scrollWidth > nav.querySelector(".bn-pills").clientWidth) act.scrollIntoView({ block: "nearest", inline: "nearest" });
    });
  }
  window.addEventListener("scroll", () => { updateStick(); spyBands(); }, { passive: true });
  window.addEventListener("resize", () => { updateStick(); spyBands(); });
  function goBand(bid) {
    if (bandSingle() && S.lastData) S.lastData.sec.bands.forEach((b) => { if (b.id !== bid) setBand(b.id, false); });
    setBand(bid, true);
    requestAnimationFrame(() => {
      const el = $(`band-${bid}`); if (!el) return;
      updateStick();
      const top = el.getBoundingClientRect().top + window.scrollY - stickTop() - ($("band-nav") ? $("band-nav").getBoundingClientRect().height : 0) + 1;
      window.scrollTo({ top, behavior: "smooth" }); flash(el);
    });
  }
  function setBand(bid, open, remember = true) {
    const el = $(`band-${bid}`); if (!el || !S.lastData) return;
    el.classList.toggle("open", open);
    $(`bandbody-${bid}`).hidden = !open;
    el.querySelector(".band-bar").setAttribute("aria-expanded", String(open));
    if (open) drawBand(S.lastData.sec, bid, S.lastData.data);
    if (remember) saveBandPref(S.lastData.sec.id, bid, open);
  }
  function renderSectionFoot(data, fresh) {
    const p = data.period;
    $("page-foot").textContent = T("page_foot", { start: fmtDate(p.start), end: fmtDate(p.end), grain: T("unit_" + p.grain), shown: S.ctx.kpi_counts.shown, total: S.ctx.kpi_counts.catalog, tpl: S.ctx.tenant.template_id });
    if (fresh) $("status-banner").hidden = true;
  }

  // ---------------------------------------------------------------- business evaluation + early warnings (v9)
  // Findings come from fixed server-side rules as a message key + typed arguments; the page formats the numbers
  // in the viewer's locale. Evaluation (Overview) follows the selected period; warnings (AI Insights) always use
  // the latest data, so they are fetched once per tenant load and shared by the bell, the strip and the panel.
  function clearPanels() {
    ["pre-panel", "post-panel", "story"].forEach((id) => { $(id).innerHTML = ""; $(id).hidden = true; });
  }
  const warnCount = () => (S.warn && S.warn.warnings ? S.warn.warnings.length : 0);
  function fetchWarnings() {
    if (!S.ctx || !S.ctx.panels || !S.ctx.panels.warnings) return Promise.resolve(null);
    if (S.warnReq) return S.warnReq;
    const tenant = S.tenant, lang = S.lang;
    S.warnReq = api(tenantUrl("/warnings")).then((w) => {
      if (tenant !== S.tenant || lang !== S.lang) return null;
      S.warn = w; renderBell(); renderNav();
      if (S.lastData && S.lastData.sec.id === "overview") renderPanels(S.lastData.sec, S.lastData.evalData, S.lastData.sc);
      return w;
    }).catch(() => { S.warnReq = null; return null; });
    return S.warnReq;
  }
  function renderBell() {
    const n = warnCount(), urgent = n && S.warn.counts.urgent;
    ["bell-btn", "bell-btn-m"].forEach((id) => {
      const b = $(id);
      b.hidden = !n;
      b.classList.toggle("urgent", !!urgent);
      b.innerHTML = icon("bell") + (n ? `<span class="bell-n">${n}</span>` : "");
      b.title = n ? `${T("bell_title")}: ${T("warn_strip", { n })}` : T("bell_title");
      b.setAttribute("aria-label", b.title);
    });
  }
  const openWarnings = () => {
    setPanel(null);
    const go = () => { const el = $("warn-card"); if (el) { el.scrollIntoView({ block: "start", behavior: "smooth" }); flash(el); } };
    if (S.section === "ai") go(); else goSection("ai", go);
  };
  $("bell-btn").addEventListener("click", openWarnings);
  $("bell-btn-m").addEventListener("click", openWarnings);

  const PLAIN_ARGS = new Set(["label", "text"]);
  function renderMsg(f, html = true) {
    // split the template on {placeholders}: literal text is escaped, numbers are bold
    const parts = String(T(f.msg)).split(/(\{\w+\})/);
    return parts.map((p) => {
      const m = /^\{(\w+)\}$/.exec(p);
      if (!m || !(m[1] in f.args)) return html ? esc(p) : p;
      const a = f.args[m[1]], txt = fmtArg(a);
      if (!html) return txt;
      return PLAIN_ARGS.has(a.fmt) ? `<span class="f-name">${esc(txt)}</span>` : `<b>${esc(txt)}</b>`;
    }).join("");
  }
  const flash = (el) => { el.classList.remove("flash"); void el.offsetWidth; el.classList.add("flash"); };

  function panelMenu(kind) {
    return `<div class="dl-wrap"><button data-pdl="${kind}" title="${esc(T("download"))}" aria-label="${esc(T("download"))}" aria-haspopup="true">${icon("download")}</button>
      <div class="menu" id="menu-panel-${kind}" hidden role="menu">
        <button role="menuitem" data-pact="xlsx" data-kind="${kind}">${icon("table")}${esc(T("dl_panel_xlsx"))}</button>
        <button role="menuitem" data-pact="csv" data-kind="${kind}">${icon("table")}${esc(T("dl_panel_csv"))}</button></div></div>`;
  }
  function findingLi(f, kind, idx) {
    const open = f.items_total || f.link;
    const warn = kind === "warnings";
    const tags = warn ? `<span class="sev-tag ${esc(f.severity)}">${esc(T("sev_" + f.severity))}</span>`
      + `<span class="basis-tag" title="${esc(T("basis_tip_" + f.basis))}">${esc(T("basis_" + f.basis))}</span>`
      + (f.is_new ? `<span class="new-tag">${esc(T("warn_new"))}</span>` : "") : "";
    const inner = `${warn ? `<span class="f-tags">${tags}</span>` : ""}<span class="f-text">${renderMsg(f)}</span>`
      + (f.args.kpi ? "" : `<span class="f-meta">${esc(f.title)}</span>`); // a KPI finding already names its KPI
    return `<li class="finding ${warn ? "w-" + esc(f.severity) : ""}">${open
      ? `<button class="f-btn" data-fk="${kind}" data-fi="${idx}">${inner}<span class="f-go" aria-hidden="true">${icon("chevron")}</span></button>`
      : `<div class="f-btn static">${inner}</div>`}</li>`;
  }
  function evalColumn(side, list) {
    const open = S.evalOpen[side], shown = open ? list : list.slice(0, 5);
    const more = list.length > 5 ? `<button class="f-more" data-more="${side}">${esc(open ? T("eval_less") : T("eval_more", { n: list.length - 5 }))}</button>` : "";
    return `<section class="eval-col ${side}">
      <h3><span class="eval-ico" aria-hidden="true">${icon(side === "positive" ? "up" : "down")}</span>${esc(T(side === "positive" ? "eval_good" : "eval_bad"))}<span class="eval-n">${list.length}</span></h3>
      ${list.length ? `<ul class="f-list">${shown.map((f, i) => findingLi(f, side, i)).join("")}</ul>${more}`
        : `<p class="f-none">${esc(T(side === "positive" ? "eval_none_good" : "eval_none_bad"))}</p>`}
    </section>`;
  }
  // v69: with a scorecard the two columns are "on track" / "needs attention" measures against targets
  function scColumn(side, list) {
    const open = S.evalOpen[side], shown = open ? list : list.slice(0, 5);
    const more = list.length > 5 ? `<button class="f-more" data-more="${side}">${esc(open ? T("eval_less") : T("eval_more", { n: list.length - 5 }))}</button>` : "";
    const key = { negative: "red", watch: "amber", positive: "green" }[side];
    return `<section class="eval-col ${side}">
      <h3><span class="eval-ico" aria-hidden="true">${icon(side === "positive" ? "up" : side === "watch" ? "alert" : "down")}</span>${esc(T("sc_status_" + key))}<span class="eval-n">${list.length}</span></h3>
      ${list.length ? `<div class="sc-list">${shown.map((m) => scRow(m, true)).join("")}</div>${more}`
        : `<p class="f-none">${esc(T("sc_none_" + key))}</p>`}
    </section>`;
  }
  function evalHtml(ev, sc) {
    const useSc = sc && sc.available;
    let body;
    if (useSc) {
      const c = sc.counts || {};
      body = `<p class="sc-counts"><span class="cnt red">${c.red || 0}</span> ${esc(T("sc_status_red"))} · <span class="cnt amber">${c.amber || 0}</span> ${esc(T("sc_status_amber"))} · <span class="cnt green">${c.green || 0}</span> ${esc(T("sc_status_green"))}</p>
        <div class="eval-cols three">${scColumn("negative", sc.issues.filter((m) => m.status === "red"))}${scColumn("watch", sc.issues.filter((m) => m.status === "amber"))}${scColumn("positive", sc.good)}</div>`;
    }
    else if (!ev) body = `<p class="f-none">${esc(T("loading"))}</p>`;
    else if (ev.error) body = `<div class="err">${esc(T("load_failed", { msg: ev.error }))}</div>`;
    else body = `<div class="eval-cols">${evalColumn("positive", ev.positive)}${evalColumn("negative", ev.negative)}</div>`
      + (S.compare === "none" ? `<p class="f-none eval-note">${esc(T("eval_no_compare"))}</p>` : "");
    const p = S.ctx.presets.find((x) => x.id === S.preset);
    const per = p ? T("preset_" + p.id) : `${fmtDate(S.start)} – ${fmtDate(S.end)}`;
    const cmp = S.compare === "yoy" ? T("cmp_yoy") : T("cmp_previous");
    const ok = !useSc && ev && !ev.error;
    return `<article class="card panel-card eval-card" id="eval-card">
      <div class="card-head"><div class="card-titles"><div class="card-title">${esc(T("eval_title"))}</div>
        <div class="card-sub"><span>${esc(S.compare === "none" ? per : T("eval_sub", { period: per, cmp }))}</span></div></div>
        <div class="card-actions">${ok ? `<button data-pinfo="eval" title="${esc(T("about_chart"))}" aria-label="${esc(T("about_chart"))}">${icon("info")}</button>${panelMenu("evaluation")}` : ""}</div></div>
      ${ok ? `<div class="info-pop" id="pinfo-eval" hidden>${esc(T("eval_checked", { n: ev.checked }))}</div>` : ""}
      ${body}
    </article>`;
  }
  function warnHtml(w) {
    const list = w ? w.warnings : [];
    const open = S.warnOpen, shown = open ? list : list.slice(0, 6);
    const more = list.length > 6 ? `<button class="f-more" data-more="warnings">${esc(open ? T("eval_less") : T("eval_more", { n: list.length - 6 }))}</button>` : "";
    const body = !w ? `<p class="f-none">${esc(T("loading"))}</p>`
      : list.length ? `<ul class="f-list warn-list">${shown.map((f, i) => findingLi(f, "warnings", i)).join("")}</ul>${more}`
        : `<p class="f-none ok">${esc(T("warn_none"))}</p>`;
    return `<article class="card panel-card warn-card" id="warn-card">
      <div class="card-head"><div class="card-titles"><div class="card-title"><span class="warn-ico" aria-hidden="true">${icon("alert")}</span>${esc(T("warn_title"))}</div>
        <div class="card-sub"><span>${esc(w && w.asof ? T("warn_sub", { date: fmtDate(w.asof) }) : "")}</span></div></div>
        <div class="card-actions">${w ? `<button data-pinfo="warn" title="${esc(T("about_chart"))}" aria-label="${esc(T("about_chart"))}">${icon("info")}</button>` : ""}${w && list.length ? panelMenu("warnings") : ""}</div></div>
      ${w ? `<div class="info-pop" id="pinfo-warn" hidden>${esc(T("warn_checked", { n: w.checked }))} · ${esc(["forecast", "model", "trend", "data"].map((b) => `${T("basis_" + b)}: ${T("basis_tip_" + b)}`).join(" · "))}</div>` : ""}
      ${body}
    </article>`;
  }
  function stripHtml(w) {
    const n = w.warnings.length, u = w.counts.urgent;
    return `<button class="warn-strip${u ? " urgent" : ""}" id="warn-strip"><span class="warn-ico" aria-hidden="true">${icon("alert")}</span>
      <span class="ws-text"><b>${esc(T("warn_strip", { n }))}</b>${u ? `<span class="sev-tag urgent">${esc(T("warn_strip_urgent", { n: u }))}</span>` : ""}</span>
      <span class="ws-go"><span class="ws-go-t">${esc(T("warn_view"))}</span>${icon("chevron")}</span></button>`;
  }
  function renderPanels(sec, evalData, sc) {
    const pre = $("pre-panel"), post = $("post-panel");
    let preH = "", postH = "";
    if (sec.id === "overview" && warnCount()) preH = stripHtml(S.warn);
    if (sec.panel === "evaluation") postH = evalHtml(evalData, sc);
    if (sec.panel === "warnings") preH = warnHtml(S.warn);
    pre.innerHTML = preH; pre.hidden = !preH;
    post.innerHTML = postH; post.hidden = !postH;
  }
  function currentFindings(kind) {
    if (kind === "warnings") return S.warn ? S.warn.warnings : [];
    const ev = S.lastData && S.lastData.evalData;
    return ev && ev[kind] ? ev[kind] : [];
  }
  function onPanelClick(e) {
    const strip = e.target.closest("#warn-strip");
    if (strip) { openWarnings(); return; }
    const b = e.target.closest("[data-fk]");
    if (b) { const f = currentFindings(b.dataset.fk)[+b.dataset.fi]; if (f) openFinding(f, b.dataset.fk === "warnings" ? "warnings" : "evaluation"); return; }
    const m = e.target.closest("[data-more]");
    if (m) {
      if (m.dataset.more === "warnings") S.warnOpen = !S.warnOpen; else S.evalOpen[m.dataset.more] = !S.evalOpen[m.dataset.more];
      renderPanels(S.lastData.sec, S.lastData.evalData, S.lastData.sc); return;
    }
    const i = e.target.closest("[data-pinfo]");
    if (i) { const el = $(`pinfo-${i.dataset.pinfo}`); if (el) el.hidden = !el.hidden; return; }
    const d = e.target.closest("[data-pdl]");
    if (d) { const mm = $(`menu-panel-${d.dataset.pdl}`); const was = mm.hidden; closeMenus(); mm.hidden = !was; return; }
    const a = e.target.closest("[data-pact]");
    if (a) { closeMenus(); downloadFindings(a.dataset.kind, a.dataset.pact); }
  }
  $("pre-panel").addEventListener("click", onPanelClick);
  $("post-panel").addEventListener("click", onPanelClick);

  function downloadFindings(kind, format, rule) {
    const params = { format };
    if (rule) params.rule = rule;
    if (kind === "evaluation") Object.assign(params, { start: S.start, end: S.end, grain: S.grain, compare: S.compare });
    downloadUrl(tenantUrl(`/${kind}/export`, params));
  }
  function resetDrillChrome() {
    $("drill-kicker").textContent = T("drill_title");
    $("drill-link").hidden = true; $("drill-link").innerHTML = "";
    $("drill-panel").classList.remove("finding-panel");
    $("drill-body").hidden = false; $("drill-csv").hidden = $("drill-xlsx").hidden = false;
  }
  function openFinding(f, panel) {
    drillState = { finding: f, panel };
    resetDrillChrome();
    $("drill-panel").classList.add("finding-panel");
    $("drill-kicker").textContent = panel === "warnings"
      ? `${T("warn_title")} · ${T("sev_" + f.severity)} · ${T("basis_" + f.basis)}`
      : `${T("eval_title")} · ${T(f.side === "positive" ? "eval_good" : "eval_bad")}`;
    $("drill-title").textContent = f.title;
    $("drill-sub").innerHTML = renderMsg(f);
    const nf = new Intl.NumberFormat(S.locale);
    $("drill-summary").innerHTML = f.items_total ? `<span class="drill-n">${esc(T("drill_rows", { n: "\u0001" })).replace("\u0001", `<b>${esc(nf.format(f.items_total))}</b>`)}</span>` : "";
    $("drill-trunc").textContent = f.items && f.items.length && f.items_total > f.items.length ? T("drill_showing", { shown: nf.format(f.items.length), n: nf.format(f.items_total) }) : "";
    $("drill-note").hidden = !f.items_hidden; $("drill-note").textContent = f.items_hidden ? T("items_hidden") : "";
    if (f.link) {
      $("drill-link").hidden = false;
      $("drill-link").innerHTML = `<button class="btn btn-sm" id="drill-see">${icon("chart")}<span>${esc(T("see_chart", { title: f.link.title }))}</span></button>`;
      $("drill-see").addEventListener("click", () => jumpToKpi(f.link));
    }
    const hasRows = f.items && f.items.length;
    $("drill-body").innerHTML = hasRows ? tableHtml(f.items_cols, f.items) : "";
    $("drill-body").hidden = !hasRows;
    $("drill-csv").hidden = $("drill-xlsx").hidden = !hasRows;
    $("drill-csv").disabled = $("drill-xlsx").disabled = !hasRows;
    $("drill-panel").hidden = false; $("drill-scrim").hidden = false;
    document.body.classList.add("drill-open");
    $("drill-close").focus();
  }
  function jumpToKpi(link) {
    closeDrill();
    const go = () => {
      const el = $(`card-${link.kpi}`) || $(`tile-${link.kpi}`);
      if (el) { el.scrollIntoView({ block: "center", behavior: "smooth" }); flash(el); }
    };
    if (S.section === link.section) go(); else goSection(link.section, go);
  }

  // ---------------------------------------------------------------- tiles
  function tileHtml(k, r, hero) {
    r = r || {};
    let delta = "";
    if (r.type === "error") delta = `<span class="err">${esc(T("could_not_compute"))}</span>`;
    else if (r.delta_pct !== undefined || r.delta_pts !== undefined) {
      const d = k.fmt === "pct" ? r.delta_pts : r.delta_pct;
      const up = d > 0, flat = Math.abs(d) < 0.05;
      const cls = flat || !k.good ? "neutral" : (up === (k.good === "up") ? "good" : "bad");
      const num = new Intl.NumberFormat(S.locale, { maximumFractionDigits: 1, minimumFractionDigits: 1, signDisplay: "always" }).format(d);
      const txt = k.fmt === "pct" ? `${num} ${T("pts")}` : `${num}%`;
      delta = `<span class="delta ${cls}">${flat ? "●" : up ? "▲" : "▼"} ${esc(txt)}</span><span>${esc(compareLabel(r))} (${esc(fmt(r.compare_value, k.fmt))})</span>`;
    } else if (r.compare_value === null && S.compare !== "none" && k.period_mode === "range") {
      delta = `<span>${esc(T("no_data_cmp", { label: compareLabel(r) }))}</span>`;
    } else if (k.period_mode !== "range") delta = modeBadge(k);
    const info = [k.desc, r.caveat, k.note].filter(Boolean).join(" ");
    return `<article class="tile${hero ? " hero" : ""}" id="tile-${esc(k.id)}" aria-label="${esc(k.title)}">
      <div class="t-label">${esc(k.title)}</div>
      ${info ? `<button class="icon-btn info" data-tinfo="${esc(k.id)}" title="${esc(info)}" aria-label="${esc(T("about_kpi"))}" aria-expanded="false">${icon("info")}</button>` : ""}
      <div class="t-value">${r.empty ? `<span class="t-empty">${esc(T("no_data_period"))}</span>` : esc(fmt(r.value, k.fmt))}</div>
      ${info ? `<div class="tile-info" id="tinfo-${esc(k.id)}" hidden>${esc(info)}</div>` : ""}
      <div class="t-delta">${delta}</div>
      ${sparkSvg(r.spark)}
    </article>`;
  }
  const onTileClick = (e) => {
    const bt = e.target.closest("[data-bandtoggle]");   // v75: open or close a group
    if (bt) {
      const id = bt.dataset.bandtoggle, willOpen = !$(`band-${id}`).classList.contains("open");
      if (willOpen && bandSingle() && S.lastData) S.lastData.sec.bands.forEach((b) => { if (b.id !== id) setBand(b.id, false); });
      setBand(id, willOpen); updateStick(); spyBands(); return;
    }
    const bs = e.target.closest("[data-bandsingle]");   // v76: one group at a time
    if (bs && S.lastData) {
      const on = !bandSingle(); try { localStorage.setItem("vdash.bandsingle", on ? "1" : "0"); } catch (err) { /* not remembered */ }
      bs.classList.toggle("on", on); bs.setAttribute("aria-pressed", String(on));
      if (on) { const cur = document.querySelector("#band-nav .bn-pill.active"); const keep = cur ? cur.id.replace("bnp-", "") : S.lastData.sec.bands[0].id;
        S.lastData.sec.bands.forEach((b) => setBand(b.id, b.id === keep)); }
      updateStick(); spyBands(); return;
    }
    const ba = e.target.closest("[data-bandall]");
    if (ba && S.lastData) { S.lastData.sec.bands.forEach((b) => setBand(b.id, ba.dataset.bandall === "1")); updateStick(); spyBands(); return; }
    const nb = e.target.closest("[data-band]");   // v64: jump link to a band of a banded page (opens it first)
    if (nb) { goBand(nb.dataset.band); return; }
    const b = e.target.closest("[data-tinfo]"); if (!b) return;
    const el = $(`tinfo-${b.dataset.tinfo}`); el.hidden = !el.hidden; b.setAttribute("aria-expanded", String(!el.hidden));
  };
  $("tiles").addEventListener("click", onTileClick);
  $("charts").addEventListener("click", onTileClick);   // v64: key figures of a band sit inside #charts
  function sparkSvg(sp) {
    if (!sp || sp.length < 2) return "";
    const vals = sp.map((p) => p[1] ?? 0), W = 200, H = 34, mn = Math.min(...vals), mx = Math.max(...vals), rng = mx - mn || 1;
    const pts = vals.map((v, i) => [(i / (vals.length - 1)) * W, H - 3 - ((v - mn) / rng) * (H - 6)]);
    const line = pts.map((p, i) => `${i ? "L" : "M"}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join("");
    return `<svg class="spark" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" aria-hidden="true"><path class="a" d="${line}L${W},${H}L0,${H}Z"/><path class="l" d="${line}" vector-effect="non-scaling-stroke"/></svg>`;
  }

  // ---------------------------------------------------------------- cards
  const exportsFor = (kid) => (S.ctx.exports || []).filter((e) => e.kpis && kid in e.kpis);
  const hasTrend = (k, r) => r && r.type === "timeseries" && r.series.some((s) => s.trend && s.data.filter((v) => v !== null).length > 5);

  function cardHtml(k, r) {
    const sub = modeBadge(k); // v11: internal KPI codes are not shown to business users
    const isChart = k.viz !== "table";
    const tv = S.tableView.has(k.id);
    const ok = r && r.type !== "error" && !r.empty;
    const exps = exportsFor(k.id);
    const menu = ok || exps.length ? `<div class="dl-wrap">
        <button data-dl="${esc(k.id)}" title="${esc(T("download"))}" aria-label="${esc(T("download"))}" aria-haspopup="true">${icon("download")}</button>
        <div class="menu" id="menu-${esc(k.id)}" hidden role="menu">
          ${ok && isChart && !tv ? `<button role="menuitem" data-act="png" data-k="${esc(k.id)}">${icon("image")}${esc(T("dl_png"))}</button>
          <button role="menuitem" data-act="svg" data-k="${esc(k.id)}">${icon("image")}${esc(T("dl_svg"))}</button>` : ""}
          ${ok ? `<button role="menuitem" data-act="xlsx" data-k="${esc(k.id)}">${icon("table")}${esc(T("dl_xlsx"))}</button>
          <button role="menuitem" data-act="csv" data-k="${esc(k.id)}">${icon("table")}${esc(T("dl_csv"))}</button>` : ""}
          ${exps.map((e) => `<button role="menuitem" class="menu-strong" data-act="list" data-exp="${esc(e.id)}" data-k="${esc(k.id)}">${icon("list")}${esc(T("dl_full", { title: e.title }))}</button>`).join("")}
        </div></div>` : "";
    const trend = hasTrend(k, r) && !tv ? `<div class="card-tools"><div class="seg-mini" role="radiogroup" aria-label="${esc(T("trend"))}" title="${esc(T("trend"))}">
        ${["ma", "linear", "off"].map((m) => `<button role="radio" data-trend="${m}" aria-checked="${S.trendMode === m}">${esc(T("trend_" + m))}</button>`).join("")}</div></div>` : "";
    return `<article class="card ${k.span > 1 ? "span-2" : ""}" id="card-${esc(k.id)}">
      <div class="card-head"><div class="card-titles"><div class="card-title">${esc(k.title)}</div><div class="card-sub">${sub}<span class="trend-chip" id="chip-${esc(k.id)}" hidden></span></div></div>
        <div class="card-actions">
          ${k.desc || k.note ? `<button data-info="${esc(k.id)}" title="${esc(T("about_chart"))}" aria-label="${esc(T("about_chart"))}">${icon("info")}</button>` : ""}
          ${isChart ? `<button data-tv="${esc(k.id)}" aria-pressed="${tv}" title="${esc(T(tv ? "show_chart" : "show_table"))}" aria-label="${esc(T(tv ? "show_chart" : "show_table"))}">${icon(tv ? "chart" : "table")}</button>` : ""}
          ${menu}
        </div></div>
      ${trend}
      <div class="info-pop" id="info-${esc(k.id)}" hidden>${esc([k.desc, k.note].filter(Boolean).join(" "))}${exps.length && isChart ? `<br><i>${esc(T("export_click_hint"))}</i>` : ""}</div>
      <div class="card-body" id="body-${esc(k.id)}"></div>
    </article>`;
  }
  function closeMenus() { document.querySelectorAll(".menu").forEach((m) => { m.hidden = true; }); }
  $("charts").addEventListener("click", (e) => {
    const t = e.target.closest("[data-tv]");
    if (t) { const id = t.dataset.tv; S.tableView.has(id) ? S.tableView.delete(id) : S.tableView.add(id); renderSection(S.lastData, false); return; }
    const i = e.target.closest("[data-info]");
    if (i) { const el = $(`info-${i.dataset.info}`); el.hidden = !el.hidden; return; }
    const tr = e.target.closest("[data-trend]");
    if (tr) { S.trendMode = tr.dataset.trend; store.set("vdash-trend", S.trendMode); savePrefs({ trend: S.trendMode }, true); renderSection(S.lastData, false); return; }
    const d = e.target.closest("[data-dl]");
    if (d) { const m = $(`menu-${d.dataset.dl}`); const was = m.hidden; closeMenus(); m.hidden = !was; return; }
    const a = e.target.closest("[data-act]");
    if (a) { closeMenus(); doExport(a.dataset.act, a.dataset.k, a.dataset.exp); }
  });

  function drawCard(k, r) {
    const body = $(`body-${k.id}`);
    if (!r) { body.innerHTML = `<div class="empty">${esc(T("not_loaded"))}</div>`; return; }
    if (r.type === "error") { body.innerHTML = `<div class="err">${esc(T("kpi_error", { msg: r.error }))}</div>`; return; }
    if (r.empty) { body.innerHTML = `<div class="empty">${esc(T("empty_period"))}<br>${esc(T("try_longer"))}</div>`; return; }
    if (k.viz === "table") { body.innerHTML = tableHtml(k.columns, r.rows); return; }
    if (S.tableView.has(k.id)) { body.innerHTML = chartAsTable(k, r); return; }
    const div = document.createElement("div");
    const view = r.type === "heatmap" && body.clientWidth < 560 ? phoneHeat(k, r) : r;
    const opt = buildOption(k, view, body.clientWidth);
    if (k.drill) addDrillHint(opt);
    div.className = "chart" + (opt.__tall ? " tall" : "");
    if (opt.__height) div.style.height = opt.__height + "px";
    body.appendChild(div);
    const chart = echarts.init(div, null, { renderer: "svg" });
    const chip = opt.__chip;
    delete opt.__tall; delete opt.__height; delete opt.__chip; delete opt.__keys;
    const note = opt.__note; delete opt.__note;
    chart.setOption(opt);
    if (note) { const n = document.createElement("div"); n.className = "chart-note"; n.textContent = note; body.appendChild(n); }
    if (chip) { const c = $(`chip-${k.id}`); c.textContent = chip.text; c.title = chip.tip; c.hidden = false; }
    const exps = exportsFor(k.id);
    if (k.drill) { div.classList.add("drillable"); wireDrill(k, view, chart); }
    else if (exps.length && r.type === "breakdown") {
      // click a bar / slice -> download that segment's full list
      chart.on("click", (p) => { const it = (r.items || []).find((x) => x.label === p.name); if (it && it.key) openExportDialog(exps[0].id, k.id, it.key); });
    }
    S.charts.set(k.id, chart);
  }
  function disposeCharts() { S.charts.forEach((c) => c.dispose()); S.charts.clear(); }

  function tableHtml(cols, rows) {
    // v74: a column with no value in any row (e.g. gross profit when no cost is known) is left out
    const kept = rows.length ? cols.filter((c) => rows.some((r) => r[c.key] !== null && r[c.key] !== undefined && r[c.key] !== "")) : cols;
    if (kept.length) cols = kept;
    const numeric = new Set(["currency", "number", "int", "pct", "ratio", "days", "hours", "score"]);
    return `<div class="table-wrap"><table class="data"><thead><tr>${cols.map((c) => `<th class="${numeric.has(c.fmt) ? "num" : ""}">${esc(c.label)}</th>`).join("")}</tr></thead>
      <tbody>${rows.map((r) => `<tr>${cols.map((c) => `<td class="${numeric.has(c.fmt) ? "num" : ""}" data-label="${esc(c.label)}">${esc(fmt(r[c.key], c.fmt, false))}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
  }
  function chartAsTable(k, r) {
    if (r.type === "timeseries") {
      const cols = [{ key: "x", label: T("period"), fmt: "text" }, ...r.series.map((s, i) => ({ key: "s" + i, label: s.name || k.title, fmt: s.fmt }))];
      const rows = r.x.map((x, j) => Object.assign({ x: fmtBucket(x, r.grain) }, ...r.series.map((s, i) => ({ ["s" + i]: s.data[j] }))));
      return tableHtml(cols, rows);
    }
    if (r.type === "heatmap") {
      const cols = [{ key: "y", label: "", fmt: "text" }, ...r.x.map((x, i) => ({ key: "c" + i, label: x, fmt: k.fmt }))];
      const rows = r.y.map((y, yi) => { const o = { y }; r.cells.filter((c) => c[1] === yi).forEach((c) => { o["c" + c[0]] = c[2]; }); return o; });
      return tableHtml(cols, rows);
    }
    return tableHtml([{ key: "label", label: T("category"), fmt: "text" }, { key: "value", label: k.title, fmt: k.fmt }], r.items);
  }

  // ---------------------------------------------------------------- exports
  async function saveBlob(blob, name) {
    if (isIOS && isStandalone() && await shareFile(blob, name)) return;
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob); a.download = name; document.body.appendChild(a); a.click();
    setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
  }
  function fileBase(kid) { return `${S.tenant}_${kid}_${S.start}_${S.end}`.replace(/[^\w\-.]+/g, "-"); }
  // Installed iPhone apps (standalone PWA) cannot save files through <a download>: use the Share sheet.
  const isIOS = /iphone|ipad|ipod/i.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  const isStandalone = () => window.matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
  async function shareFile(blob, name) {
    const file = new File([blob], name, { type: blob.type || "application/octet-stream" });
    if (navigator.canShare && navigator.canShare({ files: [file] })) { await navigator.share({ files: [file], title: name }).catch(() => {}); return true; }
    return false;
  }
  async function downloadUrl(url) { // server-built file (CSV / Excel); cookie auth, same origin
    if (isIOS && isStandalone()) {
      const res = await fetch(url, { credentials: "same-origin" });
      if (!res.ok) return;
      const cd = res.headers.get("content-disposition") || "", m = /filename="([^"]+)"/.exec(cd);
      if (await shareFile(await res.blob(), m ? m[1] : "export")) return;
    }
    const a = document.createElement("a"); a.href = url; a.download = ""; document.body.appendChild(a); a.click(); a.remove();
  }

  // v30: what web/report.js may use from the page (read-only access to the state)
  // v32: a saved report sets the dashboard's period and comparison (same steps as the period controls)
  function setPeriod(preset, start, end, compare) {
    if (preset === "custom" && start && end) { S.preset = "custom"; S.start = start; S.end = end; }
    else { S.preset = preset; applyPreset(); }
    if (compare) S.compare = compare;
    renderPresetList(); syncControls();
    return loadSection();
  }
  setInterval(refreshCredits, 45000);
  document.addEventListener("visibilitychange", refreshCredits);
  window.addEventListener("focus", refreshCredits);
  window.addEventListener("message", (e) => { if (e.origin === location.origin && e.data && e.data.vdash === "credits") refreshCredits(); });   // an answer in a report
  window.VdashApp = { get S() { return S; }, setCredits, api, T, esc, tenantUrl, icon, downloadUrl, fmtDate, setPeriod,
    goSection: (id) => { setPanel(null); return goSection(id); }, setPref, changeLanguage };   // v51: used by web/settings.js

  async function doExport(act, kid, expId) {
    if (act === "list") return openExportDialog(expId, kid, null);
    if (act === "csv" || act === "xlsx") {
      return downloadUrl(tenantUrl(`/kpis/${encodeURIComponent(kid)}/export`, { format: act, start: S.start, end: S.end, grain: S.grain, compare: S.compare }));
    }
    const chart = S.charts.get(kid); if (!chart) return;
    let svgText;
    if (typeof chart.renderToSVGString === "function") svgText = chart.renderToSVGString();
    else { // older ECharts: data URL may be URI-encoded or base64
      const u = chart.getDataURL({ type: "svg" }), body = u.slice(u.indexOf(",") + 1);
      svgText = u.slice(0, u.indexOf(",")).includes("base64") ? decodeURIComponent(escape(atob(body))) : decodeURIComponent(body);
    }
    if (act === "svg") return saveBlob(new Blob([svgText], { type: "image/svg+xml" }), fileBase(kid) + ".svg");
    // PNG: chart SVG drawn onto a canvas with a title + context header, 2x for sharp slides
    const k = S.lastData.sec.kpis.find((x) => x.id === kid);
    const el = $(`body-${kid}`).querySelector(".chart");
    const w = el.clientWidth, h = el.clientHeight, scale = 2, head = 58;
    const img = new Image();
    img.onload = () => {
      const cv = document.createElement("canvas"); cv.width = w * scale; cv.height = (h + head + 12) * scale;
      const g = cv.getContext("2d"); g.scale(scale, scale);
      g.fillStyle = css("--surface-1"); g.fillRect(0, 0, w, h + head + 12);
      g.fillStyle = css("--text-primary"); g.font = `600 16px ${css("--font")}`; g.fillText(k.title, 12, 24);
      g.fillStyle = css("--text-muted"); g.font = `12px ${css("--font")}`;
      g.fillText(`${S.ctx.tenant.display_name} · ${fmtDate(S.start)} – ${fmtDate(S.end)} · VDec BI`, 12, 44);
      g.drawImage(img, 0, head, w, h);
      cv.toBlob((b) => saveBlob(b, fileBase(kid) + ".png"), "image/png");
    };
    img.onerror = () => { $("status-banner").hidden = false; $("status-banner").textContent = T("could_not_compute"); };
    img.src = URL.createObjectURL(new Blob([svgText], { type: "image/svg+xml" }));
  }

  let dlgState = null;
  async function openExportDialog(expId, kid, preselect) {
    const dlg = $("export-dialog");
    dlgState = { expId };
    $("dlg-title").textContent = T("loading"); $("dlg-desc").textContent = ""; $("dlg-filters").innerHTML = ""; $("dlg-count").textContent = "";
    dlg.hidden = false; $("dlg-download").focus();
    try {
      const o = await api(tenantUrl(`/exports/${encodeURIComponent(expId)}/options`));
      $("dlg-title").textContent = o.title; $("dlg-desc").textContent = o.desc;
      const exp = (S.ctx.exports || []).find((e) => e.id === expId);
      const preParam = exp && kid ? exp.kpis[kid] : null;
      $("dlg-filters").innerHTML = o.filters.map((f) => `<label class="dlg-field"><span>${esc(f.label)}</span>
        <select data-param="${esc(f.param)}"><option value="">${esc(T("filter_all"))}</option>
        ${f.options.map((op) => `<option value="${esc(op.value)}" ${preselect && f.param === preParam && op.value === preselect ? "selected" : ""}>${esc(op.label)} (${op.count})</option>`).join("")}
        </select></label>`).join("");
      if (o.total_rows != null) $("dlg-count").textContent = T("rows_estimate", { n: new Intl.NumberFormat(S.locale).format(o.total_rows) });
      if ([...document.querySelectorAll("#dlg-filters select")].some((s) => s.value)) refreshDlgCount();   // opened with a segment already chosen
    } catch (e) { $("dlg-title").textContent = e.message; }
  }
  // v58: the number at the bottom left follows the filters above it (not always the total of the list)
  let dlgCountSeq = 0;
  async function refreshDlgCount() {
    if (!dlgState) return;
    const seq = ++dlgCountSeq, id = dlgState.expId, params = {};
    document.querySelectorAll("#dlg-filters select").forEach((s) => { if (s.value) params[`f_${s.dataset.param}`] = s.value; });
    try {
      const d = await api(tenantUrl(`/exports/${encodeURIComponent(id)}/count`, params));
      if (seq !== dlgCountSeq || !dlgState || dlgState.expId !== id) return;
      $("dlg-count").textContent = T("rows_estimate", { n: new Intl.NumberFormat(S.locale).format(d.rows) });
    } catch (e) { /* the old number stays; the download itself still works */ }
  }
  $("dlg-filters").addEventListener("change", refreshDlgCount);
  function closeDialog() { $("export-dialog").hidden = true; dlgState = null; }
  $("dlg-cancel").addEventListener("click", closeDialog);
  $("export-dialog").addEventListener("click", (e) => { if (e.target.id === "export-dialog") closeDialog(); });
  $("dlg-download").addEventListener("click", () => {
    if (!dlgState) return;
    const params = { format: document.querySelector('input[name="dlg-format"]:checked').value };
    document.querySelectorAll("#dlg-filters select").forEach((s) => { if (s.value) params[`f_${s.dataset.param}`] = s.value; });
    downloadUrl(tenantUrl(`/exports/${encodeURIComponent(dlgState.expId)}`, params));
    closeDialog();
  });

  // ---------------------------------------------------------------- drill-down (v5)
  // Click a bar, slice, period or heatmap cell -> the rows behind it. The browser sends only *what* was clicked
  // (raw bucket / category key / cell keys); the server rebuilds the row query from the catalog, binds the
  // tenant and the values, and removes personal columns below manager role.
  const touchOnly = () => window.matchMedia("(hover: none)").matches;
  function addDrillHint(opt) {
    const tt = opt.tooltip; if (!tt || typeof tt.formatter !== "function") return;
    const f = tt.formatter;
    tt.formatter = (p) => `${f(p)}<div class="drill-hint">${esc(T(touchOnly() ? "drill_hint_touch" : "drill_hint"))}</div>`;
  }
  let lastTap = null; // phones: first tap shows the tooltip, a second tap on the same element opens the rows
  function tapGate(id) {
    if (!touchOnly()) return true;
    const now = Date.now(), ok = lastTap && lastTap.id === id && now - lastTap.t < 6000;
    lastTap = ok ? null : { id, t: now };
    return ok;
  }
  function wireDrill(k, r, chart) {
    if (r.type === "breakdown") {
      chart.on("click", (p) => {
        const it = (r.items || []).find((x) => x.label === p.name);
        if (!it || it.key === "__other" || !tapGate(k.id + "|" + it.label)) return;
        openDrill(k, { key: it.key ?? null }, it.label);
      });
    } else if (r.type === "heatmap") {
      chart.on("click", (p) => {
        const [xi, yi] = p.value || [];
        if (xi == null || !tapGate(`${k.id}|${xi}|${yi}`)) return;
        const y = r.y_keys ? r.y_keys[yi] : r.y[yi];
        const f = r.__blocks ? { x_range: [r.__blocks[xi] * 3, r.__blocks[xi] * 3 + 2], y } : { x: r.x_keys ? r.x_keys[xi] : r.x[xi], y };
        const xl = r.__prelabeled ? r.x[xi] : (k.id === "hourly_heatmap" ? `${String(r.x[xi]).padStart(2, "0")}h` : r.x[xi]);
        openDrill(k, f, `${r.y[yi]} · ${xl}`);
      });
    } else if (r.type === "timeseries") {
      // lines have no clickable symbols, so the click position picks the period; a click that lands on a
      // stacked bar / line of a per-category chart also narrows to that category
      let hit = null;
      chart.on("click", (p) => { if (p.componentType === "series") hit = p; });
      chart.getZr().on("click", (e) => setTimeout(() => {
        const pt = [e.offsetX, e.offsetY], p = hit; hit = null;
        const grids = chart.getOption().grid.length;
        let xi = null;
        for (let gi = 0; gi < grids && xi === null; gi++) {
          if (chart.containPixel({ gridIndex: gi }, pt)) xi = Math.round(chart.convertFromPixel({ gridIndex: gi }, pt)[0]);
        }
        if (p && p.dataIndex != null) xi = p.dataIndex;
        if (xi === null || xi < 0 || xi >= r.x.length) return;
        const s = p && p.seriesIndex < r.series.length ? r.series[p.seriesIndex] : null;
        const narrow = k.drill.key && s && s.drill !== false && "key" in s;
        if (!tapGate(`${k.id}|${xi}|${narrow ? s.name : ""}`)) return;
        const f = { bucket: r.x[xi], grain: r.grain };
        if (narrow) f.key = s.key;
        const what = fmtBucket(r.x[xi], r.grain) + (narrow ? ` · ${s.name}` : (k.drill.key ? ` · ${T("drill_whole_period")}` : ""));
        openDrill(k, f, what);
      }, 0));
    }
  }

  let drillState = null;
  function drillParams(extra = {}) {
    return { f: JSON.stringify(drillState.f), start: S.start, end: S.end, grain: S.grain, ...extra };
  }
  async function openDrill(k, f, what) {
    drillState = { k, f, what };
    resetDrillChrome();
    const my = drillState;
    S.charts.forEach((c) => c.dispatchAction({ type: "hideTip" })); // phones: the tapped tooltip would float over the panel
    $("drill-title").textContent = k.title;
    $("drill-sub").textContent = what;
    $("drill-summary").innerHTML = ""; $("drill-trunc").textContent = "";
    const note = k.drill.note ? T(k.drill.note) : "";
    $("drill-note").hidden = !note; $("drill-note").textContent = note;
    $("drill-body").innerHTML = `<div class="empty">${esc(T("loading"))}</div>`;
    $("drill-csv").disabled = $("drill-xlsx").disabled = true;
    $("drill-panel").hidden = false; $("drill-scrim").hidden = false;
    document.body.classList.add("drill-open");
    $("drill-close").focus();
    try {
      const d = await api(tenantUrl(`/kpis/${encodeURIComponent(k.id)}/drill`, drillParams()));
      if (drillState !== my) return; // another click replaced this one
      const [a, b] = d.range.split("..");
      const rng = `${fmtDate(a)}${a !== b ? " – " + fmtDate(b) : ""}`;
      $("drill-sub").textContent = f.bucket ? rng + what.slice(fmtBucket(f.bucket, f.grain).length) : `${what} · ${rng}`;
      const nf = new Intl.NumberFormat(S.locale);
      $("drill-summary").innerHTML = `<span class="drill-n">${esc(T("drill_rows", { n: "\u0001" })).replace("\u0001", `<b>${esc(nf.format(d.total_rows))}</b>`)}</span>`
        + (d.total_value != null ? `<span>${esc(d.total_label)} <b>${esc(fmt(d.total_value, "currency", false))}</b></span>` : "");
      if (!d.rows.length) { $("drill-body").innerHTML = `<div class="empty">${esc(T("drill_empty"))}</div>`; return; }
      $("drill-body").innerHTML = tableHtml(d.columns, d.rows);
      $("drill-body").scrollTop = 0;
      if (d.truncated) $("drill-trunc").textContent = T("drill_showing", { shown: nf.format(d.rows.length), n: nf.format(d.total_rows) });
      $("drill-csv").disabled = $("drill-xlsx").disabled = false;
    } catch (e) {
      if (drillState !== my) return;
      $("drill-body").innerHTML = `<div class="err">${esc(e.message)}</div>`;
    }
  }
  function closeDrill() {
    if ($("drill-panel").hidden) return false;
    $("drill-panel").hidden = true; $("drill-scrim").hidden = true; drillState = null;
    document.body.classList.remove("drill-open");
    return true;
  }
  $("drill-close").innerHTML = icon("close");
  $("drill-close").addEventListener("click", closeDrill);
  $("drill-scrim").addEventListener("click", closeDrill);
  ["csv", "xlsx"].forEach((fmtName) => $(`drill-${fmtName}`).addEventListener("click", () => {
    if (!drillState) return;
    if (drillState.finding) { downloadFindings(drillState.panel, fmtName, drillState.finding.rule); return; }
    downloadUrl(tenantUrl(`/kpis/${encodeURIComponent(drillState.k.id)}/drill`, drillParams({ format: fmtName })));
  }));


  // ---------------------------------------------------------------- hidden KPIs (admins)
  function renderHidden(sec) {
    const list = (S.ctx.hidden || []).filter((h) => h.section === sec.id);
    const panel = $("hidden-panel");
    if (!list.length) { panel.hidden = true; return; }
    panel.hidden = false;
    panel.innerHTML = `<details><summary>${esc(T("hidden_summary", { n: list.length }))}</summary>
      <p class="muted hidden-note">${esc(T("hidden_note"))}</p>
      <ul>${list.map((h) => `<li><b>${esc(h.title)}</b><span>${esc(T("hidden_needs", { what: h.reasons.join(", ") }))}</span></li>`).join("")}</ul></details>`;
  }

  // ---------------------------------------------------------------- phone panels: drawer + filter sheet
  function setPanel(which) { // which: "drawer" | "sheet" | null
    document.body.classList.toggle("drawer-open", which === "drawer");
    document.body.classList.toggle("sheet-open", which === "sheet");
    $("scrim").hidden = !which;
    $("menu-btn").setAttribute("aria-expanded", String(which === "drawer"));
    $("filter-btn").setAttribute("aria-expanded", String(which === "sheet"));
  }
  $("menu-btn").innerHTML = icon("menu");
  $("settings-nav-icon").innerHTML = icon("settings");
  $("up-credits-ico").innerHTML = icon("wallet"); $("up-settings-ico").innerHTML = icon("settings"); $("up-logout-ico").innerHTML = icon("logout"); $("lang-ico").innerHTML = icon("globe");
  $("drawer-close").innerHTML = icon("close");
  $("filter-btn-icon").innerHTML = icon("sliders");
  $("menu-btn").addEventListener("click", () => setPanel("drawer"));
  $("drawer-close").addEventListener("click", () => setPanel(null));
  $("filter-btn").addEventListener("click", () => setPanel("sheet"));
  $("sheet-done").addEventListener("click", () => setPanel(null));
  $("scrim").addEventListener("click", () => setPanel(null));
  $("tenant-select").addEventListener("change", () => setPanel(null));
  window.matchMedia("(max-width: 820px), (max-height: 500px) and (max-width: 1000px)").addEventListener("change", () => setPanel(null));

  // ---------------------------------------------------------------- offline / online
  let offline = false;
  function setOffline(v) {
    if (v === offline) return;
    offline = v;
    const b = $("net-banner");
    b.hidden = !v; b.textContent = v ? T("offline_msg") : "";
    document.body.classList.toggle("is-offline", v);
  }
  window.addEventListener("offline", () => setOffline(true));
  window.addEventListener("online", () => { setOffline(false); if (S.ctx && !$("app-view").hidden) loadSection(); });

  // ---------------------------------------------------------------- PWA: service worker, updates, install
  // The service worker caches only the app shell (HTML/CSS/JS/icons) and UI translations - never /api data.
  if ("serviceWorker" in navigator && (location.protocol === "https:" || location.hostname === "localhost" || location.hostname === "127.0.0.1")) {
    // v15 (Jerry 2026-09-30): a new version is applied AUTOMATICALLY — but never in the middle of something the user
    // is doing (half-typed login, drill-down panel, export dialog, filter sheet/drawer, a focused input). Then the
    // banner stays (Reload still works) and the update is applied as soon as the page is idle or goes to the
    // background. The first install also fires controllerchange via clients.claim(); only a requested update reloads.
    let refreshing = false;
    const hadPriorController = !!navigator.serviceWorker.controller;
    const busy = () => {
      const lv = $("login-view");
      if (lv && !lv.hidden && ($("login-user").value || $("login-pass").value)) return true;
      if (!$("drill-panel").hidden || !$("export-dialog").hidden) return true;
      const cl = document.body.classList;
      if (cl.contains("drill-open") || cl.contains("sheet-open") || cl.contains("drawer-open") || cl.contains("rb-modal-open")) return true;
      const a = document.activeElement;
      return !!a && ["INPUT", "SELECT", "TEXTAREA"].includes(a.tagName);
    };

    // v19: the banner's Reload button reloads now (it had lost its click handler); the page may be busy, but the
    // user asked for it
    $("update-btn").addEventListener("click", () => { refreshing = true; location.reload(); });

    navigator.serviceWorker.addEventListener("controllerchange", () => {
      if (refreshing || !hadPriorController) return;
      const doReload = () => {
        if (busy()) {
          const banner = $("update-banner");
          if (banner) banner.hidden = false;
          setTimeout(doReload, 2000);
          return;
        }
        refreshing = true;
        location.reload();
      };
      doReload();
    });

    navigator.serviceWorker.register("/sw.js").then((reg) => {
      // Check for updates on startup, tab focus, and when returning to foreground
      reg.update().catch(() => {});
      window.addEventListener("focus", () => reg.update().catch(() => {}));
      document.addEventListener("visibilitychange", () => { if (!document.hidden) reg.update().catch(() => {}); });
      // Periodic check every 5 minutes while open
      setInterval(() => reg.update().catch(() => {}), 5 * 60 * 1000);
    }).catch(() => { /* PWA is optional: the dashboard works without it */ });
  }
  let installPrompt = null;
  $("install-icon").innerHTML = icon("install");
  window.addEventListener("beforeinstallprompt", (e) => { e.preventDefault(); installPrompt = e; $("install-btn").hidden = false; });
  $("install-btn").addEventListener("click", async () => {
    if (!installPrompt) return;
    installPrompt.prompt(); await installPrompt.userChoice.catch(() => {}); installPrompt = null; $("install-btn").hidden = true;
  });
  window.addEventListener("appinstalled", () => { $("install-btn").hidden = true; });
  if (isIOS && !isStandalone()) $("ios-install-hint").hidden = false;

  // ---------------------------------------------------------------- start
  const savedTheme = store.get("vdash-theme"); if (savedTheme === "light" || savedTheme === "dark") document.documentElement.setAttribute("data-theme", savedTheme);
  const savedTrend = store.get("vdash-trend"); if (["ma", "linear", "off"].includes(savedTrend)) S.trendMode = savedTrend;
  $("theme-btn").innerHTML = icon(currentTheme() === "dark" ? "sun" : "moon");
  const nav = (navigator.language || "en").slice(0, 2).toLowerCase();
  loadLanguage(store.get("vdash-lang") || nav).catch(() => loadLanguage("en")).finally(boot);
})();
