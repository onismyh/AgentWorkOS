// 任务 / 运行 / 工具调用: dense tables with filters (and a column chooser for tasks).
import { h, raw, enc } from '../core/html.js';
import { api } from '../core/api.js';
import { ACTS, CHANGES } from '../core/acts.js';
import { S, cacheTask } from '../core/state.js';
import { pref } from '../core/store.js';
import { setParams } from '../core/router.js';
import { when, usd, dur, compact, cut, shortId, absTime, parseTs } from '../core/format.js';
import { icon } from '../ui/icons.js';
import { btn, badge, kwChips, levelBadge, prioIcon, execAvatar, statusBadge, runBadge, progress, skeletonList, empty } from '../ui/components.js';
import { table, filterChip, presetChip, selectParam, searchParam, filterBar } from '../ui/table.js';
import { popover, closePopover } from '../ui/overlay.js';
import { META, PURPOSES, digestOf, catLabel, effLevel, execLabel, purposeLabel, SENSITIVE_RE, TERMINAL } from '../domain/vocab.js';
import { evBody } from '../ui/rundetail.js';
import { LEVEL_OPTS } from '../domain/actions.js';

/* ------------------------------------------------------------------ tasks */
const COLS = [
  { key: 'priority', label: '优先级', def: true }, { key: 'title', label: '任务', def: true, fixed: true }, { key: 'level', label: '等级', def: true },
  { key: 'status', label: '状态', def: true }, { key: 'category', label: '类别', def: true }, { key: 'executor', label: '执行器', def: true },
  { key: 'project', label: '项目', def: false }, { key: 'due', label: '截止', def: false }, { key: 'keywords', label: '关键词', def: false },
  { key: 'progress', label: '子任务', def: false }, { key: 'updated', label: '更新', def: true }, { key: 'cost', label: '花费', def: true },
];
const visCols = () => { const sel = pref.get('tasks.cols', null) || COLS.filter((c) => c.def).map((c) => c.key); return COLS.filter((c) => c.fixed || sel.includes(c.key)); };
const STATUS_PRESETS = { attention: ['需要我', 'triaged,waiting_user,review,failed,waiting_approval'], active: ['进行中', 'inbox,ready,running,blocked,waiting_approval'], closed: ['已结束', 'done,cancelled,dismissed'] };
const SORTS = { priority: (a, b) => a.priority - b.priority, level: (a, b) => effLevel(b) - effLevel(a), updated: (a, b) => String(b.updated_at).localeCompare(String(a.updated_at)), cost: (a, b) => (b.cost_usd || 0) - (a.cost_usd || 0), status: (a, b) => String(a.status).localeCompare(String(b.status)), due: (a, b) => String(a.due_at || '9').localeCompare(String(b.due_at || '9')) };

