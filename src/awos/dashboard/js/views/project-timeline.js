// 看板 · 项目时间线 = 要点审阅: every project in attention order, one row each; open a row for a SHORT review
// (what waits on you, recent highlights, milestones) and, folded away, the full record and the task diagram.
// Mounted by views/board.js when the URL has mode=timeline. Data: GET /api/timeline + GET /api/projects/{key}/timeline (only for the open row).
import { h, raw, setHTML, $, $$, enc } from '../core/html.js';
import { api, post, patch, del } from '../core/api.js';
import { ACTS } from '../core/acts.js';
import { S } from '../core/state.js';
import { pref } from '../core/store.js';
import { hashFor, setParams } from '../core/router.js';
import { cut, usd } from '../core/format.js';
import { icon } from '../ui/icons.js';
import { btn, iconBtn, badge, empty, execAvatar, seg, tipAttr } from '../ui/components.js';
import { dialog, confirmDialog, toast } from '../ui/overlay.js';
import { flowSVG } from '../ui/flow.js';

const T = { filter: pref.get('timeline.filter', 'all'), data: null, shown: null };
const FILTERS = [['all', '全部'], ['agent', 'Agent 做的'], ['human', '我干预的'], ['issue', '异常']];
const OUTCOME = {
  done: ['已完成', 'good'], failed: ['失败', 'crit'], waiting: ['等你', 'warn'], rejected: ['你拒绝', 'muted'], recovered: ['已自动恢复', 'good'],
  cancelled: ['已取消', 'muted'], running: ['进行中', 'brand'],
};
const MS_VIEW = { done: ['已完成', 'good'], active: ['进行中', 'brand'], planned: ['计划中', 'muted'], missed: ['已逾期', 'crit'] };
const HEALTH = { risk: ['circle-alert', 'crit'], attention: ['hand', 'warn'], stalled: ['hourglass', 'muted'], ok: ['circle-check', 'good'], quiet: ['moon', 'muted'] };
const GROUPS = [
  { id: 'watch', title: '需要关注', levels: ['risk', 'attention', 'stalled'] },
  { id: 'ok', title: '进展正常', levels: ['ok'] },
  { id: 'quiet', title: '暂无动静', levels: ['quiet'], fold: true },
];

/* ------------------------------------------------------------------ small helpers */
const md = (d) => { const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(d || ''); return m ? `${Number(m[2])}月${Number(m[3])}日` : ''; };
const hm = (ts) => { const d = new Date(ts); return Number.isNaN(+d) ? '' : d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false }); };
const localDay = (ts) => { const d = new Date(ts); return Number.isNaN(+d) ? '' : `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`; };
/** Short stable id fragment of a project key (ids of <details> must be unique so their open state survives a refresh). */
const hid = (key) => { let x = 0x811c9dc5; for (const c of String(key)) { x ^= c.charCodeAt(0); x = Math.imul(x, 0x01000193) >>> 0; } return x.toString(36); };
const inboxHref = (a) => `#/inbox?sel=${enc(a.ref.approval ? `approval:${a.ref.approval}` : `task:${a.ref.task}`)}`;
const PILE = '__none__';   // the 未归类 pile: it cannot be archived
const archiveData = (key, name, st) => ({ key, name, open: st?.close_open ?? 0, asks: st?.close_asks ?? 0 });
const dueWord = (days) => (days == null || days > 14 ? '' : days < 0 ? `（已逾期 ${-days} 天）` : days === 0 ? '（今天）' : `（${days} 天后）`);

export const modeSwitch = (mode) => seg([{ key: 'board', label: '任务看板', icon: 'kanban' }, { key: 'timeline', label: '项目时间线', icon: 'flag' }], mode, { act: 'board-mode', key: 'mode', label: '看板模式' });
ACTS['board-mode'] = (el) => setParams({ mode: el.dataset.value === 'timeline' ? 'timeline' : null, project: null });

/* ------------------------------------------------------------------ data */
export async function load(params) {
  const want = params.get('project') || '';
  let lanes = null; let detail = null;
  try {
    [lanes, detail] = await Promise.all([api('/timeline'), want ? api(`/projects/${enc(want)}/timeline`).catch(() => null) : null]);
  } catch (e) { return { __timeline: true, error: e.status === 404 ? '后台程序还在运行旧版本，重启后即可看到项目时间线。' : e.message }; }
  const key = detail ? want : '';
  T.data = detail;
  return { __timeline: true, lanes, key, detail };
}

