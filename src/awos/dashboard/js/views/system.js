// 异常 / 历史.
import { h, raw, enc, $, setHTML } from '../core/html.js';
import { api } from '../core/api.js';
import { ACTS } from '../core/acts.js';
import { S } from '../core/state.js';
import { setParams } from '../core/router.js';
import { when, cut, shortId, absTime } from '../core/format.js';
import { icon } from '../ui/icons.js';
import { btn, badge, empty, runBadge, skeletonList, tabs, card, statusBadge } from '../ui/components.js';
import { table, selectParam, filterBar, searchParam } from '../ui/table.js';
import { describeEvent } from '../ui/timeline.js';
import { META, ENTITY, purposeLabel, digestOf } from '../domain/vocab.js';

/* ------------------------------------------------------------------ errors */
async function loadErrors() { return api('/errors'); }
const sec = (title, hint, body, n, tone = 'crit') => h`<section class="sec-block"><div class="sec-block-h"><h3>${title}</h3>${badge(n, { tone })}<span class="muted small">${hint}</span></div>${body}</section>`;
const KIND_TONE = { budget: 'warn', queue: 'muted', upstream: 'brand', network: 'brand', restart: 'muted', killed: 'muted', timeout: 'warn', permanent: 'crit', other: 'crit' };
const runRow = (r) => { const g = r.diagnosis || {}; return h`<tr class="click" data-act="open-run" data-id="${r.id}" tabindex="0"><td class="nowrap">${when(r.ended_at || r.started_at)}</td><td>${runBadge(r.status)}</td>
    <td>${badge(g.label || r.error_class || '—', { tone: KIND_TONE[g.kind] || 'muted' })}<div class="tc-s">${g.resolved ? `已恢复：${g.why}` : (g.hint || '')}</div></td>
    <td class="small">${purposeLabel(r.purpose)} · ${r.executor}<div class="tc-s">${r.task_title ? cut(r.task_title, 40) : '后台任务'}</div></td><td class="err-cell">${cut(r.error || '', 200)}</td></tr>`; };
const RUN_HEAD = ['时间', '状态', '原因与建议', '运行', '错误原文'];
function errorsRender(d) {
  const total = d.runs.length + d.tasks.length + d.sensors.length + d.approvals.length + d.notifications.length;
  if (!total) return empty({ icon: 'circle-check', title: '一切正常', desc: '没有失败的运行、任务或传感器。' });
  const live = d.runs.filter((r) => !r.diagnosis?.resolved);
  const healed = d.runs.filter((r) => r.diagnosis?.resolved);
  const taskRows = d.tasks.map((t) => { const dg = digestOf(t, 'task'); return h`<tr class="click" data-act="open-task" data-id="${t.id}" tabindex="0"><td class="title-cell"><div class="tc-t">${dg.headline}</div><div class="tc-s">${t.status_reason || ''}</div></td><td class="nowrap">${when(t.updated_at)}</td><td class="err-cell">${cut(t.last_error?.error || '', 220)}</td></tr>`; });
  const sensorRows = d.sensors.map((s) => h`<tr><td><b>${s.name}</b></td><td class="nowrap">${when(s.last_run_at)}</td><td class="num">${s.consecutive_failures}</td><td class="err-cell">${cut(s.last_error, 360)}</td><td class="nowrap">${when(s.next_run_at)}</td><td>${btn('立即运行', { act: 'run-sensor', data: { name: s.name }, size: 'sm', icon: 'play' })}</td></tr>`);
  const kinds = {}; for (const r of d.runs) { const g = r.diagnosis; if (g) { (kinds[g.label] ||= { n: 0, open: 0, kind: g.kind }).n++; if (!g.resolved) kinds[g.label].open++; } }
  const open = d.open ?? live.length;
  const verdict = open
    ? h`<div class="callout tone-warn"><b>${open} 项还没有恢复</b>，在下面“仍需关注”里；其余 ${healed.length} 条已经被之后成功的同类运行覆盖，折叠在最下面，仅作留档。</div>`
    : h`<div class="callout tone-good"><b>当前没有未恢复的异常。</b>下面 ${healed.length} 条是已经自动恢复的历史失败，仅作留档，可以不管。</div>`;
  const causes = Object.entries(kinds).sort((a, b) => b[1].n - a[1].n);
  const causeStrip = causes.length ? h`<div class="row wrap" style="gap:8px;margin:12px 0 4px">${causes.map(([label, v]) => badge(`${label} ×${v.n}${v.open ? `（未恢复 ${v.open}）` : ''}`, { tone: v.open ? KIND_TONE[v.kind] || 'crit' : 'muted' }))}</div>` : '';
  return h`${verdict}${causeStrip}
    ${d.tasks.length ? sec('失败的任务', '重试已耗尽', table(['任务', '更新', '错误'], taskRows), d.tasks.length) : ''}
    ${live.length ? sec('仍需关注的运行', '点一行打开日志', table(RUN_HEAD, live.map(runRow)), live.length) : ''}
    ${d.sensors.length ? sec('传感器异常', '失败后按指数退避重试', table(['传感器', '上次运行', { label: '连续失败', cls: 'num' }, '错误', '下次运行', ''], sensorRows), d.sensors.length) : ''}
    ${d.approvals.length ? sec('执行失败的审批', '已批准但 Action runner 执行失败', table(['事项', '时间', '结果'], d.approvals.map((a) => h`<tr><td class="title-cell"><div class="tc-t">${a.title}</div><div class="tc-s">${a.action_type || a.kind}</div></td><td>${when(a.executed_at || a.decided_at)}</td><td class="err-cell">${cut(JSON.stringify(a.result || {}), 240)}</td></tr>`)), d.approvals.length) : ''}
    ${d.notifications.length ? sec('通知发送失败', '', table(['时间', '渠道', '标题', '错误'], d.notifications.map((n) => h`<tr><td>${when(n.created_at)}</td><td>${n.channel}</td><td>${n.title}</td><td class="err-cell">${cut(n.error || '', 300)}</td></tr>`)), d.notifications.length) : ''}
    ${healed.length ? h`<details class="sec-block healed"><summary class="sec-block-h"><h3>已恢复的历史失败</h3>${badge(healed.length, { tone: 'muted' })}<span class="muted small">之后同类运行已成功，仅作留档</span></summary>${table(RUN_HEAD, healed.map(runRow))}</details>` : ''}`;
}

