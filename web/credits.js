/* VDec BI - AI credits (v83).
 * One page in the Tools block: the business's AI credit balance, what used the credits (by feature and by person),
 * the recent AI actions, top-up packs (prices in the currency of the person's language, changeable) and - for platform
 * admins - every business with its balance, an adjust form and the top-up orders waiting for confirmation.
 * The page keeps no data and makes no access decisions: the server decides what each role may see (vdash/ai_credits_api.py).
 * Talks to the page only through window.VdashApp.
 */
(() => {
  "use strict";
  const A = () => window.VdashApp;
  const el = () => document.getElementById("credits-page");
  const Q = { seq: 0, amount: 0, data: null, calls: null, admin: null, currency: null, msg: "", msgKind: "", order: null };
  const isAdmin = () => !!(A().S.me && A().S.me.role === "platform_admin");
  const num = (n) => { const { S } = A(); return Math.round(n || 0).toLocaleString(S.lang || "en", { maximumFractionDigits: 0 }); };   // v84: credits are whole numbers
  const when = (t) => { const { S } = A(); try { return new Date(t * 1000).toLocaleString(S.lang || "en", { dateStyle: "medium", timeStyle: "short" }); } catch (e) { return ""; } };
  const day = (t) => { const { S } = A(); try { return new Date(t * 1000).toLocaleDateString(S.lang || "en", { dateStyle: "medium" }); } catch (e) { return ""; } };
  const featName = (f) => { const { T } = A(); const k = "ai_feature_" + f; const v = T(k); return v === k ? f : v; };
  const chargeName = (c) => { const { T } = A(); const k = "ai_charge_" + c; const v = T(k); return v === k ? (c || "") : v; };
  const money = (cur, v, cfg) => { const d = (cfg && cfg[cur] && cfg[cur].decimals) || 0; const s = (cfg && cfg[cur] && cfg[cur].symbol) || ""; return `${s}${Number(v).toLocaleString(A().S.lang || "en", { minimumFractionDigits: d, maximumFractionDigits: d })}`; };

  function table(head, rows) {
    const { esc } = A();
    if (!rows.length) return `<p class="muted st-note">${esc(A().T("ai_none"))}</p>`;
    return `<div class="cr-scroll"><table class="cr-table"><thead><tr>${head.map((h, i) => `<th${i > 0 && h.num ? ' class="num"' : ""}>${esc(h.t)}</th>`).join("")}</tr></thead><tbody>${rows.join("")}</tbody></table></div>`;
  }

  function balanceCard(d) {
    const { T, esc } = A(), b = d.balance;
    const pct = Math.max(0, Math.min(100, d.percent == null ? 0 : d.percent)), lv = d.level && d.level !== "none" ? d.level : "green";
    const alert = d.alert !== "ok" ? `<div class="banner cr-alert cr-${esc(d.alert)}" role="status">${esc(T("ai_alert_" + d.alert))}</div>` : "";
    const modeNote = d.mode === "shadow" ? T("ai_mode_shadow") : d.mode === "off" ? T("ai_mode_off") : "";
    const notes = [d.unlimited ? T("ai_unlimited") : "", d.exempt ? T("ai_exempt") : "", modeNote].filter(Boolean).map((x) => `<p class="muted st-note">${esc(x)}</p>`).join("");
    const line = (l, v) => `<div class="cr-line"><span>${esc(l)}</span><b>${esc(v)}</b></div>`;
    const showBal = d.mode === "enforce" && !d.unlimited;
    return `<section class="card st-card cr-balance" aria-labelledby="cr-h-bal"><h2 id="cr-h-bal">${esc(T("nav_ai"))}</h2>${alert}
      ${showBal ? `<div class="cr-big"><span class="cr-num lv-${lv}">${esc(num(b.available))}</span><span class="muted">${esc(T("ai_balance"))}</span></div>
      <div class="cr-meter lv-${lv}" role="img" aria-label="${esc(T("ai_balance"))}: ${pct}%"><i style="width:${pct}%"></i></div>
      <div class="cr-level"><span class="cr-pill lv-${lv}">${esc(T("ai_level_" + lv))}</span><span class="muted">${esc(T("ai_pct_left", { n: pct }))}</span></div>
      ${line(T("ai_monthly"), num(b.monthly))}${line(T("ai_topup"), num(b.topup))}` : ""}
      ${line(T("ai_used_month"), num(b.used_this_month))}
      ${showBal ? `<p class="muted st-note">${esc(T("ai_resets", { date: day(b.resets_at) }))} · ${esc(T("ai_never_expire"))}</p>` : ""}${notes}</section>`;
  }

  function usageCards(d) {
    const { T, esc } = A();
    const f = table([{ t: T("ai_col_what") }, { t: T("ai_col_calls"), num: 1 }, { t: T("ai_col_credits"), num: 1 }],
      (d.by_feature || []).map((r) => `<tr><td>${esc(featName(r.key))}</td><td class="num">${r.calls}</td><td class="num">${esc(num(r.credits))}</td></tr>`));
    const p = table([{ t: T("ai_col_who") }, { t: T("ai_col_calls"), num: 1 }, { t: T("ai_col_credits"), num: 1 }],
      (d.by_user || []).map((r) => `<tr><td>${esc(r.key || "-")}</td><td class="num">${r.calls}</td><td class="num">${esc(num(r.credits))}</td></tr>`));
    if (d.scope === "own") return `<section class="card st-card"><h2>${esc(T("ai_my_use"))}</h2>${f}</section>`;   // everyone but the owner sees only their own use
    return `<div class="st-grid"><section class="card st-card"><h2>${esc(T("ai_by_feature"))}</h2>${f}</section>
      <section class="card st-card"><h2>${esc(T("ai_by_person"))}</h2>${p}</section></div>`;
  }

  function recentCard() {
    const { T, esc } = A();
    const rows = ((Q.calls && Q.calls.calls) || []).map((c) => `<tr><td>${esc(when(c.at))}</td><td>${esc(c.user || "-")}</td><td>${esc(featName(c.feature))}</td>
      <td>${esc(chargeName(c.charge))}</td><td class="num">${esc(num(c.credits))}</td></tr>`);
    return `<section class="card st-card"><h2>${esc(T("ai_recent"))}</h2>
      ${table([{ t: T("ai_col_when") }, { t: T("ai_col_who") }, { t: T("ai_col_what") }, { t: T("ai_col_status") }, { t: T("ai_col_credits"), num: 1 }], rows)}
      <div class="st-actions"><button type="button" class="btn" data-cr="csv">${esc(T("ai_export"))}</button></div></section>`;
  }

  // v87: the owner chooses the amount (min / step / max come from config/ai_credits.json -> top_up); the price is shown live
  const tuOf = () => (Q.data && Q.data.top_up) || { min: 1000, step: 500, max: 50000, quick_picks: [], price_per_1000: {} };
  const curNow = () => Q.currency || (Q.data && Q.data.default_currency) || "USD";
  function normAmount(n) {
    const t = tuOf(); n = Number(n);
    if (!Number.isFinite(n)) n = t.min;
    return Math.max(t.min, Math.min(t.max, Math.round(n / t.step) * t.step));
  }
  function priceText(credits) {
    const d = Q.data, cur = curNow(), rate = (tuOf().price_per_1000 || {})[cur], dec = ((d.currencies || {})[cur] || {}).decimals;
    const f = 10 ** (dec == null ? 2 : dec), v = Math.round(credits / 1000 * rate * f + 1e-9) / f;
    return money(cur, v, d.currencies);
  }
  function syncAmount(fromInput) {
    const { T } = A(), box = el(); if (!box || !Q.data) return;
    box.querySelectorAll("[data-cr-amt]").forEach((i) => { if (i !== fromInput) i.value = String(Q.amount); });
    const tot = box.querySelector("[data-cr-total]");
    if (tot) tot.textContent = T("ai_topup_total", { credits: num(Q.amount), price: priceText(Q.amount) });
    box.querySelectorAll("[data-cr=pick]").forEach((c) => c.setAttribute("aria-pressed", String(Number(c.dataset.n) === Q.amount)));
  }
  function topUpCard(d) {
    if (!d.can_top_up || !d.top_up) return "";
    const { T, esc } = A(), t = d.top_up, cur = curNow(), curs = Object.keys(d.currencies || {});
    if (!Q.amount) Q.amount = normAmount((t.quick_picks || [])[1] || t.min);
    Q.amount = normAmount(Q.amount);
    const chips = (t.quick_picks || []).map((n) => `<button type="button" class="btn btn-sm cr-chip" data-cr="pick" data-n="${n}" aria-pressed="${n === Q.amount}">${esc(num(n))}</button>`).join("");
    const o = Q.order ? `<div class="banner cr-order" role="status"><b>${esc(T("ai_order_ref"))}: ${esc(Q.order.order.ref)}</b> · ${esc(money(Q.order.order.currency, Q.order.order.price, d.currencies))}<br>${esc(Q.order.payment_note || "")}</div>` : "";
    const orders = (d.orders || []).length ? `<h3 class="cr-h3">${esc(T("ai_orders"))}</h3>` + table([{ t: T("ai_order_ref") }, { t: T("ai_col_credits"), num: 1 }, { t: T("ai_col_status") }],
      d.orders.map((x) => `<tr><td>${esc(x.ref)}</td><td class="num">${esc(num(x.credits))}</td><td>${esc(T("ai_order_" + x.status))}</td></tr>`)) : "";
    return `<section class="card st-card"><h2>${esc(T("ai_top_up_title"))}</h2><p class="muted st-sub">${esc(T("ai_top_up_help"))}</p>
      <div class="cr-cur"><label>${esc(T("ai_currency"))} <select data-cr-cur>${curs.map((c) => `<option value="${esc(c)}"${c === cur ? " selected" : ""}>${esc(c)}</option>`).join("")}</select></label></div>
      <div class="cr-amt">
        <label class="cr-amt-in">${esc(T("ai_topup_amount"))}<input type="number" inputmode="numeric" data-cr-amt min="${t.min}" max="${t.max}" step="${t.step}" value="${Q.amount}"></label>
        <input type="range" class="cr-range" data-cr-amt min="${t.min}" max="${t.max}" step="${t.step}" value="${Q.amount}" aria-label="${esc(T("ai_topup_amount"))}">
        <div class="cr-chips">${chips}</div>
        <p class="muted st-note">${esc(T("ai_topup_range", { min: num(t.min), max: num(t.max), step: num(t.step) }))}</p>
        <div class="cr-total"><b data-cr-total>${esc(T("ai_topup_total", { credits: num(Q.amount), price: priceText(Q.amount) }))}</b>
          <button type="button" class="btn btn-primary" data-cr="topup">${esc(T("ai_request"))}</button></div>
      </div>${d.placeholder_prices ? `<p class="muted st-note">${esc(T("ai_placeholder_prices"))}</p>` : ""}${o}${orders}</section>`;
  }

  function adminCard() {
    if (!isAdmin() || !Q.admin) return "";
    const { T, esc } = A(), a = Q.admin;
    const rows = a.tenants.map((t) => `<tr><td>${esc(t.name)}<div class="muted st-note">${esc(t.tenant_id)}</div></td><td>${esc(t.plan)}${t.exempt ? " ·" : ""}${t.exempt ? ` <span class="muted">${esc(T("ai_charge_exempt"))}</span>` : ""}</td>
      <td class="num">${t.unlimited ? "∞" : `<span class="cr-st lv-${esc(t.level || "green")}" title="${esc(T("ai_level_" + (t.level || "green")))}: ${t.percent == null ? "" : t.percent}%"></span>${esc(num(t.available))}`}</td><td class="num">${esc(num(t.used_this_month))}</td><td class="num">${esc(t.cost_usd_this_month.toFixed(4))}</td>
      <td><button type="button" class="btn btn-sm" data-cr="adj-pick" data-tid="${esc(t.tenant_id)}">${esc(T("ai_admin_adjust"))}</button></td></tr>`);
    const orders = (a.pending_orders || []).map((o) => `<tr><td>${esc(o.ref)}</td><td>${esc(o.tenant_id)}</td><td class="num">${esc(num(o.credits_mc / 1000, 0))}</td><td>${esc(o.currency)} ${esc(String(o.price))}</td>
      <td><button type="button" class="btn btn-primary btn-sm" data-cr="ord-ok" data-oid="${esc(o.id)}">${esc(T("ai_admin_confirm"))}</button>
      <button type="button" class="btn btn-sm" data-cr="ord-no" data-oid="${esc(o.id)}">${esc(T("ai_admin_cancel"))}</button></td></tr>`);
    const warn = (a.placeholder_plans || []).length ? `<p class="muted st-note">${esc(T("ai_admin_placeholder_plans", { plans: a.placeholder_plans.join(", ") }))}</p>` : "";
    return `<section class="card st-card"><h2>${esc(T("ai_admin_title"))}</h2><p class="muted st-sub">mode: <b>${esc(a.mode)}</b> · markup ×${esc(String(a.markup))} · 1 credit = $${esc(String(a.credit_value_usd))}</p>${warn}
      ${table([{ t: T("ai_admin_col_business") }, { t: T("ai_admin_col_plan") }, { t: T("ai_admin_col_balance"), num: 1 }, { t: T("ai_admin_col_used"), num: 1 }, { t: T("ai_admin_col_cost"), num: 1 }, { t: "" }], rows)}
      <form class="cr-adjust" data-cr-form><select name="tid">${a.tenants.map((t) => `<option value="${esc(t.tenant_id)}"${t.tenant_id === Q.adjTid ? " selected" : ""}>${esc(t.name)}</option>`).join("")}</select>
        <input name="credits" type="number" step="1" required placeholder="${esc(T("ai_admin_adjust_prompt"))}" aria-label="${esc(T("ai_admin_adjust_prompt"))}">
        <select name="kind"><option value="adjustment">adjustment</option><option value="promo">promo</option><option value="top_up">top_up</option></select>
        <input name="note" type="text" minlength="3" maxlength="300" required placeholder="${esc(T("ai_admin_note_prompt"))}" aria-label="${esc(T("ai_admin_note_prompt"))}">
        <button type="submit" class="btn btn-primary">${esc(T("ai_admin_adjust"))}</button></form>
      ${orders.length ? `<h3 class="cr-h3">${esc(T("ai_admin_orders"))}</h3>` + table([{ t: T("ai_order_ref") }, { t: T("ai_admin_col_business") }, { t: T("ai_col_credits"), num: 1 }, { t: "" }, { t: "" }], orders) : ""}</section>`;
  }

  function render() {
    const box = el(); if (!box) return;
    const { T, esc } = A();
    if (!Q.data) { box.innerHTML = `<p class="muted">${esc(Q.msg || T("ai_loading"))}</p>`; return; }
    const msg = Q.msg ? `<p class="st-status ${Q.msgKind}" role="status">${esc(Q.msg)}</p>` : "";
    box.innerHTML = balanceCard(Q.data) + msg + usageCards(Q.data) + topUpCard(Q.data) + recentCard() + adminCard();
  }

  async function load() {
    const box = el(); if (!box) return;
    box.hidden = false; const seq = ++Q.seq; Q.msg = ""; Q.msgKind = ""; render();
    const { api, tenantUrl, T } = A();
    try {
      const [d, c] = await Promise.all([api(tenantUrl("/ai-credits")), api(tenantUrl("/ai-credits/calls", { limit: 30 }))]);
      let a = null;
      if (isAdmin()) { try { a = await api("/api/admin/ai-credits"); } catch (e) { a = null; } }
      if (seq !== Q.seq) return;
      Q.data = d; Q.calls = c; Q.admin = a; Q.currency = Q.currency || d.default_currency;
      if (A().setCredits) A().setCredits({ mode: d.mode, unlimited: d.unlimited, exempt: d.exempt, available: d.balance.available, level: d.level, percent: d.percent });
    } catch (e) { if (seq !== Q.seq) return; Q.data = null; Q.msg = T("ai_error"); }
    render();
  }

  function say(text, kind) { Q.msg = text; Q.msgKind = kind || ""; render(); }

  document.addEventListener("change", (e) => {
    const s = e.target.closest("[data-cr-cur]"); if (!s || !el() || !el().contains(s)) return;
    Q.currency = s.value; render();
  });
  document.addEventListener("input", (e) => {          // typing or sliding: the price follows at once
    const i = e.target.closest("[data-cr-amt]"); if (!i || !el() || !el().contains(i)) return;
    const n = Number(i.value); if (!Number.isFinite(n) || n <= 0) return;
    Q.amount = Math.max(1, n); syncAmount(i);
  });
  document.addEventListener("change", (e) => {         // leaving the field: snap to the allowed range and step
    const i = e.target.closest("[data-cr-amt]"); if (!i || !el() || !el().contains(i)) return;
    Q.amount = normAmount(i.value); syncAmount();
  });
  document.addEventListener("click", async (e) => {
    const b = e.target.closest("[data-cr]"); if (!b || !el() || !el().contains(b)) return;
    const { api, tenantUrl, T, S, downloadUrl } = A(), act = b.dataset.cr;
    try {
      if (act === "csv") return downloadUrl(tenantUrl("/ai-credits/calls", { format: "csv", limit: 20000 }));
      if (act === "pick") { Q.amount = normAmount(b.dataset.n); syncAmount(); return; }
      if (act === "topup") {
        Q.amount = normAmount(Q.amount); syncAmount();
        Q.order = await api(tenantUrl("/ai-credits/top-up"), { method: "POST", body: JSON.stringify({ credits: Q.amount, currency: curNow() }) });
        return load().then(() => { render(); });
      }
      if (act === "adj-pick") { Q.adjTid = b.dataset.tid; render(); const f = el().querySelector("[data-cr-form] input[name=credits]"); if (f) f.focus(); return; }
      if (act === "ord-ok" || act === "ord-no") {
        await api(`/api/admin/ai-credits/orders/${encodeURIComponent(b.dataset.oid)}/${act === "ord-ok" ? "confirm" : "cancel"}`, { method: "POST" });
        await load(); return say(T("ai_admin_done"), "ok");
      }
    } catch (err) { say(String((err && err.message) || T("ai_error")), "bad"); }
  });
  document.addEventListener("submit", async (e) => {
    const f = e.target.closest("[data-cr-form]"); if (!f || !el() || !el().contains(f)) return;
    e.preventDefault();
    const { api, T } = A(), v = Object.fromEntries(new FormData(f).entries());
    try {
      const r = await api(`/api/admin/ai-credits/${encodeURIComponent(v.tid)}/adjust`, { method: "POST", body: JSON.stringify({ credits: Number(v.credits), kind: v.kind, note: v.note }) });
      await load(); say(`${T("ai_admin_done")}: ${num(r.changed)} → ${num(r.balance)}`, "ok");
    } catch (err) { say(String((err && err.message) || T("ai_error")), "bad"); }
  });

  window.VdashCredits = {
    load,
    hide() { const b = el(); if (b) b.hidden = true; },
    reset() { Q.amount = 0; Q.data = null; Q.calls = null; Q.admin = null; Q.order = null; Q.currency = null; ++Q.seq; },
  };
})();