async function loadTasks(p) {
  const qs = new URLSearchParams();
  for (const k of ['status', 'level', 'category', 'kind']) if (p.get(k)) qs.set(k, p.get(k));
  if (p.get('q')) qs.set('search', p.get('q'));
  if (p.get('root') === '1') qs.set('root_only', '1');
  qs.set('limit', p.get('limit') || '300');
  const d = await api(`/tasks?${qs}`);
  d.tasks.forEach(cacheTask);
  return d;
}
function taskRow(t, cols) {
  const dg = digestOf(t, 'task');
  const due = t.due_at ? parseTs(t.due_at) : null; const overdue = due && due.getTime() < Date.now() && !TERMINAL.has(t.status);
  const cell = {
    priority: () => h`<td>${prioIcon(t.priority)}</td>`,
    title: () => h`<td class="title-cell"><div class="tc-t">${dg.headline}</div><div class="tc-s">${dg.headline !== t.title ? h`<span class="tc-full">${cut(t.title, 70)}</span>` : ''}<span class="mono">${shortId(t.id)}</span>${t.parent_title ? h`<span>子任务 · ${cut(t.parent_title, 30)}</span>` : ''}${t.pending_approvals ? badge(`${t.pending_approvals} 待审批`, { tone: 'warn', variant: 'soft', icon: 'shield-alert' }) : ''}</div></td>`,
    level: () => h`<td>${levelBadge(t)}</td>`, status: () => h`<td>${statusBadge(t.status)}</td>`, category: () => h`<td class="nowrap">${catLabel(t.category)}</td>`,
    executor: () => h`<td class="nowrap"><span class="exchip">${execAvatar(t.executor, { size: 18, tip: false })}<span>${execLabel(t.executor)}</span></span></td>`,
    project: () => h`<td class="nowrap">${t.project || h`<span class="muted">—</span>`}</td>`,
    due: () => h`<td class="nowrap${overdue ? ' tone-crit-ink' : ''}">${due ? absTime(t.due_at) : h`<span class="muted">—</span>`}</td>`,
    keywords: () => h`<td>${kwChips(dg.keywords, { max: 3 })}</td>`,
    progress: () => h`<td>${t.children_total ? progress(t.children_done / t.children_total, { label: `${t.children_done}/${t.children_total}` }) : h`<span class="muted">—</span>`}</td>`,
    updated: () => h`<td class="nowrap">${when(t.updated_at)}</td>`, cost: () => h`<td class="num">${t.cost_usd ? usd(t.cost_usd) : ''}</td>`,
  };
  return h`<tr class="click" data-act="open-task" data-id="${t.id}" tabindex="0">${cols.map((c) => cell[c.key]())}</tr>`;
}
function tasksRender(d, p) {
  const counts = d.counts || {}; const cur = p.get('status') || '';
  const cols = visCols();
  const sort = p.get('sort') || '';
  let list = d.tasks.slice();
  if (sort && SORTS[sort]) list.sort(SORTS[sort]);
  const heads = cols.map((c) => ({ label: c.label, cls: c.key === 'cost' ? 'num' : '', act: SORTS[c.key] ? 'tasks-sort' : null, key: c.key, sort: sort === c.key ? 'descending' : null }));
  return h`${filterBar(
    h`<div class="chip-row">${presetChip('status', '', '全部')}${Object.values(STATUS_PRESETS).map(([l, v]) => presetChip('status', v, l))}</div>`,
    searchParam('q', '搜索标题 / 描述 / 任务 ID（/）'),
    selectParam('level', LEVEL_OPTS(), '全部等级'), selectParam('category', Object.fromEntries(Object.keys(META.categories).map((k) => [k, catLabel(k)])), '全部类别'), selectParam('kind', META.kinds, '全部类型'),
    h`<label class="check small"><input type="checkbox" data-change="toggle-root"${p.get('root') === '1' ? raw(' checked') : ''}><span>只看顶层</span></label>`,
    btn('列', { act: 'col-chooser', icon: 'columns', size: 'sm', variant: 'secondary', tip: '选择显示的列' }))}
    <div class="chip-row spaced">${Object.keys(META.statusLabel).map((s) => filterChip('status', s, META.statusLabel[s], counts[s] || 0))}</div>
    ${table(heads, list.map((t) => taskRow(t, cols)), { emptyTitle: '没有符合条件的任务', emptyDesc: '调整筛选条件，或新建一个任务。', emptyIcon: 'tasks' })}
    <p class="foot-note">显示 ${d.total} 条${d.total >= Number(p.get('limit') || 300) ? h` · <button type="button" class="linkish" data-act="set-param" data-key="limit" data-value="2000">加载更多</button>` : ''}</p>`;
}
ACTS['tasks-sort'] = (el) => setParams({ sort: S.params.get('sort') === el.dataset.key ? null : el.dataset.key });
CHANGES['toggle-root'] = (el) => setParams({ root: el.checked ? '1' : null });
ACTS['col-chooser'] = (el) => {
  const sel = new Set(visCols().map((c) => c.key));
  popover(el, h`<div class="pop-h">显示的列</div><div class="pop-list">${COLS.map((c) => h`<label class="check pop-i"><input type="checkbox" data-change="col-toggle" data-key="${c.key}"${sel.has(c.key) ? raw(' checked') : ''}${c.fixed ? raw(' disabled') : ''}><span>${c.label}</span></label>`)}</div>`, { align: 'end' });
};
CHANGES['col-toggle'] = (el) => {
  const cur = new Set(visCols().map((c) => c.key)); if (el.checked) cur.add(el.dataset.key); else cur.delete(el.dataset.key);
  pref.set('tasks.cols', [...cur]); S.refresh(true);
};

