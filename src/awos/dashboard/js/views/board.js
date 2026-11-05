// 看板: kanban with swim-lanes, filters, density, drag & drop (accept / dismiss) and hover quick actions.
import { h, raw, setHTML, $, $$, enc } from '../core/html.js';
import { api, apiOpt } from '../core/api.js';
import { ACTS, CHANGES, INPUTS } from '../core/acts.js';
import { S, cacheTask } from '../core/state.js';
import { pref } from '../core/store.js';
import { openTask } from '../core/router.js';
import { when, usd, cut, parseTs, absTime } from '../core/format.js';
import { icon } from '../ui/icons.js';
import { btn, iconBtn, badge, kwChips, levelBadge, prioIcon, execAvatar, empty, progress, statusIcon, seg, skeletonList, kbd, tipAttr, projectChip } from '../ui/components.js';
import { toast } from '../ui/overlay.js';
import { META, digestOf, catLabel, effLevel, TERMINAL } from '../domain/vocab.js';
import { taskAction, taskActionUndoable } from '../domain/actions.js';
import { loadProjects, projectOfTopic } from '../domain/projects.js';

const COL_VIEW = {
  suggested: { icon: 'circle-dashed', tone: 'brand' }, queued: { icon: 'circle-half', tone: 'muted' }, running: { icon: 'loader', tone: 'brand', spin: true },
  needs_you: { icon: 'hand', tone: 'warn' }, done: { icon: 'circle-check', tone: 'good' }, failed: { icon: 'circle-x', tone: 'crit' }, dismissed: { icon: 'ban', tone: 'muted' },
};
const GROUPS = [{ key: 'none', label: '无' }, { key: 'topic', label: '话题' }, { key: 'level', label: '等级' }, { key: 'category', label: '类别' }, { key: 'project', label: '项目' }];
const B = {
  data: null, f: { q: '', level: '', category: '', project: '', executor: '' },
  group: pref.get('board.group', 'none'), density: pref.get('board.density', 'comfortable'),
  collapsed: pref.get('board.collapsed', { failed: true, dismissed: true }), dragging: null, laneCollapsed: {},
};
export const boardBusy = () => !!B.dragging;

/* ------------------------------------------------------------------ data */
const COLS_FALLBACK = [
  ['suggested', '建议', 'AI 发现的事项，等你决定做不做', ['inbox', 'triaged', 'snoozed']], ['queued', '排队', '已接受，等待调度或依赖', ['ready', 'blocked']],
  ['running', '执行中', 'Agent 正在推进', ['running']], ['needs_you', '等你', '需要你审批、验收或接手', ['waiting_approval', 'waiting_user', 'review']],
  ['done', '已完成', '最近完成的', ['done']], ['failed', '失败', '重试耗尽或不可恢复', ['failed']], ['dismissed', '已忽略', '你忽略或取消的', ['dismissed', 'cancelled']],
];
async function fallbackBoard() {
  const d = await api('/tasks?root_only=true&limit=500');
  return {
    columns: COLS_FALLBACK.map(([key, title, hint, statuses]) => {
      const items = d.tasks.filter((t) => statuses.includes(t.status)).map((t) => ({ ...t, children: { total: t.children_total || 0, done: t.children_done || 0, running: 0 }, snippet: (t.summary || t.description || '').slice(0, 180) }));
      return { key, title, hint, statuses, count: items.length, items };
    }),
    totals: { open: 0, done_7d: 0 }, updated_at: null,
  };
}
export async function load() {
  const [board] = await Promise.all([apiOpt('/board'), loadProjects().catch(() => null)]);   // the project list maps topics to projects (chips, lanes)
  const d = board || (await fallbackBoard());
  d.columns.forEach((c) => c.items.forEach(cacheTask));
  B.data = d;
  return d;
}

