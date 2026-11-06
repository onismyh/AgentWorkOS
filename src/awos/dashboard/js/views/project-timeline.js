// 看板 · 项目时间线: per project - what the agents did, how it went, what you changed; milestones; a block diagram of the work.
// Mounted by views/board.js when the URL has mode=timeline. Data: GET /api/timeline (lanes) + GET /api/projects/{key}/timeline.
import { h, raw, setHTML, $, $$, enc } from '../core/html.js';
import { api, post, patch, del } from '../core/api.js';
import { ACTS } from '../core/acts.js';
import { S } from '../core/state.js';
import { pref } from '../core/store.js';
import { hashFor, setParams } from '../core/router.js';
import { cut, usd, rel } from '../core/format.js';
import { icon } from '../ui/icons.js';
import { btn, iconBtn, badge, card, empty, execAvatar, seg, tipAttr } from '../ui/components.js';
import { dialog, confirmDialog, toast } from '../ui/overlay.js';
import { flowSVG } from '../ui/flow.js';
import { execLabel } from '../domain/vocab.js';
import { projectSlot } from '../domain/projects.js';

const T = { filter: pref.get('timeline.filter', 'all'), data: null };
const FILTERS = [['all', '全部'], ['agent', 'Agent 做的'], ['human', '我干预的'], ['issue', '异常']];
const OUTCOME = {
  done: ['已完成', 'good'], failed: ['失败', 'crit'], waiting: ['等你', 'warn'], rejected: ['你拒绝', 'muted'], recovered: ['已自动恢复', 'good'],
  cancelled: ['已取消', 'muted'], running: ['进行中', 'brand'],
};
const MS_VIEW = { done: ['已完成', 'good'], active: ['进行中', 'brand'], planned: ['计划中', 'muted'], missed: ['已逾期', 'crit'] };

/* ------------------------------------------------------------------ small helpers */
const md = (d) => { const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(d || ''); return m ? `${Number(m[2])}月${Number(m[3])}日` : ''; };
const hm = (ts) => { const d = new Date(ts); return Number.isNaN(+d) ? '' : d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false }); };
const localDay = (ts) => { const d = new Date(ts); return Number.isNaN(+d) ? '' : `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`; };

export const modeSwitch = (mode) => seg([{ key: 'board', label: '任务看板', icon: 'kanban' }, { key: 'timeline', label: '项目时间线', icon: 'flag' }], mode, { act: 'board-mode', key: 'mode', label: '看板模式' });
ACTS['board-mode'] = (el) => setParams({ mode: el.dataset.value === 'timeline' ? 'timeline' : null, project: null });

/* ------------------------------------------------------------------ data */
export async function load(params) {
  let lanes = null;
  try { lanes = await api('/timeline'); } catch (e) { return { __timeline: true, error: e.status === 404 ? '后台程序还在运行旧版本，重启后即可看到项目时间线。' : e.message }; }
  const keys = lanes.projects.map((p) => p.key);
  let key = params.get('project') || '';
  if (!keys.includes(key)) key = (lanes.projects.find((p) => p.last_activity) || lanes.projects[0])?.key || '';
  const detail = key ? await api(`/projects/${enc(key)}/timeline`) : null;
  T.data = detail;
  return { __timeline: true, lanes, key, detail };
}

