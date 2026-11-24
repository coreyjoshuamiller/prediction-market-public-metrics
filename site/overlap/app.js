/* Prediction markets x perps: trader overlap. Data: data/overlap.json (pipeline/overlap_build.py) */
(() => {
  "use strict";

  let D = null;
  const charts = {};
  const $ = (s, el = document) => el.querySelector(s);
  const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
  const escapeHtml = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  // ---------- formatting ----------
  const int = (n) => Math.round(n).toLocaleString("en-US");
  const pct = (a, b) => (b ? (a / b) * 100 : null);
  const pctText = (v, digits) => (v == null ? "–" : `${v.toFixed(digits ?? (Math.abs(v) < 10 ? 1 : 0))}%`);
  const usd = (v) => {
    const a = Math.abs(v);
    if (a >= 1e9) return `$${(v / 1e9).toFixed(a >= 1e10 ? 0 : 1)}B`;
    if (a >= 1e6) return `$${(v / 1e6).toFixed(a >= 1e7 ? 0 : 1)}M`;
    if (a >= 1e3) return `$${(v / 1e3).toFixed(a >= 1e4 ? 0 : 1)}k`;
    return `$${Math.round(v)}`;
  };
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const dayLabel = (iso) => `${MONTHS[+iso.slice(5, 7) - 1]} ${+iso.slice(8, 10)}`;
  const stamp = (ms) => new Date(ms).toISOString().replace("T", " ").slice(0, 16) + " UTC";

  // How closely the two products overlap in time. Same colors everywhere on the page.
  const LEVELS = () => [css("--series-1"), css("--series-2"), css("--series-3"), css("--other")];

  // ---------- chart chrome (same look as the main dashboard) ----------
  function baseOption() {
    const text = css("--text-secondary"), muted = css("--text-muted"), grid = css("--grid"), axis = css("--axis");
    return {
      animation: false,
      textStyle: { fontFamily: 'system-ui, -apple-system, "Segoe UI", sans-serif', color: text },
      grid: { left: 8, right: 16, top: 16, bottom: 8, containLabel: true },
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
  // horizontal bars: categories on the y axis, read top to bottom
  function horizontal(option, labels, axisFmt, max) {
    const { xAxis, yAxis } = option;
    option.xAxis = { ...yAxis, max, axisLabel: { ...yAxis.axisLabel, formatter: axisFmt } };
    option.yAxis = { ...xAxis, data: labels, inverse: true, axisLabel: { ...xAxis.axisLabel, fontSize: 12, color: css("--text-secondary"), hideOverlap: false } };
    option.tooltip.axisPointer = { type: "shadow", shadowStyle: { color: css("--surface-2"), opacity: 0.6 } };
    return option;
  }

  // Tooltip: value first, then the series; `detail(seriesIndex, dataIndex)` adds the counts behind a share.
  function tooltip(valueFmt, detail, { total = false, sort = false } = {}) {
    return (params) => {
      const ps = [].concat(params).filter((p) => p.value != null && p.value !== "-");
      if (!ps.length) return "";
      if (sort) ps.sort((a, b) => b.value - a.value);
      const muted = css("--text-secondary");
      const rows = ps.map((p) => {
        const extra = detail ? detail(p.seriesIndex, p.dataIndex) : "";
        return `<div style="display:flex;align-items:center;gap:8px;min-width:200px">
          <span style="width:12px;height:${p.seriesType === "line" ? 2 : 10}px;background:${p.color};flex:none;border-radius:2px"></span>
          <b style="font-variant-numeric:tabular-nums">${valueFmt(p.value)}</b>
          <span style="color:${muted};flex:1">${escapeHtml(p.seriesName)}</span>
          ${extra ? `<span style="color:${muted};font-variant-numeric:tabular-nums;margin-left:12px">${extra}</span>` : ""}</div>`;
      });
      const sum = ps.reduce((a, p) => a + p.value, 0);
      const tot = total ? `<div style="margin-top:4px;padding-top:4px;border-top:1px solid ${css("--grid")}"><b>${valueFmt(sum)}</b> <span style="color:${muted}">Total</span></div>` : "";
      return `<div style="font-size:12px;color:${muted};margin-bottom:4px">${escapeHtml(ps[0].axisValueLabel ?? ps[0].name)}</div>${rows.join("")}${tot}`;
    };
  }

  function card(name, { title, note, short } = {}) {
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
    $(".title", fig).textContent = title || "";
    $(".note", fig).textContent = note || "";
    const el = $(".chart", fig);
    el.setAttribute("aria-label", title || name);
    return { fig, el };
  }

  function renderChart(name, cfg, option, table) {
    const { fig, el } = card(name, cfg);
    if (cfg.empty) {
      charts[name]?.dispose(); delete charts[name];
      fig.classList.add("is-empty");
      el.innerHTML = `<div class="empty">${cfg.empty}</div>`;
      return;
    }
    let ch = charts[name];
    if (!ch || ch.getDom() !== el) ch = charts[name] = echarts.init(el, null, { renderer: "canvas" });
    ch.setOption(option, { notMerge: true });
    renderLegend(fig, ch, option);
    renderTable(fig, table);
  }

  function renderLegend(fig, ch, option) {
    const box = $(".legend", fig);
    box.innerHTML = "";
    if (option.series.length < 2) return;
    for (const s of option.series) {
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
    const head = t.createTHead().insertRow();
    table.cols.forEach((c, i) => { const th = document.createElement("th"); th.textContent = c; if (i) th.className = "num"; head.appendChild(th); });
    const body = t.createTBody();
    table.rows.forEach((r) => {
      const tr = body.insertRow();
      r.forEach((v, i) => { const td = tr.insertCell(); td.textContent = v; if (i) td.className = "num"; });
    });
    box.appendChild(t);
  }

  const lineSeries = (name, data, color) => ({
    name, type: "line", data, symbol: "circle", symbolSize: 7, showSymbol: false, connectNulls: false,
    lineStyle: { width: 2, color, cap: "round", join: "round" },
    itemStyle: { color, borderColor: css("--surface-1"), borderWidth: 2 },
    emphasis: { focus: "none" },
  });
  const barSeries = (name, data, color, stack) => ({
    name, type: "bar", stack, data, barMaxWidth: 28,
    itemStyle: { color, borderColor: css("--surface-1"), borderWidth: 1, borderRadius: 0 },
    emphasis: { focus: "none", itemStyle: { opacity: 0.85 } },
  });
  // round the data-end of the last segment in each stacked bar
  function roundEnds(series, radius) {
    const n = series[0]?.data.length || 0;
    for (let i = 0; i < n; i++) {
      for (let j = series.length - 1; j >= 0; j--) {
        const v = series[j].data[i];
        if (v) { series[j].data[i] = { value: v, itemStyle: { borderRadius: radius } }; break; }
      }
    }
    return series;
  }
  const UP = [4, 4, 0, 0], RIGHT = [0, 4, 4, 0];

  // a dashed vertical marker on a category axis, e.g. the Perps public launch
  function marker(option, label, text) {
    option.grid.top = 26;  // room for the label above the line
    option.series[0].markLine = {
      silent: true, symbol: "none", animation: false,
      lineStyle: { color: css("--axis"), type: [4, 4], width: 1 },
      label: { formatter: text, color: css("--text-muted"), fontSize: 11, position: "end", rotate: 0, distance: 4 },
      data: [{ xAxis: label }],
    };
  }

  function drawTiles(box, tiles) {
    box.innerHTML = "";
    for (const t of tiles) {
      const el = document.createElement("div");
      el.className = "tile";
      const lab = document.createElement("div"); lab.className = "label";
      if (t.key) { const k = document.createElement("span"); k.className = "key"; k.style.background = t.key; lab.appendChild(k); }
      lab.appendChild(document.createTextNode(t.label));
      const val = document.createElement("div"); val.className = "value"; val.textContent = t.value;
      const note = document.createElement("div"); note.className = "delta"; note.textContent = t.note || "";
      el.append(lab, val, note);
      box.appendChild(el);
    }
  }

  // ---------- Polymarket ----------
  function polyView() {
    const p = D.poly;
    // drop the current, incomplete week
    const n = p.weeks[p.weeks.length - 1] === p.partial_week ? p.weeks.length - 1 : p.weeks.length;
    const cut = (o) => Object.fromEntries(Object.entries(o).map(([k, v]) => [k, v.slice(0, n)]));
    const weeks = p.weeks.slice(0, n);
    const launchIdx = weeks.findIndex((w, i) => w <= p.launch && (i + 1 === weeks.length || weeks[i + 1] > p.launch));
    return { p, n, weeks, labels: weeks.map(dayLabel), perps: cut(p.perps), pred: cut(p.pred), launchLabel: launchIdx >= 0 ? dayLabel(weeks[launchIdx]) : null };
  }

  function renderPoly() {
    const { p, n, weeks, labels, perps, pred, launchLabel } = polyView();
    const C = LEVELS();
    const last = n - 1, lastWeek = `week of ${dayLabel(weeks[last])}`;
    const T = p.totals;

    drawTiles($("#poly-tiles"), [
      { label: "Perps accounts that have traded", value: int(T.perps_traders), note: `${int(T.depositors)} accounts have deposited; ${int(T.deposited_never_traded)} never traded` },
      { label: "…that have ever traded prediction markets", value: pctText(pct(T.pred_ever, T.perps_traders)), note: `${int(T.pred_ever)} accounts; ${int(T.pred_4w)} in the last 4 full weeks` },
      { label: "Share of Perps volume from them", value: pctText(pct(T.notional_pred_ever, T.notional)), note: `${usd(T.notional_pred_ever)} of ${usd(T.notional)} all-time notional` },
      { label: "Prediction traders also on Perps", value: pctText(pct(pred.perps_same[last], pred.traders[last]), 2), note: `${int(pred.perps_same[last])} of ${int(pred.traders[last])} wallets, ${lastWeek}` },
    ]);

    // 1. share of weekly perps traders who also trade prediction markets
    {
      const defs = [["Same week", perps.pred_same], ["Within 4 weeks", perps.pred_4w], ["Ever, to that week", perps.pred_ever]];
      const o = baseOption();
      o.xAxis.data = labels;
      o.yAxis.axisLabel.formatter = (v) => `${v}%`;
      o.yAxis.max = 100;
      o.series = defs.map(([name, arr], i) => lineSeries(name, arr.map((v, k) => +pct(v, perps.active[k]).toFixed(2)), C[i]));
      if (launchLabel) marker(o, launchLabel, "Public launch");
      o.tooltip.formatter = tooltip((v) => pctText(v), (s, k) => `${int(defs[s][1][k])} of ${int(perps.active[k])}`);
      renderChart("poly-perps-share", { title: "Perps traders who also trade prediction markets", note: "% of accounts with a Perps fill that week" }, o, {
        cols: ["Week of", "Perps traders", ...defs.map((d) => d[0])],
        rows: weeks.map((w, k) => [w, int(perps.active[k]), ...defs.map((d) => pctText(pct(d[1][k], perps.active[k])))]).reverse(),
      });
    }

    // 2. share of weekly prediction-market traders who also trade perps
    {
      const defs = [["Traded Perps that week", pred.perps_same], ["Has traded Perps, to that week", pred.perps_ever]];
      const o = baseOption();
      o.xAxis.data = labels;
      o.yAxis.axisLabel.formatter = (v) => `${v}%`;
      o.series = defs.map(([name, arr], i) => lineSeries(name, arr.map((v, k) => (pred.traders[k] ? +pct(v, pred.traders[k]).toFixed(3) : null)), C[i * 2]));
      if (launchLabel) marker(o, launchLabel, "Public launch");
      o.tooltip.formatter = tooltip((v) => pctText(v, 2), (s, k) => `${int(defs[s][1][k])} of ${int(pred.traders[k])}`);
      renderChart("poly-pred-share", { title: "Prediction-market traders who also trade Perps", note: "% of wallets with a prediction-market fill that week" }, o, {
        cols: ["Week of", "Prediction traders", ...defs.map((d) => d[0])],
        rows: weeks.map((w, k) => [w, int(pred.traders[k]), ...defs.map((d) => pctText(pct(d[1][k], pred.traders[k]), 2))]).reverse(),
      });
    }

    // 3. weekly perps traders by how recently they traded prediction markets
    {
      const parts = [
        ["Predictions the same week", perps.pred_same],
        ["In the 3 weeks before", perps.pred_4w.map((v, k) => v - perps.pred_same[k])],
        ["Earlier only", perps.pred_ever.map((v, k) => v - perps.pred_4w[k])],
        ["Never", perps.active.map((v, k) => v - perps.pred_ever[k])],
      ];
      const o = baseOption();
      o.xAxis.data = labels;
      o.yAxis.axisLabel.formatter = (v) => (v >= 1000 ? `${v / 1000}k` : v);
      o.series = roundEnds(parts.map(([name, arr], i) => barSeries(name, arr.slice(), C[i], "s")), UP);
      o.tooltip.formatter = tooltip(int, (s, k) => pctText(pct(parts[s][1][k], perps.active[k])), { total: true });
      renderChart("poly-perps-bars", { title: "Weekly Perps traders, by prediction-market activity", note: "accounts" }, o, {
        cols: ["Week of", ...parts.map((d) => d[0]), "Total"],
        rows: weeks.map((w, k) => [w, ...parts.map((d) => int(d[1][k])), int(perps.active[k])]).reverse(),
      });
    }

    // 4. share of prediction-market dollars traded by wallets that trade perps
    {
      const defs = [["Traded Perps that week", pred.perps_same_vol], ["Has traded Perps, to that week", pred.perps_ever_vol]];
      const o = baseOption();
      o.xAxis.data = labels;
      o.yAxis.axisLabel.formatter = (v) => `${v}%`;
      o.series = defs.map(([name, arr], i) => lineSeries(name, arr.map((v, k) => (pred.vol[k] ? +pct(v, pred.vol[k]).toFixed(3) : null)), C[i * 2]));
      if (launchLabel) marker(o, launchLabel, "Public launch");
      o.tooltip.formatter = tooltip((v) => pctText(v, 2), (s, k) => `${usd(defs[s][1][k])} of ${usd(pred.vol[k])}`);
      renderChart("poly-vol-share", { title: "Prediction-market volume from wallets that trade Perps", note: "% of dollars traded, both sides of each fill" }, o, {
        cols: ["Week of", "Prediction volume", ...defs.map((d) => d[0])],
        rows: weeks.map((w, k) => [w, usd(pred.vol[k]), ...defs.map((d) => pctText(pct(d[1][k], pred.vol[k]), 2))]).reverse(),
      });
    }

    // 5. new perps traders each week by prior prediction-market history
    {
      const parts = [["Already traded prediction markets", perps.new_before], ["Started prediction markets after", perps.new_later], ["No prediction-market trades", perps.new_never]];
      const colors = [C[0], C[2], C[3]];
      const o = baseOption();
      o.xAxis.data = labels;
      o.series = roundEnds(parts.map(([name, arr], i) => barSeries(name, arr.slice(), colors[i], "s")), UP);
      if (launchLabel) marker(o, launchLabel, "Public launch");
      o.tooltip.formatter = tooltip(int, (s, k) => pctText(pct(parts[s][1][k], perps.new[k])), { total: true });
      renderChart("poly-new", { title: "New Perps traders each week, by prediction-market history", note: "accounts, by week of first Perps fill" }, o, {
        cols: ["Week of", ...parts.map((d) => d[0]), "New Perps traders"],
        rows: weeks.map((w, k) => [w, ...parts.map((d) => int(d[1][k])), int(perps.new[k])]).reverse(),
      });
    }

    // 6. which came first
    {
      const total = p.gaps.reduce((a, g) => a + g.accounts, 0);
      const o = horizontal(baseOption(), p.gaps.map((g) => g.label), (v) => `${v}%`);
      o.series = [barSeries("Perps traders", p.gaps.map((g) => ({ value: +pct(g.accounts, total).toFixed(2), itemStyle: { borderRadius: RIGHT } })), C[0])];
      o.series[0].barMaxWidth = 22;
      o.series[0].label = { show: true, position: "right", color: css("--text-secondary"), fontSize: 11, formatter: (q) => pctText(q.value) };
      o.grid.right = 44;
      o.tooltip.formatter = tooltip((v) => pctText(v), (s, k) => `${int(p.gaps[k].accounts)} accounts`);
      renderChart("poly-gaps", { title: "First prediction trade relative to first Perps trade", note: `${int(total)} Perps traders with prediction-market history` }, o, {
        cols: ["First prediction trade", "Accounts", "Share"],
        rows: p.gaps.map((g) => [g.label, int(g.accounts), pctText(pct(g.accounts, total))]),
      });
    }

    // 7. perps take-up by prediction trader size
    {
      const b = p.bands;
      const o = baseOption();
      o.xAxis.data = b.map((x) => x.label);
      o.xAxis.axisLabel.interval = 0; o.xAxis.axisLabel.hideOverlap = false;
      o.yAxis.axisLabel.formatter = (v) => `${v}%`;
      o.series = [barSeries("Have traded Perps", b.map((x) => ({ value: +pct(x.perps, x.wallets).toFixed(2), itemStyle: { borderRadius: UP } })), C[0])];
      o.series[0].label = { show: true, position: "top", color: css("--text-secondary"), fontSize: 11, formatter: (q) => pctText(q.value) };
      o.tooltip.axisPointer = { type: "shadow", shadowStyle: { color: css("--surface-2"), opacity: 0.6 } };
      o.tooltip.formatter = tooltip((v) => pctText(v, 2), (s, k) => `${int(b[k].perps)} of ${int(b[k].wallets)} wallets`);
      renderChart("poly-bands", { title: "Perps take-up by prediction trader size", note: "% of wallets, by prediction-market dollars traded since 3 Sep" }, o, {
        cols: ["Dollars traded since 3 Sep", "Wallets", "Have traded Perps", "Share"],
        rows: b.map((x) => [x.label, int(x.wallets), int(x.perps), pctText(pct(x.perps, x.wallets), 2)]),
      });
    }

    // 8. prediction take-up by perps trader size
    {
      const s = p.size;
      const defs = [["In the last 4 weeks", "pred_4w", C[0]], ["Ever", "pred_ever", C[2]]];
      const o = baseOption();
      o.xAxis.data = s.map((x) => x.label);
      o.xAxis.axisLabel.interval = 0; o.xAxis.axisLabel.hideOverlap = false;
      o.yAxis.axisLabel.formatter = (v) => `${v}%`;
      o.series = defs.map(([name, key, color]) => ({ ...barSeries(name, s.map((x) => ({ value: +pct(x[key], x.accounts).toFixed(2), itemStyle: { borderRadius: UP } })), color), barGap: "8%" }));
      o.tooltip.axisPointer = { type: "shadow", shadowStyle: { color: css("--surface-2"), opacity: 0.6 } };
      o.tooltip.formatter = tooltip((v) => pctText(v), (i, k) => `${int(s[k][defs[i][1]])} of ${int(s[k].accounts)} accounts`);
      renderChart("poly-size", { title: "Prediction-market take-up by Perps trader size", note: "% of accounts, by all-time Perps notional" }, o, {
        cols: ["All-time Perps notional", "Accounts", "Traded predictions, last 4 weeks", "Ever", "Perps notional"],
        rows: s.map((x) => [x.label, int(x.accounts), pctText(pct(x.pred_4w, x.accounts)), pctText(pct(x.pred_ever, x.accounts)), usd(x.notional)]),
      });
    }

    // 9. what perps traders trade on the prediction side
    {
      const cats = p.categories.filter((c) => c.vol > 0);
      const all = cats.reduce((a, c) => a + c.vol, 0), per = cats.reduce((a, c) => a + c.perps_vol, 0);
      const o = horizontal(baseOption(), cats.map((c) => c.category), (v) => `${v}%`);
      const defs = [["Wallets that trade Perps", (c) => pct(c.perps_vol, per), C[0]], ["All wallets", (c) => pct(c.vol, all), C[3]]];
      o.series = defs.map(([name, f, color]) => {
        const s = barSeries(name, cats.map((c) => ({ value: +f(c).toFixed(2), itemStyle: { borderRadius: RIGHT } })), color);
        s.barMaxWidth = 12; s.barGap = "20%";
        return s;
      });
      o.tooltip.formatter = tooltip((v) => pctText(v), (i, k) => usd(i ? cats[k].vol : cats[k].perps_vol));
      renderChart("poly-cats", { title: "What Perps traders trade on prediction markets", note: "% of each group's prediction-market dollars since 3 Sep" }, o, {
        cols: ["Category", "Wallets that trade Perps", "All wallets", "Perps traders' share of the category"],
        rows: cats.map((c) => [c.category, pctText(pct(c.perps_vol, per)), pctText(pct(c.vol, all)), pctText(pct(c.perps_vol, c.vol), 2)]),
      });
    }

    // 10. when perps traders joined Polymarket
    {
      const j = p.joined;
      const parts = [["Have traded prediction markets", j.map((x) => x.pred_ever), C[0]], ["Perps only", j.map((x) => x.accounts - x.pred_ever), C[3]]];
      const o = baseOption();
      o.xAxis.data = j.map((x) => x.label);
      o.xAxis.axisLabel.interval = 0; o.xAxis.axisLabel.hideOverlap = false;
      o.yAxis.axisLabel.formatter = (v) => (v >= 1000 ? `${v / 1000}k` : v);
      o.series = roundEnds(parts.map(([name, arr, color]) => barSeries(name, arr.slice(), color, "s")), UP);
      o.tooltip.axisPointer = { type: "shadow", shadowStyle: { color: css("--surface-2"), opacity: 0.6 } };
      o.tooltip.formatter = tooltip(int, (i, k) => pctText(pct(parts[i][1][k], j[k].accounts)), { total: true });
      renderChart("poly-joined", { title: "Perps traders by when they joined Polymarket", note: "accounts, by when their first Polymarket wallet was created" }, o, {
        cols: ["Joined Polymarket", "Perps traders", "Have traded prediction markets", "Share"],
        rows: j.map((x) => [x.label, int(x.accounts), int(x.pred_ever), pctText(pct(x.pred_ever, x.accounts))]),
      });
    }
  }

  // ---------- Hyperliquid ----------
  const HL_LEVELS = ["Perps in the last 7 days", "Perps in the last 30 days", "Perps earlier only", "Never traded perps"];

  function renderHL() {
    const h = D.hl;
    const C = LEVELS();
    const U = h.universe, share = (o, upto) => pct(o.s.slice(0, upto + 1).reduce((a, v) => a + v, 0), o.wallets);
    const R = h.reverse;

    $("#hl-lede").textContent =
      `Outcome markets (HIP-4, live since 2 May 2026) and perps share one Hyperliquid account, so the overlap is by wallet. Hyperliquid keeps no public history of who traded an outcome market, ` +
      `so outcome traders are the ${int(U.wallets)} wallets seen in this project's own trade feed for price markets (daily since ${dayLabel(h.feed_from)}) plus the latest trades on every live outcome market, including sports. ` +
      `Each wallet's perps volume is as reported by Hyperliquid on ${stamp(h.asof)}, for the trailing 24 hours, 7 days, 30 days and all time.`;

    drawTiles($("#hl-tiles"), [
      { label: "Outcome traders identified", value: int(U.wallets), note: `${int(h.feed.wallets)} from price markets since ${dayLabel(h.feed_from)}; rest from the latest trades on all ${int(h.sample_markets)} live markets` },
      { label: "…traded perps in the last 7 days", value: pctText(share(U, 0)), note: `${int(U.s[0])} wallets; ${pctText(pct(U.day, U.wallets))} in the last 24 hours` },
      { label: "…in the last 30 days", value: pctText(share(U, 1)), note: `${int(U.s[0] + U.s[1])} wallets` },
      { label: "…ever", value: pctText(share(U, 2)), note: `${int(U.s[3])} wallets (${pctText(pct(U.s[3], U.wallets))}) have never traded a perp` },
      { label: "Account older than outcome markets", value: pctText(pct(h.before_launch[0], h.before_launch[1])), note: `${int(h.before_launch[0])} of ${int(h.before_launch[1])} wallets were on Hyperliquid before 2 May 2026` },
    ]);

    // weekly back-history, rebuilt from the fill history of the wallets known today
    {
      const H = h.history;
      const empty = H ? "" : "Wallet fill histories haven't been read yet.";
      const n = H ? (H.weeks[H.weeks.length - 1] === h.partial_week ? H.weeks.length - 1 : H.weeks.length) : 0;
      const weeks = H ? H.weeks.slice(0, n) : [], labels = weeks.map(dayLabel);
      const parts = H ? [["Perps the same week", H.same], ["In the 3 weeks before", H.prior3], ["Earlier only", H.earlier], ["No perps yet", H.never]].map(([name, arr]) => [name, arr.slice(0, n)]) : [];
      const tr = H ? H.traders.slice(0, n) : [];
      const note = H ? `${int(H.wallets)} wallets seen trading outcomes since ${dayLabel(h.feed_from)}, traced back through their own fills` : "";
      const defs = H ? [["Same week", (k) => parts[0][1][k]], ["Within 4 weeks", (k) => parts[0][1][k] + parts[1][1][k]], ["Ever, to that week", (k) => tr[k] - parts[3][1][k]]] : [];
      const o = baseOption();
      o.xAxis.data = labels;
      o.yAxis.axisLabel.formatter = (v) => `${v}%`;
      o.yAxis.max = 100;
      o.series = defs.map(([name, f], i) => lineSeries(name, weeks.map((_, k) => +pct(f(k), tr[k]).toFixed(2)), C[i]));
      o.tooltip.formatter = tooltip((v) => pctText(v), (s, k) => `${int(defs[s][1](k))} of ${int(tr[k])}`);
      renderChart("hl-hist-share", { title: "Outcome traders who also trade perps, by week", note: "% of today's known outcome traders active that week", empty }, o, {
        cols: ["Week of", "Outcome traders", ...defs.map((d) => d[0])],
        rows: weeks.map((w, k) => [w, int(tr[k]), ...defs.map((d) => pctText(pct(d[1](k), tr[k])))]).reverse(),
      });
      const b = baseOption();
      b.xAxis.data = labels;
      b.series = parts.length ? roundEnds(parts.map(([name, arr], i) => barSeries(name, arr.slice(), C[i], "s")), UP) : [];
      b.tooltip.axisPointer = { type: "shadow", shadowStyle: { color: css("--surface-2"), opacity: 0.6 } };
      b.tooltip.formatter = tooltip(int, (s, k) => pctText(pct(parts[s][1][k], tr[k])), { total: true });
      renderChart("hl-hist", { title: "Weekly outcome traders, by perps activity", note, empty }, b, {
        cols: ["Week of", ...parts.map((d) => d[0]), "Total"],
        rows: weeks.map((w, k) => [w, ...parts.map((d) => int(d[1][k])), int(tr[k])]).reverse(),
      });
    }

    // complete days only: the feed's first day and today are partial
    const keep = h.days.map((d, i) => i).filter((i) => i > 0 && h.days[i] !== h.partial_day);
    const days = keep.map((i) => h.days[i]), labels = days.map(dayLabel);
    const S = [0, 1, 2, 3].map((s) => keep.map((i) => h.daily[`s${s}`][i]));
    const known = keep.map((_, k) => S.reduce((a, arr) => a + arr[k], 0));

    {
      const defs = [["In the last 7 days", (k) => S[0][k]], ["In the last 30 days", (k) => S[0][k] + S[1][k]], ["Ever", (k) => S[0][k] + S[1][k] + S[2][k]]];
      const o = baseOption();
      o.xAxis.data = labels;
      o.yAxis.axisLabel.formatter = (v) => `${v}%`;
      o.yAxis.max = 100;
      o.series = defs.map(([name, f], i) => lineSeries(name, keep.map((_, k) => +pct(f(k), known[k]).toFixed(2)), C[i]));
      o.series.forEach((s) => { s.showSymbol = true; });
      o.tooltip.formatter = tooltip((v) => pctText(v), (s, k) => `${int(defs[s][1](k))} of ${int(known[k])}`);
      renderChart("hl-daily-share", { title: "Outcome traders who also trade perps, by day", note: "% of each day's price-market outcome traders" }, o, {
        cols: ["Day", "Outcome traders", ...defs.map((d) => d[0])],
        rows: days.map((d, k) => [d, int(known[k]), ...defs.map((x) => pctText(pct(x[1](k), known[k])))]).reverse(),
      });
    }
    {
      const o = baseOption();
      o.xAxis.data = labels;
      o.series = roundEnds(HL_LEVELS.map((name, i) => barSeries(name, S[i].slice(), C[i], "s")), UP);
      o.tooltip.axisPointer = { type: "shadow", shadowStyle: { color: css("--surface-2"), opacity: 0.6 } };
      o.tooltip.formatter = tooltip(int, (s, k) => pctText(pct(S[s][k], known[k])), { total: true });
      renderChart("hl-daily", { title: "Daily outcome traders, by perps activity", note: "wallets trading price markets that day; perps activity as of the latest read" }, o, {
        cols: ["Day", ...HL_LEVELS, "Total"],
        rows: days.map((d, k) => [d, ...S.map((arr) => int(arr[k])), int(known[k])]).reverse(),
      });
    }

    // 100% bars: mix of perps activity within each group
    const mix = (name, rows, cfg, firstCol) => {
      const o = horizontal(baseOption(), rows.map((r) => `${r.label}  (${int(r.wallets)})`), (v) => `${v}%`, 100);
      o.series = roundEnds(HL_LEVELS.map((lvl, i) => barSeries(lvl, rows.map((r) => +pct(r.s[i], r.wallets).toFixed(2)), C[i], "s")), RIGHT);
      o.series.forEach((s) => { s.barMaxWidth = 26; });
      o.tooltip.formatter = tooltip((v) => pctText(v), (s, k) => `${int(rows[k].s[s])} wallets`);
      renderChart(name, cfg, o, {
        cols: [firstCol, "Wallets", ...HL_LEVELS],
        rows: rows.map((r) => [r.label, int(r.wallets), ...r.s.map((v) => pctText(pct(v, r.wallets)))]),
      });
    };
    mix("hl-groups", h.groups.filter((g) => g.wallets).map((g) => ({ label: g.group, wallets: g.wallets, s: g.s })),
      { title: "Perps activity by type of outcome market", note: "% of wallets trading each type", short: true }, "Outcome market type");
    mix("hl-bands", h.bands.filter((b) => b.wallets),
      { title: "Perps activity by outcome trader size", note: `% of wallets, by outcome dollars since ${dayLabel(h.feed_from)}`, short: true }, "Outcome dollars traded");

    {
      const a = h.age;
      const o = baseOption();
      o.xAxis.data = a.map((x) => x.label.replace(" ", "\n"));   // two lines, so seven labels fit a narrow card
      o.xAxis.axisLabel.interval = 0; o.xAxis.axisLabel.hideOverlap = false;
      o.series = roundEnds(HL_LEVELS.map((name, i) => barSeries(name, a.map((x) => x.s[i]), C[i], "s")), UP);
      o.tooltip.axisPointer = { type: "shadow", shadowStyle: { color: css("--surface-2"), opacity: 0.6 } };
      o.tooltip.formatter = tooltip(int, (s, k) => pctText(pct(a[k].s[s], a[k].wallets)), { total: true });
      renderChart("hl-age", { title: "Outcome traders by when they joined Hyperliquid", note: "wallets, by first account activity" }, o, {
        cols: ["Joined", "Wallets", ...HL_LEVELS],
        rows: a.map((x) => [x.label, int(x.wallets), ...x.s.map(int)]),
      });
    }
    {
      const f = h.firsts || [], total = f.reduce((a, x) => a + x.wallets, 0);
      const colors = [C[0], C[1], C[2], C[3], css("--axis")];
      const o = horizontal(baseOption(), f.map((x) => x.label.replace(", ", ",\n")), (v) => `${v}%`);
      o.series = [barSeries("Outcome traders", f.map((x, i) => ({ value: +pct(x.wallets, total || 1).toFixed(2), itemStyle: { color: colors[i], borderRadius: RIGHT } })), C[0])];
      o.series[0].barMaxWidth = 22;
      o.series[0].label = { show: true, position: "right", color: css("--text-secondary"), fontSize: 11, formatter: (q) => pctText(q.value) };
      o.grid.right = 44;
      o.tooltip.formatter = tooltip((v) => pctText(v), (s, k) => `${int(f[k].wallets)} wallets`);
      renderChart("hl-firsts", { title: "What outcome traders traded first on Hyperliquid", note: `${int(total)} wallets, from each wallet's own fill history`, empty: total ? "" : "First trades haven't been read yet." }, o, {
        cols: ["First product", "Wallets", "Share"],
        rows: f.map((x) => [x.label, int(x.wallets), pctText(pct(x.wallets, total))]),
      });
    }
    {
      const r = h.ratio, V = h.volumes, total = r.reduce((a, x) => a + x.wallets, 0);
      const o = baseOption();
      o.xAxis.data = r.map((x) => x.label);
      o.xAxis.axisLabel.interval = 0; o.xAxis.axisLabel.hideOverlap = false;
      o.yAxis.axisLabel.formatter = (v) => `${v}%`;
      o.series = [barSeries("Wallets trading both", r.map((x) => ({ value: +pct(x.wallets, total || 1).toFixed(2), itemStyle: { borderRadius: UP } })), C[0])];
      o.series[0].label = { show: true, position: "top", color: css("--text-secondary"), fontSize: 11, formatter: (q) => pctText(q.value) };
      o.tooltip.axisPointer = { type: "shadow", shadowStyle: { color: css("--surface-2"), opacity: 0.6 } };
      o.tooltip.formatter = tooltip((v) => pctText(v), (s, k) => `${int(r[k].wallets)} wallets`);
      renderChart("hl-ratio", { title: "Perps volume relative to outcome volume", note: `${int(total)} wallets trading both in the last 7 days; perps dollars ÷ outcome dollars` }, o, {
        cols: ["Perps volume ÷ outcome volume", "Wallets", "Share"],
        rows: r.map((x) => [x.label, int(x.wallets), pctText(pct(x.wallets, total))]),
      });
      const tiles = $("#hl-tiles");
      if (V.overlap_wallets && !$(".tile.ratio", tiles)) {
        const el = document.createElement("div");
        el.className = "tile ratio";
        el.innerHTML = `<div class="label">Trade more in perps than outcomes</div><div class="value">${pctText(pct(V.perp_bigger, V.overlap_wallets))}</div>
          <div class="delta">${int(V.perp_bigger)} of ${int(V.overlap_wallets)} wallets doing both; ${usd(V.overlap_perp)} of perps against ${usd(V.overlap_outcome)} of outcomes in 7 days</div>`;
        tiles.appendChild(el);
      }
    }
    {
      const wins = [["Perps in the last 7 days", R.week], ["Perps in the last 30 days", R.month]];
      const defs = [["Hold an outcome position now", "holding", C[0]], ["Seen trading outcomes", "seen_trading", C[1]], ["Either", "either", C[2]]];
      const base = (w, key) => (key === "holding" ? w.checked : w.active);
      const o = baseOption();
      o.xAxis.data = wins.map(([name, w]) => `${name}  (${int(w.active)} accounts)`);
      o.xAxis.axisLabel.interval = 0; o.xAxis.axisLabel.hideOverlap = false; o.xAxis.axisLabel.fontSize = 12;
      o.yAxis.axisLabel.formatter = (v) => `${v}%`;
      o.series = defs.map(([name, key, color]) => {
        const s = barSeries(name, wins.map(([, w]) => ({ value: base(w, key) ? +pct(w[key], base(w, key)).toFixed(3) : null, itemStyle: { borderRadius: UP } })), color);
        s.label = { show: true, position: "top", color: css("--text-secondary"), fontSize: 11, formatter: (q) => pctText(q.value, 2) };
        s.barMaxWidth = 44; s.barGap = "8%";
        return s;
      });
      o.tooltip.axisPointer = { type: "shadow", shadowStyle: { color: css("--surface-2"), opacity: 0.6 } };
      o.tooltip.formatter = tooltip((v) => pctText(v, 2), (i, k) => `${int(wins[k][1][defs[i][1]])} of ${int(base(wins[k][1], defs[i][1]))}`);
      const unchecked = R.month.active - R.month.checked;
      renderChart("hl-reverse", {
        title: "Perps traders who also trade outcomes", note: "% of perps accounts on Hyperliquid's public leaderboard",
        empty: R.month.checked ? "" : "Outcome holdings of leaderboard accounts haven't been read yet.",
      }, o, {
        cols: ["Perps accounts", "Accounts", ...defs.map((d) => d[0])],
        rows: wins.map(([name, w]) => [name, int(w.active), ...defs.map((d) => `${int(w[d[1]])} (${pctText(pct(w[d[1]], base(w, d[1])), 2)})`)]),
      });
      if (unchecked > 0 && R.month.checked) $('figure[data-chart="hl-reverse"] .note').textContent += ` · ${int(R.month.checked)} of ${int(R.month.active)} checked so far`;
    }
  }

  // ---------- headline + notes ----------
  function renderHeadline() {
    const tiles = [];
    if (D.poly) {
      const { weeks, perps, pred, n } = polyView();
      const k = n - 1, wk = `Polymarket, week of ${dayLabel(weeks[k])}`;
      tiles.push({ key: css("--poly"), label: "Perps traders who also traded prediction markets", value: pctText(pct(perps.pred_same[k], perps.active[k])), note: `${int(perps.pred_same[k])} of ${int(perps.active[k])} accounts · ${wk}` });
      tiles.push({ key: css("--poly"), label: "Prediction traders who also traded Perps", value: pctText(pct(pred.perps_same[k], pred.traders[k]), 2), note: `${int(pred.perps_same[k])} of ${int(pred.traders[k])} wallets · ${wk}` });
    }
    if (D.hl) {
      const U = D.hl.universe, R = D.hl.reverse.week;
      tiles.push({ key: css("--hyperliquid"), label: "Outcome traders who also traded perps", value: pctText(pct(U.s[0], U.wallets)), note: `${int(U.s[0])} of ${int(U.wallets)} wallets · Hyperliquid, last 7 days` });
      if (R.checked) tiles.push({ key: css("--hyperliquid"), label: "Perps traders who hold or trade outcomes", value: pctText(pct(R.either, R.active), 2), note: `${int(R.either)} of ${int(R.active)} leaderboard accounts active in the last 7 days · Hyperliquid` });
    }
    drawTiles($("#tiles"), tiles);
  }

  function renderNotes() {
    const p = D.poly, h = D.hl;
    $("#notes").innerHTML = `
      <p><b>What counts as a trader.</b> Anyone with at least one fill in the period, on either side of the trade. The two directions of overlap share a numerator and differ in what it is divided by, so a venue where one product is far larger shows a high share one way and a low one the other.</p>
      ${p ? `<p><b>Polymarket Perps accounts.</b> Perps orders match off-chain, but collateral only enters through the Perps exchange contract on Polygon. Each deposit names the wallet that paid and the Perps account credited, which gives every account ever funded (${int(p.totals.depositors)}). Each account's fills come from Polymarket's public Perps API, read as weekly activity; accounts too busy to read in full have their all-time notional taken from the Perps leaderboard. An account funded only by transfer from another Perps account has no deposit and isn't included.</p>
      <p><b>Linking to prediction markets.</b> A Perps account is the user's signer address, and one user can hold more than one Polymarket wallet, so each account is matched to the wallets that funded it and to every wallet it owns on-chain: deposit wallets and Safes by owner, and its email-login proxy wallet by deriving the address (${int(p.wallets_found)} of ${int(p.totals.depositors)} accounts own at least one). Checked against Polymarket's profile API on 3,000 accounts, this finds the profile's wallet 99% of the time. ${int(p.routers)} addresses funded more than 10 accounts each (routers and multi-account desks) and are not used as links. Prediction-market activity is every on-chain fill of those wallets on Polymarket's international exchange, from Dune. Someone who trades prediction markets under a different signer is counted as Perps only, so overlap is if anything understated. Polymarket US is a separate exchange with no Perps and no public account data.</p>
      <p><b>Polymarket definitions.</b> “Same week” means a fill in both products in the same Monday–Sunday week. “Within 4 weeks” means a prediction-market fill in that week or the three before it. “Ever, to that week” means any prediction-market fill up to the end of that week. Prediction-market volume is dollars paid or received by each wallet on its own side of every fill, so the total is twice the usual taker volume.</p>` : ""}
      ${h ? `<p><b>Hyperliquid outcome traders.</b> Hyperliquid's API keeps no history of outcome-market trades once a market settles, so the list of outcome traders is built two ways: this project's trade feed, which has followed every trade on price markets (crypto, equity and commodity strikes and ranges) since ${dayLabel(h.feed_from)}, and one snapshot of the most recent trades on all ${int(h.sample_markets)} live outcome markets (${int(h.sample_trades)} trades), which adds sports and other markets but only their latest few dozen trades each. It is a sample of current outcome traders, not a census, and there is no history before ${dayLabel(h.feed_from)}.</p>
      <p><b>Hyperliquid back-history.</b> The weekly charts trace the wallets known today back through each wallet's own fill history. They show when those wallets traded outcomes and perps in earlier weeks, with both products measured in the same week. They leave out anyone who traded outcomes earlier in the year and had stopped by ${dayLabel(h.feed_from)}, so early weeks cover fewer traders and lean toward the ones who stayed.${h.history && h.history.busy ? ` Hyperliquid may not serve a busy wallet's oldest fills, so the ${int(h.history.busy)} wallets with more than 10,000 fills count only from the week after their oldest fill on record.` : ""}${h.history && h.history.unread ? ` ${int(h.history.unread)} of them haven't been read through to the present yet and are missing from the most recent weeks.` : ""}</p>
      <p><b>Hyperliquid perps activity.</b> Each wallet's perps volume comes from Hyperliquid's <i>portfolio</i> request, which reports trailing 24-hour, 7-day, 30-day and all-time totals as of the time it was read. So the daily chart shows each day's outcome traders against their perps activity as of ${stamp(h.asof)}, not on that day itself. The date a wallet joined is the start of its account-value history.</p>
      <p><b>Perps traders who trade outcomes.</b> Hyperliquid publishes no complete list of perps traders. The denominator here is its public leaderboard (${int(h.reverse.leaderboard)} accounts, which leaves out small accounts), limited to those with perps volume in the window. An account counts if its spot balances show an outcome position right now, or it appears among the outcome traders above. Accounts that traded outcomes and are flat again, outside the days and markets this project has seen, are missed, so read these as lower bounds.</p>` : ""}
      <p><b>Sources.</b> Polygon logs and Polymarket trades via Dune; Polymarket Perps API (api.perpetuals.polymarket.com); Hyperliquid info API, public trades feed and leaderboard file. Built by <code>pipeline/overlap_poly.py</code>, <code>overlap_hl.py</code> and <code>overlap_build.py</code>.</p>`;
  }

  function renderAll() {
    renderHeadline();
    if (D.poly) renderPoly(); else $("#polymarket").hidden = true;
    if (D.hl) renderHL(); else $("#hyperliquid").hidden = true;
  }

  async function load() {
    D = await fetch("data/overlap.json", { cache: "no-cache" }).then((r) => (r.ok ? r.json() : null)).catch(() => null);
    if (!D) { $("#updated").textContent = "No data yet"; return; }
    const parts = [];
    if (D.poly) parts.push(`Polymarket ${stamp(D.poly.asof)}`);
    if (D.hl) parts.push(`Hyperliquid ${stamp(D.hl.asof)}`);
    $("#updated").textContent = `Data as of ${parts.join(" · ")}`;
    renderNotes();
    renderAll();
  }

  window.addEventListener("resize", () => Object.values(charts).forEach((c) => c.resize()));
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => D && renderAll());
  load();
})();
