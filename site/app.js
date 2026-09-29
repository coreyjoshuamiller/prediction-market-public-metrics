/* Short-Term Price Markets dashboard. Data: data/metrics.json, data/leaders.json, data/orderbook.json */
(() => {
  "use strict";

  // ---------- state ----------
  const state = {
    range: 90, gran: "day", group: "market", cls: "", measure: "notional",
    lbPlatform: "polymarket", lbWindow: "7d", obAsset: "BTC", obRes: "recent",
  };
  const PREF_KEY = "stpm-prefs";
  try { Object.assign(state, JSON.parse(localStorage.getItem(PREF_KEY) || "{}")); } catch (e) {}
  const savePrefs = () => { try { localStorage.setItem(PREF_KEY, JSON.stringify(state)); } catch (e) {} };

  let M = null, L = null, OB = null;
  const charts = {};
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];
  const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
  const MAX_SERIES = 7; // + "Other" = 8 categorical slots
  const PLATFORMS = { polymarket: "Polymarket", kalshi: "Kalshi" };
  const GROUP_LABEL = { market: "market", asset: "asset", dur: "duration", cls: "asset class", ctype: "contract type" };
  const TRADER_DIM = { market: "market", asset: "asset", dur: "duration", cls: "class", ctype: "type" };

  // ---------- formatting ----------
  const fmtCompact = (v, prefix = "") => {
    if (v == null || isNaN(v)) return "–";
    const a = Math.abs(v), s = v < 0 ? "-" : "";
    if (a >= 1e9) return `${s}${prefix}${(a / 1e9).toFixed(a >= 1e10 ? 1 : 2)}B`;
    if (a >= 1e6) return `${s}${prefix}${(a / 1e6).toFixed(a >= 1e7 ? 1 : 2)}M`;
    if (a >= 1e3) return `${s}${prefix}${(a / 1e3).toFixed(a >= 1e4 ? 0 : 1)}K`;
    return `${s}${prefix}${Math.round(a)}`;
  };
  const fmtUsd = (v) => fmtCompact(v, "$");
  const fmtInt = (v) => (v == null ? "–" : Math.round(v).toLocaleString("en-US"));
  const fmtPct = (v, d = 1) => (v == null || !isFinite(v) ? "–" : `${v.toFixed(d)}%`);
  const measureLabel = () => (state.measure === "notional" ? "Notional ($1 contracts)" : "Cash traded ($)");
  const fmtDate = (iso, gran = state.gran) => {
    const d = new Date(iso + "T00:00:00Z");
    const opt = gran === "month" ? { month: "short", year: "2-digit", timeZone: "UTC" } : { month: "short", day: "numeric", timeZone: "UTC" };
    return d.toLocaleDateString("en-US", opt);
  };

  // ---------- time bucketing ----------
  const bucketOf = (iso, gran) => {
    if (gran === "day") return iso;
    const d = new Date(iso + "T00:00:00Z");
    if (gran === "month") return iso.slice(0, 8) + "01";
    const wd = (d.getUTCDay() + 6) % 7; // Monday = 0
    d.setUTCDate(d.getUTCDate() - wd);
    return d.toISOString().slice(0, 10);
  };

  function timeFrame() {
    const days = M.days;
    const last = days.length - 1;
    let from = Math.max(0, last - state.range + 1);
    // don't start on a partial week/month: skip ahead to the first bucket boundary
    if (state.gran !== "day" && from > 0) {
      while (from < last && bucketOf(days[from], state.gran) === bucketOf(days[from - 1], state.gran)) from++;
    }
    const idx = [];
    for (let i = from; i <= last; i++) idx.push(i);
    const buckets = [], bIndex = {}, dayToBucket = {};
    idx.forEach((i) => {
      const b = bucketOf(days[i], state.gran);
      if (!(b in bIndex)) { bIndex[b] = buckets.length; buckets.push(b); }
      dayToBucket[i] = bIndex[b];
    });
    // the trailing bucket is partial if the data doesn't reach the end of that week/month
    const nextDay = new Date(days[last] + "T00:00:00Z"); nextDay.setUTCDate(nextDay.getUTCDate() + 1);
    const partialLast = state.gran !== "day" && bucketOf(nextDay.toISOString().slice(0, 10), state.gran) === buckets[buckets.length - 1];
    return { idx, buckets, dayToBucket, from, last, partialLast };
  }
  const bucketLabels = (tf) => tf.buckets.map((b, i) => fmtDate(b) + (tf.partialLast && i === tf.buckets.length - 1 ? " (partial)" : ""));
  // dim the still-running bucket so a half-finished week doesn't read as a collapse
  function markPartial(series, tf) {
    if (!tf.partialLast) return series;
    const i = tf.buckets.length - 1;
    for (const s of series) {
      if (s.type !== "bar") continue;
      const v = s.data[i];
      if (v == null) continue;
      const o = typeof v === "object" ? v : { value: v };
      o.itemStyle = { ...(o.itemStyle || {}), opacity: 0.4 };
      s.data[i] = o;
    }
    return series;
  }

  // ---------- series grouping ----------
  const keyOf = (s, group) => (group === "market" ? `${s.asset} ${s.dur}` : s[group]);
  const inClass = (s) => !state.cls || s.cls === state.cls;

  // Stable, class-aware entity order -> color slot. Color follows the entity, never its rank in the current window.
  function entityOrder(group) {
    const all = M.order[group] || [];
    if (!state.cls) return all;
    const present = new Set();
    for (const p of ["polymarket", "kalshi"]) for (const s of M[p].series) if (inClass(s)) present.add(keyOf(s, group));
    return all.filter((k) => present.has(k));
  }
  function colorFor(group) {
    const order = entityOrder(group);
    const map = {};
    order.slice(0, MAX_SERIES).forEach((k, i) => (map[k] = css(`--series-${i + 1}`)));
    return (k) => map[k] || css("--other");
  }
  const shownKeys = (group) => entityOrder(group).slice(0, MAX_SERIES);

  function aggregate(platform, tf, group) {
    const keep = new Set(shownKeys(group));
    const out = {};
    const nb = tf.buckets.length;
    for (const s of M[platform].series) {
      if (!inClass(s)) continue;
      let k = keyOf(s, group);
      if (!keep.has(k)) k = "Other";
      const arr = (out[k] ||= new Array(nb).fill(0));
      const vals = s[state.measure];
      for (const i of tf.idx) arr[tf.dayToBucket[i]] += vals[i] || 0;
    }
    return out;
  }
  function scopeTotal(platform, tf, measure = state.measure) {
    const arr = new Array(tf.buckets.length).fill(0);
    for (const s of M[platform].series) {
      if (!inClass(s)) continue;
      for (const i of tf.idx) arr[tf.dayToBucket[i]] += s[measure][i] || 0;
    }
    return arr;
  }
  function platformTotal(platform, tf, measure = state.measure) {
    const arr = new Array(tf.buckets.length).fill(0);
    const vals = M[platform].totals[measure];
    for (const i of tf.idx) arr[tf.dayToBucket[i]] += vals[i] || 0;
    return arr;
  }
  const sumRange = (arr, from, to) => { let t = 0; for (let i = from; i <= to; i++) t += arr[i] || 0; return t; };

  // ---------- chart chrome ----------
  function baseOption() {
    const text = css("--text-secondary"), muted = css("--text-muted"), grid = css("--grid"), axis = css("--axis");
    return {
      animation: false,
      textStyle: { fontFamily: 'system-ui, -apple-system, "Segoe UI", sans-serif', color: text },
      grid: { left: 8, right: 16, top: 12, bottom: 8, containLabel: true },
      tooltip: {
        trigger: "axis", confine: true,
        backgroundColor: css("--surface-1"), borderColor: css("--border"), borderWidth: 1,
        textStyle: { color: css("--text-primary"), fontSize: 12 },
        axisPointer: { type: "line", lineStyle: { color: axis, width: 1 } },
        extraCssText: "box-shadow:0 4px 16px rgba(0,0,0,.12);border-radius:8px;",
      },
      xAxis: {
        type: "category", axisLine: { lineStyle: { color: axis } }, axisTick: { show: false },
        axisLabel: { color: muted, fontSize: 11, hideOverlap: true },
      },
      yAxis: {
        type: "value", splitLine: { lineStyle: { color: grid, width: 1 } },
        axisLabel: { color: muted, fontSize: 11 }, axisLine: { show: false },
      },
    };
  }

  // Tooltip: values lead, labels follow; line keys, sorted by value; skip zeros.
  function tooltipFormatter(valueFmt, { total = true } = {}) {
    return (params) => {
      const ps = [].concat(params);
      if (!ps.length) return "";
      const title = ps[0].axisValueLabel ?? ps[0].name;
      let sum = 0;
      const rows = ps
        .filter((p) => p.value != null && p.value !== 0 && p.value !== "-")
        .sort((a, b) => b.value - a.value)
        .map((p) => {
          if (p.seriesType === "bar") sum += p.value;
          return `<div style="display:flex;align-items:center;gap:8px;min-width:170px">
            <span style="width:12px;height:2px;background:${p.color};flex:none;border-radius:1px"></span>
            <b style="font-variant-numeric:tabular-nums">${valueFmt(p.value)}</b>
            <span style="color:${css("--text-secondary")}">${escapeHtml(p.seriesName)}</span></div>`;
        });
      const tot = total && sum ? `<div style="margin-top:4px;padding-top:4px;border-top:1px solid ${css("--grid")}"><b>${valueFmt(sum)}</b> <span style="color:${css("--text-secondary")}">Total</span></div>` : "";
      return `<div style="font-size:12px;color:${css("--text-secondary")};margin-bottom:4px">${escapeHtml(title)}</div>${rows.join("")}${tot}`;
    };
  }
  const escapeHtml = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  // ---------- figure cards ----------
  function card(name, { title, note, keyColor, short } = {}) {
    const fig = $(`figure[data-chart="${name}"]`);
    if (!fig.dataset.ready) {
      fig.innerHTML = `
        <figcaption>
          <span class="title"></span>
          <span style="display:flex;gap:12px;align-items:baseline"><span class="note"></span>
          <button class="table-toggle" type="button" aria-pressed="false">Table</button></span>
        </figcaption>
        <div class="legend" role="group" aria-label="Series"></div>
        <div class="chart${short ? " short" : ""}" role="img"></div>
        <div class="table-view"></div>`;
      $(".table-toggle", fig).addEventListener("click", (e) => {
        const on = fig.classList.toggle("show-table");
        e.currentTarget.setAttribute("aria-pressed", String(on));
        e.currentTarget.textContent = on ? "Chart" : "Table";
        if (!on) charts[name]?.resize();
      });
      fig.dataset.ready = "1";
    }
    const t = $(".title", fig);
    t.textContent = "";
    if (keyColor) { const k = document.createElement("span"); k.className = "key"; k.style.background = keyColor; t.appendChild(k); }
    t.appendChild(document.createTextNode(title || ""));
    $(".note", fig).textContent = note || "";
    const el = $(".chart", fig);
    el.setAttribute("aria-label", title || name);
    return { fig, el };
  }

  function renderChart(name, cfg, option, table) {
    const { fig, el } = card(name, cfg);
    const empty = $(".empty", el);
    fig.classList.toggle("is-empty", !!cfg.empty);
    if (cfg.empty) {
      fig.classList.remove("show-table");
      charts[name]?.dispose(); delete charts[name];
      el.innerHTML = `<div class="empty">${cfg.empty}</div>`;
      $(".legend", fig).innerHTML = "";
      $(".table-view", fig).innerHTML = "";
      return;
    }
    if (empty) el.innerHTML = "";
    let ch = charts[name];
    if (!ch || ch.getDom() !== el) { ch = charts[name] = echarts.init(el, null, { renderer: "canvas" }); }
    ch.setOption(option, { notMerge: true });
    renderLegend(fig, ch, option);
    renderTable(fig, table);
  }

  function renderLegend(fig, ch, option) {
    const box = $(".legend", fig);
    box.innerHTML = "";
    const series = option.series.filter((s) => !s.noLegend);
    if (series.length < 2) return;
    for (const s of series) {
      const b = document.createElement("button");
      b.type = "button";
      b.setAttribute("aria-pressed", "true");
      const sw = document.createElement("span");
      sw.className = "key";
      sw.style.background = s.itemStyle?.color || s.lineStyle?.color;
      if (s.type === "line") { sw.style.height = "2px"; sw.style.width = "12px"; sw.style.borderRadius = "1px"; }
      b.append(sw, document.createTextNode(s.name));
      b.addEventListener("click", () => {
        const on = b.getAttribute("aria-pressed") !== "true";
        b.setAttribute("aria-pressed", String(on));
        ch.dispatchAction({ type: on ? "legendSelect" : "legendUnSelect", name: s.name });
      });
      box.appendChild(b);
    }
  }

  function renderTable(fig, table) {
    const box = $(".table-view", fig);
    box.innerHTML = "";
    if (!table) return;
    const t = document.createElement("table");
    t.className = "data";
    const thead = t.createTHead().insertRow();
    table.cols.forEach((c, i) => { const th = document.createElement("th"); th.textContent = c; if (i) th.className = "num"; thead.appendChild(th); });
    const tb = t.createTBody();
    table.rows.forEach((r) => {
      const tr = tb.insertRow();
      r.forEach((v, i) => { const td = tr.insertCell(); td.textContent = v; if (i) td.className = "num"; });
    });
    box.appendChild(t);
  }

  const barSeries = (name, data, color, stack = "s") => ({
    name, type: "bar", stack, data, barMaxWidth: 24,
    itemStyle: { color, borderColor: css("--surface-1"), borderWidth: 1, borderRadius: 0 },
    emphasis: { focus: "none", itemStyle: { opacity: 0.85 } },
  });
  const lineSeries = (name, data, color, extra = {}) => ({
    name, type: "line", data, symbol: "circle", symbolSize: 7, showSymbol: false, connectNulls: false,
    lineStyle: { width: 2, color, cap: "round", join: "round" }, itemStyle: { color, borderColor: css("--surface-1"), borderWidth: 2 },
    ...extra,
  });
  // round the data-end of the top segment of each stacked column
  function roundTops(series) {
    const n = series[0]?.data.length || 0;
    for (let i = 0; i < n; i++) {
      for (let j = series.length - 1; j >= 0; j--) {
        const v = series[j].data[i];
        if (v) { series[j].data[i] = { value: v, itemStyle: { borderRadius: [4, 4, 0, 0] } }; break; }
      }
    }
    return series;
  }
  const rawVal = (v) => (v && typeof v === "object" ? v.value : v);

  // ---------- sections ----------
  function renderTiles(tf) {
    const box = $("#tiles");
    const n = tf.buckets.length;
    // previous equal-length window for deltas
    const prevTf = { idx: [], dayToBucket: {}, buckets: [0] };
    for (let i = Math.max(0, tf.from - state.range); i < tf.from; i++) { prevTf.idx.push(i); prevTf.dayToBucket[i] = 0; }
    const hasPrev = tf.from - state.range >= 0;
    const tiles = [];
    for (const p of ["polymarket", "kalshi"]) {
      const cur = sumRange(scopeTotal(p, tf), 0, n - 1);
      const prev = hasPrev ? scopeTotal(p, prevTf)[0] : null;
      const tot = sumRange(platformTotal(p, tf), 0, n - 1);
      tiles.push({ label: `${PLATFORMS[p]} volume`, key: css(p === "polymarket" ? "--poly" : "--kalshi"), value: fmtUsd(cur), delta: prev ? (cur / prev - 1) * 100 : null });
      tiles.push({ label: `${PLATFORMS[p]} share of platform`, key: css(p === "polymarket" ? "--poly" : "--kalshi"), value: fmtPct(tot ? (cur / tot) * 100 : null) });
    }
    // Polymarket average daily unique traders in scope (total or class)
    const tr = M.polymarket.traders.day || {};
    const src = state.cls ? tr.class?.[state.cls] : tr.total?.All;
    let s = 0, c = 0;
    if (src) for (const i of tf.idx) { const v = src[M.days[i]]; if (v != null) { s += v; c++; } }
    tiles.push({ label: "Polymarket avg daily traders", key: css("--poly"), value: c ? fmtInt(s / c) : "–" });
    const pv = sumRange(scopeTotal("polymarket", tf), 0, n - 1), kv = sumRange(scopeTotal("kalshi", tf), 0, n - 1);
    tiles.push({ label: "Kalshi : Polymarket volume", value: pv ? `${(kv / pv).toFixed(1)}×` : "–" });

    box.innerHTML = "";
    for (const t of tiles) {
      const el = document.createElement("div");
      el.className = "tile";
      const lab = document.createElement("div"); lab.className = "label";
      if (t.key) { const k = document.createElement("span"); k.className = "key"; k.style.background = t.key; lab.appendChild(k); }
      lab.appendChild(document.createTextNode(t.label));
      const val = document.createElement("div"); val.className = "value"; val.textContent = t.value;
      el.append(lab, val);
      const dl = document.createElement("div"); dl.className = "delta";
      if (t.delta != null && isFinite(t.delta)) {
        dl.classList.add(t.delta >= 0 ? "up" : "down");
        dl.textContent = `${t.delta >= 0 ? "▲" : "▼"} ${Math.abs(t.delta).toFixed(1)}% vs prior ${state.range}d`;
      } else dl.textContent = `last ${state.range} days · ${state.measure}`;
      el.appendChild(dl);
      box.appendChild(el);
    }
  }

  function renderVolume(tf) {
    const color = colorFor(state.group);
    const cats = bucketLabels(tf);
    for (const p of ["polymarket", "kalshi"]) {
      const agg = aggregate(p, tf, state.group);
      const keys = [...shownKeys(state.group).filter((k) => agg[k]), ...(agg.Other ? ["Other"] : [])];
      const series = markPartial(roundTops(keys.map((k) => barSeries(k, agg[k].slice(), color(k)))), tf);
      const opt = baseOption();
      opt.xAxis.data = cats;
      opt.yAxis.axisLabel.formatter = (v) => fmtUsd(v);
      opt.tooltip.formatter = tooltipFormatter(fmtUsd);
      opt.series = series;
      const table = { cols: ["Period", ...keys, "Total"], rows: tf.buckets.map((b, i) => [b, ...keys.map((k) => fmtUsd(agg[k][i])), fmtUsd(keys.reduce((a, k) => a + agg[k][i], 0))]) };
      const empty = keys.length ? null : `<strong>No ${PLATFORMS[p]} volume</strong><span>No markets in this asset class on ${PLATFORMS[p]} for the range.</span>`;
      renderChart(`vol-${p}`, { title: PLATFORMS[p], note: measureLabel(), keyColor: css(p === "polymarket" ? "--poly" : "--kalshi"), empty }, opt, table);
    }
  }

  function renderTraders(tf) {
    const dimName = TRADER_DIM[state.group];
    const period = state.gran;
    const T = M.polymarket.traders?.[period]?.[dimName] || {};
    const color = colorFor(state.group);
    let keys = shownKeys(state.group).filter((k) => T[k]);
    // class filter can only be honoured when the split is by an entity that belongs to one class
    const classNote = state.cls && !["market", "asset", "cls"].includes(state.group) ? " · class filter not applied to this split" : "";
    if (state.cls && !["market", "asset", "cls"].includes(state.group)) keys = (M.order[state.group] || []).filter((k) => T[k]).slice(0, MAX_SERIES);
    const cats = bucketLabels(tf);
    const series = keys.map((k) => barSeries(k, tf.buckets.map((b) => T[k][b] ?? 0), color(k)));
    markPartial(roundTops(series), tf);
    const totSrc = state.cls ? M.polymarket.traders?.[period]?.class?.[state.cls] : M.polymarket.traders?.[period]?.total?.All;
    if (totSrc) series.push({ ...lineSeries("All (unique)", tf.buckets.map((b) => totSrc[b] ?? null), css("--text-secondary")), stack: null, noLegend: false });
    const opt = baseOption();
    opt.xAxis.data = cats;
    opt.yAxis.axisLabel.formatter = (v) => fmtCompact(v);
    opt.tooltip.formatter = tooltipFormatter(fmtInt, { total: false });
    opt.series = series;
    const table = { cols: ["Period", ...keys, "All (unique)"], rows: tf.buckets.map((b) => [b, ...keys.map((k) => fmtInt(T[k][b])), fmtInt(totSrc?.[b])]) };
    const hidden = entityOrder(state.group).length > MAX_SERIES && ["market", "asset"].includes(state.group) ? ` · top ${MAX_SERIES} shown` : "";
    renderChart("tr-polymarket", { title: "Polymarket", note: `Unique wallets per ${period}${hidden}${classNote}`, keyColor: css("--poly"), empty: keys.length ? null : "<strong>No trader data for this selection</strong>" }, opt, table);
    renderChart("tr-kalshi", {
      title: "Kalshi", keyColor: css("--kalshi"), note: "",
      empty: `<strong>Not published by Kalshi</strong><span>Kalshi doesn't expose account IDs on trades, so trader counts per market can't be computed from public data.</span><span>See the Kalshi leaderboard in Top traders below.</span>`,
    });
  }

  function renderShare(tf) {
    const cats = bucketLabels(tf);
    const share = {}, scope = {};
    for (const p of ["polymarket", "kalshi"]) {
      scope[p] = scopeTotal(p, tf);
      const tot = platformTotal(p, tf);
      share[p] = scope[p].map((v, i) => (tot[i] ? +((v / tot[i]) * 100).toFixed(2) : null));
    }
    const opt = baseOption();
    opt.xAxis.data = cats; opt.xAxis.boundaryGap = false;
    opt.yAxis.axisLabel.formatter = (v) => `${v}%`;
    opt.tooltip.formatter = tooltipFormatter((v) => fmtPct(v), { total: false });
    opt.series = ["polymarket", "kalshi"].map((p) => lineSeries(PLATFORMS[p], share[p], css(p === "polymarket" ? "--poly" : "--kalshi"), {
      endLabel: { show: true, formatter: (x) => fmtPct(x.value), color: css("--text-secondary"), fontSize: 11 },
    }));
    opt.grid.right = 48;
    renderChart("share", { title: "Share of platform volume", note: measureLabel() + (state.cls ? ` · ${state.cls}` : "") }, opt,
      { cols: ["Period", "Polymarket", "Kalshi"], rows: tf.buckets.map((b, i) => [b, fmtPct(share.polymarket[i]), fmtPct(share.kalshi[i])]) });

    const opt2 = baseOption();
    opt2.xAxis.data = cats; opt2.xAxis.boundaryGap = false;
    opt2.yAxis.axisLabel.formatter = (v) => fmtUsd(v);
    opt2.tooltip.formatter = tooltipFormatter(fmtUsd, { total: false });
    opt2.series = ["polymarket", "kalshi"].map((p) => lineSeries(PLATFORMS[p], scope[p], css(p === "polymarket" ? "--poly" : "--kalshi"),
      { areaStyle: { color: css(p === "polymarket" ? "--poly" : "--kalshi"), opacity: 0.08 } }));
    renderChart("totals", { title: "Short-term price volume: Polymarket vs Kalshi", note: measureLabel() }, opt2,
      { cols: ["Period", "Polymarket", "Kalshi"], rows: tf.buckets.map((b, i) => [b, fmtUsd(scope.polymarket[i]), fmtUsd(scope.kalshi[i])]) });
  }

  // ---------- leaders ----------
  const LB_WINDOWS = {
    polymarket: [["7d", "7 days"], ["30d", "30 days"]],
    kalshi: [["Crypto|monthly", "Crypto · month"], ["Crypto|all_time", "Crypto · all time"], ["Financials|monthly", "Financials · month"], ["Financials|all_time", "Financials · all time"]],
  };
  let lbSort = { key: "volume", dir: -1 };

  function renderLeaders() {
    const seg = $("#f-lb-window");
    const wins = LB_WINDOWS[state.lbPlatform];
    if (!wins.some(([v]) => v === state.lbWindow)) state.lbWindow = wins[0][0];
    seg.innerHTML = "";
    for (const [v, label] of wins) {
      const b = document.createElement("button"); b.dataset.v = v; b.textContent = label;
      b.setAttribute("aria-pressed", String(v === state.lbWindow));
      b.addEventListener("click", () => { state.lbWindow = v; savePrefs(); renderLeaders(); });
      seg.appendChild(b);
    }
    setPressed("#f-lb-platform", state.lbPlatform);
    const box = $("#leaders-table");
    box.innerHTML = "";
    const lede = $("#leaders-lede");
    if (!L) { box.innerHTML = `<div class="empty">Leaderboard data not built yet.</div>`; return; }

    let cols, rows;
    if (state.lbPlatform === "polymarket") {
      const w = L.polymarket?.[state.lbWindow];
      lede.textContent = `Wallets ranked by dollars traded in short-term price markets (all asset classes), ${w ? `${w.start} to ${w.end}` : ""}. PnL is realized on markets that resolved in the window: trade cashflows plus $1 per winning share, net of fees. “Maker %” is the share of a wallet's volume from resting orders; near 100% usually means a market maker. The X link appears when the trader has connected X to their Polymarket profile.`;
      rows = w?.rows || [];
      const withX = rows.filter((r) => r.x).length;
      lede.textContent += ` ${withX} of these ${rows.length} wallets have linked an X account.`;
      cols = [
        { k: "rank", t: "#", num: true },
        { k: "name", t: "Trader", render: traderCell },
        { k: "x", t: "X", render: (r) => r.x ? link(`https://x.com/${r.x}`, `@${r.x}`) : dash() },
        { k: "volume", t: "Volume", num: true, f: fmtUsd },
        { k: "pnl", t: "Realized PnL", num: true, f: fmtUsd, cls: (r) => (r.pnl > 0 ? "pos" : r.pnl < 0 ? "neg" : "") },
        { k: "fills", t: "Fills", num: true, f: fmtInt },
        { k: "markets", t: "Markets", num: true, f: fmtInt },
        { k: "maker_share", t: "Maker %", num: true, f: (v) => (v == null ? "–" : `${Math.round(v * 100)}%`) },
        { k: "top_market", t: "Most traded", f: (v) => v || "–" },
      ];
    } else {
      const w = L.kalshi?.[state.lbWindow];
      lede.textContent = "Kalshi doesn't publish trades by account, so this is Kalshi's own public leaderboard for the category. That includes weekly and other markets beyond the short-term ones, and only traders who opted into a public profile. Volume is in contracts. PnL is Kalshi's projected PnL, shown when the trader is also in the category's top 100 by PnL. Kalshi profiles don't expose linked X accounts.";
      rows = w?.rows || [];
      cols = [
        { k: "rank", t: "#", num: true },
        { k: "name", t: "Trader", render: (r) => link(r.url, r.name) },
        { k: "volume_contracts", t: "Volume (contracts)", num: true, f: fmtCompact },
        { k: "pnl", t: "PnL", num: true, f: fmtUsd, cls: (r) => (r.pnl > 0 ? "pos" : r.pnl < 0 ? "neg" : "") },
      ];
    }
    const sortCol = cols.find((c) => c.k === lbSort.key) ? lbSort.key : "rank";
    rows = [...rows].sort((a, b) => {
      const x = a[sortCol], y = b[sortCol];
      if (x == null) return 1; if (y == null) return -1;
      return (typeof x === "string" ? x.localeCompare(y) : x - y) * (sortCol === "rank" ? 1 : lbSort.dir);
    });
    const t = document.createElement("table"); t.className = "data";
    const hr = t.createTHead().insertRow();
    for (const c of cols) {
      const th = document.createElement("th");
      th.textContent = c.t + (sortCol === c.k && c.k !== "rank" ? (lbSort.dir < 0 ? " ↓" : " ↑") : "");
      if (c.num) th.className = "num";
      th.addEventListener("click", () => { lbSort = { key: c.k, dir: lbSort.key === c.k ? -lbSort.dir : -1 }; renderLeaders(); });
      hr.appendChild(th);
    }
    const tb = t.createTBody();
    for (const r of rows) {
      const tr = tb.insertRow();
      for (const c of cols) {
        const td = tr.insertCell();
        if (c.num) td.className = "num";
        if (c.cls) td.classList.add(...[c.cls(r)].filter(Boolean));
        if (c.render) td.appendChild(c.render(r));
        else td.textContent = c.f ? c.f(r[c.k]) : r[c.k] ?? "–";
      }
    }
    if (!rows.length) box.innerHTML = `<div class="empty">No rows for this window.</div>`;
    else box.appendChild(t);
  }
  function link(href, text) { const a = document.createElement("a"); a.href = href; a.target = "_blank"; a.rel = "noopener"; a.textContent = text; return a; }
  function dash() { const s = document.createElement("span"); s.className = "addr"; s.textContent = "–"; return s; }
  function traderCell(r) {
    const d = document.createElement("div"); d.className = "trader";
    if (r.image) { const img = document.createElement("img"); img.src = r.image; img.alt = ""; img.loading = "lazy"; img.onerror = () => img.replaceWith(Object.assign(document.createElement("span"), { className: "avatar-fallback" })); d.appendChild(img); }
    else d.appendChild(Object.assign(document.createElement("span"), { className: "avatar-fallback" }));
    const col = document.createElement("div");
    col.appendChild(link(r.url, r.name || `${r.address.slice(0, 6)}…${r.address.slice(-4)}`));
    if (r.verified) col.appendChild(document.createTextNode(" ✓"));
    if (r.name) { const a = document.createElement("div"); a.className = "addr"; a.textContent = `${r.address.slice(0, 6)}…${r.address.slice(-4)}`; col.appendChild(a); }
    d.appendChild(col);
    return d;
  }

  // ---------- liquidity ----------
  function renderLiquidity() {
    setPressed("#f-ob-asset", state.obAsset);
    setPressed("#f-ob-res", state.obRes);
    $("#ob-first").textContent = OB?.first_sample ? OB.first_sample.slice(0, 16).replace("T", " ") + " UTC" : "–";
    const block = OB?.[state.obRes] || {};
    const tsFmt = (t) => {
      const d = new Date(t * 1000);
      return state.obRes === "recent"
        ? d.toLocaleString("en-US", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit", timeZone: "UTC" })
        : d.toLocaleString("en-US", { month: "short", day: "numeric", hour: "numeric", timeZone: "UTC" });
    };
    const bidC = css("--series-1"), askC = css("--series-2");
    const pair = {};
    let yMaxTop = 0, yMaxDepth = 0;
    for (const p of ["polymarket", "kalshi"]) {
      pair[p] = block[`${p}|${state.obAsset}`];
      if (pair[p]) {
        yMaxTop = Math.max(yMaxTop, ...pair[p].bid_usd, ...pair[p].ask_usd);
        yMaxDepth = Math.max(yMaxDepth, ...pair[p].bid_depth5_usd, ...pair[p].ask_depth5_usd);
      }
    }
    const noData = `<strong>No snapshots yet</strong><span>The collector samples every minute. Charts fill in as data accumulates.</span>`;
    for (const p of ["polymarket", "kalshi"]) {
      const d = pair[p];
      for (const [kind, bidK, askK, yMax, title] of [
        ["top", "bid_usd", "ask_usd", yMaxTop, "$ at best bid / best offer"],
        ["depth", "bid_depth5_usd", "ask_depth5_usd", yMaxDepth, "$ within 5¢ of best bid / offer"],
      ]) {
        const name = `ob-${kind}-${p}`;
        if (!d) { renderChart(name, { title: `${PLATFORMS[p]}: ${title}`, keyColor: css(p === "polymarket" ? "--poly" : "--kalshi"), empty: noData }); continue; }
        const opt = baseOption();
        opt.xAxis.data = d.t.map(tsFmt); opt.xAxis.boundaryGap = false;
        opt.yAxis.max = Math.ceil(yMax * 1.05) || null;
        opt.yAxis.axisLabel.formatter = (v) => fmtUsd(v);
        opt.tooltip.formatter = tooltipFormatter(fmtUsd, { total: false });
        opt.dataZoom = [{ type: "inside" }];
        opt.series = [lineSeries("Bid (buy Up)", d[bidK], bidC), lineSeries("Offer (sell Up)", d[askK], askC)];
        renderChart(name, { title: `${PLATFORMS[p]}: ${title}`, note: `${state.obAsset} 15m · median per ${state.obRes === "recent" ? "5 min" : "hour"} · same scale`, keyColor: css(p === "polymarket" ? "--poly" : "--kalshi") }, opt,
          { cols: ["Time (UTC)", "Bid $", "Offer $"], rows: d.t.map((t, i) => [new Date(t * 1000).toISOString().slice(0, 16), fmtUsd(d[bidK][i]), fmtUsd(d[askK][i])]) });
      }
    }
    // spread, both venues on one time axis
    const times = [...new Set(["polymarket", "kalshi"].flatMap((p) => pair[p]?.t || []))].sort((a, b) => a - b);
    if (!times.length) renderChart("ob-spread", { title: "Bid–offer spread", empty: noData });
    else {
      const opt = baseOption();
      opt.xAxis.data = times.map(tsFmt); opt.xAxis.boundaryGap = false;
      opt.yAxis.axisLabel.formatter = (v) => `${v}¢`;
      opt.tooltip.formatter = tooltipFormatter((v) => `${(+v).toFixed(1)}¢`, { total: false });
      opt.dataZoom = [{ type: "inside" }];
      opt.series = ["polymarket", "kalshi"].map((p) => {
        const m = new Map((pair[p]?.t || []).map((t, i) => [t, pair[p].spread_c[i]]));
        return lineSeries(PLATFORMS[p], times.map((t) => m.get(t) ?? null), css(p === "polymarket" ? "--poly" : "--kalshi"));
      });
      renderChart("ob-spread", { title: "Bid–offer spread", note: `${state.obAsset} 15m · cents` }, opt,
        { cols: ["Time (UTC)", "Polymarket", "Kalshi"], rows: times.map((t, i) => [new Date(t * 1000).toISOString().slice(0, 16), opt.series[0].data[i] ?? "–", opt.series[1].data[i] ?? "–"]) });
    }
    // profile by minute into the 15m window
    const prof = ["polymarket", "kalshi"].map((p) => OB?.profile?.[`${p}|${state.obAsset}`]);
    if (!prof[0] && !prof[1]) renderChart("ob-profile", { title: "Liquidity through the 15-minute window", empty: noData });
    else {
      const mins = Array.from({ length: 15 }, (_, i) => i);
      const opt = baseOption();
      opt.xAxis.data = mins.map((m) => `${m}–${m + 1}m`);
      opt.xAxis.name = "minutes into window"; opt.xAxis.nameLocation = "middle"; opt.xAxis.nameGap = 28;
      opt.xAxis.nameTextStyle = { color: css("--text-muted"), fontSize: 11 };
      opt.grid.bottom = 28;
      opt.yAxis.axisLabel.formatter = (v) => fmtUsd(v);
      opt.tooltip.formatter = tooltipFormatter(fmtUsd, { total: false });
      opt.series = ["polymarket", "kalshi"].map((p, j) => {
        const d = prof[j];
        const m = new Map((d?.minute || []).map((x, i) => [x, (d.bid_depth5_usd[i] + d.ask_depth5_usd[i]) / 2]));
        return lineSeries(PLATFORMS[p], mins.map((x) => m.get(x) ?? null), css(p === "polymarket" ? "--poly" : "--kalshi"), { showSymbol: true });
      });
      renderChart("ob-profile", { title: "Liquidity through the 15-minute window", note: `${state.obAsset} · median of bid & offer depth within 5¢ · last 14d` }, opt,
        { cols: ["Minute", "Polymarket", "Kalshi"], rows: mins.map((m, i) => [`${m}`, fmtUsd(opt.series[0].data[i]), fmtUsd(opt.series[1].data[i])]) });
    }
  }

  // ---------- controls ----------
  function setPressed(sel, v) { $$(`${sel} button`).forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.v === String(v)))); }
  function bindSeg(sel, key, render, cast = (x) => x) {
    $$(`${sel} button`).forEach((b) => b.addEventListener("click", () => {
      state[key] = cast(b.dataset.v); savePrefs(); setPressed(sel, state[key]); render();
    }));
  }

  function renderAll() {
    if (!M) return;
    // coarse granularity for long ranges keeps bars legible
    const tf = timeFrame();
    setPressed("#f-range", state.range); setPressed("#f-gran", state.gran); setPressed("#f-group", state.group); setPressed("#f-measure", state.measure);
    $("#f-cls").value = state.cls;
    renderTiles(tf);
    renderVolume(tf);
    renderTraders(tf);
    renderShare(tf);
  }

  function renderNotes() {
    $("#notes").innerHTML = `
      <p><b>Scope.</b> Markets that settle on a financial price within one day: Polymarket Up/Down (5m, 15m, 1h, 4h, daily), “above X” strike ladders (hourly and daily), price-range and same-day “reach / dip to” markets; Kalshi 15-minute Up/Down plus hourly and daily above/below and range series. Covers crypto, equity indices and single stocks, commodities, and FX and Treasury yields. Weekly, monthly and longer-dated markets are excluded.</p>
      <p><b>Volume.</b> <i>Notional</i> counts contracts traded, each paying $1 if it wins. For Polymarket that's shares on the taker side of each fill; for Kalshi it's contracts from Kalshi's daily market report. <i>Cash</i> is dollars paid: the taker's USDC on Polymarket, and yes-price × contracts on Kalshi. Polymarket's own site shows roughly 2× these numbers because it counts both sides of every trade.</p>
      <p><b>Traders.</b> Unique Polymarket wallets, makers and takers, from on-chain fills. Counts use approximate distinct counting, accurate to about 2%. Kalshi doesn't publish account-level trades.</p>
      <p><b>Sources.</b> Polymarket on-chain trades and Kalshi daily reports via Dune; Polymarket profiles via the Gamma API; Kalshi series metadata and leaderboard via Kalshi's public API; order books from the Polymarket CLOB and Kalshi APIs. History refreshes daily and order-book snapshots hourly. Days are UTC; the current partial day is excluded.</p>`;
  }

  async function load() {
    const get = (u) => fetch(u, { cache: "no-cache" }).then((r) => (r.ok ? r.json() : null)).catch(() => null);
    [M, L, OB] = await Promise.all([get("data/metrics.json"), get("data/leaders.json"), get("data/orderbook.json")]);
    const upd = [M?.updated, OB?.updated].filter(Boolean).map((s) => s.replace("T", " ").slice(0, 16) + " UTC");
    $("#updated").textContent = M ? `History updated ${upd[0]}${upd[1] ? ` · order books ${upd[1]}` : ""}` : "No data yet";
    renderNotes();
    renderAll();
    renderLeaders();
    renderLiquidity();
  }

  bindSeg("#f-range", "range", renderAll, Number);
  bindSeg("#f-gran", "gran", renderAll);
  bindSeg("#f-group", "group", renderAll);
  bindSeg("#f-measure", "measure", renderAll);
  $("#f-cls").addEventListener("change", (e) => { state.cls = e.target.value; savePrefs(); renderAll(); });
  bindSeg("#f-lb-platform", "lbPlatform", renderLeaders);
  bindSeg("#f-ob-asset", "obAsset", renderLiquidity);
  bindSeg("#f-ob-res", "obRes", renderLiquidity);

  window.addEventListener("resize", () => Object.values(charts).forEach((c) => c.resize()));
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { renderAll(); renderLiquidity(); });
  load();
})();