/* ------------------------------------------------------------------ left: the project list */
function laneHTML(p, on) {
  const s = p.stats; const pr = p.progress;
  const next = p.next_milestone;
  const sub = next ? `${next.status === 'missed' ? '已逾期：' : '下一个里程碑：'}${cut(next.title, 16)}${next.target_date ? ` · ${md(next.target_date)}` : ''}` : p.last_activity ? `最近动态 ${rel(p.last_activity)}` : '还没有动态';
  const chips = [s.agent_runs ? `Agent ${s.agent_runs} 次` : '', s.interventions ? `你 ${s.interventions} 次` : ''].filter(Boolean);
  return h`<a class="tl-lane pj-c${projectSlot(p.key)}${on ? ' on' : ''}" href="${hashFor('board', { mode: 'timeline', project: p.key })}"${on ? raw(' aria-current="true"') : ''}>
    <span class="pj-dot">${icon('folder', { size: 13 })}</span>
    <span class="tl-lane-m"><b class="tl-lane-t">${p.name}</b>
      <span class="tl-pb${pr.pct >= 100 ? ' full' : ''}" role="img" aria-label="进度 ${pr.pct}%"><i data-w="${Math.max(2, pr.pct)}"></i></span>
      <span class="tl-lane-s">${pr.basis === 'milestones' ? `里程碑 ${pr.milestones.done}/${pr.milestones.total}` : `任务 ${pr.tasks.done}/${pr.tasks.total}`} · ${pr.pct}%</span>
      <span class="tl-lane-n">${sub}</span>
      ${chips.length || s.open_asks ? h`<span class="tl-lane-c">${chips.map((c) => h`<span>${c}</span>`)}${s.open_asks ? h`<span class="warn">待你 ${s.open_asks}</span>` : ''}</span>` : ''}</span></a>`;
}

/* ------------------------------------------------------------------ right: header, milestones, diagram, feed */
const stat = (label, value, foot, tone = '') => h`<div class="tl-stat${tone ? ` tone-${tone}` : ''}"><span class="tl-stat-l">${label}</span><b class="tl-stat-v">${value}</b><span class="tl-stat-f">${foot}</span></div>`;

function headHTML(d) {
  const s = d.stats;
  const ex = Object.entries(s.executors || {}).sort((a, b) => b[1].runs - a[1].runs);
  return h`<header class="tl-head pj-c${projectSlot(d.key)}"><div class="tl-head-t"><span class="pj-dot">${icon('folder', { size: 14 })}</span><div><h2>${d.name}</h2><p class="muted">${d.summary}</p></div></div>
    <div class="tl-stats">
      ${stat('Agent 做了', `${s.agent_runs} 轮`, s.agent_runs ? `${s.agent_ok} 轮成功` : '还没有运行')}
      ${stat('完成的任务', `${s.tasks_done}/${s.tasks}`, s.tasks_active ? `${s.tasks_active} 个在推进` : s.tasks_failed ? `${s.tasks_failed} 个没做成` : '没有在推进的')}
      ${stat('你干预了', `${s.interventions} 次`, '审批、返工、取消等', s.interventions ? 'brand' : '')}
      ${stat('等你处理', `${s.open_asks} 项`, s.open_asks ? '审批、验收或接手' : '没有积压', s.open_asks ? 'warn' : '')}
      ${stat('花费', usd(s.cost_usd), s.cost_usd ? '按量计费的执行器' : 'Codex 不计费')}</div>
    ${ex.length ? h`<div class="tl-exs"><span class="muted small">谁干的：</span>${ex.map(([name, v]) => h`<span class="tl-ex">${execAvatar(name, { size: 18, tip: false })}<span>${execLabel(name)} ${v.runs} 轮${v.ok < v.runs ? h`<small>（${v.runs - v.ok} 轮没成功）</small>` : ''}</span></span>`)}</div>` : ''}</header>`;
}

