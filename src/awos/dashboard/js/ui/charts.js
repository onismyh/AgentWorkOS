// Inline-SVG / CSS charts (no libraries). Follows the dataviz rules: thin marks, 2px surface gaps, hairline grid,
// tooltips built with textContent, a table-view twin for every chart, status colours only for status.
import { h, raw, setHTML, $, $$, esc } from '../core/html.js';
import { niceStep, compact, usd } from '../core/format.js';
import { tipShow, tipHide } from './overlay.js';
import { execSlot } from '../domain/vocab.js';

const n1 = (v) => Math.round(v * 10) / 10;

/* ------------------------------------------------------------------ sparkline */
export function sparkline(values, { labels = [], name = '', w = 112, hh = 32, unit = '', money = false } = {}) {
  const vals = (values || []).map((v) => Number(v) || 0);
  if (vals.length < 2) return h`<span class="spark-empty" style="width:${w}px;height:${hh}px"></span>`;
  const pad = 4; const max = Math.max(...vals, 1e-9);
  const x = (i) => pad + (i * (w - pad * 2)) / (vals.length - 1);
  const y = (v) => hh - pad - ((hh - pad * 2) * v) / max;
  const pts = vals.map((v, i) => `${n1(x(i))},${n1(y(v))}`);
  const last = vals.length - 1;
  const lineD = `M${pts.join('L')}`;
  const areaD = `${lineD}L${n1(x(last))},${hh - pad}L${n1(x(0))},${hh - pad}Z`;
  const segD = `M${pts[last - 1]}L${pts[last]}`;
  const spec = JSON.stringify({ vals, labels, name, unit, money, w, pad });
  return h`<svg class="spark" width="${w}" height="${hh}" viewBox="0 0 ${w} ${hh}" role="img" aria-label="${name}：${vals.join('、')}" data-spark="${spec}">
    <path class="spark-area" d="${areaD}"/><path class="spark-line" d="${lineD}"/><path class="spark-now" d="${segD}"/>
    <line class="spark-cross" x1="0" x2="0" y1="2" y2="${hh - 2}"/><circle class="spark-hov" r="3.5" cx="-10" cy="-10"/>
    <circle class="spark-dot" r="3.5" cx="${n1(x(last))}" cy="${n1(y(vals[last]))}"/><rect class="spark-hit" width="${w}" height="${hh}" fill="transparent"/></svg>`;
}

/* ------------------------------------------------------------------ level / segment bars */
/** Part-to-whole segmented bar. items: [{key,label,value,color,ink,href,tip}] */
export function segBar(items, { height = 12, showCounts = true } = {}) {
  const total = items.reduce((s, i) => s + i.value, 0);
  if (!total) return h`<div class="segbar empty" style="height:${height}px"></div>`;
  return h`<div class="segbar" style="height:${height}px" role="group">${items.filter((i) => i.value > 0).map((i) => {
    const frac = i.value / total;
    return h`<${raw(i.href ? 'a' : 'span')} class="segbar-i" style="flex:${i.value} 1 0;background:${i.color};color:${i.ink || '#fff'}"${i.href ? h` href="${i.href}"` : ''} tabindex="0" aria-label="${i.label}：${i.value}" data-tipj="${JSON.stringify({ title: i.label, rows: [['数量', `${i.value}`, i.color], ['占比', `${Math.round(frac * 100)}%`]] })}">${showCounts && frac >= 0.09 ? i.value : ''}</${raw(i.href ? 'a' : 'span')}>`;
  })}</div>`;
}

/* ------------------------------------------------------------------ funnel (ordinal ramp) */
export function funnel(stages) {
  const max = Math.max(...stages.map((s) => s.value), 1);
  return h`<ol class="funnel">${stages.map((s, i) => {
    const prev = i ? stages[i - 1].value : null;
    const c0 = prev ? Math.round((s.value / prev) * 100) : null;
    const conv = c0 != null && c0 <= 100 ? c0 : null;   // stages are not strictly nested; only show meaningful ratios
    return h`<li class="fn-row"><span class="fn-l">${s.label}</span>
      <span class="fn-track"><i class="fn-bar" style="width:${Math.max(2, (s.value / max) * 100).toFixed(1)}%;background:var(--l${i + 1})" tabindex="0" data-tipj="${JSON.stringify({ title: s.label, rows: [['数量', `${s.value}`, `var(--l${i + 1})`], ...(conv != null ? [[`较「${stages[i - 1].label}」`, `${conv}%`]] : [])], foot: s.hint || '' })}"></i></span>
      <span class="fn-v"><b>${s.value}</b>${conv != null ? h`<small>${conv}%</small>` : ''}</span></li>`;
  })}</ol>`;
}

