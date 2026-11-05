// 总览: KPI cards with sparklines, what needs you, pipeline, autonomy mix, agent activity, cost, sensors, keywords.
import { h, raw, setHTML, $, enc } from '../core/html.js';
import { api, apiOpt } from '../core/api.js';
import { ACTS } from '../core/acts.js';
import { S, cacheTask, cacheApproval } from '../core/state.js';
import { pref } from '../core/store.js';
import { go, setParams } from '../core/router.js';
import { when, usd, dur, durShort, cut, compact, parseTs, rel, absTime } from '../core/format.js';
import { icon } from '../ui/icons.js';
import { card, btn, badge, kwChips, levelBadge, prioIcon, execAvatar, empty, riskBadge, progress, seg, statusDot, skeletonPage, tipAttr, projectChip } from '../ui/components.js';
import { sparkline, segBar, funnel, hbars, costSpec, mountCostChart, costTable } from '../ui/charts.js';
import { META, digestOf, levelName, sourceIcon, sourceLabel, purposeLabel, execSlot, catLabel } from '../domain/vocab.js';
import { evBody } from '../ui/rundetail.js';
import { countChips } from '../ui/topic-reader.js';
import { ageWord, groupByTopic, oldTone, topicSel } from '../domain/topics.js';

export async function loadDashboard() {
  let dash = await apiOpt('/dashboard?days=14');
  return dash;
}

function fallbackDash(ov) {
  const c = ov.tasks || {};
  return {
    kpis: { inbox: ov.inbox?.tasks_total ?? 0, approvals: ov.approvals_pending ?? 0, running: ov.runs_running ?? 0, done_today: ov.done_today ?? 0,
      failures_24h: ov.failures_24h?.total ?? 0, cost_today: ov.budget?.today_usd ?? 0, budget_daily: ov.budget?.daily_usd ?? 0 },
    series: { days: (ov.cost?.days || []).map((d) => d.day.slice(5)), cost: (ov.cost?.days || []).map((d) => ({ day: d.day.slice(5), ...Object.fromEntries(Object.entries(d.by_executor || {}).map(([k, v]) => [k, v.usd])), total: d.usd })) },
    levels: {}, categories: [], funnel: null, executors: [], sensors: [], keywords: [], activity: [], _fallback: true, _tasks: c,
  };
}

export async function load() {
  const [dash0, inbox, ov, tc] = await Promise.all([loadDashboard(), api('/inbox'), api('/overview'), api('/tool-calls?limit=80').catch(() => ({ tool_calls: [] }))]);
  const dash = dash0 || fallbackDash(ov);
  S.dash = dash;
  return { dash, inbox, ov, tc: tc.tool_calls || [] };
}


function delta(vals, { good = 'up', unit = '' } = {}) {
  if (!vals || vals.length < 2) return '';
  const cur = vals[vals.length - 1]; const prev = vals[vals.length - 2];
  const d = cur - prev;
  if (!d) return h`<span class="kpi-d">与昨日持平</span>`;
  const better = good === 'up' ? d > 0 : d < 0;
  return h`<span class="kpi-d ${better ? 'tone-good' : 'tone-warn'}">${icon(d > 0 ? 'arrow-up-right' : 'arrow-right', { size: 12, cls: d > 0 ? '' : 'down' })}较昨日 ${d > 0 ? '+' : ''}${unit === '$' ? usd(d) : d}</span>`;
}

function kpiCard({ label, ic, value, foot, href, spark, tone = '' }) {
  return h`<a class="kpi${tone ? ` kpi-${tone}` : ''}" href="${href}"><div class="kpi-h"><span class="kpi-ic">${icon(ic, { size: 15 })}</span><span>${label}</span></div>
    <div class="kpi-m"><div class="kpi-v">${value}</div><div class="kpi-s">${spark || ''}</div></div><div class="kpi-f">${foot}</div></a>`;
}

