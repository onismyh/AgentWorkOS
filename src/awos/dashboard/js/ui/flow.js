// Block diagram of a project's work: tasks as boxes, dependencies / roll-ups as arrows, your approvals as gates.
// Pure inline SVG (no library): layered left-to-right layout, colours come from CSS classes so both themes work.
import { h } from '../core/html.js';
import { META, STATUS_VIEW, execLabel } from '../domain/vocab.js';

const W = 184; const H = 60; const GH = 46; const CG = 54; const RG = 12; const PAD = 14; const MAXROWS = 8;

const cw = (ch) => (ch.charCodeAt(0) > 0x2e80 ? 12 : 6.6);
/** Wrap text into at most `maxLines` lines of `maxPx` pixels (CJK 12px, latin 6.6px per glyph); the last line ends with an ellipsis if cut. */
export function wrapText(text, maxPx, maxLines) {
  const lines = []; let cur = ''; let w = 0;
  for (const ch of String(text ?? '')) {
    const c = cw(ch);
    if (w + c > maxPx && cur) { lines.push(cur); cur = ''; w = 0; }
    cur += ch; w += c;
  }
  if (cur) lines.push(cur);
  if (lines.length <= maxLines) return lines;
  const out = lines.slice(0, maxLines);
  out[maxLines - 1] = `${out[maxLines - 1].replace(/.$/, '')}…`;
  return out;
}

/** Longest-path ranks (left to right), barycentre ordering inside a rank, tall ranks wrapped into several sub-columns. */
export function layoutFlow(graph) {
  const nodes = (graph.nodes || []).map((n) => ({ ...n }));
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const edges = (graph.edges || []).filter((e) => byId.has(e.from) && byId.has(e.to) && e.from !== e.to);
  const rank = new Map(nodes.map((n) => [n.id, 0]));
  for (let i = 0; i < nodes.length; i++) {
    let changed = false;
    for (const e of edges) {
      const r = rank.get(e.from) + 1;
      if (r > rank.get(e.to) && r <= nodes.length) { rank.set(e.to, r); changed = true; }
    }
    if (!changed) break;
  }
  const maxRank = Math.max(0, ...rank.values());
  const cols = Array.from({ length: maxRank + 1 }, () => []);
  nodes.sort((a, b) => String(a.ts || '').localeCompare(String(b.ts || '')));
  nodes.forEach((n) => cols[rank.get(n.id)].push(n));
  const idx = new Map();
  cols[0].forEach((n, i) => idx.set(n.id, i));
  for (let r = 1; r <= maxRank; r++) {
    const bary = (n) => {
      const ps = edges.filter((e) => e.to === n.id && idx.has(e.from)).map((e) => idx.get(e.from));
      return ps.length ? ps.reduce((s, v) => s + v, 0) / ps.length : 1e6;
    };
    cols[r].sort((a, b) => bary(a) - bary(b));
    cols[r].forEach((n, i) => idx.set(n.id, i));
  }
  let x = PAD; let height = 0;
  cols.forEach((col) => {
    const subs = Math.max(1, Math.ceil(col.length / MAXROWS));
    for (let s = 0; s < subs; s++) {
      let y = PAD;
      col.slice(s * MAXROWS, (s + 1) * MAXROWS).forEach((n) => {
        n.w = W; n.h = n.type === 'gate' ? GH : H; n.x = x; n.y = y; y += n.h + RG;
      });
      height = Math.max(height, y - RG + PAD);
      x += W + CG;
    }
  });
  const laid = new Map(nodes.map((n) => [n.id, n]));
  const paths = edges.map((e) => {
    const a = laid.get(e.from); const b = laid.get(e.to);
    const x1 = a.x + a.w; const y1 = a.y + a.h / 2; const x2 = b.x; const y2 = b.y + b.h / 2;
    const dx = Math.max(24, Math.abs(x2 - x1) / 2);
    return { ...e, d: `M${x1} ${y1} C${x1 + dx} ${y1} ${x2 - dx} ${y2} ${x2} ${y2}` };
  });
  return { nodes, edges: paths, width: Math.max(x - CG + PAD, 2 * PAD + W), height: Math.max(height, 2 * PAD + H) };
}

const GATE_VIEW = { waiting: { tone: 'warn', head: '等你确认' }, decided: { tone: 'brand', head: '你已决定' }, rejected: { tone: 'muted', head: '你拒绝了' }, expired: { tone: 'muted', head: '过期未处理' } };

function taskNode(n) {
  const v = STATUS_VIEW[n.status] || { tone: 'muted' };
  const lines = wrapText(n.title, W - 26, 2);
  const sub = [`L${n.level}`, n.executor ? execLabel(n.executor) : '', META.statusLabel[n.status] || n.status].filter(Boolean).join(' · ');
  return h`<g class="fl-n fl-task tone-${v.tone}${n.status === 'running' ? ' fl-live' : ''}" transform="translate(${n.x} ${n.y})" data-act="open-task" data-id="${n.id}" tabindex="0" role="button" aria-label="任务：${n.title}，${META.statusLabel[n.status] || n.status}">
    <title>${n.title}（${META.statusLabel[n.status] || n.status}）</title>
    <rect class="fl-bg" width="${n.w}" height="${n.h}" rx="11"/><rect class="fl-bar" x="0" y="10" width="4" height="${n.h - 20}" rx="2"/>
    ${lines.map((l, i) => h`<text class="fl-t" x="15" y="${20 + i * 14}">${l}</text>`)}
    <text class="fl-s" x="15" y="${n.h - 8}">${sub}</text></g>`;
}

function gateNode(n) {
  const v = GATE_VIEW[n.state] || GATE_VIEW.decided;
  const plain = !n.decision || /^(批准|同意|approve)/i.test(n.decision);   // "批准执行" is just a yes
  const head = n.state === 'decided' ? (plain ? '你已批准' : `你选了「${n.decision}」`) : v.head;
  const [line] = wrapText(n.title, W - 30, 1);
  return h`<g class="fl-n fl-gate tone-${v.tone}" transform="translate(${n.x} ${n.y})" data-act="open-task" data-id="${n.task}" tabindex="0" role="button" aria-label="${head}：${n.title}">
    <title>${head}：${n.title}</title>
    <rect class="fl-bg" width="${n.w}" height="${n.h}" rx="${n.h / 2}"/><circle class="fl-dot" cx="17" cy="${n.h / 2}" r="5"/>
    <text class="fl-t" x="30" y="${n.h / 2 - 3}">${wrapText(head, W - 40, 1)[0]}</text><text class="fl-s" x="30" y="${n.h / 2 + 11}">${line}</text></g>`;
}

export function flowSVG(graph) {
  const L = layoutFlow(graph);
  return h`<svg class="fl" width="${L.width}" height="${L.height}" viewBox="0 0 ${L.width} ${L.height}" role="group" aria-label="任务流程图：方框是任务，胶囊是需要你拍板的节点，箭头表示先后顺序">
    <defs><marker id="fl-arrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto"><path class="fl-ah" d="M0 0 L8 4 L0 8 z"/></marker></defs>
    ${L.edges.map((e) => h`<path class="fl-e fl-e-${e.type}" d="${e.d}" marker-end="url(#fl-arrow)"/>`)}
    ${L.nodes.map((n) => (n.type === 'gate' ? gateNode(n) : taskNode(n)))}</svg>`;
}
