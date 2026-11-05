// Run detail drawer: event stream, raw logs, result, prompt. Live updates poll at most every 5 s, only while the drawer is
// open, the run is live and the tab is visible (SSE-driven refresh covers the rest).
import { h, raw, setHTML, appendHTML, $, $$, enc } from '../core/html.js';
import { md } from '../core/markdown.js';
import { absTime, bytes, compact, cut, dur, usd, shortId, jsonText } from '../core/format.js';
import { api } from '../core/api.js';
import { ACTS, CHANGES } from '../core/acts.js';
import { S } from '../core/state.js';
import { icon } from './icons.js';
import { btn, iconBtn, runBadge, badge, empty, execAvatar, kvList, kbd, skel, tipAttr } from './components.js';
import { confirmDialog, toast } from './overlay.js';
import { META, EV_KIND, SENSITIVE_RE, purposeLabel } from '../domain/vocab.js';
import { artifactRow } from './reader.js';
import { post } from '../core/api.js';

export const RS = { data: null, tab: 'events', filter: '', follow: true, timer: null, log: null };
const TABS = [['events', '事件流'], ['log', '原始日志'], ['result', '结果'], ['prompt', 'Prompt']];

export function evBody(e) {
  const dt = e.data;
  if (dt == null) return '';
  if (typeof dt === 'string') return dt;
  if (e.kind === 'tool_call') return jsonText(dt.input ?? dt.arguments ?? dt.args ?? dt.command ?? dt, 4000);
  for (const k of ['text', 'content', 'message', 'output', 'line']) if (typeof dt[k] === 'string') return dt[k];
  return jsonText(dt, 4000);
}
const KIND_ICON = { message: 'bot', tool_call: 'wrench', tool_result: 'terminal', reasoning: 'sparkles', stderr: 'circle-alert', error: 'circle-x', status: 'info', usage: 'gauge' };
export function runEventHtml(e) {
  const body = evBody(e);
  const sensitive = e.kind === 'tool_call' && SENSITIVE_RE.test(`${e.name || ''} ${body}`);
  return h`<div class="ev ev-${e.kind}${sensitive ? ' sensitive' : ''}" data-kind="${e.kind}">
    <div class="ev-h"><span class="ev-k">${icon(KIND_ICON[e.kind] || 'circle', { size: 13 })}${EV_KIND[e.kind] || e.kind}</span>${e.name ? h`<code>${e.name}</code>` : ''}${sensitive ? badge('疑似对外动作', { tone: 'crit', icon: 'shield-alert', variant: 'soft' }) : ''}<span class="grow"></span><time title="${absTime(e.ts, true)}">${absTime(e.ts)}</time><span class="ev-id">#${e.id}</span></div>
    ${body ? (e.kind === 'message' ? h`<div class="ev-b">${md(body)}</div>` : h`<pre class="ev-b mono">${body}</pre>`) : ''}</div>`;
}

export function runDrawerHTML(d, backId) {
  const r = d.run; const live = ['running', 'queued'].includes(r.status);
  return h`<div class="dr-head"><div class="dr-top">${runBadge(r.status)}<h2>${purposeLabel(r.purpose)}<span class="muted"> · ${r.executor}${r.model ? ` · ${r.model}` : ''}</span></h2>
      ${d.cancellable ? btn('取消运行', { act: 'cancel-run', data: { id: r.id }, variant: 'destructive', size: 'sm', icon: 'ban' }) : ''}${iconBtn('x', { act: 'close-drawer', tip: '关闭 Esc', cls: 'dr-x' })}</div>
    <div class="rd-meta">${backId ? h`<button type="button" class="meta-i" data-act="open-task" data-id="${backId}">${icon('arrow-left', { size: 13 })}<span>返回任务</span></button>` : ''}
      ${d.task ? h`<button type="button" class="meta-i" data-act="open-task" data-id="${d.task.id}">${icon('tasks', { size: 13 })}<span>${cut(d.task.headline || d.task.title, 50)}</span></button>` : ''}
      <span class="meta-i">${execAvatar(r.executor, { size: 18, tip: false })}<span>${r.executor}</span></span>
      <span class="meta-i">${icon('rotate', { size: 13 })}<span>第 ${r.attempt} 次</span></span><span class="meta-i">${icon('clock', { size: 13 })}<span>${absTime(r.started_at)} · ${dur(r.duration_s)}</span></span>
      <span class="meta-i">${icon('dollar', { size: 13 })}<span>${usd(r.cost_usd)}</span></span><span class="meta-i">${icon('cpu', { size: 13 })}<span>${compact(r.tokens_in)} / ${compact(r.tokens_out)} tokens</span></span>
      ${r.pid ? h`<span class="meta-i muted">pid ${r.pid}</span>` : ''}${r.exit_code != null ? h`<span class="meta-i muted">exit ${r.exit_code}</span>` : ''}${r.error_class ? badge(r.error_class, { tone: 'crit', variant: 'soft' }) : ''}
      <button type="button" class="meta-i meta-id" data-act="copy" data-text="${r.id}" data-tip="复制运行 ID">${icon('copy', { size: 12 })}<span class="mono">${shortId(r.id)}</span></button></div>
    ${r.error ? h`<pre class="errbox">${r.error}</pre>` : ''}
    <div class="tabs tabs-line" role="tablist">${TABS.map(([k, label]) => h`<button type="button" role="tab" class="tab${RS.tab === k ? ' on' : ''}" aria-selected="${RS.tab === k}" data-act="run-tab" data-tab="${k}">${label}${k === 'events' ? h`<span class="tab-n" id="run-ev-count">${d.events.length}</span>` : ''}</button>`)}
      ${live ? h`<span class="live-tag"><i class="sdot tone-good pulse"></i>实时</span>` : ''}</div></div>
    <div class="dr-body" id="run-body">${runTab(d)}</div>`;
}