/* ------------------------------------------------------------------ one project row */
const pill = (level, label) => { const [ic, tone] = HEALTH[level] || HEALTH.ok; return h`<span class="tl-pill tone-${tone}">${icon(ic, { size: 13 })}<span>${label}</span></span>`; };

function rowHTML(p, open, detail) {
  const nm = p.next_milestone;
  const last = p.last ? h`<span class="tl-last">最近：${cut(p.last.text, 64)}<time> · ${md(localDay(p.last.ts))}</time></span>` : h`<span class="tl-last">还没有可汇报的动态</span>`;
  const next = nm && !p.health.reason.includes(cut(nm.title, 14)) ? h`<span class="tl-next${nm.status === 'missed' ? ' late' : ''}">${icon('flag', { size: 12 })}<span>${cut(nm.title, 18)}${nm.target_date ? ` · ${md(nm.target_date)}${dueWord(nm.days)}` : ''}</span></span>` : '';
  return h`<article class="tl-row hl-${p.health.level}${open ? ' open' : ''}">
    <a class="tl-rh" href="${hashFor('board', open ? { mode: 'timeline' } : { mode: 'timeline', project: p.key })}" aria-expanded="${open}">
      ${pill(p.health.level, p.health.label)}
      <span class="tl-rm"><b class="tl-name">${p.name}</b><span class="tl-state">${p.state}</span>${last}</span>
      <span class="tl-rs"><span class="tl-why">${p.health.reason}</span>${next}<span class="tl-pb${p.progress.pct >= 100 ? ' full' : ''}" role="img" aria-label="进度 ${p.progress.pct}%"><i data-w="${Math.max(2, p.progress.pct)}"></i></span></span>
      <span class="tl-ch">${icon('chevron-down', { size: 16 })}</span></a>
    ${p.key === PILE ? '' : iconBtn('package', { act: 'tl-archive', data: archiveData(p.key, p.name, p.stats), tip: '归档：不再主动盯这个项目', size: 'icon-sm', cls: 'tl-arch' })}
    ${open && detail ? reviewHTML(detail) : ''}</article>`;
}

/* ------------------------------------------------------------------ the short review (an open row) */
const sec = (title, body, actions = '') => h`<section class="tl-sec"><h4>${title}${actions ? h`<span class="tl-sec-a">${actions}</span>` : ''}</h4>${body}</section>`;
const none = (text) => h`<p class="tl-none">${text}</p>`;

const askHTML = (a) => h`<li class="tl-ask"><span class="tl-ask-k">${a.label}</span><span class="tl-ask-t">${a.title}</span><a class="btn btn-secondary btn-sm" href="${inboxHref(a)}">去处理</a></li>`;
const hlHTML = (x) => h`<li class="tl-hl ${x.human ? 'you' : 'ag'}${x.outcome === 'failed' ? ' bad' : ''}"${x.refs?.task ? h` data-act="open-task" data-id="${x.refs.task}" role="button" tabindex="0"` : ''}${x.detail ? tipAttr(x.detail) : ''}>
  <span class="tl-hl-d">${md(x.day)}</span><span class="tl-who">${x.human ? '你' : 'Agent'}</span><span class="tl-hl-t">${x.text}</span></li>`;
function msLine(m, ro = false) {
  const [label, tone] = MS_VIEW[m.status] || MS_VIEW.planned;
  const when = m.status === 'done' && m.done_at ? `${md(localDay(m.done_at))} 完成` : m.target_date ? md(m.target_date) : '没定日期';
  const task = m.evidence?.task_id;
  return h`<li class="msl ms-${m.status}${m.editable ? '' : ' auto'}"><span class="ms-d" aria-hidden="true"></span>
    <span class="msl-t"${task ? h` data-act="open-task" data-id="${task}" role="button" tabindex="0"` : ''}${m.summary ? tipAttr(m.summary) : ''}>${m.title}</span>
    <span class="msl-w">${when}</span>${badge(label, { tone, variant: 'soft' })}${m.source === 'auto' ? h`<span class="ms-src"${tipAttr(m.origin || '自动生成')}>自动</span>` : ''}
    ${m.editable && !ro ? h`<span class="msl-a">${iconBtn(m.stored_status === 'done' ? 'undo' : 'check', { act: 'tl-ms-done', data: { id: m.id, status: m.stored_status === 'done' ? 'planned' : 'done' }, tip: m.stored_status === 'done' ? '改回未完成' : '标为完成', size: 'icon-sm' })}${iconBtn('pencil', { act: 'tl-ms-edit', data: { id: m.id }, tip: '编辑', size: 'icon-sm' })}${iconBtn('trash', { act: 'tl-ms-del', data: { id: m.id }, tip: '删除', size: 'icon-sm' })}</span>` : ''}</li>`;
}