/* ------------------------------------------------------------------ filtering / grouping */
/** The project name of a card: the canonical project of its topic when known, else the task's own `project` text. */
const projectName = (t) => projectOfTopic(t.topic_key)?.name || t.project || '';
const matches = (t) => {
  const f = B.f;
  if (f.q) {
    const hay = `${t.headline} ${t.title} ${t.gist} ${(t.keywords || []).join(' ')} ${projectName(t)} ${t.topic_title || ''} ${t.id}`.toLowerCase();
    if (!hay.includes(f.q.toLowerCase())) return false;
  }
  if (f.level && String(effLevel(t)) !== f.level) return false;
  if (f.category && t.category !== f.category) return false;
  if (f.project && projectName(t) !== f.project) return false;
  if (f.executor && (t.executor || '') !== f.executor) return false;
  return true;
};
const laneKey = (t) => (B.group === 'topic' ? (t.topic_title || '（无话题）') : B.group === 'level' ? `L${effLevel(t)}` : B.group === 'category' ? catLabel(t.category) : B.group === 'project' ? (projectName(t) || '（无项目）') : '');
const laneSort = (a, b) => (B.group === 'level' ? a.localeCompare(b) : a.startsWith('（') ? 1 : b.startsWith('（') ? -1 : a.localeCompare(b, 'zh'));

/* ------------------------------------------------------------------ card */
function quickActions(t, col) {
  if (col === 'suggested') return h`<div class="bc-qa">${iconBtn('check', { act: 'bc-act', data: { id: t.id, action: 'accept' }, tip: '接受并排队', size: 'icon-sm', variant: 'secondary' })}${iconBtn('clock', { act: 'bc-act', data: { id: t.id, action: 'snooze' }, tip: '稍后（24 小时）', size: 'icon-sm', variant: 'secondary' })}${iconBtn('x', { act: 'bc-act', data: { id: t.id, action: 'dismiss' }, tip: '忽略…', size: 'icon-sm', variant: 'secondary' })}</div>`;
  if (col === 'failed') return h`<div class="bc-qa">${iconBtn('rotate', { act: 'bc-act', data: { id: t.id, action: 'retry' }, tip: '重试', size: 'icon-sm', variant: 'secondary' })}${iconBtn('x', { act: 'bc-act', data: { id: t.id, action: 'dismiss' }, tip: '忽略…', size: 'icon-sm', variant: 'secondary' })}</div>`;
  return '';
}
export function boardCard(t, col, density) {
  const dg = digestOf(t, 'task');
  const pj = projectOfTopic(t.topic_key);
  const due = t.due_at ? parseTs(t.due_at) : null;
  const overdue = due && due.getTime() < Date.now() && !TERMINAL.has(t.status);
  const ch = t.children || { total: 0, done: 0 };
  const primary = { suggested: 'triaged', queued: 'ready', running: 'running', done: 'done', failed: 'failed' }[col] || '';
  return h`<article class="bcard${overdue ? ' overdue' : ''}" draggable="true" tabindex="0" role="button" data-act="open-task" data-id="${t.id}" data-col="${col}" data-status="${t.status}" aria-label="${dg.headline}">
    <div class="bc-top">${statusIcon(t.status)}<h4 class="bc-title">${dg.headline}</h4>${quickActions(t, col)}</div>
    ${density === 'comfortable' && dg.gist ? h`<p class="bc-gist">${dg.gist}</p>` : ''}
    ${t.topic_key ? h`<div class="bc-tp">${projectChip(pj, { act: 'open-project-inbox', data: { key: pj?.key }, cls: 'bc-pj' })}<button type="button" class="bc-topic" data-act="open-topic" data-key="${t.topic_key}" data-tip="话题：${t.topic_title}（点击查看前情提要）">${icon('layers', { size: 11 })}<span>${t.topic_title}</span></button></div>` : ''}
    ${dg.keywords.length ? h`<div class="bc-kws">${kwChips(dg.keywords, { max: density === 'comfortable' ? 4 : 3 })}</div>` : ''}
    <div class="bc-meta">${levelBadge(t)}${prioIcon(t.priority)}<span class="bc-cat">${catLabel(t.category)}</span>${t.status !== primary && META.statusLabel[t.status] && col !== 'running' ? h`<span class="bc-st">${META.statusLabel[t.status]}</span>` : ''}<span class="grow"></span>${t.executor ? execAvatar(t.executor, { size: 20 }) : ''}</div>
    ${density === 'comfortable' && ((t.project && !pj) || due || t.cost_usd) ? h`<div class="bc-sub">${t.project && !pj ? h`<span class="bc-i">${icon('folder', { size: 12 })}${cut(t.project, 16)}</span>` : ''}${due ? h`<span class="bc-i${overdue ? ' tone-crit-ink' : ''}">${icon('calendar', { size: 12 })}${overdue ? '逾期 ' : ''}${absTime(t.due_at)}</span>` : ''}${t.cost_usd ? h`<span class="bc-i">${icon('dollar', { size: 12 })}${usd(t.cost_usd).replace('$', '')}</span>` : ''}</div>` : ''}
    <div class="bc-foot">${ch.total ? progress(ch.done / ch.total, { label: `${ch.done}/${ch.total}`, tip: `子任务 ${ch.done}/${ch.total} 已完成${ch.running ? `，${ch.running} 个运行中` : ''}` }) : ''}${t.pending_approvals ? badge(`${t.pending_approvals} 待审批`, { tone: 'warn', icon: 'shield-alert', variant: 'soft' }) : ''}<span class="grow"></span><span class="bc-w">${when(t.updated_at, { short: true })}</span></div>
  </article>`;
}