const NY_ICON = { decision: 'scale', approval: 'shield-alert', review: 'circle-dot', failed: 'circle-x', handoff: 'hand', suggested: 'sparkles' };
const nyItem = (it) => {
  const dg = digestOf(it, it.kind);
  return h`<li><a class="ny" href="#/inbox?sel=${enc(`${it.kind}:${it.id}`)}">
    <span class="ny-ic tone-${it.kind === 'approval' ? (it.risk === 'high' ? 'crit' : 'warn') : it.section === 'failed' ? 'crit' : it.section === 'review' || it.section === 'handoff' ? 'warn' : 'brand'}">${icon(NY_ICON[it.section] || 'sparkles', { size: 16 })}</span>
    <span class="ny-m"><span class="ny-t">${dg.headline}</span><span class="ny-g">${dg.gist || ''}</span></span>
    <span class="ny-r">${badge(it.section_title, { tone: it.section === 'failed' ? 'crit' : it.section === 'decision' || it.section === 'approval' ? 'warn' : 'muted' })}<span class="ny-w">${when(it.ts, { short: true })}</span></span></a></li>`;
};

/** 需要你处理: one block per topic (title, counts, how long it has waited), expandable to its items. */
function needsYou(dash, inbox) {
  const feed = inbox.feed || [];
  const total = feed.length;
  const groups = groupByTopic(feed, inbox.topics || []);
  const shown = groups.slice(0, 5);
  const body = shown.length ? h`<ul class="nylist ny-topics">${shown.map((g, i) => {
    const more = g.items.length - 4;
    return h`<li class="ny-tp"><details id="nyt-${g.key}"${i === 0 ? raw(' open') : ''}>
      <summary><span class="ny-chev">${icon('chevron-down', { size: 14 })}</span>
        <span class="ny-tm"><span class="ny-tl"><a class="ny-tt" href="#/inbox?sel=${enc(topicSel(g.key))}" data-tip="打开话题：先看前情提要">${g.title}</a>${projectChip(g.project, { act: 'open-project-inbox', data: { key: g.project?.key }, cls: 'ny-pj' })}</span>${g.state_now ? h`<span class="ny-state">${g.state_now}</span>` : ''}</span>
        <span class="ny-tr">${countChips(g.counts)}${g.oldest_ts ? h`<span class="tg-age tone-${oldTone(g.oldest_ts)}">${icon('clock', { size: 11 })}最早 ${ageWord(g.oldest_ts)}前</span>` : ''}</span></summary>
      <ul class="ny-items">${g.items.slice(0, 4).map(nyItem)}${more > 0 ? h`<li><a class="ny-more" href="#/inbox?sel=${enc(topicSel(g.key))}">还有 ${more} 项，打开话题查看${icon('arrow-right', { size: 12 })}</a></li>` : ''}</ul></details></li>`;
  })}${groups.length > shown.length ? h`<li class="ny-rest"><a href="#/inbox">还有 ${groups.length - shown.length} 个话题 · 共 ${total} 项，打开收件箱${icon('arrow-right', { size: 12 })}</a></li>` : ''}</ul>`
    : empty({ icon: 'check-check', title: '没有需要你处理的事项', desc: '新的建议、审批和验收会实时出现在这里。' });
  return card({ title: '需要你处理', ic: 'inbox', icon: 'inbox', desc: total ? `${groups.length} 个话题 · 共 ${total} 项，等得最久、需要你决定的在前` : '', cls: 'span-8', actions: total ? btn('打开收件箱', { href: '#/inbox', size: 'sm', variant: 'ghost', icon: 'arrow-right' }) : '', body, flush: true });
}

function levelsCard(dash) {
  const lv = dash.levels || {};
  const total = [1, 2, 3, 4, 5].reduce((s, n) => s + (lv[n] || 0), 0);
  const items = [1, 2, 3, 4, 5].map((n) => ({ key: n, label: levelName(n), value: lv[n] || 0, color: `var(--l${n})`, ink: `var(--l${n}-ink)`, href: `#/tasks?level=${n}&root=1` }));
  const body = total ? h`${segBar(items, { height: 16 })}<ul class="lvl-legend">${items.map((i) => h`<li><a href="${i.href}"><i class="sw" style="background:${i.color}"></i><span class="ll-n">${i.label}</span><b class="ll-v">${i.value}</b></a></li>`)}</ul>` : empty({ icon: 'layers', title: '暂无开放任务' });
  return card({ title: '自主等级分布', icon: 'layers', desc: `${total} 个开放任务`, cls: '', body });
}

