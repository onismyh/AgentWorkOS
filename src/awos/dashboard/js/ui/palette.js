// Command palette (Ctrl/⌘-K): jump to views, run actions, search tasks.
import { h, raw, setHTML, clearEl, $, $$, enc } from '../core/html.js';
import { api } from '../core/api.js';
import { ACTS } from '../core/acts.js';
import { S } from '../core/state.js';
import { go, openTask } from '../core/router.js';
import { icon } from './icons.js';
import { kbd, statusIcon } from './components.js';
import { digestOf, META } from '../domain/vocab.js';
import { NAV_ITEMS } from '../shell-nav.js';

let open = false;
export const paletteOpen = () => open;

const norm = (s) => String(s).toLowerCase().replace(/\s+/g, '');
const score = (q, label, extra = '') => {
  if (!q) return 1;
  const l = norm(label); const x = norm(extra); const n = norm(q);
  if (l.startsWith(n)) return 4; if (l.includes(n)) return 3; if (x.includes(n)) return 2;
  let i = 0; for (const c of l) { if (c === n[i]) i++; if (i === n.length) return 1; }
  return 0;
};

export function openPalette(prefill = '') {
  if (open) { $('#pal-input')?.focus(); return; }
  open = true;
  const host = $('#palette-host');
  const prev = document.activeElement;
  setHTML(host, h`<div class="overlay pal-overlay" data-overlay><div class="palette" role="dialog" aria-modal="true" aria-label="命令面板">
    <div class="pal-in">${icon('search', { size: 16 })}<input id="pal-input" role="combobox" aria-expanded="true" aria-controls="pal-list" aria-autocomplete="list" autocomplete="off" spellcheck="false" placeholder="跳转到、搜索任务或执行操作…" value="${prefill}">${kbd('Esc')}</div>
    <div class="pal-list" id="pal-list" role="listbox"></div>
    <div class="pal-foot"><span>${kbd('↑ ↓')}选择</span><span>${kbd('Enter')}执行</span><span>${kbd('Esc')}关闭</span></div></div></div>`);
  const input = $('#pal-input'); const list = $('#pal-list');
  let items = []; let idx = 0; let seq = 0; let timer = null;

  const actions = () => [
    { id: 'a-new', group: '操作', label: '新建任务', ic: 'plus', hint: 'N', run: () => ACTS['new-task']?.() },
    { id: 'a-refresh', group: '操作', label: '刷新当前视图', ic: 'refresh', hint: 'R', run: () => S.refresh(true) },
    { id: 'a-theme', group: '操作', label: '切换主题（浅色 / 深色 / 跟随系统）', ic: 'moon', run: () => ACTS.theme?.() },
    { id: 'a-nav', group: '操作', label: '折叠 / 展开侧栏', ic: 'panel-left', hint: '[', run: () => ACTS['toggle-nav']?.() },
    { id: 'a-keys', group: '操作', label: '查看键盘快捷键', ic: 'keyboard', hint: '?', run: () => ACTS.shortcuts?.() },
  ];
  const compute = (q, remote = []) => {
    const out = [];
    for (const n of NAV_ITEMS) { const s = score(q, n.title, `${n.group} ${n.key}`); if (s) out.push({ id: `v-${n.key}`, group: '跳转', label: n.title, ic: n.icon, hint: n.group, s, run: () => go(n.key) }); }
    for (const a of actions()) { const s = score(q, a.label); if (s) out.push({ ...a, s }); }
    for (const t of remote) { const dg = digestOf(t, 'task'); out.push({ id: `t-${t.id}`, group: '任务', label: dg.headline, sub: dg.gist, ic: null, status: t.status, s: 5, run: () => openTask(t.id) }); }
    const order = { 任务: 0, 跳转: 1, 操作: 2 };
    return out.sort((a, b) => (order[a.group] - order[b.group]) || b.s - a.s);
  };
  const paint = () => {
    if (!items.length) { setHTML(list, h`<div class="pal-empty">没有匹配的结果</div>`); return; }
    let last = '';
    setHTML(list, h`${items.map((it, i) => {
      const head = it.group !== last ? h`<div class="pal-g" role="presentation">${it.group}</div>` : '';
      last = it.group;
      return h`${head}<div class="pal-i${i === idx ? ' on' : ''}" role="option" id="pal-${i}" aria-selected="${i === idx}" data-i="${i}">${it.ic ? icon(it.ic, { size: 16 }) : statusIcon(it.status)}<span class="pal-t"><span>${it.label}</span>${it.sub ? h`<small>${it.sub}</small>` : ''}</span>${it.hint ? (it.hint.length <= 2 ? kbd(it.hint) : h`<small class="muted">${it.hint}</small>`) : ''}</div>`;
    })}`);
    input.setAttribute('aria-activedescendant', `pal-${idx}`);
    $('.pal-i.on', list)?.scrollIntoView({ block: 'nearest' });
  };
  const update = async () => {
    const q = input.value.trim();
    items = compute(q); idx = 0; paint();
    clearTimeout(timer);
    if (q.length >= 2) {
      const my = ++seq;
      timer = setTimeout(async () => {
        try {
          const d = await api(`/tasks?search=${enc(q)}&limit=8`);
          if (my !== seq || !open) return;
          items = compute(q, d.tasks); idx = Math.min(idx, items.length - 1); paint();
        } catch { /* offline: local results only */ }
      }, 220);
    }
  };
  const close = () => {
    if (!open) return;
    open = false; clearTimeout(timer);
    document.removeEventListener('keydown', onKey, true);
    clearEl(host);
    try { prev?.focus?.(); } catch { /* gone */ }
  };
  const run = (i) => { const it = items[i]; if (!it) return; close(); setTimeout(() => it.run(), 0); };
  const onKey = (e) => {
    if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); close(); return; }
    if (e.key === 'ArrowDown') { e.preventDefault(); idx = Math.min(items.length - 1, idx + 1); paint(); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); idx = Math.max(0, idx - 1); paint(); }
    else if (e.key === 'Enter') { e.preventDefault(); run(idx); }
    else if (e.key === 'Tab') { e.preventDefault(); }
  };
  document.addEventListener('keydown', onKey, true);
  input.addEventListener('input', update);
  list.addEventListener('mousemove', (e) => { const el = e.target.closest('.pal-i'); if (el && Number(el.dataset.i) !== idx) { idx = Number(el.dataset.i); $$('.pal-i', list).forEach((n, i) => { n.classList.toggle('on', i === idx); n.setAttribute('aria-selected', i === idx); }); } });
  list.addEventListener('click', (e) => { const el = e.target.closest('.pal-i'); if (el) run(Number(el.dataset.i)); });
  $('.pal-overlay', host).addEventListener('mousedown', (e) => { if (e.target.classList.contains('pal-overlay')) close(); });
  update();
  input.focus();
  if (prefill) input.setSelectionRange(prefill.length, prefill.length);
}