/* ------------------------------------------------------------------ board */
function cell(col, items, laneId) {
  return h`<div class="cell${B.collapsed[col.key] ? ' collapsed' : ''}" data-col="${col.key}" data-scroll="cell-${col.key}-${laneId}" role="list" aria-label="${col.title}">${B.collapsed[col.key] ? h`<button type="button" class="cell-vert" data-act="bc-collapse" data-col="${col.key}" aria-label="展开${col.title}"><b>${col.title}</b><span class="bh-n">${items.length}</span></button>` : items.length ? items.map((t) => boardCard(t, col.key, B.density)) : h`<div class="cell-empty">${icon(COL_VIEW[col.key]?.icon || 'circle', { size: 18 })}<span>${col.key === 'suggested' ? '没有待决定的建议' : col.key === 'needs_you' ? '没有在等你的事项' : col.key === 'running' ? '没有运行中的任务' : col.key === 'done' ? '近期没有完成的任务' : col.key === 'failed' ? '没有失败的任务' : col.key === 'dismissed' ? '没有被忽略的事项' : '这一列是空的'}</span></div>`}</div>`;
}

function boardHTML(d) {
  const cols = d.columns;
  const tpl = cols.map((c) => (B.collapsed[c.key] ? '44px' : 'minmax(256px, 1fr)')).join(' ');
  const minw = cols.reduce((s, c) => s + (B.collapsed[c.key] ? 44 : 256), 0) + 10 * (cols.length - 1);
  const filtered = cols.map((c) => c.items.filter(matches));
  const head = cols.map((c, i) => {
    const v = COL_VIEW[c.key] || { icon: 'circle', tone: 'muted' };
    const collapsed = B.collapsed[c.key];
    const n = filtered[i].length;
    return h`<div class="bhead${collapsed ? ' collapsed' : ''} tone-${v.tone}" data-col="${c.key}"><button type="button" class="bh-b" data-act="bc-collapse" data-col="${c.key}" aria-expanded="${!collapsed}" data-tip="${collapsed ? `${c.title} · ${n}（点击展开）` : `${c.hint}（点击折叠）`}" aria-label="${c.title}"><span class="bh-i${v.spin ? ' spin' : ''}">${icon(v.icon, { size: 15 })}</span>${collapsed ? '' : h`<span class="bh-t">${c.title}</span><span class="bh-h">${c.hint}</span><span class="bh-n">${n === c.count ? n : `${n}/${c.count}`}</span>`}</button></div>`;
  });
  let lanes;
  if (B.group === 'none') {
    lanes = h`<div class="lane lane-flat"><div class="lane-row">${cols.map((c, i) => cell(c, filtered[i], 'all'))}</div></div>`;
  } else {
    const keys = [...new Set(filtered.flat().map(laneKey))].sort(laneSort);
    lanes = keys.length ? keys.map((k, li) => {
      const total = filtered.reduce((s, items) => s + items.filter((t) => laneKey(t) === k).length, 0);
      const closed = B.laneCollapsed[k];
      return h`<div class="lane"><button type="button" class="lane-h" data-act="lane-toggle" data-lane="${k}" aria-expanded="${!closed}">${icon(closed ? 'chevron-right' : 'chevron-down', { size: 14 })}<b>${k}</b><span class="bh-n">${total}</span></button>
        ${closed ? '' : h`<div class="lane-row">${cols.map((c, i) => cell(c, filtered[i].filter((t) => laneKey(t) === k), `l${li}`))}</div>`}</div>`;
    }) : empty({ icon: 'search', title: '没有符合筛选条件的事项' });
  }
  return h`<div class="board${B.group !== 'none' ? ' swim' : ''}${B.density === 'compact' ? ' compact' : ''}" style="--cols:${tpl};--minw:${minw}px" id="board" data-density="${B.density}"><div class="board-head">${head}</div><div class="board-lanes">${lanes}</div></div>`;
}