function msHTML(m) {
  const [label, tone] = MS_VIEW[m.status] || MS_VIEW.planned;
  const when = m.status === 'done' && m.done_at ? `${md(localDay(m.done_at))} 完成` : m.target_date ? `目标 ${md(m.target_date)}` : '没定日期';
  const task = m.evidence?.task_id;
  return h`<li class="ms ms-${m.status}${m.editable ? '' : ' auto'}"><span class="ms-d">${m.status === 'done' ? icon('check', { size: 11 }) : ''}</span>
    <div class="ms-b"${task ? h` data-act="open-task" data-id="${task}" role="button" tabindex="0"` : ''}><b class="ms-t">${m.title}</b><span class="ms-w">${when}</span>
      <span class="ms-tags">${badge(label, { tone, variant: 'soft' })}${m.source === 'auto' ? h`<span class="ms-src"${tipAttr(m.origin || '自动生成')}>自动</span>` : ''}</span>
      ${m.summary ? h`<span class="ms-sum">${cut(m.summary, 70)}</span>` : ''}</div>
    ${m.editable ? h`<div class="ms-a">${iconBtn(m.stored_status === 'done' ? 'undo' : 'check', { act: 'tl-ms-done', data: { id: m.id, status: m.stored_status === 'done' ? 'planned' : 'done' }, tip: m.stored_status === 'done' ? '改回未完成' : '标为完成', size: 'icon-sm' })}${iconBtn('pencil', { act: 'tl-ms-edit', data: { id: m.id }, tip: '编辑', size: 'icon-sm' })}${iconBtn('trash', { act: 'tl-ms-del', data: { id: m.id }, tip: '删除', size: 'icon-sm' })}</div>` : ''}</li>`;
}
function milestonesCard(d) {
  const ms = d.milestones;
  return card({ title: '里程碑', icon: 'flag', desc: ms.length ? `${ms.filter((m) => m.status === 'done').length}/${ms.length} 已完成 · 标「自动」的来自截止日期或 Agent 的重要交付` : '',
    actions: btn('添加里程碑', { act: 'tl-ms-add', icon: 'plus', size: 'sm' }),
    body: ms.length ? h`<ol class="ms-rail" data-scroll="tl-ms">${ms.map(msHTML)}</ol>` : h`<p class="ms-none">${icon('flag', { size: 14 })}<span>还没有里程碑。点右上角「添加里程碑」写下关键节点；有截止日期、或 Agent 完成重要交付时，也会自动出现在这里。</span></p>` });
}

function flowCard(d) {
  const f = d.flow; const tasks = f.nodes.filter((n) => n.type === 'task').length; const gates = f.nodes.length - tasks;
  return card({ title: '任务流程图', icon: 'layers', desc: tasks ? `${tasks} 个任务${gates ? ` · ${gates} 个需要你拍板的节点` : ''}${f.truncated ? ' · 只画最近的 36 个' : ''}；点方框打开任务` : '',
    body: tasks ? h`<div class="fl-wrap" data-scroll="tl-flow:${d.key}">${flowSVG(f)}</div>
      <ul class="fl-legend" aria-label="图例"><li><i class="lg lg-task"></i>任务（左边的色条 = 状态）</li><li><i class="lg lg-gate"></i>需要你拍板</li><li><i class="lg lg-dep"></i>先后依赖</li><li><i class="lg lg-roll"></i>子任务汇总到上级</li></ul>` : empty({ icon: 'layers', title: '这个项目还没有任务', desc: '有任务之后，这里会画出它们之间的先后关系。' }) });
}