function funnelCard(dash) {
  const f = dash.funnel;
  const body = f ? funnel([
    { label: '信号', value: f.signals, hint: '传感器近 14 天采集' },
    { label: '任务', value: f.tasks, hint: `已分拣 ${f.triaged ?? '—'}` },
    { label: '执行', value: f.executed },
    { label: '交付', value: f.delivered },
  ]) : empty({ icon: 'activity', title: '暂无流水线数据' });
  return card({ title: '流水线', icon: 'activity', desc: '信号 → 任务 → 执行 → 交付（近 14 天）', body });
}

function liveTool(tc, runId) {
  const c = tc.find((x) => x.run_id === runId);
  if (!c) return '';
  const arg = cut(String(evBody({ kind: 'tool_call', data: c.data })).replace(/\s+/g, ' '), 48);
  return h`<span class="ag-tool">${icon('wrench', { size: 12 })}<code>${c.name || 'tool'}</code><span class="muted">${arg}</span></span>`;
}
function agentCard(dash, ov, tc) {
  const running = (ov.running || []).filter((r) => r.status === 'running');
  const queued = (ov.running || []).filter((r) => r.status === 'queued');
  const exs = dash.executors || [];
  const body = h`${running.length ? h`<ul class="aglist">${running.map((r) => h`<li><button type="button" class="ag" data-act="open-run" data-id="${r.id}">
      ${execAvatar(r.executor, { size: 24, tip: true })}<span class="ag-m"><span class="ag-t">${cut(r.task_title || purposeLabel(r.purpose), 40)}</span><span class="ag-s"><span class="muted">${purposeLabel(r.purpose)}${r.model ? ` · ${r.model}` : ''}</span>${liveTool(tc, r.id)}</span></span>
      <span class="ag-r"><i class="sdot tone-brand pulse"></i><time class="elapsed" data-elapsed="${r.started_at}">${dur(r.duration_s)}</time></span></button></li>`)}</ul>${queued.length ? h`<p class="hint">另有 ${queued.length} 个运行在排队</p>` : ''}` : empty({ icon: 'bot', title: '当前没有运行中的 Agent', desc: '接受建议后，任务会在这里实时显示。' })}
    ${exs.length ? h`<div class="exs"><div class="exs-h"><span>执行器</span><span>成功率</span><span class="num">运行</span><span class="num">花费</span><span class="num">均时</span></div>${exs.map((e) => {
      const rate = e.runs ? e.ok / e.runs : 0; const fail = e.runs ? e.failed / e.runs : 0;
      return h`<div class="exs-r"><span class="exs-n">${execAvatar(e.name, { size: 18, tip: false })}${e.name}</span>
        <span class="exs-b" data-tip="成功 ${e.ok} · 失败 ${e.failed} · 其他 ${e.runs - e.ok - e.failed}"><span class="exs-bar"><i class="ok" style="width:${(rate * 100).toFixed(0)}%"></i><i class="bad" style="width:${(fail * 100).toFixed(0)}%"></i></span><b>${Math.round(rate * 100)}%</b></span>
        <span class="num">${e.runs}</span><span class="num">${usd(e.cost)}</span><span class="num muted">${e.avg_s > 36000 ? '—' : durShort(e.avg_s)}</span></div>`;
    })}</div>` : ''}`;
  return card({ title: 'Agent 活动', icon: 'bot', desc: running.length ? `${running.length} 个正在运行` : '空闲', cls: 'span-5', actions: btn('全部运行', { href: '#/runs', size: 'sm', variant: 'ghost', icon: 'arrow-right' }), body });
}

function costCard(dash) {
  const spec = costSpec(dash.series, dash.kpis.budget_daily);
  const total = spec.data.reduce((s, d) => s + d.total, 0);
  const view = pref.get('costView', 'chart');
  const byEx = spec.names.map((k) => [k, spec.data.reduce((s, d) => s + d.by[k], 0)]);
  const body = h`<ul class="legend">${byEx.map(([k, v]) => h`<li><i class="sw" style="background:var(--s${execSlot(k)})"></i><span>${k}</span><b>${usd(v)}</b></li>`)}</ul>
    <div class="cost-chart${view === 'chart' ? '' : ' hidden'}" id="cost-chart" data-cost></div>
    <div class="cost-table${view === 'table' ? '' : ' hidden'}" id="cost-table">${costTable(spec)}</div>`;
  return card({ title: '花费', icon: 'dollar', desc: `近 ${spec.data.length} 天合计 ${usd(total)} · 日均 ${usd(total / Math.max(1, spec.data.length))}`, cls: 'span-7', body,
    actions: seg([{ key: 'chart', label: '图表' }, { key: 'table', label: '表格' }], view, { act: 'cost-view', key: 'v', label: '视图' }) });
}
ACTS['cost-view'] = (el) => {
  pref.set('costView', el.dataset.value);
  document.querySelectorAll('[data-act="cost-view"]').forEach((b) => { const on = b.dataset.value === el.dataset.value; b.classList.toggle('on', on); b.setAttribute('aria-pressed', on); });
  $('#cost-chart')?.classList.toggle('hidden', el.dataset.value !== 'chart');
  $('#cost-table')?.classList.toggle('hidden', el.dataset.value !== 'table');
  if (el.dataset.value === 'chart') window.dispatchEvent(new Event('resize'));
};

