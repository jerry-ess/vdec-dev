/* Veridex BI - number formatting and ECharts chart options (v24).
 *
 * Shared by the web page (loaded before app.js, global ``VdashCharts``) and by server-side tools in Node
 * (``require("./web/chart_options.js")``), so a chart in a report is built by exactly the same code as on screen.
 * Moved unchanged from web/app.js v23; the page passes what used to be its globals as ``env``:
 *
 *   const CH = VdashCharts.create({
 *     css: (name) => "#1f6feb",          // value of a CSS colour token (--s1..--s8, --grid, --surface-1 ...)
 *     locale: () => "de-DE",             // Intl locale of the UI language
 *     currency: () => "VND",             // tenant currency
 *     T: (key, args) => "...",           // UI text by key (with {placeholder} args)
 *     trendMode: () => "ma",             // "ma" | "linear" | "off"
 *     scope: () => "phung-kitchen",      // colour slots are kept per scope + KPI for the session
 *   });
 *   CH.buildOption(kpi, result, widthPx, marks) -> ECharts option (+ __tall/__height/__chip/__note hints for the page)
 *     marks (v42, reports only, optional): {an: [{i, k, dir, n}], focus: [label]} - numbered badges on unusual points
 *     of a trend chart; the named bars / slices of a breakdown chart stay strong, the others are drawn lighter.
 *   CH.fmt / fmtDate / fmtMonth / fmtBucket / fmtArg / signed / palette / phoneHeat
 *
 * No DOM access here: everything page-specific comes through env.
 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.VdashCharts = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  function create(env) {
    const S = { get locale() { return env.locale(); }, get trendMode() { return env.trendMode(); }, get tenant() { return env.scope(); } };
    const css = env.css, T = env.T, cur = env.currency;
    const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
    const palette = () => [1, 2, 3, 4, 5, 6, 7, 8].map((i) => css(`--s${i}`));

    // -------------------------------------------------------------- formatting (page: fmt, fmtArg)
    function fmt(v, kind, compact = true) {
      if (v === null || v === undefined || v === "" || (typeof v === "number" && !isFinite(v))) return "—";
      if (kind === "text" || kind === undefined) return String(v);
      if (kind === "date") return fmtDate(String(v).slice(0, 10));
      if (kind === "month") return fmtMonth(v);
      const n = Number(v);
      if (isNaN(n)) return String(v);
      const big = Math.abs(n) >= 10000, L = S.locale;
      try {
        switch (kind) {
          case "currency":
            return new Intl.NumberFormat(L, { style: "currency", currency: cur(), notation: compact && big ? "compact" : "standard", maximumFractionDigits: compact && big ? 2 : (Math.abs(n) < 100 ? 2 : 0) }).format(n);
          case "pct": return new Intl.NumberFormat(L, { maximumFractionDigits: 1, minimumFractionDigits: Math.abs(n) < 10 ? 1 : 0 }).format(n) + "%";
          case "int": return new Intl.NumberFormat(L, { notation: compact && big ? "compact" : "standard", maximumFractionDigits: compact && big ? 1 : 0 }).format(n);
          case "ratio": return new Intl.NumberFormat(L, { maximumFractionDigits: 2, minimumFractionDigits: 2 }).format(n);
          case "days": return new Intl.NumberFormat(L, { maximumFractionDigits: 1 }).format(n) + " d";
          case "hours": return new Intl.NumberFormat(L, { maximumFractionDigits: 1 }).format(n) + " h";
          case "score": return new Intl.NumberFormat(L, { maximumFractionDigits: 1 }).format(n);
          default: return new Intl.NumberFormat(L, { notation: compact && big ? "compact" : "standard", maximumFractionDigits: compact && big ? 1 : 2 }).format(n);
        }
      } catch (e) { return String(n); }
    }
    const fmtDate = (iso) => { const d = new Date(iso + "T00:00:00"); return isNaN(d) ? iso : d.toLocaleDateString(S.locale, { day: "numeric", month: "short", year: "numeric" }); };
    const fmtMonth = (ym) => { const d = new Date(String(ym).slice(0, 7) + "-01T00:00:00"); return isNaN(d) ? ym : d.toLocaleDateString(S.locale, { month: "short", year: "numeric" }); };
    function fmtBucket(k, grain) {
      if (/^\d{4}-Q\d$/.test(k) || /^\d{4}$/.test(k)) return k;
      if (/^\d{4}-\d{2}$/.test(k)) return fmtMonth(k);
      const d = new Date(k + "T00:00:00");
      if (isNaN(d)) return k;
      const s = d.toLocaleDateString(S.locale, { day: "numeric", month: "short" });
      return grain === "week" ? T("wk", { d: s }) : s;
    }

    const signed = (n, digits = 1) => new Intl.NumberFormat(S.locale, { maximumFractionDigits: digits, minimumFractionDigits: digits, signDisplay: "always" }).format(n);
    function fmtArg(a) {
      if (!a || a.v === null || a.v === undefined) return "—";
      switch (a.fmt) {
        case "signed_pct": return `${signed(a.v)}%`;
        case "signed_pts": return `${signed(a.v)} ${T("pts")}`;
        case "pts": return `${new Intl.NumberFormat(S.locale, { maximumFractionDigits: 1, minimumFractionDigits: 1 }).format(a.v)} ${T("pts")}`;
        case "signed_currency": {
          try { return new Intl.NumberFormat(S.locale, { style: "currency", currency: cur(), notation: Math.abs(a.v) >= 10000 ? "compact" : "standard", maximumFractionDigits: 1, signDisplay: "always" }).format(a.v); } catch (e) { return String(a.v); }
        }
        case "label": case "text": return String(a.v);
        default: return fmt(a.v, a.fmt);
      }
    }

    // ---------------------------------------------------------------- trend lines
    const MA_WINDOW = { day: 7, week: 4, month: 3, quarter: 2 };
    function linearFit(vals) {
      const pts = vals.map((v, i) => [i, v]).filter((p) => p[1] !== null && isFinite(p[1]));
      const n = pts.length; if (n < 2) return null;
      const mx = pts.reduce((a, p) => a + p[0], 0) / n, my = pts.reduce((a, p) => a + p[1], 0) / n;
      const sxx = pts.reduce((a, p) => a + (p[0] - mx) ** 2, 0), sxy = pts.reduce((a, p) => a + (p[0] - mx) * (p[1] - my), 0);
      const b = sxx ? sxy / sxx : 0;
      return { b, a: my - b * mx, mean: my };
    }
    function movingAvg(vals, w) {
      return vals.map((_, i) => {
        if (i < w - 1) return null;
        const win = vals.slice(i - w + 1, i + 1).filter((v) => v !== null);
        return win.length ? win.reduce((a, b) => a + b, 0) / win.length : null;
      });
    }
    function trendOverlay(s, grain, axisIdx) {
      const vals = s.data.map((v) => (v === null ? null : Number(v)));
      const fit = linearFit(vals); if (!fit) return null;
      const w = MA_WINDOW[grain];
      const mode = S.trendMode === "ma" && (!w || vals.length <= w) ? "linear" : S.trendMode;
      const unit = T("unit_" + grain);
      const data = mode === "ma" ? movingAvg(vals, w) : vals.map((_, i) => fit.a + fit.b * i);
      const name = mode === "ma" ? T("ma_label", { n: w, unit }) : T("linear_label");
      const pct = fit.mean ? (100 * fit.b) / Math.abs(fit.mean) : 0;
      const pctTxt = new Intl.NumberFormat(S.locale, { maximumFractionDigits: 1, minimumFractionDigits: 1, signDisplay: "always" }).format(pct) + "%";
      return {
        series: { name, type: "line", data, xAxisIndex: axisIdx, yAxisIndex: axisIdx, showSymbol: false, smooth: mode === "ma", z: 5, silent: false,
          lineStyle: { type: [6, 4], width: 2, color: css("--text-secondary") }, itemStyle: { color: css("--text-secondary") }, emphasis: { disabled: true } },
        chip: { text: T("trend_chip", { pct: pctTxt, unit: T("per_" + grain) }), tip: T("trend_tip", { what: name }) },
        fmt: s.fmt,
      };
    }

    // ---------------------------------------------------------------- ECharts options
    // Colour follows the entity, never its rank: first time a series/category is seen for a KPI it gets the next
    // palette slot and keeps it for the session (filters and language changes never repaint survivors).
    const slots = {};
    function slotFor(kpiId, key) {
      const m = (slots[S.tenant + "|" + kpiId] = slots[S.tenant + "|" + kpiId] || {});
      if (!(key in m)) m[key] = Object.keys(m).length;
      return m[key];
    }
    function base() {
      return {
        animationDuration: 300, textStyle: { fontFamily: css("--font").replace(/"/g, "'"), color: css("--text-secondary") }, // single quotes keep exported SVG valid XML
        tooltip: { backgroundColor: css("--surface-1"), borderColor: css("--border"), textStyle: { color: css("--text-primary"), fontSize: 12 }, confine: true },
      };
    }
    const axisCommon = () => ({ axisLine: { lineStyle: { color: css("--baseline") } }, axisTick: { show: false }, axisLabel: { color: css("--axis-muted"), fontSize: 11 }, splitLine: { lineStyle: { color: css("--grid"), type: "solid" } } });

    function buildOption(k, r, width, marks) {
      if (r.type === "timeseries") return tsOption(k, r, marks);
      if (r.type === "heatmap") return heatOption(k, r);
      return breakdownOption(k, r, width, marks);
    }
    // Phones: 24 hour columns -> eight 3-hour blocks (sums); cohort curve -> first 6 months
    function phoneHeat(k, r) {
      if (k.id === "hourly_heatmap") {
        const blocks = [...new Set(r.x.map((h) => Math.floor(Number(h) / 3)))].sort((a, b) => a - b);
        const bi = new Map(blocks.map((b, i) => [b, i])), acc = new Map();
        r.cells.forEach(([xi, yi, v]) => { const key = bi.get(Math.floor(Number(r.x[xi]) / 3)) + "|" + yi; acc.set(key, (acc.get(key) || 0) + v); });
        const cells = [...acc.entries()].map(([key, v]) => { const [x, y] = key.split("|").map(Number); return [x, y, v]; });
        return { ...r, x: blocks.map((b) => `${String(b * 3).padStart(2, "0")}–${String(b * 3 + 3).padStart(2, "0")}h`), cells, __note: T("heat_phone_note"), __prelabeled: true, __blocks: blocks };
      }
      if (k.id === "cohort_retention") {
        const keep = r.x.map((x, i) => [x, i]).filter(([x]) => Number(x) <= 6);
        const map = new Map(keep.map(([, i], j) => [i, j]));
        return { ...r, x: keep.map(([x]) => x), cells: r.cells.filter((c) => map.has(c[0])).map((c) => [map.get(c[0]), c[1], c[2]]), __note: T("cohort_phone_note") };
      }
      return r;
    }

    function tsOption(k, r, marks) {
      const pal = palette();
      const xLabels = r.x.map((x) => fmtBucket(x, r.grain));
      const split = r.series.some((s) => s.axis === 1); // never dual-axis: second measure gets its own panel
      const grids = split ? [{ left: 8, right: 16, top: 34, height: "50%", containLabel: true }, { left: 8, right: 16, top: "74%", bottom: 8, containLabel: true }]
        : [{ left: 8, right: 16, top: 34, bottom: 8, containLabel: true }];
      const few = r.x.length <= 2;
      const pivot = k.series.length === 1 && r.series.length >= 1 && !k.series[0].name;
      const bySeries = {};
      const series = r.series.map((s, i) => {
        const color = pal[slotFor(k.id, pivot ? "p:" + s.name : "s:" + i) % 8];
        const name = s.name || k.title;
        bySeries[name] = s.fmt || k.fmt;
        const o = { name, data: s.data, xAxisIndex: split ? s.axis : 0, yAxisIndex: split ? s.axis : 0, stack: s.stack || undefined, emphasis: { focus: "series" } };
        if (s.type === "bar") Object.assign(o, { type: "bar", barMaxWidth: 24, itemStyle: { color, borderRadius: s.stack ? 0 : [4, 4, 0, 0], borderColor: s.stack ? css("--surface-1") : undefined, borderWidth: s.stack ? 1 : 0 } });
        else Object.assign(o, { type: "line", showSymbol: few, symbolSize: 8, lineStyle: { width: 2, color }, itemStyle: { color, borderColor: css("--surface-1"), borderWidth: 2 }, areaStyle: s.type === "area" ? { color, opacity: 0.1 } : undefined, smooth: false, connectNulls: false });
        return o;
      });
      let chip = null;
      if (S.trendMode !== "off") {
        r.series.forEach((s) => {
          if (!s.trend || s.data.filter((v) => v !== null).length <= 5) return;
          const ov = trendOverlay(s, r.grain, split ? s.axis : 0);
          if (!ov) return;
          series.push(ov.series); bySeries[ov.series.name] = ov.fmt;
          chip = chip || ov.chip;
        });
      }
      const fmts = [r.series.find((s) => (split ? s.axis : 0) === 0)?.fmt || k.fmt, r.series.find((s) => s.axis === 1)?.fmt];
      const yAxes = grids.map((_, gi) => ({ type: "value", gridIndex: gi, ...axisCommon(), axisLine: { show: false }, axisLabel: { color: css("--axis-muted"), fontSize: 11, formatter: (v) => fmt(v, fmts[gi]) },
        name: split && gi === 1 ? r.series.filter((s) => s.axis === 1).map((s) => s.name).join(" / ") : "", nameTextStyle: { color: css("--text-muted"), align: "left", fontSize: 11 }, nameGap: 10 }));
      const xAxes = grids.map((_, gi) => ({ type: "category", gridIndex: gi, data: xLabels, ...axisCommon(), splitLine: { show: false }, axisLabel: { ...axisCommon().axisLabel, show: !split || gi === 1, hideOverlap: true } }));
      const legendOn = series.length > 1;
      if (!legendOn) grids[0].top = 14;
      // v42: numbered badges on the unusual points (dark disc, white number; above a high point, below a low one)
      const badges = (marks && marks.an || []).filter((m) => series[m.k] && r.series[m.k] && r.series[m.k].data[m.i] != null);
      if (badges.length) {
        grids[0].top += 16;
        const ink = css("--text-primary"), paper = css("--surface-1");
        badges.forEach((m) => {
          const s = series[m.k];
          s.markPoint = s.markPoint || { silent: true, animation: false, data: [] };
          s.markPoint.data.push({ coord: [m.i, r.series[m.k].data[m.i]], symbol: "circle", symbolSize: 20, symbolOffset: [0, m.dir === "up" ? -14 : 14],
            itemStyle: { color: ink, borderColor: paper, borderWidth: 2 }, label: { show: true, formatter: String(m.n), color: paper, fontSize: 11, fontWeight: 700 } });
        });
      }
      return {
        ...base(), __tall: split, __chip: chip, color: pal, grid: grids, xAxis: xAxes, yAxis: yAxes, series,
        axisPointer: { link: [{ xAxisIndex: "all" }] },
        legend: legendOn ? { top: 0, left: 0, icon: "roundRect", itemWidth: 12, itemHeight: 8, textStyle: { color: css("--text-secondary"), fontSize: 12 } } : undefined,
        tooltip: { ...base().tooltip, trigger: "axis", axisPointer: { type: "line", lineStyle: { color: css("--baseline") } },
          formatter: (ps) => `<b>${esc(ps[0].axisValueLabel)}</b><br>` + ps.filter((p) => p.value !== null && p.value !== undefined).map((p) => `${p.marker}${esc(p.seriesName)}: <b>${esc(fmt(p.value, bySeries[p.seriesName]))}</b>`).join("<br>") },
      };
    }

    function breakdownOption(k, r, width = 600, marks) {
      const pal = palette();
      const fset = new Set((marks && marks.focus) || []), dim = (label) => fset.size > 0 && !fset.has(label);   // v42: highlight
      let items = r.items.slice();
      let viz = k.viz;
      // v56: a donut shows 2 to 10 parts (two parts are a clear share; ten still fit with their legend); one part is a bar,
      // more than ten keep the biggest nine and group the rest as "Other"
      if (viz === "donut") {
        if (items.length < 2) viz = "hbar";
        else if (items.length > 10) { const top = items.slice(0, 9); const rest = items.slice(9).reduce((a, b) => a + (b.value || 0), 0); items = [...top, { key: "__other", label: T("other"), value: rest }]; }
      }
      if (viz === "donut") {
        const total = items.reduce((a, b) => a + (b.value || 0), 0);
        const pctOf = (v) => (total ? Math.round((100 * (v || 0)) / total) : 0);
        // Long labels (e.g. "Emerging · low volume, fast growth") never truncate: legend moves under the donut
        // and wraps; short labels sit beside it.
        const longest = Math.max(...items.map((i) => String(i.label).length));
        const below = longest > 18 || width < 560; // phones: legend always under the donut
        const legend = below
          ? { orient: "horizontal", left: 8, right: 8, bottom: 0, icon: "roundRect", itemWidth: 12, itemHeight: 8, itemGap: 14, textStyle: { color: css("--text-secondary"), fontSize: 12 } }
          : { orient: "vertical", left: "56%", top: "middle", icon: "roundRect", itemWidth: 12, itemHeight: 8, itemGap: 10, textStyle: { color: css("--text-secondary"), fontSize: 12 } };
        return {
          ...base(), color: pal, __height: below ? 300 + 22 * Math.ceil(items.length / 2) : 300,
          tooltip: { ...base().tooltip, trigger: "item", formatter: (p) => `${p.marker}${esc(p.name)}<br><b>${esc(fmt(p.value, k.fmt))}</b> (${p.percent}%)` },
          // labels that already carry their numbers (e.g. ABC classes) are shown as-is
          legend: { ...legend, formatter: (n) => { const it = items.find((i) => i.label === n); return it && it.label_key ? n : `${n}  ${pctOf(it?.value)}%`; } },
          series: [{ type: "pie", radius: below ? ["44%", "66%"] : ["50%", "74%"], center: below ? ["50%", "38%"] : ["28%", "50%"], avoidLabelOverlap: true, label: { show: false },
            itemStyle: { borderColor: css("--surface-1"), borderWidth: 2 },
            selectedMode: fset.size ? "multiple" : false, selectedOffset: 8,
            // more than 8 parts: colours go by position (the 9th and 10th repeat the first two, a little lighter) so no two neighbours match
            data: items.map((i, idx) => ({ name: i.label, value: i.value, selected: fset.has(i.label), itemStyle: { color: i.key === "__other" ? css("--axis-muted") : pal[(items.length > 8 ? idx : slotFor(k.id, i.key ?? i.label)) % 8], opacity: dim(i.label) ? 0.4 : (items.length > 8 && idx >= 8 ? 0.6 : 1) } })) }],
        };
      }
      const horizontal = viz === "hbar";
      const labels = items.map((i) => i.label), vals = items.map((i) => i.value);
      const cat = { type: "category", data: horizontal ? labels.slice().reverse() : labels, ...axisCommon(), splitLine: { show: false },
        axisLabel: { ...axisCommon().axisLabel, color: css("--text-secondary"), width: horizontal ? Math.min(170, Math.round(width * 0.36)) : Math.max(36, Math.floor((width - 70) / Math.max(items.length, 1))),
        overflow: horizontal && width >= 560 ? "break" : (horizontal ? "truncate" : "break"), hideOverlap: !horizontal && items.length > 8,
        interval: horizontal || items.length <= 8 ? 0 : "auto" } };
      const val = { type: "value", ...axisCommon(), axisLine: { show: false }, splitNumber: horizontal ? (width < 560 ? 2 : 3) : 5,
        axisLabel: { ...axisCommon().axisLabel, hideOverlap: true, formatter: (v) => fmt(v, k.fmt) } };
      const h = horizontal ? Math.max(220, items.length * 32 + 40) : undefined;
      return {
        ...base(), __height: h,
        grid: { left: 8, right: horizontal ? 76 : 12, top: 12, bottom: 8, containLabel: true },
        xAxis: horizontal ? val : cat, yAxis: horizontal ? cat : val,
        tooltip: { ...base().tooltip, trigger: "item", formatter: (p) => `${esc(p.name)}<br><b>${esc(fmt(p.value, k.fmt))}</b>` },
        series: [{ type: "bar", barMaxWidth: 24, barCategoryGap: "35%",
          data: (horizontal ? items.slice().reverse() : items).map((i) => ({ value: i.value,
            itemStyle: fset.size ? { opacity: dim(i.label) ? 0.35 : 1, borderColor: dim(i.label) ? undefined : css("--text-primary"), borderWidth: dim(i.label) ? 0 : 1.5 } : {},
            label: fset.size ? { opacity: 1 } : {} })),
          itemStyle: { color: pal[0], borderRadius: horizontal ? [0, 4, 4, 0] : [4, 4, 0, 0] },
          label: { show: horizontal || items.length <= (width < 560 ? 4 : 8), position: horizontal ? "right" : "top", color: css("--text-secondary"), fontSize: 11, formatter: (p) => fmt(p.value, k.fmt) } }],
      };
    }

    function heatOption(k, r) {
      const vals = r.cells.map((c) => c[2]);
      const isHour = k.id === "hourly_heatmap";
      const xl = r.__prelabeled ? r.x : r.x.map((x) => (isHour ? `${String(x).padStart(2, "0")}h` : `M${x}`));
      return {
        ...base(), __note: r.__note, __height: Math.max(260, r.y.length * 26 + 90),
        grid: { left: 8, right: 12, top: 8, bottom: 56, containLabel: true },
        xAxis: { type: "category", data: xl, ...axisCommon(), splitLine: { show: false }, axisLine: { show: false } },
        yAxis: { type: "category", data: r.y.map((y) => (/^\d{4}-\d{2}$/.test(y) ? fmtMonth(y) : y)), inverse: true, ...axisCommon(), splitLine: { show: false }, axisLine: { show: false } },
        visualMap: { min: Math.min(...vals), max: Math.max(...vals), calculable: false, orient: "horizontal", left: "center", bottom: 0, itemWidth: 12, itemHeight: 160,
          inRange: { color: [css("--seq-0"), css("--seq-1"), css("--seq-2"), css("--seq-3"), css("--seq-4")] }, textStyle: { color: css("--axis-muted"), fontSize: 11 }, formatter: (v) => fmt(v, k.fmt) },
        tooltip: { ...base().tooltip, formatter: (p) => `${esc(r.y[p.value[1]])} · ${esc(xl[p.value[0]])}<br><b>${esc(fmt(p.value[2], k.fmt))}</b>` },
        series: [{ type: "heatmap", data: r.cells, itemStyle: { borderColor: css("--surface-1"), borderWidth: 2, borderRadius: 3 }, emphasis: { itemStyle: { borderColor: css("--text-primary"), borderWidth: 1 } } }],
      };
    }

    return { fmt, fmtDate, fmtMonth, fmtBucket, fmtArg, signed, palette, buildOption, phoneHeat };
  }

  return { create };
});