/* ------------------------------------------------------------------ horizontal bars (nominal categories: one hue) */
export function hbars(items, { act, max } = {}) {
  const m = max || Math.max(...items.map((i) => i.value), 1);
  return h`<ol class="hbars">${items.map((i, k) => h`<li class="hb-row"><span class="hb-rank">${k + 1}</span>
    ${act ? h`<button type="button" class="hb-l" data-act="${act}" data-kw="${i.label}">${i.label}</button>` : h`<span class="hb-l">${i.label}</span>`}
    <span class="hb-track"><i style="width:${Math.max(4, (i.value / m) * 100).toFixed(1)}%"></i></span><b class="hb-v">${i.value}</b></li>`)}</ol>`;
}

/* ------------------------------------------------------------------ stacked cost columns */
const roundTop = (x, y, w, hh, r) => {
  const rr = Math.min(r, w / 2, hh);
  return `M${x},${y + hh}V${y + rr}A${rr},${rr} 0 0 1 ${x + rr},${y}H${x + w - rr}A${rr},${rr} 0 0 1 ${x + w},${y + rr}V${y + hh}Z`;
};

export function costSpec(series, budget) {
  const days = series?.days || [];
  const rows = series?.cost || [];
  const names = [];
  for (const k of ['codex', 'claude', 'pi', 'dsh', 'internal']) if (rows.some((r) => Number(r[k]) > 0)) names.push(k);
  if (!names.length) names.push('claude');
  const data = days.map((d, i) => {
    const r = rows[i] || {};
    const by = Object.fromEntries(names.map((k) => [k, Number(r[k]) || 0]));
    return { day: d, by, total: names.reduce((s, k) => s + by[k], 0) };
  });
  return { names, data, budget: Number(budget) || 0 };
}

export function renderCostChart(el, spec) {
  const { names, data, budget } = spec;
  const peak = Math.max(0, ...data.map((d) => d.total));
  if (!peak) { setHTML(el, h`<div class="chart-empty">近 14 天没有计费花费。订阅制执行器（Codex 等）不产生按量费用。</div>`); return; }
  const W = Math.max(300, el.clientWidth || 600); const H = 250; const L = 40; const R = 12; const T = 14; const B = 26;
  const top = Math.max(peak, budget * 1.02);
  const step = niceStep(top);
  const max = Math.ceil(top / step) * step;
  const y = (v) => T + (H - T - B) * (1 - v / max);
  const band = (W - L - R) / data.length;
  const bw = Math.min(24, band * 0.6);
  const tick = (v) => `$${v < 1 && v > 0 ? v.toFixed(2) : compact(v)}`;
  const grid = [];
  for (let v = 0; v <= max + 1e-9; v += step) {
    const yy = Math.round(y(v)) + 0.5;
    grid.push(h`<line class="${v === 0 ? 'ax-base' : 'ax-grid'}" x1="${L}" x2="${W - R}" y1="${yy}" y2="${yy}"/><text class="ax-t" x="${L - 8}" y="${yy + 3.5}" text-anchor="end">${tick(v)}</text>`);
  }
  const every = band < 34 ? 2 : 1;
  const bars = data.map((d, i) => {
    const cx = L + band * i + band / 2;
    let base = y(0);
    const segs = names.map((k) => [k, d.by[k]]).filter(([, v]) => v > 0);
    const marks = segs.map(([k, v], si) => {
      const topSeg = si === segs.length - 1;
      const hh = Math.max(2, (H - T - B) * (v / max));
      const gap = topSeg ? 0 : 2;
      const yTop = base - hh;
      const drawH = Math.max(1, hh - gap);
      base = yTop;
      return topSeg
        ? h`<path d="${roundTop(cx - bw / 2, yTop + gap, bw, drawH, 4)}" fill="var(--s${execSlot(k)})"/>`
        : h`<rect x="${cx - bw / 2}" y="${yTop + gap}" width="${bw}" height="${drawH}" fill="var(--s${execSlot(k)})"/>`;
    });
    const tip = JSON.stringify({ title: d.day, rows: [...names.map((k) => [k, usd(d.by[k]), `var(--s${execSlot(k)})`]), ['合计', usd(d.total)]], foot: budget ? `预算 ${usd(budget)}（${Math.round((d.total / budget) * 100)}%）` : '' });
    const isLast = i === data.length - 1;
    return h`<g class="cbar" tabindex="0" aria-label="${d.day} 合计 ${usd(d.total)}" data-tipj="${tip}" data-tip-at="${cx},${y(d.total)}"><rect class="hit" x="${L + band * i}" y="${T}" width="${band}" height="${H - T - B}"/>${marks}
      ${isLast && d.total > 0 ? h`<text class="ax-v" x="${cx}" y="${y(d.total) - 6}" text-anchor="middle">${usd(d.total)}</text>` : ''}
      ${i % every === (data.length - 1) % every ? h`<text class="ax-t${isLast ? ' now' : ''}" x="${cx}" y="${H - 8}" text-anchor="middle">${d.day.replace('-', '/')}</text>` : ''}</g>`;
  });
  const bud = budget ? h`<g class="bud"><line x1="${L}" x2="${W - R}" y1="${Math.round(y(budget)) + 0.5}" y2="${Math.round(y(budget)) + 0.5}"/><text x="${W - R}" y="${y(budget) - 5}" text-anchor="end">日预算 ${usd(budget)}</text></g>` : '';
  setHTML(el, h`<svg class="cost-svg" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="img" aria-label="近 ${data.length} 天花费，按执行器堆叠，最近一天 ${usd(data[data.length - 1].total)}">${grid}${bars}${bud}</svg>`);
}