function toolbar(d) {
  const all = d.columns.flatMap((c) => c.items);
  const uniq = (fn) => [...new Set(all.map(fn).filter(Boolean))].sort((a, b) => String(a).localeCompare(String(b), 'zh'));
  const sel = (key, label, opts) => h`<select class="select select-sm" data-change="board-f" data-key="${key}" aria-label="${label}"><option value="">${label}</option>${opts.map(([v, l]) => h`<option value="${v}"${B.f[key] === String(v) ? raw(' selected') : ''}>${l}</option>`)}</select>`;
  const shown = d.columns.reduce((s, c) => s + c.items.filter(matches).length, 0);
  const total = d.columns.reduce((s, c) => s + c.items.length, 0);
  return h`<div class="toolbar"><div class="search-box">${icon('search', { size: 15 })}<input class="input input-bare" type="search" id="board-q" data-input="board-q" placeholder="筛选卡片：标题、关键词、项目…" value="${B.f.q}" aria-label="筛选卡片" autocomplete="off"></div>
    ${sel('level', '全部等级', [1, 2, 3, 4, 5].filter((n) => all.some((t) => effLevel(t) === n)).map((n) => [n, `L${n} ${(META.levels[n]?.name || '').replace(/^L\d\s*/, '')}`]))}
    ${sel('category', '全部类别', uniq((t) => t.category).map((c) => [c, catLabel(c)]))}
    ${sel('project', '全部项目', uniq(projectName).map((p) => [p, p]))}
    ${sel('executor', '全部执行器', uniq((t) => t.executor).map((e) => [e, e]))}
    <span class="grow"></span><span class="tb-count muted small">${shown === total ? `${total} 项` : `${shown}/${total} 项`}${d.totals ? ` · 近 7 天完成 ${d.totals.done_7d}` : ''}</span>
    <label class="tb-l muted small">分组</label>${seg(GROUPS, B.group, { act: 'board-group', key: 'g', label: '分组方式' })}
    ${seg([{ key: 'comfortable', icon: 'rows', tip: '舒适' }, { key: 'compact', icon: 'list', tip: '紧凑' }], B.density, { act: 'board-density', key: 'd', label: '密度' })}</div>`;
}

export function render(d) {
  return h`<div class="board-page">${toolbar(d)}<div class="board-scroll" id="board-host" data-scroll="board-x">${boardHTML(d)}</div></div>`;
}
function paintBoard() {
  const host = $('#board-host'); if (!host || !B.data) return;
  const sx = host.scrollLeft;
  const snap = $$('[data-scroll]', host).map((n) => [n.dataset.scroll, n.scrollTop]);
  setHTML(host, boardHTML(B.data));
  host.scrollLeft = sx;
  snap.forEach(([k, v]) => { const n = $(`[data-scroll="${CSS.escape(k)}"]`, host); if (n) n.scrollTop = v; });
  const tb = $('.toolbar .tb-count');
  if (tb) { const shown = B.data.columns.reduce((s, c) => s + c.items.filter(matches).length, 0); const total = B.data.columns.reduce((s, c) => s + c.items.length, 0); tb.textContent = `${shown === total ? total : `${shown}/${total}`} 项${B.data.totals ? ` · 近 7 天完成 ${B.data.totals.done_7d}` : ''}`; }
}