/** One event: who did it, in plain words, how it ended, a short excerpt, pictures / files it produced. */
function eventHTML(e) {
  const kind = e.human ? 'human' : e.actor.startsWith('agent') ? 'agent' : 'system';
  const [olabel, otone] = OUTCOME[e.outcome] || [];
  const av = e.human ? h`<span class="tl-dot you" ${tipAttr('你')}>${icon('user', { size: 15 })}</span>`
    : e.actor.startsWith('agent') ? h`<span class="tl-dot">${e.executor ? execAvatar(e.executor, { size: 26 }) : h`<span class="exav ex-auto" style="--sz:26px">${icon('sparkles', { size: 15 })}</span>`}</span>`
      : h`<span class="tl-dot sys">${icon('cpu', { size: 14 })}</span>`;
  const open = e.refs.task ? h` data-act="open-task" data-id="${e.refs.task}" role="button" tabindex="0"` : '';
  return h`<li class="tl-ev tl-${kind} o-${e.outcome}"><span class="tl-rail">${av}</span>
    <div class="tl-body"${open}>
      <div class="tl-line"><b class="tl-t">${e.title}</b>${olabel ? badge(olabel, { tone: otone, variant: 'soft' }) : ''}<time class="tl-time" datetime="${e.ts}">${hm(e.ts)}</time></div>
      ${e.detail ? h`<p class="tl-d">${e.detail}</p>` : ''}
      ${e.media?.length ? h`<div class="tl-media">${e.media.map((m) => h`<a class="tl-img" href="${m.url}" data-act="tl-zoom" data-src="${m.url}" data-title="${m.title}"${tipAttr(m.title)}><img src="${m.url}" alt="${m.title}" loading="lazy"></a>`)}</div>` : ''}
      ${e.files?.length || e.cost ? h`<div class="tl-foot">${(e.files || []).map((f) => h`<a class="tl-file" href="${f.url}" target="_blank" rel="noopener noreferrer"${tipAttr('在新标签页打开')}>${icon('file-text', { size: 12 })}<span>${cut(f.title, 26)}</span></a>`)}${e.cost ? h`<span class="tl-cost">${usd(e.cost)}</span>` : ''}</div>` : ''}
    </div></li>`;
}
const match = (e, f) => (f === 'agent' ? e.actor.startsWith('agent') : f === 'human' ? e.human : f === 'issue' ? ['failed', 'recovered'].includes(e.outcome) : true);
function feedHTML(d, f) {
  const days = d.days.map((g) => ({ ...g, events: g.events.filter((e) => match(e, f)) })).filter((g) => g.events.length);
  if (!days.length) return empty({ icon: 'activity', title: f === 'all' ? '还没有动态' : '这一类里还没有内容', desc: f === 'human' ? '你审批、取消、返工或新建任务之后会记在这里。' : '' });
  return h`${days.map((g) => h`<section class="tl-day"><h4><span>${g.label}</span><small>${g.day}</small><em>${g.events.length} 件</em></h4><ul class="tl-evs">${g.events.map(eventHTML)}</ul></section>`)}`;
}
const count = (d, f) => d.days.reduce((n, g) => n + g.events.filter((e) => match(e, f)).length, 0);
function chipsHTML(d) {
  return h`${FILTERS.map(([k, l]) => h`<button type="button" class="chip-f${T.filter === k ? ' on' : ''}" data-act="tl-filter" data-value="${k}" aria-pressed="${T.filter === k}">${l}<b>${count(d, k)}</b></button>`)}`;
}
function feedCard(d) {
  return card({ title: '发生了什么', icon: 'history', desc: 'Agent 做了什么、结果怎样，以及你干预过什么（蓝色框是你）', cls: 'tl-feedcard', actions: h`<div class="tl-chips" id="tl-chips" role="group" aria-label="筛选">${chipsHTML(d)}</div>`,
    body: h`<div class="tl-feed" id="tl-feed">${feedHTML(d, T.filter)}</div>` });
}

/* ------------------------------------------------------------------ view */
export function render(data) {
  const bar = h`<div class="tl-bar">${modeSwitch('timeline')}<span class="grow"></span><span class="muted small">按项目看：Agent 干了什么、干成什么样、你干预了什么</span></div>`;
  if (data.error) return h`<div class="tl-page">${bar}${empty({ icon: 'circle-alert', title: '暂时看不了时间线', desc: data.error })}</div>`;
  const { lanes, key, detail } = data;
  if (!detail) return h`<div class="tl-page">${bar}${empty({ icon: 'folder', title: '还没有项目', desc: '在「上下文」页添加项目后，这里会按项目整理时间线。' })}</div>`;
  return h`<div class="tl-page">${bar}<div class="tl-split">
    <nav class="tl-lanes" aria-label="项目" data-scroll="tl-lanes">${lanes.projects.map((p) => laneHTML(p, p.key === key))}</nav>
    <div class="tl-main" data-scroll="tl-main:${key}">${headHTML(detail)}${milestonesCard(detail)}${flowCard(detail)}${feedCard(detail)}</div></div></div>`;
}