function sensorCard(dash) {
  const sensors = dash.sensors || [];
  const bad = sensors.filter((s) => s.error).length;
  const body = sensors.length ? h`<div class="sstrip">${sensors.map((s) => {
    const tone = !s.enabled ? 'muted' : s.error ? 'crit' : 'good';
    const tip = [`${s.name}${s.enabled ? '' : '（已停用）'}`, s.last_ok ? `上次成功：${absTime(s.last_ok, true)}` : '从未成功', s.next ? `下次：${absTime(s.next)}` : '', s.error ? `错误：${cut(s.error, 140)}` : ''].filter(Boolean).join('\n');
    return h`<a class="sens tone-${tone}" href="#/sensors" data-tip="${tip}"><span class="sens-t">${statusDot(tone, { label: s.error ? '异常' : s.enabled ? '正常' : '停用' })}${icon(sourceIcon(s.name), { size: 14 })}<b>${s.name}</b></span>
      <span class="sens-s">${s.error ? h`<span class="tone-crit-ink">连续失败 ${s.fails} 次</span>` : s.last_ok ? h`成功 ${when(s.last_ok, { short: true })}` : '未运行'}</span><span class="sens-n">${s.new ? h`<b>+${s.new}</b> 新` : '无新信号'}</span></a>`;
  })}</div>` : empty({ icon: 'activity', title: '没有传感器' });
  return card({ title: '传感器健康', icon: 'activity', desc: bad ? `${bad} 个异常` : `${sensors.filter((s) => s.enabled).length} 个运行正常`, cls: 'span-12', body, actions: btn('管理', { href: '#/sensors', size: 'sm', variant: 'ghost', icon: 'arrow-right' }) });
}

function keywordCard(dash) {
  const kws = (dash.keywords || []).filter((k) => !/^L[1-5]$/.test(k.word)).slice(0, 10);
  const body = kws.length ? hbars(kws.map((k) => ({ label: k.word, value: k.count })), { act: 'kw-filter' }) : empty({ icon: 'hash', title: '还没有关键词', desc: 'AI 摘要生成后会出现在这里。' });
  return card({ title: '关键词', icon: 'hash', desc: '开放事项中出现最多的词，点击筛选收件箱', cls: 'span-4', body });
}
function deliverCard(ov) {
  const arts = ov.deliverables || [];
  const body = arts.length ? h`<ul class="dlist">${arts.map((a) => h`<li><span class="dl-ic">${icon(a.uri && !a.path ? 'link' : 'file-text', { size: 16 })}</span><span class="dl-m"><span class="dl-t">${a.title}</span><span class="dl-s">${a.task_id ? h`<button type="button" class="linkish" data-act="open-task" data-id="${a.task_id}">${cut(a.task_title || a.task_id, 30)}</button> · ` : ''}${when(a.created_at, { short: true })}</span></span>
    ${a.previewable ? btn('', { act: 'preview-artifact', data: { id: a.id, ext: a.ext, title: a.title }, icon: 'eye', variant: 'ghost', size: 'sm', tip: '预览', aria: '预览' }) : ''}</li>`)}</ul>` : empty({ icon: 'package', title: '还没有交付物' });
  return card({ title: '最近交付物', icon: 'package', cls: 'span-4', body, actions: btn('全部', { href: '#/artifacts', size: 'sm', variant: 'ghost', icon: 'arrow-right' }), flush: true });
}
function activityCard(dash) {
  const acts = (dash.activity || []).slice(0, 10);
  const icons = { task: 'tasks', run: 'play', approval: 'shield-alert', signal: 'sparkles', sensor: 'activity', artifact: 'package', context: 'book', system: 'cpu' };
  const body = acts.length ? h`<ol class="feed">${acts.map((a) => h`<li><span class="fd-ic">${icon(icons[a.entity_type] || 'circle', { size: 14 })}</span><span class="fd-t">${cut(a.text, 60)}</span><span class="fd-w">${when(a.ts, { short: true })}</span></li>`)}</ol>` : empty({ icon: 'history', title: '暂无动态' });
  return card({ title: '实时动态', icon: 'history', cls: 'span-4', body, actions: btn('历史', { href: '#/history', size: 'sm', variant: 'ghost', icon: 'arrow-right' }), flush: true });
}