/* ------------------------------------------------------------------ runs */
async function loadRuns(p) {
  const qs = new URLSearchParams({ limit: '300' });
  for (const k of ['status', 'purpose', 'executor', 'task_id']) if (p.get(k)) qs.set(k, p.get(k));
  return api(`/runs?${qs}`);
}
function runsRender(d, p) {
  const rows = d.runs.map((r) => h`<tr class="click" data-act="open-run" data-id="${r.id}" tabindex="0"><td>${runBadge(r.status)}</td><td>${purposeLabel(r.purpose)}${r.attempt > 1 ? ` #${r.attempt}` : ''}</td>
    <td class="nowrap"><span class="exchip">${execAvatar(r.executor, { size: 18, tip: false })}<span>${r.executor}</span></span>${r.model ? h`<div class="tc-s">${r.model}</div>` : ''}</td>
    <td class="title-cell">${r.task_title ? h`<div class="tc-t">${cut(r.task_title, 60)}</div>` : h`<span class="muted">—</span>`}<div class="tc-s mono">${shortId(r.id)}</div></td>
    <td class="nowrap">${when(r.started_at)}</td><td class="num nowrap">${dur(r.duration_s)}</td><td class="num">${usd(r.cost_usd)}</td><td class="num">${compact((r.tokens_in || 0) + (r.tokens_out || 0))}</td><td class="err-cell">${cut(r.error || '', 100)}</td></tr>`);
  return h`${filterBar(h`<div class="chip-row">${Object.keys(META.runLabel).map((s) => filterChip('status', s, META.runLabel[s]))}</div>`,
    selectParam('purpose', PURPOSES, '全部用途'), selectParam('executor', Object.fromEntries(META.executors.map((x) => [x, x])), '全部执行器'),
    p.get('task_id') ? h`<button type="button" class="chip-f on" data-act="set-param" data-key="task_id" data-value="">任务 ${shortId(p.get('task_id'))} ${icon('x', { size: 12 })}</button>` : '')}
    ${table(['状态', '用途', '执行器', '任务', '开始', { label: '时长', cls: 'num' }, { label: '花费', cls: 'num' }, { label: 'Tokens', cls: 'num' }, '错误'], rows, { emptyTitle: '没有运行记录', emptyIcon: 'play' })}`;
}

/* ------------------------------------------------------------------ tool calls */
async function loadTools(p) { return api(`/tool-calls?limit=${enc(p.get('limit') || '500')}`); }
function toolsRender(d, p) {
  const term = (p.get('q') || '').toLowerCase(); const only = p.get('sensitive') === '1';
  const items = d.tool_calls.map((c) => { const args = evBody({ kind: 'tool_call', data: c.data }); return { c, args, sensitive: SENSITIVE_RE.test(`${c.name || ''} ${args}`) }; })
    .filter(({ c, args, sensitive }) => (!term || `${c.name} ${args}`.toLowerCase().includes(term)) && (!only || sensitive));
  const nSens = items.filter((x) => x.sensitive).length;
  const rows = items.map(({ c, args, sensitive }) => h`<tr class="click${sensitive ? ' hl-warn' : ''}" data-act="open-run" data-id="${c.run_id}" tabindex="0"><td class="nowrap">${when(c.ts)}</td>
    <td class="nowrap"><code>${c.name || '—'}</code>${sensitive ? h`<div>${badge('疑似对外动作', { tone: 'crit', icon: 'shield-alert', variant: 'soft' })}</div>` : ''}</td>
    <td class="title-cell"><div class="mono small" data-tip="${cut(args, 800)}">${cut(args.replace(/\s+/g, ' '), 200)}</div></td>
    <td><span class="exchip">${execAvatar(c.executor, { size: 18, tip: false })}<span>${c.executor || '—'}</span></span></td><td class="small">${c.task_title ? cut(c.task_title, 40) : '—'}</td><td class="mono small muted">${shortId(c.run_id)}</td></tr>`);
  return h`${filterBar(searchParam('q', '按工具名或参数过滤'), h`<label class="check small"><input type="checkbox" data-change="toggle-sensitive"${only ? raw(' checked') : ''}><span>只看疑似对外动作</span></label>`,
    h`<span class="muted small">最近 ${d.tool_calls.length} 次 · ${nSens} 次疑似对外动作（Agent 运行时不应直接执行对外动作）</span>`)}
    ${table(['时间', '工具', '参数', '执行器', '任务', '运行'], rows, { emptyTitle: '没有工具调用记录', emptyIcon: 'wrench' })}`;
}
CHANGES['toggle-sensitive'] = (el) => setParams({ sensitive: el.checked ? '1' : null });

const mk = (id, title, sub, ic, load, render) => ({ id, title, sub, icon: ic, load: () => load(S.params), render: (d) => render(d, S.params), skeleton: skeletonList });
export const tasksView = mk('tasks', '任务', '统一任务中心', 'tasks', loadTasks, tasksRender);
export const runsView = mk('runs', '运行', '每一次 Agent 调用', 'play', loadRuns, runsRender);
export const toolsView = mk('tools', '工具调用', '跨运行的工具调用审计', 'wrench', loadTools, toolsRender);