export function mount(el) {
  // progress bars grow from zero on arrival (width is set from data-w so no inline style ships in the markup)
  const bars = $$('.tl-pb > i[data-w]', el);
  bars.forEach((b) => { b.style.width = '0%'; });
  requestAnimationFrame(() => requestAnimationFrame(() => bars.forEach((b) => { b.style.width = `${b.dataset.w}%`; })));
  const flow = $('.fl-wrap', el);
  if (flow && !flow.dataset.keys) {   // SVG nodes are not HTML elements: give them keyboard activation
    flow.dataset.keys = '1';
    flow.addEventListener('keydown', (e) => {
      if ((e.key === 'Enter' || e.key === ' ') && e.target.closest?.('.fl-n')) { e.preventDefault(); e.stopPropagation(); e.target.closest('.fl-n').dispatchEvent(new MouseEvent('click', { bubbles: true })); }
    }, true);
  }
}

/* ------------------------------------------------------------------ interactions */
ACTS['tl-filter'] = (el) => {
  T.filter = el.dataset.value; pref.set('timeline.filter', T.filter);
  const d = T.data; if (!d) return;
  setHTML($('#tl-feed'), feedHTML(d, T.filter));
  setHTML($('#tl-chips'), chipsHTML(d));
};
ACTS['tl-zoom'] = (el) => dialog({ title: el.dataset.title || '图片', body: h`<img class="tl-zoom" src="${el.dataset.src}" alt="${el.dataset.title || ''}">`, hideSubmit: true, wide: true });

const msForm = (m = {}) => h`<label class="field"><span>标题</span><input class="input" name="title" required maxlength="120" value="${m.title || ''}" placeholder="例如：初稿完成 / 投稿"></label>
  <div class="form-grid"><label class="field"><span>目标日期（可不填）</span><input class="input" type="date" name="target_date" value="${m.target_date || ''}"></label>
  <label class="field"><span>状态</span><select class="select" name="status">${[['planned', '计划中'], ['active', '进行中'], ['done', '已完成']].map(([k, l]) => h`<option value="${k}"${(m.stored_status || 'planned') === k ? raw(' selected') : ''}>${l}</option>`)}</select></label></div>
  <label class="field"><span>说明（可不填）</span><textarea class="textarea" name="summary" rows="2" maxlength="1000" placeholder="做到什么程度算完成">${m.summary || ''}</textarea></label>`;
const refresh = () => S.refresh(true);
const find = (id) => T.data?.milestones.find((m) => m.id === id);
ACTS['tl-ms-add'] = async () => {
  const d = T.data; if (!d) return;
  await dialog({ title: '添加里程碑', desc: `项目：${d.name}`, icon: 'flag', body: msForm(), submitLabel: '添加',
    onSubmit: async (f) => { await post('/milestones', { project_key: d.key, title: f.title.trim(), summary: f.summary?.trim() || null, target_date: f.target_date || null, status: f.status }); } });
  refresh();
};
ACTS['tl-ms-edit'] = async (el) => {
  const m = find(el.dataset.id); if (!m) return;
  await dialog({ title: '编辑里程碑', icon: 'pencil', body: msForm(m), submitLabel: '保存',
    onSubmit: async (f) => { await patch(`/milestones/${enc(m.id)}`, { title: f.title.trim(), summary: f.summary?.trim() || '', target_date: f.target_date || null, status: f.status }); } });
  refresh();
};
ACTS['tl-ms-done'] = async (el) => { await patch(`/milestones/${enc(el.dataset.id)}`, { status: el.dataset.status }); toast(el.dataset.status === 'done' ? '已标为完成' : '已改回未完成'); refresh(); };
ACTS['tl-ms-del'] = async (el) => {
  const m = find(el.dataset.id);
  if (!await confirmDialog('删除这个里程碑？', m ? `「${m.title}」会被删除，时间线里会留下一条记录。` : '', { danger: true, submitLabel: '删除' })) return;
  await del(`/milestones/${enc(el.dataset.id)}`); toast('已删除'); refresh();
};

export default { load, render, mount };
