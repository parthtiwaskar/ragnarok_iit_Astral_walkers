// Small dependency-free SVG charts for the dashboard: line chart, range chart, stacked bars.
// Each returns { update(cfg), destroy() }. All hover/click behaviour is driven by callbacks in cfg.
(() => {
  const NS = "http://www.w3.org/2000/svg";
  const svgEl = (tag, attrs = {}, parent) => {
    const e = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
    if (parent) parent.appendChild(e);
    return e;
  };
  const nice = (v) => { const p = 10 ** Math.floor(Math.log10(v || 1)); const n = v / p; return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 2.5 ? 2.5 : n <= 5 ? 5 : 10) * p; };
  const compact = (v) => (Math.abs(v) >= 1e6 ? `${+(v / 1e6).toFixed(1)}M` : Math.abs(v) >= 1e3 ? `${+(v / 1e3).toFixed(1)}k` : String(+v.toFixed(1)));
  const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };

  function tooltip(container) {
    let el = container.querySelector(":scope > .ch-tip");
    if (!el) { el = document.createElement("div"); el.className = "ch-tip"; el.hidden = true; container.appendChild(el); }
    return {
      show(html, x, y) {
        el.innerHTML = html; el.hidden = false;
        const w = container.clientWidth, tw = el.offsetWidth;
        el.style.left = `${Math.min(Math.max(x - tw / 2, 4), Math.max(w - tw - 4, 4))}px`;
        el.style.top = `${Math.max(y - el.offsetHeight - 14, 0)}px`;
      },
      hide() { el.hidden = true; },
    };
  }

  // Common wrapper: re-render on width change only (height depends on width, so observing height would loop).
  function mount(container, render) {
    container.classList.add("ch-host");
    let cfg = null, lastW = 0;
    const draw = () => { if (!cfg) return; lastW = container.clientWidth; render(container, cfg, tooltip(container)); };
    const ro = new ResizeObserver(debounce(() => { if (Math.abs(container.clientWidth - lastW) > 2) draw(); }, 120));
    ro.observe(container);
    return { update(next) { cfg = next; draw(); }, destroy() { ro.disconnect(); container.innerHTML = ""; } };
  }

  /* ---- line chart: cfg { months[], series[{key,name,color,dash,values[],hidden}], tooltip(i), marker{index,label}, onPoint(i), label } ---- */
  function lineChart(container) {
    return mount(container, (root, cfg, tip) => {
      root.querySelectorAll(":scope > svg").forEach((s) => s.remove());
      const W = Math.max(root.clientWidth, 280), H = Math.round(Math.min(Math.max(W * 0.4, 210), 320));
      const m = { l: 44, r: 12, t: 16, b: 26 }, iw = W - m.l - m.r, ih = H - m.t - m.b;
      const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, width: W, height: H, class: "ch-svg", role: "img", "aria-label": cfg.label || "line chart" }, root);
      const visible = cfg.series.filter((s) => !s.hidden);
      const vals = visible.flatMap((s) => s.values).filter((v) => v != null);
      const max = nice(Math.max(...vals, 1) * 1.05);
      const n = cfg.months.length;
      const x = (i) => m.l + (n === 1 ? iw / 2 : (iw * i) / (n - 1));
      const y = (v) => m.t + ih - (ih * v) / max;
      const grid = svgEl("g", { class: "ch-grid" }, svg);
      for (let k = 0; k <= 4; k++) {
        const v = (max * k) / 4;
        svgEl("line", { x1: m.l, x2: W - m.r, y1: y(v), y2: y(v) }, grid);
        svgEl("text", { x: m.l - 8, y: y(v) + 3, "text-anchor": "end" }, grid).textContent = compact(v);
      }
      cfg.months.forEach((lab, i) => { svgEl("text", { x: x(i), y: H - 8, "text-anchor": "middle", class: "ch-xl" }, grid).textContent = lab; });
      if (cfg.marker && cfg.marker.index != null) {
        const mx = x(cfg.marker.index);
        svgEl("line", { x1: mx, x2: mx, y1: m.t, y2: m.t + ih, class: "ch-marker" }, svg);
        const lab = svgEl("text", { x: Math.min(mx + 6, W - m.r - 4), y: m.t + 10, class: "ch-marker-label", "text-anchor": mx > W - 90 ? "end" : "start" }, svg);
        lab.textContent = cfg.marker.label;
      }
      visible.forEach((s) => {
        const pts = s.values.map((v, i) => (v == null ? null : [x(i), y(v)]));
        let d = "", pen = false;
        pts.forEach((p) => { if (!p) { pen = false; return; } d += `${pen ? "L" : "M"}${p[0].toFixed(1)},${p[1].toFixed(1)} `; pen = true; });
        if (s.area && pts.every(Boolean)) {
          svgEl("path", { d: `M${pts[0][0]},${m.t + ih} ` + pts.map((p) => `L${p[0]},${p[1]}`).join(" ") + ` L${pts[n - 1][0]},${m.t + ih} Z`, fill: s.color, opacity: ".08" }, svg);
        }
        const path = svgEl("path", { d, fill: "none", stroke: s.color, "stroke-width": s.dash ? 1.6 : 2.4, "stroke-linejoin": "round", "stroke-linecap": "round", class: s.dash ? "" : "ch-draw" }, svg);
        if (s.dash) path.setAttribute("stroke-dasharray", s.dash); else path.setAttribute("pathLength", "1");
        pts.forEach((p) => { if (p) svgEl("circle", { cx: p[0], cy: p[1], r: s.dash ? 2.4 : 3.4, fill: s.dash ? "#090e1a" : s.color, stroke: s.color, "stroke-width": 1.6 }, svg); });
      });
      const cross = svgEl("line", { y1: m.t, y2: m.t + ih, class: "ch-cross", visibility: "hidden" }, svg);
      const hot = svgEl("g", {}, svg);
      const cols = cfg.months.map((_, i) => {
        const w = n === 1 ? iw : iw / (n - 1);
        const r = svgEl("rect", { x: x(i) - w / 2, y: m.t, width: w, height: ih, fill: "transparent", style: "cursor:" + (cfg.onPoint ? "pointer" : "default") }, hot);
        r.addEventListener("mouseenter", () => {
          cross.setAttribute("x1", x(i)); cross.setAttribute("x2", x(i)); cross.setAttribute("visibility", "visible");
          const top = Math.min(...visible.map((s) => (s.values[i] == null ? Infinity : y(s.values[i]))));
          tip.show(cfg.tooltip(i), x(i), Number.isFinite(top) ? top : m.t);
        });
        r.addEventListener("mouseleave", () => { cross.setAttribute("visibility", "hidden"); tip.hide(); });
        if (cfg.onPoint) r.addEventListener("click", () => cfg.onPoint(i));
        return r;
      });
      return cols;
    });
  }

  /* ---- range chart: cfg { rows[{label, sub, bars[{key,name,color,range:[lo,hi]|null}]}], hidden:Set, tooltip(row), unit } ---- */
  function rangeChart(container) {
    return mount(container, (root, cfg, tip) => {
      root.querySelectorAll(":scope > svg").forEach((s) => s.remove());
      const W = Math.max(root.clientWidth, 280), narrow = W < 520, rowH = narrow ? 78 : 58, m = { l: narrow ? 10 : 150, r: 16, t: 8, b: 26 };
      const H = m.t + cfg.rows.length * rowH + m.b, iw = W - m.l - m.r;
      const all = cfg.rows.flatMap((r) => r.bars).filter((b) => b.range && !cfg.hidden?.has(b.key)).map((b) => b.range[1]);
      const max = Math.max(nice(Math.max(...all, 20)), 50);
      const x = (v) => m.l + (iw * Math.min(v, max)) / max;
      const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, width: W, height: H, class: "ch-svg", role: "img", "aria-label": cfg.label || "range chart" }, root);
      const grid = svgEl("g", { class: "ch-grid" }, svg);
      for (let k = 0; k <= 4; k++) {
        const v = (max * k) / 4;
        svgEl("line", { x1: x(v), x2: x(v), y1: m.t, y2: H - m.b }, grid);
        svgEl("text", { x: x(v), y: H - 8, "text-anchor": "middle" }, grid).textContent = `${+v.toFixed(0)}%`;
      }
      cfg.rows.forEach((row, ri) => {
        const y0 = m.t + ri * rowH;
        const g = svgEl("g", { class: "ch-row" }, svg);
        svgEl("rect", { x: 0, y: y0 + 2, width: W, height: rowH - 4, rx: 8, class: "ch-rowbg" }, g);
        const lab = svgEl("text", { x: 6, y: narrow ? y0 + 20 : y0 + rowH / 2 - 2, class: "ch-rowlabel" }, g); lab.textContent = row.label;
        if (!narrow) svgEl("text", { x: 4, y: y0 + rowH / 2 + 12, class: "ch-rowsub" }, g).textContent = row.sub || "";
        if (row.bars.every((b) => !b.range)) {
          svgEl("text", { x: m.l + (narrow ? 0 : 4), y: y0 + (narrow ? 40 : rowH / 2 + 4), class: "ch-none" }, g).textContent = "No range from any side";
          g.addEventListener("mouseenter", (e) => tip.show(cfg.tooltip(row), e.clientX - root.getBoundingClientRect().left, y0 + 12));
          g.addEventListener("mouseleave", () => tip.hide());
          return;
        }
        row.bars.filter((b) => !cfg.hidden?.has(b.key)).forEach((b, bi, arr) => {
          const by = y0 + (narrow ? 30 : 10) + bi * 14;
          if (!b.range) { svgEl("text", { x: m.l + 4, y: by + 9, class: "ch-none" }, g).textContent = `${b.name}: no range`; return; }
          const w = Math.max(x(b.range[1]) - x(b.range[0]), 5);
          svgEl("rect", { x: x(b.range[0]), y: by, width: w, height: 9, rx: 4.5, fill: b.color, class: "ch-bar", style: `--c:${b.color}` }, g);
        });
        g.addEventListener("mouseenter", (e) => { const r = svg.getBoundingClientRect(), br = root.getBoundingClientRect(); tip.show(cfg.tooltip(row), (e.clientX - br.left), y0 + 12); });
        g.addEventListener("mousemove", (e) => { const br = root.getBoundingClientRect(); tip.show(cfg.tooltip(row), e.clientX - br.left, y0 + 12); });
        g.addEventListener("mouseleave", () => tip.hide());
      });
    });
  }

  /* ---- stacked bars: cfg { data[{label, values:{key:n}, id}], stacks[{key,name,color}], selected, onSelect(id), tooltip(d) } ---- */
  function stackedBars(container) {
    return mount(container, (root, cfg, tip) => {
      root.querySelectorAll(":scope > svg").forEach((s) => s.remove());
      const W = Math.max(root.clientWidth, 280), H = 150, m = { l: 26, r: 6, t: 8, b: 26 }, iw = W - m.l - m.r, ih = H - m.t - m.b;
      const totals = cfg.data.map((d) => cfg.stacks.reduce((a, s) => a + (d.values[s.key] || 0), 0));
      const max = nice(Math.max(...totals, 1));
      const bw = Math.min((iw / cfg.data.length) * 0.62, 34), step = iw / cfg.data.length;
      const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, width: W, height: H, class: "ch-svg", role: "img", "aria-label": cfg.label || "bar chart" }, root);
      const grid = svgEl("g", { class: "ch-grid" }, svg);
      for (let k = 0; k <= 2; k++) {
        const v = (max * k) / 2, yy = m.t + ih - (ih * v) / max;
        svgEl("line", { x1: m.l, x2: W - m.r, y1: yy, y2: yy }, grid);
        svgEl("text", { x: m.l - 6, y: yy + 3, "text-anchor": "end" }, grid).textContent = String(+v.toFixed(1));
      }
      cfg.data.forEach((d, i) => {
        const cx = m.l + step * i + step / 2;
        let acc = 0;
        const sel = cfg.selected === d.id;
        const g = svgEl("g", { class: `ch-col${sel ? " sel" : ""}`, style: "cursor:pointer" }, svg);
        svgEl("rect", { x: cx - step / 2, y: m.t, width: step, height: ih + 18, fill: "transparent" }, g);
        cfg.stacks.forEach((s) => {
          const v = d.values[s.key] || 0; if (!v) return;
          const h = (ih * v) / max, yy = m.t + ih - (ih * (acc + v)) / max;
          svgEl("rect", { x: cx - bw / 2, y: yy, width: bw, height: Math.max(h - 1, 1), rx: 2, fill: s.color, opacity: cfg.selected && !sel ? ".35" : "1" }, g);
          acc += v;
        });
        if (step > 22 || i % 2 === 0) svgEl("text", { x: cx, y: H - 8, "text-anchor": "middle", class: `ch-xl${sel ? " on" : ""}` }, g).textContent = d.label;
        g.addEventListener("mouseenter", () => tip.show(cfg.tooltip(d), cx, m.t + ih - (ih * totals[i]) / max));
        g.addEventListener("mouseleave", () => tip.hide());
        g.addEventListener("click", () => cfg.onSelect?.(sel ? null : d.id));
      });
    });
  }

  window.Charts = { lineChart, rangeChart, stackedBars, compact };
})();