/* ------------------------------------------------------------------ interactions */
INPUTS['board-q'] = (el) => { B.f.q = el.value.trim(); clearTimeout(INPUTS._bt); INPUTS._bt = setTimeout(paintBoard, 120); };
CHANGES['board-f'] = (el) => { B.f[el.dataset.key] = el.value; paintBoard(); };
ACTS['board-group'] = (el) => { B.group = el.dataset.value; pref.set('board.group', B.group); S.refresh(true); };
ACTS['board-density'] = (el) => { B.density = el.dataset.value; pref.set('board.density', B.density); S.refresh(true); };
ACTS['bc-collapse'] = (el) => { B.collapsed[el.dataset.col] = !B.collapsed[el.dataset.col]; pref.set('board.collapsed', B.collapsed); paintBoard(); };
ACTS['lane-toggle'] = (el) => { B.laneCollapsed[el.dataset.lane] = !B.laneCollapsed[el.dataset.lane]; paintBoard(); };

const findItem = (id) => { for (const c of B.data.columns) { const t = c.items.find((x) => x.id === id); if (t) return [t, c]; } return [null, null]; };
function moveLocal(id, toCol, status) {
  const [t, from] = findItem(id);
  if (!t) return null;
  const to = B.data.columns.find((c) => c.key === toCol);
  if (!to) return null;
  from.items = from.items.filter((x) => x.id !== id); from.count -= 1;
  const prev = { status: t.status };
  t.status = status || t.status;
  to.items.unshift(t); to.count += 1;
  paintBoard();
  return { from: from.key, prev };
}

ACTS['bc-act'] = async (el, e) => {
  e.stopPropagation();
  const [t, col] = findItem(el.dataset.id);
  if (!t) return;
  const action = el.dataset.action;
  if (action === 'accept') {
    const from = col.key; const prevStatus = t.status;
    await taskActionUndoable(t, 'accept', { apply: () => moveLocal(t.id, 'queued', 'ready'), revert: () => { moveLocal(t.id, from, prevStatus); } });
  } else if (action === 'snooze') {
    const from = col.key; const prevStatus = t.status;
    await taskActionUndoable(t, 'snooze', { hours: 24, apply: () => moveLocal(t.id, from, 'snoozed'), revert: () => { moveLocal(t.id, from, prevStatus); } });
  } else await taskAction(t, action);
};

/** What a drop means. Mirrors what the API accepts: accept (non-forced READY transition), dismiss / retry (forced). */
export function dropRule(t, toCol, fromCol) {
  if (fromCol === toCol) return { ok: false, silent: true };
  const st = t.status;
  if (toCol === 'queued') {
    if (['inbox', 'triaged', 'snoozed'].includes(st)) return { ok: true, action: 'accept', label: '接受并排队' };
    if (['failed', 'dismissed', 'cancelled'].includes(st)) return { ok: true, action: 'retry', label: st === 'failed' ? '重试（重新排队）' : '重新打开（排队）' };
    return { ok: false, why: `「${META.statusLabel[st] || st}」的事项不能直接拖进排队：只有「建议」可接受，失败 / 已忽略的可重新排队。` };
  }
  if (toCol === 'dismissed') {
    if (['inbox', 'triaged', 'snoozed', 'ready', 'blocked', 'waiting_user', 'failed'].includes(st)) return { ok: true, action: 'dismiss', label: '忽略' };
    return { ok: false, why: `「${META.statusLabel[st] || st}」的事项不能直接忽略：${st === 'running' || st === 'waiting_approval' ? '请先在详情里取消任务（会同时停止 Agent）' : st === 'review' ? '请先验收或返工' : '它已经结束了'}。` };
  }
  const why = {
    running: '「执行中」由调度器和 Agent 推进，不能手动拖入。接受建议后任务会自动开始。',
    needs_you: '「等你」由 AI 在需要审批 / 验收 / 接手时自动放入，不能手动拖入。',
    done: '不能直接拖到「已完成」：请打开任务，使用「验收通过」或「标记完成」。',
    failed: '「失败」由系统在重试耗尽后设置。',
    suggested: '不能放回「建议」：已接受的任务可在详情里取消，已忽略的可重新排队。',
  }[toCol];
  return { ok: false, why: why || '这个移动不被任务状态机允许。' };
}