function runTab(d) {
  if (RS.tab === 'events') {
    const kinds = Object.keys(d.event_counts || {}); const f = RS.filter;
    const evs = f ? d.events.filter((e) => e.kind === f) : d.events;
    return h`<div class="filter-row"><button type="button" class="chip-f${!f ? ' on' : ''}" data-act="ev-filter" data-kind="">全部 <b>${d.events.length}</b></button>${kinds.map((k) => h`<button type="button" class="chip-f${f === k ? ' on' : ''}" data-act="ev-filter" data-kind="${k}">${EV_KIND[k] || k} <b>${d.event_counts[k]}</b></button>`)}<span class="grow"></span>
      <label class="check"><input type="checkbox" data-change="run-follow"${RS.follow ? raw(' checked') : ''}><span>自动滚动</span></label></div>
      <div class="events" id="run-events">${evs.length ? evs.map(runEventHtml) : empty({ icon: 'terminal', title: '还没有事件' })}</div>`;
  }
  if (RS.tab === 'log') return h`<div data-lazy="run-log">${skel('100%', 10)}${skel('80%', 10)}</div>`;
  if (RS.tab === 'prompt') return h`<div data-lazy="run-prompt">${skel('100%', 10)}${skel('80%', 10)}</div>`;
  const r = d.run;
  return h`${r.final_text ? h`<div class="card"><header class="card-h"><div class="card-t"><h3>最终消息</h3></div></header><div class="card-b">${md(r.final_text)}</div></div>` : ''}
    ${r.structured ? h`<div class="card"><header class="card-h"><div class="card-t"><h3>结构化输出</h3></div></header><div class="card-b"><pre class="code">${jsonText(r.structured, 40000)}</pre></div></div>` : ''}
    ${d.artifacts?.length ? h`<div class="card"><header class="card-h"><div class="card-t"><h3>交付物</h3></div></header><div class="card-b art-list">${d.artifacts.map((a) => artifactRow(a))}</div></div>` : ''}
    ${r.command ? h`<details class="rd-sec"><summary><span class="rd-sec-t">命令行</span></summary><div class="rd-sec-b"><pre class="code">${jsonText(r.command)}</pre><p class="hint">cwd：<span class="mono">${r.cwd || '—'}</span></p></div></details>` : ''}
    ${!r.final_text && !r.structured ? empty({ icon: 'file', title: '暂无结果' }) : ''}`;
}

