/* VDec BI - Talk to Data chat (Step 5).
 * One page in the Tools block: ask questions about the business in English, Vietnamese or German and read the answer
 * with its table / chart, what it is based on, next questions, copy and thumbs. Conversations are the person's own
 * (server: vdash/ask/chat_api.py), kept 90 days and deletable at any time.
 * The page decides nothing about access or numbers: every figure arrives already computed and checked by the server.
 * The answer arrives as a server-sent stream (status -> evidence -> answer -> done) read with fetch, because the
 * request is a POST. Talks to the dashboard only through window.VdashApp.
 */
(() => {
  "use strict";
  const A = () => window.VdashApp;
  const el = () => document.getElementById("talk-page");
  const LANGS = ["en", "vi", "de"];
  const LANG_NAMES = { en: "English", vi: "Tiếng Việt", de: "Deutsch" };
  const Q = { seq: 0, cfg: null, convs: [], cid: null, msgs: [], busy: false, lang: null, histOpen: false, status: "", error: "",
              started: false, charts: [], ctrl: null, reasonFor: null, copied: null, usage: null, credits: null, tenant: null, draft: "" };

  const store = {
    get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* private window: the choice is simply not remembered */ } },
  };
  const base = () => `/api/tenants/${encodeURIComponent(A().S.tenant)}/ask`;
  const T = (k, a) => A().T(k, a);
  const esc = (s) => A().esc(s);
  const num = (n) => Math.round(n || 0).toLocaleString(A().S.lang || "en");
  const when = (t) => { try { return new Date(t * 1000).toLocaleDateString(A().S.lang || "en", { dateStyle: "medium" }); } catch (e) { return ""; } };

  function pickLang() {
    const saved = store.get("vdash-talk-lang");
    const ui = (A().S.lang || "en").slice(0, 2);
    return LANGS.includes(saved) ? saved : (LANGS.includes(ui) ? ui : "en");
  }

  // ------------------------------------------------------------------------------------------------ server calls
  async function call(path, opts = {}) {
    let res;
    try { res = await fetch(base() + path, { credentials: "same-origin", headers: { "Content-Type": "application/json" }, ...opts }); }
    catch (e) { throw new Error(T("offline_msg")); }
    if (res.status === 401) { location.reload(); throw new Error("signed out"); }
    const body = (res.headers.get("content-type") || "").includes("json") ? await res.json().catch(() => ({})) : {};
    if (!res.ok) throw new Error(body.error || body.detail || `HTTP ${res.status}`);
    return body;
  }

  // ------------------------------------------------------------------------------------------------ rendering
  function disposeCharts() { Q.charts.forEach((c) => { try { c.dispose(); } catch (e) { /* already gone */ } }); Q.charts = []; }

  function evidenceHtml(items) {
    const ok = (items || []).filter((x) => x.ok && (x.title || x.period));
    if (!ok.length) return "";
    const seen = new Set(), lines = [];
    for (const x of ok) {
      const parts = [x.title, x.period].filter(Boolean);
      if (x.compare === "previous") parts.push(T("talk_compared"));
      else if (x.compare === "yoy") parts.push(T("talk_compared_yoy"));
      const line = parts.join(" · ");
      if (!seen.has(line)) { seen.add(line); lines.push(line); }
    }
    const through = ok.find((x) => x.data_through);
    const foot = through ? `<li class="muted">${esc(T("talk_data_through", { date: A().fmtDate ? A().fmtDate(through.data_through) : through.data_through }))}</li>` : "";
    return `<details class="tk-basis"><summary>${esc(T("talk_based_on"))}: ${esc(lines[0])}${lines.length > 1 ? ` +${lines.length - 1}` : ""}</summary>
      <ul>${lines.map((l) => `<li>${esc(l)}</li>`).join("")}${foot}</ul></details>`;
  }

  function tableHtml(t) {
    if (!t || !t.rows || !t.rows.length) return "";
    const cols = (t.columns || []).map((c) => (c && typeof c === "object" ? (c.label || c.key || "") : c));
    const more = t.total_rows > t.rows.length ? `<p class="muted tk-more">${esc(T("talk_table_more", { n: t.rows.length, m: t.total_rows }))}</p>` : "";
    return `<div class="tk-table"><div class="tk-table-title">${esc(t.title || "")}</div><div class="cr-scroll"><table class="cr-table">
      <thead><tr>${cols.map((c, i) => `<th${i > 0 ? ' class="num"' : ""}>${esc(c)}</th>`).join("")}</tr></thead>
      <tbody>${t.rows.map((r) => `<tr>${r.map((v, i) => `<td${i > 0 ? ' class="num"' : ""}>${esc(v)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>${more}</div>`;
  }

  function deeperQuestion(m) {
    const d = (Q.cfg && Q.cfg.deeper) || {};
    const last = (m.tools || []).slice(-1)[0] || "";
    if (["kpi_value", "compare_periods", "kpi_trend"].includes(last)) return d.metric || null;
    if (["kpi_breakdown"].includes(last)) return d.breakdown || null;
    if (["explain_change", "top_movers"].includes(last)) return d.explain || null;
    return null;
  }

  // The answer is shown in three layers: a verdict chip + the direct answer, then the detail lines (built by the server,
  // the same in every language), then notes. Older messages saved without them show their plain text.
  function pointsHtml(points) {
    const rows = []; let open = false;
    const close = () => { if (open) { rows.push("</ul>"); open = false; } };
    for (const p of points || []) {
      if (!p || !p.text) continue;
      if (p.heading) { close(); rows.push(`<div class="tk-h">${esc(p.text)}</div>`); continue; }
      if (!open) { rows.push('<ul class="tk-points">'); open = true; }
      const tone = p.tone && ["good", "bad", "flat"].includes(p.tone) ? ` tk-t-${p.tone}` : "";
      const rank = p.rank ? `<span class="tk-rank">${esc(p.rank)}</span>` : "";
      const meta = p.meta ? ` <span class="muted">(${esc(p.meta)})</span>` : "";
      const why = p.why ? `<div class="muted tk-why">${esc(p.why)}</div>` : "";
      rows.push(`<li class="${p.indent ? "tk-ind" : ""}${tone}">${rank}<span>${esc(p.text)}${meta}</span>${why}</li>`);
    }
    close();
    return rows.join("");
  }

  function answerHtml(m) {
    if (!m.headline) return `<div class="tk-text">${esc(m.content).replace(/\n/g, "<br>")}</div>`;
    const v = m.verdict && m.verdict.label ? `<span class="tk-verdict tk-v-${esc(m.verdict.tone || "flat")}">${esc(m.verdict.label)}</span>` : "";
    return `${v}<div class="tk-text tk-head">${esc(m.headline).replace(/\n/g, "<br>")}</div>${pointsHtml(m.points)}`;
  }

  function msgHtml(m, i) {
    if (m.role === "user") return `<div class="tk-msg tk-user"><div class="tk-bubble">${esc(m.content)}</div></div>`;
    const bad = m.kind === "error" || m.behavior === "error";
    const rated = m.rating === 1 ? "up" : m.rating === -1 ? "down" : "";
    const deeper = !bad ? deeperQuestion(m) : null;
    const fups = (m.follow_ups || []).filter(Boolean);
    const reasons = (Q.cfg.feedback_reasons || []);
    const showReasons = Q.reasonFor === m.id && rated !== "up";
    return `<div class="tk-msg tk-bot" data-i="${i}"><div class="tk-bubble ${bad ? "tk-bad" : ""}">
      ${answerHtml(m)}
      ${m.note ? `<div class="banner tk-note" role="status">${esc(m.note)}</div>` : ""}
      ${bad ? "" : tableHtml(m.table)}
      ${bad || !m.chart ? "" : `<div class="tk-chart" data-chart="${i}"><div class="tk-chart-title">${esc(m.chart.title || "")}</div><div class="tk-chart-box"></div></div>`}
      ${(m.caveats || []).map((c) => `<p class="muted tk-cav">${esc(c)}</p>`).join("")}
      ${bad ? "" : evidenceHtml(m.evidence)}
      <div class="tk-actions">
        <button type="button" class="tk-ic" data-copy="${i}" title="${esc(T("talk_copy"))}">${Q.copied === m.id ? esc(T("talk_copied")) : esc(T("talk_copy"))}</button>
        ${bad ? `<button type="button" class="tk-ic" data-retry="${i}">${esc(T("talk_retry"))}</button>` : `
        <span class="tk-rate" role="group" aria-label="${esc(T("talk_helpful"))}">
          <button type="button" class="tk-ic ${rated === "up" ? "on" : ""}" data-rate="1" data-id="${esc(m.id)}" aria-pressed="${rated === "up"}" title="${esc(T("talk_helpful"))}">👍</button>
          <button type="button" class="tk-ic ${rated === "down" ? "on" : ""}" data-rate="-1" data-id="${esc(m.id)}" aria-pressed="${rated === "down"}" title="${esc(T("talk_why_bad"))}">👎</button></span>
        ${m.source === "rules" && !m.limited ? `<span class="muted tk-src">${esc(T("talk_source_rules"))}</span>` : ""}`}
      </div>
      ${showReasons ? `<div class="tk-reasons"><span class="muted">${esc(T("talk_why_bad"))}</span>${reasons.map((r) =>
        `<button type="button" class="tk-chip" data-reason="${esc(r)}" data-id="${esc(m.id)}">${esc(T("talk_r_" + r))}</button>`).join("")}</div>` : ""}
      ${rated && !showReasons && Q.thanked === m.id ? `<p class="muted tk-thanks">${esc(T("talk_thanks"))}</p>` : ""}
      ${!bad && (deeper || fups.length) ? `<div class="tk-next">
        ${deeper ? `<button type="button" class="btn btn-sm tk-deeper" data-ask="${esc(deeper)}">${esc(T("talk_deeper"))}</button>` : ""}
        ${fups.length ? `<span class="muted">${esc(T("talk_followups"))}</span>${fups.map((f) => `<button type="button" class="tk-chip" data-ask="${esc(f)}">${esc(f)}</button>`).join("")}` : ""}</div>` : ""}
    </div></div>`;
  }

  function pendingHtml() {
    if (!Q.busy) return "";
    return `<div class="tk-msg tk-bot"><div class="tk-bubble tk-pending" role="status" aria-live="polite"><span class="tk-dots" aria-hidden="true"><i></i><i></i><i></i></span>${esc(Q.status || T("talk_status_understanding"))}</div></div>`;
  }

  function emptyHtml() {
    const st = (Q.cfg.starters || []);
    return `<div class="tk-empty"><div class="st-talk-ico" aria-hidden="true">${A().icon("chat")}</div><h2>${esc(T("talk_empty_title"))}</h2>
      <p class="muted">${esc(T("talk_empty_text"))}</p>
      <div class="tk-starters">${st.map((q) => `<button type="button" class="tk-chip tk-starter" data-ask="${esc(q)}">${esc(q)}</button>`).join("")}</div></div>`;
  }

  function usageLine() {
    const u = Q.usage, c = Q.credits;
    if (c && c.mode === "enforce" && !c.unlimited) return c.available > 0 ? T("talk_credits_left", { n: num(c.available) }) : "";
    if (u && u.limit != null && u.counted) return u.remaining > 0 ? T("talk_usage", { n: num(u.remaining) }) : T("talk_usage_none", { date: u.resets_on || "" });
    return "";
  }

  function sideHtml() {
    const items = Q.convs.map((c) => `<li class="${c.id === Q.cid ? "on" : ""}"><button type="button" class="tk-conv" data-open="${esc(c.id)}">
        <b>${esc(c.title || T("talk_untitled"))}</b><span class="muted">${esc(when(c.updated_at))}</span></button>
        <button type="button" class="tk-del" data-del="${esc(c.id)}" title="${esc(T("talk_delete"))}" aria-label="${esc(T("talk_delete"))}">×</button></li>`).join("");
    return `<aside class="tk-side ${Q.histOpen ? "open" : ""}" aria-label="${esc(T("talk_history"))}">
      <button type="button" class="btn btn-primary tk-new" data-new="1">+ ${esc(T("talk_new"))}</button>
      ${items ? `<ul class="tk-list">${items}</ul>` : `<p class="muted st-note">${esc(T("talk_history_empty"))}</p>`}
      <p class="muted st-note">${esc(T("talk_retention", { n: Q.cfg.history_days }))}</p>
      ${items ? `<button type="button" class="tk-link" data-delall="1">${esc(T("talk_delete_all"))}</button>` : ""}</aside>`;
  }

  function render(keepDraft = true) {
    const root = el();
    if (!root) return;
    const draft = keepDraft ? ((root.querySelector("#tk-input") || {}).value ?? Q.draft) : Q.draft;
    disposeCharts();
    if (!Q.cfg) { root.innerHTML = `<p class="muted">${esc(T("talk_loading"))}</p>`; return; }
    if (!Q.cfg.enabled) { root.innerHTML = `<div class="card st-card st-talk"><h2>${esc(T("nav_talk"))}</h2><p class="muted">${esc(T("talk_off"))}</p></div>`; return; }
    const thread = Q.msgs.length || Q.busy ? Q.msgs.map(msgHtml).join("") + pendingHtml() : emptyHtml();
    const usage = usageLine();
    root.innerHTML = `<div class="tk ${Q.histOpen ? "hist" : ""}">
      ${sideHtml()}
      <section class="tk-main">
        <div class="tk-top"><button type="button" class="btn btn-sm tk-histbtn" data-hist="1">☰ ${esc(T("talk_history"))}</button>
          <label class="tk-lang"><span class="muted">${esc(T("talk_lang"))}</span><select id="tk-lang" aria-label="${esc(T("talk_lang"))}">${LANGS.map((l) =>
            `<option value="${l}" ${l === Q.lang ? "selected" : ""}>${LANG_NAMES[l]}</option>`).join("")}</select></label></div>
        <div class="tk-thread" id="tk-thread" tabindex="-1">${thread}</div>
        ${Q.error ? `<div class="banner tk-err" role="alert">${esc(Q.error)}</div>` : ""}
        <form class="tk-form" id="tk-form"><textarea id="tk-input" rows="1" maxlength="${Q.cfg.max_question_chars}" placeholder="${esc(T("talk_placeholder"))}"
            aria-label="${esc(T("talk_placeholder"))}" ${Q.busy ? "disabled" : ""}>${esc(draft)}</textarea>
          <button type="submit" class="btn btn-primary" ${Q.busy ? "disabled" : ""}>${esc(T("talk_send"))}</button></form>
        ${usage ? `<p class="muted st-note tk-usage">${esc(usage)}</p>` : ""}
      </section></div>`;
    drawCharts();
    const th = $id("tk-thread");
    if (th) th.scrollTop = th.scrollHeight;
    autosize();
  }
  const $id = (i) => document.getElementById(i);

  function drawCharts() {
    document.querySelectorAll("#talk-page [data-chart]").forEach((box) => {
      const m = Q.msgs[+box.dataset.chart];
      if (!m || !m.chart || !m.chart.def || !window.echarts) { box.remove(); return; }
      try {
        const inner = box.querySelector(".tk-chart-box");
        const opt = A().buildOption(m.chart.def, m.chart.result, inner.clientWidth || 520);
        inner.className = "tk-chart-box chart" + (opt.__tall ? " tall" : "");
        if (opt.__height) inner.style.height = opt.__height + "px";
        delete opt.__tall; delete opt.__height; delete opt.__chip; delete opt.__keys; delete opt.__note;
        const ch = echarts.init(inner, null, { renderer: "svg" });
        ch.setOption(opt);
        Q.charts.push(ch);
      } catch (e) { box.remove(); }                       // a chart that cannot be drawn never hides the answer
    });
  }

  function autosize() {
    const t = $id("tk-input");
    if (!t) return;
    t.style.height = "auto";
    t.style.height = Math.min(t.scrollHeight, 140) + "px";
  }

  // ------------------------------------------------------------------------------------------------ asking
  function fromServer(m) {
    return { id: m.id, role: m.role, content: m.content, behavior: m.behavior, source: m.source, limited: m.limited,
             rating: m.rating, table: m.table, chart: m.chart, follow_ups: m.follow_ups, caveats: m.caveats, evidence: m.evidence,
             note: m.note, kind: m.kind, tools: m.tools, headline: m.headline, points: m.points, verdict: m.verdict };
  }

  async function ask(question, opts = {}) {
    question = (question || "").trim();
    if (!question || Q.busy) return;
    Q.busy = true; Q.error = ""; Q.status = T("talk_status_understanding"); Q.reasonFor = null; Q.draft = "";
    if (!opts.retry) Q.msgs.push({ role: "user", content: question });
    render(false);
    const seq = ++Q.seq;
    Q.started = false;
    Q.ctrl = new AbortController();
    try {
      const res = await fetch(base(), { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
        body: JSON.stringify({ question, lang: Q.lang, ...(Q.cid ? { conversation_id: Q.cid } : {}) }), signal: Q.ctrl.signal });
      if (res.status === 401) { location.reload(); return; }
      if (!res.ok) {
        const body = (res.headers.get("content-type") || "").includes("json") ? await res.json().catch(() => ({})) : {};
        throw new Error(body.error || body.detail || `HTTP ${res.status}`);
      }
      await readStream(res, seq);
    } catch (e) {
      if (e.name === "AbortError" || seq !== Q.seq) return;
      Q.error = e.message && !/^HTTP \d+$/.test(e.message) ? e.message : T("talk_error");
      if (!Q.started && !opts.retry && Q.msgs.length && Q.msgs[Q.msgs.length - 1].role === "user") { Q.msgs.pop(); Q.draft = question; }   // refused before anything was stored: give the text back
    } finally {
      if (seq === Q.seq) { Q.busy = false; Q.status = ""; Q.ctrl = null; render(false); const i = $id("tk-input"); if (i && !i.disabled) i.focus({ preventScroll: true }); }
    }
    refreshList();
  }

  async function readStream(res, seq) {
    const reader = res.body.getReader(), dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let ix;
      while ((ix = buf.indexOf("\n\n")) >= 0) {
        const block = buf.slice(0, ix); buf = buf.slice(ix + 2);
        const ev = (block.match(/^event: (.+)$/m) || [])[1], data = (block.match(/^data: (.+)$/m) || [])[1];
        if (ev && data && seq === Q.seq) onEvent(ev, JSON.parse(data));
      }
    }
  }

  function onEvent(ev, d) {
    if (ev === "start") { Q.cid = d.conversation_id; Q.started = true; return; }
    if (ev === "status") { Q.status = T("talk_status_" + d.stage); const p = document.querySelector(".tk-pending"); if (p) p.lastChild.textContent = Q.status; return; }
    if (ev === "answer") {
      Q.msgs.push({ id: d.id, role: "assistant", content: d.text, behavior: d.behavior, source: d.source, limited: d.limited, table: d.table, chart: d.chart,
                    follow_ups: d.follow_ups, caveats: d.caveats, evidence: d.evidence, note: d.note, kind: d.kind, tools: d.tools, headline: d.headline, points: d.points, verdict: d.verdict });
      Q.busy = false; Q.status = ""; render(false); return;
    }
    if (ev === "done") { if (d.usage) Q.usage = d.usage; if (d.credits) Q.credits = d.credits; const u = document.querySelector(".tk-usage"); if (u) render(); return; }
    if (ev === "error") { Q.error = d.message || T("talk_error"); }
  }

  async function refreshList() {
    try { Q.convs = (await call("/conversations")).conversations || []; if (!Q.busy) render(); } catch (e) { /* the list is a convenience */ }
  }

  async function openConv(cid) {
    try {
      const d = await call(`/conversations/${encodeURIComponent(cid)}`);
      Q.cid = cid; Q.msgs = d.messages.map(fromServer); Q.error = ""; Q.histOpen = false; Q.reasonFor = null;
      if (LANGS.includes(d.conversation.lang)) { Q.lang = d.conversation.lang; }
      render(false);
    } catch (e) { Q.error = e.message; render(); }
  }

  function newChat() { if (Q.busy) return; Q.cid = null; Q.msgs = []; Q.error = ""; Q.histOpen = false; Q.reasonFor = null; Q.draft = ""; render(false); const i = $id("tk-input"); if (i) i.focus(); }

  async function rate(id, rating, reason) {
    const m = Q.msgs.find((x) => x.id === id);
    if (!m) return;
    m.rating = rating;
    Q.reasonFor = rating === -1 && !reason ? id : null;
    Q.thanked = rating === 1 || reason ? id : null;
    render();
    try { await call(`/messages/${encodeURIComponent(id)}/feedback`, { method: "POST", body: JSON.stringify({ rating, ...(reason ? { reason } : {}) }) }); }
    catch (e) { m.rating = null; Q.error = e.message; render(); }
  }

  // ------------------------------------------------------------------------------------------------ events
  function wire() {
    const root = el();
    if (!root || root.dataset.wired) return;
    root.dataset.wired = "1";
    root.addEventListener("click", async (e) => {
      const t = e.target.closest("button");
      if (!t) return;
      if (t.dataset.ask) return ask(t.dataset.ask);
      if (t.dataset.new) return newChat();
      if (t.dataset.hist) { Q.histOpen = !Q.histOpen; return render(); }
      if (t.dataset.open) return openConv(t.dataset.open);
      if (t.dataset.del) {
        if (!confirm(T("talk_delete_confirm"))) return;
        try { await call(`/conversations/${encodeURIComponent(t.dataset.del)}`, { method: "DELETE" }); } catch (err) { Q.error = err.message; return render(); }
        if (Q.cid === t.dataset.del) { Q.cid = null; Q.msgs = []; }
        return refreshList();
      }
      if (t.dataset.delall) {
        if (!confirm(T("talk_delete_all_confirm"))) return;
        try { await call("/conversations", { method: "DELETE" }); } catch (err) { Q.error = err.message; return render(); }
        Q.cid = null; Q.msgs = []; return refreshList();
      }
      if (t.dataset.copy) {
        const m = Q.msgs[+t.dataset.copy];
        try { await navigator.clipboard.writeText(m.content); Q.copied = m.id; render(); setTimeout(() => { if (Q.copied === m.id) { Q.copied = null; render(); } }, 1500); } catch (err) { /* clipboard blocked: nothing to do */ }
        return;
      }
      if (t.dataset.retry) {
        const i = +t.dataset.retry;
        const prev = Q.msgs.slice(0, i).reverse().find((x) => x.role === "user");
        if (prev) { Q.msgs.splice(i, 1); return ask(prev.content, { retry: true }); }
        return;
      }
      if (t.dataset.rate) return rate(t.dataset.id, +t.dataset.rate);
      if (t.dataset.reason) return rate(t.dataset.id, -1, t.dataset.reason);
    });
    root.addEventListener("submit", (e) => { if (e.target.id === "tk-form") { e.preventDefault(); const i = $id("tk-input"); ask(i.value); } });
    root.addEventListener("keydown", (e) => { if (e.target.id === "tk-input" && e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); ask(e.target.value); } });
    root.addEventListener("input", (e) => { if (e.target.id === "tk-input") { Q.draft = e.target.value; autosize(); } });
    root.addEventListener("change", (e) => {
      if (e.target.id === "tk-lang") { Q.lang = e.target.value; store.set("vdash-talk-lang", Q.lang); loadConfig().then(() => render()); }
    });
  }

  async function loadConfig() {
    try { const c = await call(`/config?lang=${encodeURIComponent(Q.lang)}`); Q.cfg = c; Q.usage = c.usage; Q.credits = c.credits; }
    catch (e) { Q.cfg = Q.cfg || { enabled: true, starters: [], feedback_reasons: [], history_days: 90, max_question_chars: 400, deeper: {} }; Q.error = e.message; }
  }

  // ------------------------------------------------------------------------------------------------ page entry
  async function load() {
    wire();
    const seq = ++Q.seq;
    if (Q.tenant !== A().S.tenant) { Q.tenant = A().S.tenant; Q.cid = null; Q.msgs = []; Q.convs = []; Q.cfg = null; Q.error = ""; }
    Q.lang = Q.lang || pickLang();
    if (!Q.cfg) render();
    await loadConfig();
    if (seq !== Q.seq) return;
    try { Q.convs = (await call("/conversations")).conversations || []; } catch (e) { Q.convs = []; }
    if (seq !== Q.seq) return;
    render();
  }
  function hide() { ++Q.seq; if (Q.ctrl) { try { Q.ctrl.abort(); } catch (e) { /* done */ } } Q.busy = false; Q.status = ""; disposeCharts(); }

  window.VdashTalk = { load, hide };
})();