export function bindBoard(root) {
  root.addEventListener('dragstart', (e) => {
    const c = e.target.closest?.('.bcard'); if (!c) return;
    B.dragging = { id: c.dataset.id, col: c.dataset.col };
    c.classList.add('dragging');
    e.dataTransfer.effectAllowed = 'move';
    try { e.dataTransfer.setData('text/plain', c.dataset.id); } catch { /* old browsers */ }
    $('#board')?.classList.add('is-dragging');
  });
  root.addEventListener('dragend', () => { B.dragging = null; $$('.dragging, .drop-ok, .drop-no').forEach((n) => n.classList.remove('dragging', 'drop-ok', 'drop-no')); $('#board')?.classList.remove('is-dragging'); });
  root.addEventListener('dragover', (e) => {
    const cellEl = e.target.closest?.('.cell, .bhead'); if (!cellEl || !B.dragging) return;
    const [t] = findItem(B.dragging.id); if (!t) return;
    const rule = dropRule(t, cellEl.dataset.col, B.dragging.col);
    $$('.drop-ok, .drop-no').forEach((n) => { if (n !== cellEl) n.classList.remove('drop-ok', 'drop-no'); });
    if (rule.silent) return;
    cellEl.classList.toggle('drop-ok', !!rule.ok); cellEl.classList.toggle('drop-no', !rule.ok);
    if (rule.ok) { e.preventDefault(); e.dataTransfer.dropEffect = 'move'; } else e.dataTransfer.dropEffect = 'none';
  });
  root.addEventListener('dragleave', (e) => { const c = e.target.closest?.('.cell, .bhead'); if (c && !c.contains(e.relatedTarget)) c.classList.remove('drop-ok', 'drop-no'); });
  root.addEventListener('drop', async (e) => {
    const cellEl = e.target.closest?.('.cell, .bhead'); if (!cellEl || !B.dragging) return;
    e.preventDefault();
    const { id, col: fromCol } = B.dragging; const toCol = cellEl.dataset.col;
    B.dragging = null;
    $$('.drop-ok, .drop-no, .dragging').forEach((n) => n.classList.remove('drop-ok', 'drop-no', 'dragging'));
    $('#board')?.classList.remove('is-dragging');
    const [t] = findItem(id); if (!t) return;
    const rule = dropRule(t, toCol, fromCol);
    if (rule.silent) return;
    if (!rule.ok) { toast(rule.why, 'warn', { duration: 7000 }); return; }
    try {
      if (rule.action === 'accept') {
        const prevStatus = t.status;
        await taskActionUndoable(t, 'accept', { apply: () => moveLocal(id, 'queued', 'ready'), revert: () => { moveLocal(id, fromCol, prevStatus); } });
      } else await taskAction(t, rule.action);
    } catch (err) { toast(err.message || String(err), 'err'); S.refresh(true); }
  });
  root.addEventListener('keydown', (e) => {
    const c = e.target.closest?.('.bcard');
    if (c && (e.key === 'Enter' || e.key === ' ') && e.target === c) { e.preventDefault(); openTask(c.dataset.id); }
  });
}

export function mount(el) { const host = $('#board-host', el); if (host && !host.dataset.bound) { host.dataset.bound = '1'; bindBoard(host); } }

export default { id: 'board', title: '看板', sub: '建议 → 排队 → 执行 → 等你 → 完成，拖拽卡片即可接受或忽略', icon: 'kanban', load, render, mount, skeleton: skeletonList, busy: boardBusy };