export function render({ dash, inbox, ov, tc }) {
  const k = dash.kpis; const s = dash.series || {};
  const days = s.days || [];
  const costTotals = (s.cost || []).map((r) => r.total);
  const need = k.inbox + k.approvals;
  const budgetFrac = k.budget_daily ? k.cost_today / k.budget_daily : 0;
  return h`<div class="page-head"><div><h2>${need ? h`有 <b>${need}</b> 件事等你处理` : '没有等你处理的事项'}</h2><p class="muted">${k.running ? `${k.running} 个 Agent 正在运行` : '当前没有运行中的 Agent'} · 今日已完成 ${k.done_today} 项${k.failures_24h ? ` · 24 小时内 ${k.failures_24h} 次失败` : ''}</p></div></div>
    <div class="kpis">
      ${kpiCard({ label: '待你处理', ic: 'inbox', value: need, href: '#/inbox', foot: h`任务 ${k.inbox} · 审批 ${k.approvals}`, spark: sparkline(s.tasks_created, { labels: days, name: '新增任务' }) })}
      ${kpiCard({ label: '执行中', ic: 'play', value: k.running, href: '#/runs?status=running,queued', foot: h`排队任务 ${(ov.tasks?.ready) ?? 0} · 近 14 天运行`, spark: sparkline(s.runs, { labels: days, name: '运行次数' }) })}
      ${kpiCard({ label: '今日完成', ic: 'circle-check', value: k.done_today, href: '#/tasks?status=done', foot: delta(s.tasks_done), spark: sparkline(s.tasks_done, { labels: days, name: '完成任务' }) })}
      ${kpiCard({ label: '24h 失败', ic: 'circle-x', value: k.failures_24h, href: '#/errors', tone: k.failures_24h ? 'crit' : '', foot: k.failures_24h ? h`<span class="tone-crit-ink">${icon('circle-alert', { size: 12 })} 需要关注</span>` : h`<span class="tone-good-ink">${icon('check', { size: 12 })} 一切正常</span>`, spark: s.failures ? sparkline(s.failures, { labels: days, name: '失败次数' }) : '' })}
      ${kpiCard({ label: '今日花费', ic: 'dollar', value: usd(k.cost_today), href: '#/runs', foot: h`<span class="kpi-meter">${progress(budgetFrac, { tone: budgetFrac >= 0.9 ? 'crit' : budgetFrac >= 0.7 ? 'warn' : 'brand', label: `${Math.round(budgetFrac * 100)}% / ${usd(k.budget_daily)}` })}</span>`, spark: sparkline(costTotals, { labels: days, name: '花费', money: true }) })}
    </div>
    <div class="grid12">
      ${needsYou(dash, inbox)}<div class="stack span-4">${funnelCard(dash)}${levelsCard(dash)}</div>
      ${costCard(dash)}${agentCard(dash, ov, tc)}
      ${keywordCard(dash)}${deliverCard(ov)}${activityCard(dash)}
      ${sensorCard(dash)}
    </div>`;
}

export function mount(el, { dash }) {
  const c = $('#cost-chart', el);
  if (c) mountCostChart(c, costSpec(dash.series, dash.kpis.budget_daily));
}

ACTS['kw-filter'] = (el) => {
  const kw = el.dataset.kw;
  if (S.view === 'inbox') setParams({ kw, sel: null }); else go('inbox', { kw });
};
ACTS['new-task'] = async () => { const { newTaskDialog } = await import('../domain/actions.js'); await newTaskDialog(); };

export default { id: 'overview', title: '总览', sub: '健康状况、队列、预算与最新动态', icon: 'dashboard', load, render, mount, skeleton: skeletonPage };
