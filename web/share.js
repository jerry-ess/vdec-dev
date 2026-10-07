/* VDec BI - share links (v50, AI plan Step 27).
 * "Share" on a created report opens this dialog: it explains what a link allows, makes one (valid for a few days,
 * shown once), and lists the links made earlier for the report with "Stop sharing". The server keeps only a hash of
 * the secret (vdash/share_routes.py). Talks to the page only through window.VdashApp.
 */
(() => {
  "use strict";
  const A = () => window.VdashApp;
  const X = { box: null, rid: null, name: "", data: null, made: null, busy: false, err: "", copied: false };
  const su = (suffix) => A().tenantUrl(suffix);
  function stamp(ts) {
    try { return new Intl.DateTimeFormat(A().S.locale, { dateStyle: "medium", timeStyle: "short" }).format(new Date(ts * 1000)); }
    catch (e) { return new Date(ts * 1000).toLocaleString(); }
  }
  function close() { if (X.box) { X.box.remove(); X.box = null; } document.removeEventListener("keydown", onKey, true); }
  function onKey(e) { if (e.key === "Escape") { e.stopPropagation(); close(); } }

  async function refresh() {
    try { X.data = await A().api(su("/report-shares") + "&report=" + encodeURIComponent(X.rid)); X.err = ""; }
    catch (e) { X.err = e.message; }
    render();
  }
  function render() {
    if (!X.box) return;
    const { T, esc } = A();
    const days = (X.data && X.data.valid_days) || 7;
    const made = X.made
      ? `<div class="sh-made"><b>${esc(T("sh_new_title"))}</b>
          <div class="sh-url"><input readonly value="${esc(X.made.link)}" aria-label="${esc(T("sh_new_title"))}" data-sh-url>
            <button type="button" class="btn btn-primary btn-sm" data-sh-copy>${esc(T(X.copied ? "sh_copied" : "sh_copy"))}</button></div>
          <p class="muted">${esc(T("sh_until", { date: stamp(X.made.expires_at) }))} ${esc(T("sh_new_hint"))}</p></div>` : "";
    const list = ((X.data && X.data.shares) || []).map((s) => `<li><div><span>${esc(T("sh_item", { made: stamp(s.created_at), until: stamp(s.expires_at), n: s.views }))}</span>
        ${s.mine ? "" : `<span class="muted"> · ${esc(T("sh_by", { name: s.created_by }))}</span>`}</div>
        <button type="button" class="btn btn-sm" data-sh-stop="${esc(s.id)}">${esc(T("sh_stop"))}</button></li>`).join("");
    X.box.querySelector(".sh-body").innerHTML = `
      <p class="sh-warn">${esc(T("sh_warn", { days }))}</p>
      <button type="button" class="btn btn-primary" data-sh-create ${X.busy ? "disabled" : ""}>${esc(T(X.busy ? "sh_creating" : "sh_create"))}</button>
      ${X.err ? `<div class="rb-msg rs-bad" role="alert">${esc(X.err)}</div>` : ""}
      ${made}
      <h3 class="sh-h">${esc(T("sh_existing"))}</h3>
      ${list ? `<ul class="sh-list">${list}</ul>` : `<p class="muted">${esc(T("sh_none"))}</p>`}`;
  }
  function open(rid, name) {
    close();
    const { T, esc } = A();
    X.rid = rid; X.name = name || ""; X.data = null; X.made = null; X.err = ""; X.busy = false; X.copied = false;
    const m = document.createElement("div");
    m.className = "rb-modal sh-overlay"; m.setAttribute("role", "dialog"); m.setAttribute("aria-modal", "true");
    m.innerHTML = `<div class="sh-box"><div class="sh-head"><b>${esc(T("sh_title"))}</b>
        <button type="button" class="btn btn-sm" data-sh-close>${esc(T("rb_close"))}</button></div>
      ${X.name ? `<div class="muted sh-name">${esc(X.name)}</div>` : ""}<div class="sh-body"></div></div>`;
    m.addEventListener("click", async (e) => {
      if (e.target === m || e.target.closest("[data-sh-close]")) { close(); return; }
      const st = e.target.closest("[data-sh-stop]");
      if (st) {
        try { await A().api(su(`/report-shares/${encodeURIComponent(st.dataset.shStop)}`), { method: "DELETE" }); X.err = ""; }
        catch (er) { X.err = er.message; }
        await refresh(); return;
      }
      if (e.target.closest("[data-sh-create]")) {
        X.busy = true; X.err = ""; render();
        try {
          const d = await A().api(su(`/reports/mine/${encodeURIComponent(X.rid)}/share`), { method: "POST" });
          X.made = { link: d.url || (location.origin + d.path), expires_at: d.expires_at }; X.copied = false;
        } catch (er) { X.err = er.message; }
        X.busy = false; await refresh();
        const i = X.box && X.box.querySelector("[data-sh-url]"); if (i) { i.focus(); i.select(); }
        return;
      }
      if (e.target.closest("[data-sh-copy]")) {
        const i = X.box.querySelector("[data-sh-url]");
        try { await navigator.clipboard.writeText(i.value); }
        catch (er) { i.focus(); i.select(); try { document.execCommand("copy"); } catch (e2) { /* the person can copy it by hand */ } }
        X.copied = true; render();
      }
    });
    document.body.appendChild(m); X.box = m;
    document.addEventListener("keydown", onKey, true);
    render(); refresh();
    m.querySelector("[data-sh-close]").focus();
  }
  window.VdashShare = { open, close, _state: X };
})();