/* ------------------------------------------------------------------ shortcuts sheet */
export const SHORTCUTS = [
  ['全局', [['Ctrl K', '命令面板'], ['G 然后 O/B/I/X/T', '跳转：总览 / 看板 / 收件箱 / Outbox / 任务'], ['N', '新建任务'], ['R', '刷新（收件箱里 R = 返工）'], ['[', '折叠 / 展开侧栏'], ['Esc', '关闭抽屉 / 返回'], ['?', '本帮助']]],
  ['收件箱 / Outbox', [['J / K', '下一条 / 上一条'], ['Enter', '聚焦阅读窗格'], ['E', '接受 / 验收通过 / 批准 AI 推荐项'], ['X', '忽略（会询问原因）'], ['S', '稍后处理'], ['R', '返工'], ['#', '取消任务（会询问原因）'], ['U', '标为未读'], ['/', '搜索']]],
  ['看板', [['拖拽 建议→排队', '接受'], ['拖拽 建议/排队→已忽略', '忽略'], ['Enter', '打开聚焦的卡片']]],
];
ACTS.shortcuts = async () => {
  const { dialog } = await import('./overlay.js');
  dialog({ title: '键盘快捷键', icon: 'keyboard', hideSubmit: true, wide: true,
    body: h`<div class="keys-grid">${SHORTCUTS.map(([g, rows]) => h`<section><h4>${g}</h4>${rows.map(([k, t]) => h`<div class="keys-r">${kbd(k)}<span>${t}</span></div>`)}</section>`)}</div>` });
};