function graphHTML(d) {
  const f = d.flow; const tasks = f.nodes.filter((n) => n.type === 'task').length;
  return h`<details class="tl-graph" id="tl-graph-${hid(d.key)}"><summary>${icon('layers', { size: 14 })}<span>任务关系图</span><small>${tasks} 个任务${f.truncated ? '（只画最近的）' : ''}</small></summary>
    ${tasks ? h`<div class="fl-wrap" data-scroll="tl-flow:${d.key}">${flowSVG(f)}</div>
      <ul class="fl-legend" aria-label="图例"><li><i class="lg lg-task"></i>任务（左边色条 = 状态，点开看详情）</li><li><i class="lg lg-gate"></i>需要你拍板</li><li><i class="lg lg-dep"></i>先后依赖</li><li><i class="lg lg-roll"></i>子任务汇总到上级</li></ul>` : none('这个项目还没有任务。')}</details>`;
}

/** One event of the full record: who did it, in plain words, how it ended, a short excerpt, pictures / files it produced. */
function eventHTML(e) {
  const kind = e.human ? 'human' : e.actor.startsWith('agent') ? 'agent' : 'system';
  const [olabel, otone] = OUTCOME[e.outcome] || [];
  const av = e.human ? h`<span class="tl-dot you"${tipAttr('你')}>${icon('user', { size: 15 })}</span>`
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
const chipsHTML = (d) => h`${FILTERS.map(([k, l]) => h`<button type="button" class="chip-f${T.filter === k ? ' on' : ''}" data-act="tl-filter" data-value="${k}" aria-pressed="${T.filter === k}">${l}<b>${count(d, k)}</b></button>`)}`;

function reviewHTML(d, ro = false) {
  const id = hid(d.key);
  const canArchive = !ro && d.key !== PILE;
  return h`<div class="tl-rb${ro ? ' ro' : ''}">
    ${canArchive ? h`<div class="tl-rb-h">${btn('归档', { act: 'tl-archive', data: archiveData(d.key, d.name, d.stats), icon: 'package', size: 'sm', variant: 'ghost', tip: '不再主动盯这个项目，随时可以恢复' })}</div>` : ''}
    ${ro ? '' : sec('要你处理', d.asks.length ? h`<ul class="tl-asks">${d.asks.map(askHTML)}</ul>` : none('现在没有等你处理的事。'))}
    ${sec('近期要点', d.highlights.length ? h`<ul class="tl-hls">${d.highlights.map(hlHTML)}</ul>` : none('最近还没有值得汇报的要点。'))}
    ${sec('里程碑', d.milestones.length ? h`<ul class="tl-mss">${d.milestones.map((m) => msLine(m, ro))}</ul>` : none(ro ? '没有里程碑。' : '还没有里程碑；有截止日期或 Agent 完成重要交付时也会自动出现。'),
      ro ? '' : btn('添加里程碑', { act: 'tl-ms-add', icon: 'plus', size: 'sm', variant: 'ghost' }))}
    <details class="tl-full" id="tl-full-${id}"><summary>${icon('history', { size: 14 })}<span>完整记录</span><small>${d.event_count} 条</small></summary>
      <div class="tl-fullb">${graphHTML(d)}
        <div class="tl-chips" id="tl-chips" role="group" aria-label="筛选">${chipsHTML(d)}</div><div class="tl-feed" id="tl-feed">${feedHTML(d, T.filter)}</div></div></details></div>`;
}

/* ------------------------------------------------------------------ the board */
export function render(data) {
  const bar = (extra = '') => h`<div class="tl-bar">${modeSwitch('timeline')}<span class="grow"></span>${extra}</div>`;
  if (data.error) return h`<div class="tl-page">${bar()}${empty({ icon: 'circle-alert', title: '暂时看不了项目', desc: data.error })}</div>`;
  const { lanes, key, detail } = data;
  const all = lanes.projects;
  const arch = lanes.archived || [];
  if (!all.length && !arch.length) return h`<div class="tl-page">${bar()}${empty({ icon: 'folder', title: '还没有项目', desc: '在「上下文」页添加项目后，这里会按项目帮你盯着。' })}</div>`;
  const n = (lv) => all.filter((p) => p.health.level === lv).length;
  const bits = [['risk', '有风险'], ['attention', '要你处理'], ['stalled', '停滞']].filter(([lv]) => n(lv)).map(([lv, l]) => `${n(lv)} 个${l}`);
  const sum = h`<span class="tl-sum${bits.length ? ' warn' : ''}">${all.length} 个项目 · ${bits.length ? bits.join('，') : '一切正常'}</span>`;
  const groups = GROUPS.map((g) => ({ ...g, items: all.filter((p) => g.levels.includes(p.health.level)) })).filter((g) => g.items.length);
  const rows = (g) => g.items.map((p) => rowHTML(p, p.key === key, p.key === key ? detail : null));
  const archived = arch.length
    ? h`<details class="tl-group fold arch" id="tl-g-arch"${arch.some((a) => a.key === key) ? raw(' open') : ''}><summary><h3>已归档</h3><span class="tl-n">${arch.length}</span>${icon('chevron-down', { size: 15 })}</summary><div class="tl-rows">${arch.map((a) => archRow(a, a.key === key, a.key === key ? detail : null))}</div></details>` : '';
  return h`<div class="tl-page">${bar(sum)}<div class="tl-board" data-scroll="tl-board">
    ${groups.map((g) => (g.fold
    ? h`<details class="tl-group fold" id="tl-g-${g.id}"${g.items.some((p) => p.key === key) ? raw(' open') : ''}><summary><h3>${g.title}</h3><span class="tl-n">${g.items.length}</span>${icon('chevron-down', { size: 15 })}</summary><div class="tl-rows">${rows(g)}</div></details>`
    : h`<section class="tl-group"><header><h3>${g.title}</h3><span class="tl-n">${g.items.length}</span></header><div class="tl-rows">${rows(g)}</div></section>`))}${archived}</div></div>`;
}

/** An archived project: one quiet line (name opens its read-only history) with a restore button. */
function archRow(a, open, detail) {
  return h`<div class="tl-ai${open ? ' open' : ''}"><div class="tl-arow">
    <a class="tl-ar-n" href="${hashFor('board', open ? { mode: 'timeline' } : { mode: 'timeline', project: a.key })}" aria-expanded="${open}">${a.name}</a>
    <span class="tl-ar-s">${a.summary}</span><time class="tl-ar-t">归档于 ${md(localDay(a.archived_at))}</time>
    ${btn('恢复', { act: 'tl-restore', data: { key: a.key, name: a.name }, icon: 'undo', size: 'sm' })}</div>
    ${open && detail ? reviewHTML(detail, true) : ''}</div>`;
}

export function mount(el, data) {
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
  // a refresh restores which groups were folded; the group that holds the open project must stay open
  const rb = $('.tl-rb', el);
  const grp = rb?.closest('details.tl-group');
  if (grp) grp.open = true;
  // a row that has just been opened by the user eases in and scrolls into view; a live refresh of the same row does not replay it
  const key = data?.key || '';
  if (key && key !== T.shown) {
    const body = rb;   // only one row is open at a time
    if (body && !matchMedia('(prefers-reduced-motion: reduce)').matches) {
      body.classList.add('arrive');
      setTimeout(() => body.classList.remove('arrive'), 700);
      body.parentElement.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    }
  }
  T.shown = key;
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
/** Leave the open review if it was the project that has just been archived. */
const leave = (key) => ((S.params.get('project') || '') === key ? setParams({ project: null }, { replace: true }) : refresh());
ACTS['tl-archive'] = async (el) => {
  const { key, name } = el.dataset; const open = Number(el.dataset.open || 0); const asks = Number(el.dataset.asks || 0);
  let out = null;
  const done = await dialog({ title: `归档「${name}」？`, icon: 'package', submitLabel: '归档',
    body: h`<p class="dialog-p">归档后不再出现在这里，系统和 Agent 也不再主动盯它，随时可以恢复。</p>
      ${open ? h`<label class="check"><input type="checkbox" name="close_open" checked><span>同时忽略它名下还没处理的 ${open} 项${asks ? `（${asks} 项等你处理的也会一并关掉）` : ''}</span></label>` : ''}`,
    onSubmit: async (f) => { out = await post(`/projects/${enc(key)}/archive`, { close_open: !!f.close_open }); } });
  if (!done) return;
  toast(`已归档 ${name}${out?.closed ? `，并关掉 ${out.closed} 项` : ''}`);
  leave(key);
};
ACTS['tl-restore'] = async (el) => {
  await post(`/projects/${enc(el.dataset.key)}/restore`);
  toast(`已恢复「${el.dataset.name}」`);
  refresh();
};
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
  if (!await confirmDialog('删除这个里程碑？', m ? `「${m.title}」会被删除，完整记录里会留下一条说明。` : '', { danger: true, submitLabel: '删除' })) return;
  await del(`/milestones/${enc(el.dataset.id)}`); toast('已删除'); refresh();
};

export default { load, render, mount };