async function lazyLog(n, kind) {
  const d = RS.data;
  if (!d?.run) return;
  try {
    const lg = RS.log || (RS.log = await api(`/runs/${enc(d.run.id)}/log?tail=300`));
    if (kind === 'prompt') { setHTML(n, lg.prompt ? h`<pre class="code tall">${lg.prompt}</pre>` : empty({ icon: 'file', title: '没有 prompt 文件' })); return; }
    if (!lg.available) { setHTML(n, empty({ icon: 'terminal', title: '没有日志目录', desc: '运行尚未开始或已被清理' })); return; }
    const block = (f, title) => h`<div class="card"><header class="card-h"><div class="card-t"><h3>${title}</h3></div><div class="card-a muted small">${f?.exists ? `${bytes(f.bytes)}${f.truncated ? ' · 仅显示末尾' : ''}` : '不存在'}</div></header><div class="card-b">${f?.lines?.length ? h`<pre class="code">${f.lines.join('\n')}</pre>` : h`<span class="muted small">（空）</span>`}</div></div>`;
    setHTML(n, h`<div class="row"><span class="small muted mono grow">${lg.log_dir}</span>${btn('刷新日志', { act: 'reload-log', size: 'sm', icon: 'refresh' })}</div>${block(lg.stdout, 'stdout.jsonl')}${block(lg.stderr, 'stderr.log')}
      ${lg.last_message ? h`<div class="card"><header class="card-h"><div class="card-t"><h3>last_message.txt</h3></div></header><div class="card-b"><pre class="code">${lg.last_message}</pre></div></div>` : ''}
      <p class="hint">文件：${lg.files.map((f) => `${f.name} (${bytes(f.bytes)})`).join('，') || '—'}</p>`);
  } catch (e) { setHTML(n, empty({ icon: 'circle-alert', title: '读取失败', desc: e.message })); }
}

export function paintRun(dr, backId) {
  setHTML(dr, h`${runDrawerHTML(RS.data, backId)}`);
  afterPaint(dr);
}
function afterPaint(dr) {
  $$('[data-lazy="run-log"]', dr).forEach((n) => lazyLog(n, 'log'));
  $$('[data-lazy="run-prompt"]', dr).forEach((n) => lazyLog(n, 'prompt'));
}

export function stopRunPoll() { if (RS.timer) { clearInterval(RS.timer); RS.timer = null; } }
export function startRunPoll(dr) {
  stopRunPoll();
  const d = RS.data;
  if (!d?.run || !['running', 'queued'].includes(d.run.status)) return;
  RS.timer = setInterval(() => { if (!document.hidden) pollRun(dr); }, 5000);
}
export async function pollRun(dr) {
  const d = RS.data;
  if (!d?.run || S.drawer?.type !== 'run') { stopRunPoll(); return; }
  let ev;
  try { ev = await api(`/runs/${enc(d.run.id)}/events?after=${d.lastId}&limit=1000`); } catch { return; }
  if (RS.data !== d) return;
  if (ev.events.length) {
    d.events.push(...ev.events); d.lastId = ev.last_id;
    for (const e of ev.events) d.event_counts[e.kind] = (d.event_counts[e.kind] || 0) + 1;
    const cnt = $('#run-ev-count'); if (cnt) cnt.textContent = String(d.events.length);
    const box = $('#run-events');
    if (box && RS.tab === 'events') {
      const fresh = RS.filter ? ev.events.filter((e) => e.kind === RS.filter) : ev.events;
      if (fresh.length) {
        $('.empty', box)?.remove();
        appendHTML(box, h`${fresh.map(runEventHtml)}`);
        if (RS.follow) { const b = $('#run-body'); if (b) b.scrollTop = b.scrollHeight; }
      }
    }
  }
  if (ev.run_status !== d.run.status) { stopRunPoll(); S.refresh(true); }
}

export async function loadRun(id) {
  const [d, ev] = await Promise.all([api(`/runs/${enc(id)}`), api(`/runs/${enc(id)}/events?after=0&limit=5000`)]);
  return { ...d, events: ev.events, lastId: ev.last_id };
}

ACTS['run-tab'] = (el) => {
  RS.tab = el.dataset.tab;
  const dr = $('#drawer'); paintRun(dr, S.drawer?.back);
  if (RS.tab === 'events' && RS.follow) { const b = $('#run-body'); if (b) b.scrollTop = b.scrollHeight; }
};
ACTS['ev-filter'] = (el) => { RS.filter = el.dataset.kind; paintRun($('#drawer'), S.drawer?.back); };
ACTS['reload-log'] = () => { RS.log = null; paintRun($('#drawer'), S.drawer?.back); };
CHANGES['run-follow'] = (el) => { RS.follow = el.checked; };
ACTS['cancel-run'] = async (el) => {
  if (!await confirmDialog('取消运行', '将向执行器发送取消信号并结束整个进程树。', { danger: true, submitLabel: '取消运行', icon: 'ban' })) return;
  const r = await post(`/runs/${enc(el.dataset.id)}/cancel`);
  toast(r.ok ? '已发送取消信号' : r.detail, r.ok ? 'ok' : 'err');
  await S.refresh(true);
};