export function costTable(spec) {
  const { names, data } = spec;
  return h`<div class="table-wrap"><table class="t t-compact"><thead><tr><th>日期</th>${names.map((n) => h`<th class="num">${n}</th>`)}<th class="num">合计</th></tr></thead><tbody>${
    data.slice().reverse().map((d) => h`<tr><td class="num">${d.day}</td>${names.map((k) => h`<td class="num">${usd(d.by[k])}</td>`)}<td class="num"><b>${usd(d.total)}</b></td></tr>`)}</tbody></table></div>`;
}

const resizeObs = new WeakMap();
export function mountCostChart(el, spec) {
  renderCostChart(el, spec);
  resizeObs.get(el)?.disconnect();
  if (typeof ResizeObserver === 'undefined') return;
  let last = el.clientWidth; let timer = null;
  const ro = new ResizeObserver(() => {
    if (!el.isConnected) { ro.disconnect(); return; }
    if (Math.abs(el.clientWidth - last) < 8) return;
    last = el.clientWidth;
    clearTimeout(timer); timer = setTimeout(() => renderCostChart(el, spec), 120);
  });
  ro.observe(el);
  resizeObs.set(el, ro);
}

/* ------------------------------------------------------------------ interaction (one delegated binder) */
export function bindCharts() {
  // sparklines: crosshair snaps to the nearest day
  document.addEventListener('pointermove', (e) => {
    const sp = e.target.closest?.('.spark');
    if (sp) {
      let spec; try { spec = JSON.parse(sp.dataset.spark); } catch { return; }
      const r = sp.getBoundingClientRect();
      const n = spec.vals.length;
      const rx = ((e.clientX - r.left) / r.width) * spec.w;
      const i = Math.max(0, Math.min(n - 1, Math.round(((rx - spec.pad) / (spec.w - spec.pad * 2)) * (n - 1))));
      const x = spec.pad + (i * (spec.w - spec.pad * 2)) / (n - 1);
      const max = Math.max(...spec.vals, 1e-9);
      const yy = 32 - spec.pad - ((32 - spec.pad * 2) * spec.vals[i]) / max;
      const cross = $('.spark-cross', sp); const hov = $('.spark-hov', sp);
      cross.setAttribute('x1', x); cross.setAttribute('x2', x); hov.setAttribute('cx', x); hov.setAttribute('cy', yy);
      sp.classList.add('on');
      const v = spec.money ? usd(spec.vals[i]) : `${spec.vals[i]}${spec.unit || ''}`;
      tipShow(r.left + (x / spec.w) * r.width, r.top, { title: spec.labels[i] || `第 ${i + 1} 天`, rows: [[spec.name || '数值', v]] });
      return;
    }
    const t = e.target.closest?.('[data-tipj]');
    if (t) {
      let spec; try { spec = JSON.parse(t.dataset.tipj); } catch { return; }
      $$('.cbar.on').forEach((n) => n !== t && n.classList.remove('on'));
      t.classList.add('on');
      tipShow(e.clientX, e.clientY, spec, { below: false });
    }
  });
  document.addEventListener('pointerout', (e) => {
    const sp = e.target.closest?.('.spark'); if (sp && !sp.contains(e.relatedTarget)) { sp.classList.remove('on'); tipHide(); }
    const t = e.target.closest?.('[data-tipj]');
    if (t && !t.contains(e.relatedTarget)) { t.classList.remove('on'); tipHide(); }
  });
  document.addEventListener('focusin', (e) => {
    const t = e.target.closest?.('[data-tipj]');
    if (!t) return;
    let spec; try { spec = JSON.parse(t.dataset.tipj); } catch { return; }
    const r = t.getBoundingClientRect();
    tipShow(r.left + r.width / 2, r.top, spec);
    t.classList.add('on');
  });
  document.addEventListener('focusout', (e) => { const t = e.target.closest?.('[data-tipj]'); if (t) { t.classList.remove('on'); tipHide(); } });
}
export { esc };