/* ------------------------------------------------------------------ history */
const HS = { rows: [], next: null, qs: '' };
async function loadHistory(p) {
  const tab = p.get('tab') || 'events';
  if (tab === 'notifications') return { tab, d: await api('/notifications?limit=300') };
  const qs = new URLSearchParams({ limit: '100' });
  for (const k of ['entity_type', 'entity_id', 'type', 'actor']) if (p.get(k)) qs.set(k, p.get(k));
  const d = await api(`/events?${qs}`);
  HS.rows = d.events; HS.next = d.next_before; HS.qs = qs.toString();
  return { tab, d };
}
function entityLink(e) {
  const title = e.entity_title || shortId(e.entity_id) || '—';
  if (e.entity_type === 'task' && e.entity_id) return h`<button type="button" class="linkish" data-act="open-task" data-id="${e.entity_id}">${cut(title, 56)}</button>`;
  if (e.entity_type === 'run' && e.entity_id) return h`<button type="button" class="linkish" data-act="open-run" data-id="${e.entity_id}">${cut(title, 56)}</button>`;
  return h`<span>${cut(title, 56)}</span>`;
}
const histRow = (e) => h`<tr><td class="nowrap num" data-tip="${absTime(e.ts, true)}">${absTime(e.ts)}</td><td class="small">${e.actor}</td><td><span class="tl-ent">${ENTITY[e.entity_type] || e.entity_type}</span> ${entityLink(e)}</td><td>${describeEvent(e)}</td><td class="mono small muted">#${e.id}</td></tr>`;
const histTable = () => table(['时间', '操作者', '对象', '事件', ''], HS.rows.map(histRow), { emptyTitle: '没有符合条件的事件', emptyIcon: 'history' });
const histMore = () => h`<span class="small muted">已显示 ${HS.rows.length} 条</span>${HS.next ? btn('加载更早的事件', { act: 'history-more', icon: 'chevron-down', size: 'sm' }) : h`<span class="small muted">· 已到最早</span>`}`;
function historyRender({ tab, d }, p) {
  const tb = h`<div class="view-tabs">${tabs([{ key: 'events', label: '审计事件' }, { key: 'notifications', label: '通知记录' }], tab, { act: 'set-param', key: 'tab', label: '历史分类' })}</div>`;
  if (tab === 'notifications') {
    return h`${tb}${table(['时间', '渠道', '标题', '内容', '状态', '关联', '错误'], d.notifications.map((n) => h`<tr><td class="nowrap">${when(n.created_at)}</td><td>${n.channel}</td><td>${n.title}</td><td class="small">${cut(n.body || '', 160)}</td>
      <td>${badge(n.status, { tone: { sent: 'good', failed: 'crit', pending: 'brand', suppressed: 'muted' }[n.status] || 'muted' })}</td><td class="small">${n.ref_type === 'task' ? h`<button type="button" class="linkish" data-act="open-task" data-id="${n.ref_id}">任务</button>` : n.ref_type ? `${n.ref_type} ${shortId(n.ref_id)}` : '—'}</td><td class="err-cell">${cut(n.error || '', 200)}</td></tr>`), { emptyTitle: '没有通知记录', emptyIcon: 'mail' })}`;
  }
  return h`${tb}${filterBar(selectParam('entity_type', ENTITY, '全部对象'),
    h`<input class="input select-sm" id="f-ev-type" data-input="param" data-param="type" placeholder="事件类型（status_changed）" value="${p.get('type') || ''}" aria-label="事件类型">`,
    h`<input class="input select-sm" id="f-ev-actor" data-input="param" data-param="actor" placeholder="操作者前缀（user / sensor:）" value="${p.get('actor') || ''}" aria-label="操作者">`,
    h`<input class="input select-sm grow" id="f-ev-id" data-input="param" data-param="entity_id" placeholder="对象 ID" value="${p.get('entity_id') || ''}" aria-label="对象 ID">`)}
    <div id="history-table">${histTable()}</div><div class="row spaced" id="history-more">${histMore()}</div>`;
}
ACTS['history-more'] = async (el) => {
  if (!HS.next) return;
  el.disabled = true;
  const qs = new URLSearchParams(HS.qs); qs.set('before', HS.next);
  const d = await api(`/events?${qs}`);
  HS.rows.push(...d.events); HS.next = d.next_before;
  setHTML($('#history-table'), histTable()); setHTML($('#history-more'), histMore());
};

const mk = (id, title, sub, ic, load, render, extra = {}) => ({ id, title, sub, icon: ic, load: () => load(S.params), render: (d) => render(d, S.params), skeleton: skeletonList, ...extra });
export const errorsView = mk('errors', '异常', '失败的运行、任务与传感器', 'alert', loadErrors, errorsRender);
export const historyView = mk('history', '历史', '审计事件与通知记录', 'history', loadHistory, historyRender, { paged: () => HS.rows.length > 100 });
