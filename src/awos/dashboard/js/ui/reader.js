// The reader: one place that shows EVERYTHING about a task or an approval. Used by the inbox / outbox reading pane
// and by the slide-over drawer (board, tasks table, run links, command palette).
import { h, raw, join, setHTML, $, $$, enc, safeUrl } from '../core/html.js';
import { md, plain } from '../core/markdown.js';
import { absTime, bytes, cut, dur, usd, shortId, jsonText, countdown, parseTs } from '../core/format.js';
import { getAuth, fetchText, artifactUrl, ApiError } from '../core/api.js';
import { ACTS, CHANGES } from '../core/acts.js';
import { S, CACHE, cacheTask, cacheApproval } from '../core/state.js';
import { openTask, openRun } from '../core/router.js';
import { icon } from './icons.js';
import { btn, iconBtn, badge, statusBadge, apvBadge, runBadge, riskBadge, levelBadge, prioIcon, execAvatar, kwChips, kbd, empty, skel,
  kvList, progress, statusIcon, meta, tipAttr, card } from './components.js';
import { timeline } from './timeline.js';
import { recapCard, mountRecap } from './recap.js';
import { META, TERMINAL, digestOf, catLabel, levelName, effLevel, execLabel, purposeLabel, sourceIcon, sourceLabel, recommendedKey,
  isRejectish } from '../domain/vocab.js';
import { taskAction, decideApproval, patchTask, editTaskDialog, copyText, options, LEVEL_OPTS, PRIO_OPTS } from '../domain/actions.js';

/* ------------------------------------------------------------------ sections */
const sec = (title, body, { icon: ic, count, open = true, id, cls = '' } = {}) =>
  h`<details class="rd-sec${cls ? ` ${cls}` : ''}"${id ? h` id="${id}"` : ''}${open ? raw(' open') : ''}><summary><span class="rd-sec-t">${ic ? icon(ic, { size: 15 }) : ''}<span>${title}</span>${count != null ? h`<span class="rd-sec-n">${count}</span>` : ''}</span>${icon('chevron-down', { size: 15, cls: 'rd-chev' })}</summary><div class="rd-sec-b">${body}</div></details>`;

const callout = (title, body, { tone = 'brand', icon: ic = 'sparkles' } = {}) =>
  h`<aside class="callout tone-${tone}"><div class="callout-h">${icon(ic, { size: 15 })}<b>${title}</b></div><div class="callout-b">${body}</div></aside>`;

/* ------------------------------------------------------------------ action list per status */
export function taskActionList(t) {
  const st = t.status;
  const a = (action, label, o = {}) => ({ action, label, ...o });
  const out = [];
  if (['inbox', 'triaged', 'snoozed'].includes(st)) out.push(a('accept', '接受并排队', { variant: 'default', icon: 'check', kbd: 'E' }), a('snooze', '稍后', { icon: 'clock', kbd: 'S' }), a('dismiss', '忽略', { icon: 'x', kbd: 'X' }));
  else if (st === 'review') out.push(a('complete', '验收通过', { variant: 'default', icon: 'check', kbd: 'E' }), a('rework', '返工…', { icon: 'rotate', kbd: 'R' }), a('snooze', '稍后', { icon: 'clock', kbd: 'S' }));
  else if (st === 'waiting_user') out.push(a('complete', '我已处理', { variant: 'default', icon: 'check', kbd: 'E' }), a('retry', '交给 AI 继续…', { icon: 'bot', ask: true }), a('snooze', '稍后', { icon: 'clock', kbd: 'S' }), a('dismiss', '忽略', { icon: 'x', kbd: 'X' }));
  else if (st === 'failed') out.push(a('retry', '重试', { variant: 'default', icon: 'rotate', kbd: 'E' }), a('retry', '附说明重试…', { icon: 'pencil', ask: true }), a('dismiss', '忽略', { icon: 'x', kbd: 'X' }));
  else if (['running', 'blocked', 'ready', 'waiting_approval'].includes(st)) {
    out.push(a('complete', '标记完成', { icon: 'check' }));
    if (['running', 'blocked', 'waiting_approval'].includes(st)) out.push(a('retry', '重启…', { icon: 'rotate', ask: true }));
  } else if (st === 'done') out.push(a('rework', '返工…', { icon: 'rotate', kbd: 'R' }), a('retry', '重新打开…', { icon: 'rotate', ask: true }));
  else if (['cancelled', 'dismissed'].includes(st)) out.push(a('retry', '重新打开…', { icon: 'rotate', ask: true }));
  if (!TERMINAL.has(st)) out.push(a('cancel', '取消任务', { variant: 'ghost-destructive', icon: 'ban', kbd: '#' }));
  return out;
}
export function actionBar(t, { shortcuts = true } = {}) {
  const items = taskActionList(t);
  if (!items.length) return raw('');
  return h`<div class="rd-actions" role="toolbar" aria-label="任务操作">${items.map((i) => btn(i.label, {
    act: 'task-action', data: { id: t.id, action: i.action, ask: i.ask ? '1' : null }, variant: i.variant || 'secondary', size: 'sm', icon: i.icon, kb: null, kbd: shortcuts ? i.kbd : null,
  }))}</div>`;
}
ACTS['task-action'] = async (el) => {
  const id = el.dataset.id;
  const t = CACHE.tasks.get(id) || id;
  await taskAction(t, el.dataset.action, { ask: !!el.dataset.ask });
};

/* ------------------------------------------------------------------ artifacts */
const EXT_ICON = { '.md': 'file-text', '.markdown': 'file-text', '.txt': 'file-text', '.json': 'file', '.html': 'globe', '.htm': 'globe', '.csv': 'list', '.py': 'terminal', '.diff': 'git', '.patch': 'git' };
export const artifactIcon = (a) => (a.uri && !a.path ? 'link' : EXT_ICON[a.ext] || 'file');

function csvTable(text) {
  const rows = [];
  for (const line of text.split(/\r?\n/).slice(0, 201)) {
    if (!line.trim()) continue;
    const cells = []; let cur = ''; let q = false;
    for (let i = 0; i < line.length; i++) {
      const c = line[i];
      if (c === '"') { if (q && line[i + 1] === '"') { cur += '"'; i++; } else q = !q; } else if ((c === ',' || c === '\t') && !q) { cells.push(cur); cur = ''; } else cur += c;
    }
    cells.push(cur); rows.push(cells);
  }
  if (!rows.length) return h`<pre class="code">（空）</pre>`;
  const [head, ...body] = rows;
  return h`<div class="md-table"><table><thead><tr>${head.map((c) => h`<th>${c}</th>`)}</tr></thead><tbody>${body.map((r) => h`<tr>${r.map((c) => h`<td>${c}</td>`)}</tr>`)}</tbody></table></div>`;
}

export async function artifactPreview(id, ext) {
  if (ext === '.html' || ext === '.htm') return h`<iframe class="art-frame" sandbox="" src="${artifactUrl(id)}" title="HTML 预览（沙箱）"></iframe>`;
  const { text, truncated } = await fetchText(artifactUrl(id));
  const note = truncated ? h`<div class="hint">文件较大，仅显示前 4MB</div>` : '';
  if (ext === '.md' || ext === '.markdown') return h`${note}${md(text)}`;
  if (ext === '.json') { let pretty = text; try { pretty = JSON.stringify(JSON.parse(text), null, 2); } catch { /* keep raw */ } return h`${note}<pre class="code">${pretty}</pre>`; }
  if (ext === '.csv' || ext === '.tsv') return h`${note}${csvTable(text)}`;
  return h`${note}<pre class="code">${text}</pre>`;
}

export function artifactButtons(a) {
  const out = [];
  if (a.previewable) out.push(btn('预览', { act: 'toggle-preview', data: { id: a.id, ext: a.ext }, size: 'sm', icon: 'eye', variant: 'secondary' }));
  if (a.servable) {
    out.push(btn('打开', { href: artifactUrl(a.id), external: true, size: 'sm', icon: 'external', variant: 'ghost' }));
    out.push(btn('下载', { href: artifactUrl(a.id, true), size: 'sm', icon: 'download', variant: 'ghost' }));
  }
  if (a.uri) out.push(btn('链接', { href: safeUrl(a.uri), external: true, size: 'sm', icon: 'link', variant: 'ghost' }));
  if (!out.length && a.path) out.push(h`<span class="hint" data-tip="${a.path}">${a.exists ? '位于 var/ 之外' : '文件不存在'}</span>`);
  return join(out);
}
export function artifactRow(a, { auto = false } = {}) {
  return h`<div class="art-row" data-artifact="${a.id}"><div class="art-h"><span class="art-ic">${icon(artifactIcon(a), { size: 18 })}</span>
    <div class="art-m"><div class="art-t">${a.title}</div><div class="art-s">${badge(a.kind, { variant: 'outline' })}<span>${bytes(a.bytes)}</span>${a.path ? h`<span class="mono" data-tip="${a.path}">${cut(a.path.split(/[\\/]/).pop(), 40)}</span>` : ''}</div></div>
    <div class="art-a">${artifactButtons(a)}</div></div><div class="art-body" data-preview-slot="${a.id}"${auto && a.previewable ? h` data-autopreview="${a.ext}"` : ''}></div></div>`;
}
async function loadPreviewInto(slot, id, ext) {
  setHTML(slot, h`<div class="skel-block">${skel('90%', 10)}${skel('70%', 10)}${skel('80%', 10)}</div>`);
  try { setHTML(slot, h`<div class="art-prev">${await artifactPreview(id, ext)}</div>`); slot.dataset.open = '1'; } catch (e) { setHTML(slot, h`<div class="err-text small">预览失败：${e.message}</div>`); }
}
ACTS['toggle-preview'] = async (el) => {
  const slot = document.querySelector(`[data-preview-slot="${CSS.escape(el.dataset.id)}"]`);
  if (!slot) return;
  const label = el.querySelector('.btn-label');
  if (slot.dataset.open) { slot.replaceChildren(); delete slot.dataset.open; if (label) label.textContent = '预览'; return; }
  if (label) label.textContent = '收起';
  await loadPreviewInto(slot, el.dataset.id, el.dataset.ext);
};

/* ------------------------------------------------------------------ evidence (source mails / signals), lazy */
const evCache = new Map();
const mailParty = (m) => {
  const to = (m.to || []).slice(0, 4).join('、'); const cc = (m.cc || []).slice(0, 3).join('、');
  return h`<div class="mail-p"><span><b>发件人</b> ${m.from || '—'}</span>${to ? h`<span><b>收件人</b> ${to}${(m.to || []).length > 4 ? ' 等' : ''}</span>` : ''}${cc ? h`<span><b>抄送</b> ${cc}</span>` : ''}</div>`;
};
function mailItem(m) {
  const body = m.body_available === false
    ? h`<div class="mail-na">本地没有缓存正文，只有主题和预览。</div>`
    : h`<pre class="mail-body" data-mail-body="${m.ref}">${m.body || ''}</pre>`;
  return h`<article class="mail${m.is_source ? ' src' : ''}">
    <header class="mail-h"><span class="mail-ic">${icon('mail', { size: 16 })}</span><div class="mail-m"><div class="mail-s">${m.subject || '（无主题）'}</div>${mailParty(m)}</div>
      <div class="mail-r">${m.is_source ? badge('触发来源', { tone: 'brand', variant: 'soft' }) : ''}${m.has_attachments ? badge('附件', { icon: 'link' }) : ''}<time>${m.received || ''}</time></div></header>
    ${body}
    ${m.truncated && m.ref ? h`<div class="mail-f">${btn('查看全文', { act: 'mail-full', data: { ref: m.ref }, size: 'sm', icon: 'chevron-down', variant: 'ghost' })}</div>` : ''}</article>`;
}
function signalItem(s) {
  return h`<article class="mail sig"><header class="mail-h"><span class="mail-ic">${icon(sourceIcon(s.source), { size: 16 })}</span><div class="mail-m"><div class="mail-s">${s.title}</div>
    <div class="mail-p"><span><b>来源</b> ${sourceLabel(s.source)}</span>${s.url ? h`<a href="${safeUrl(s.url)}" target="_blank" rel="noopener noreferrer">${cut(s.url, 70)}</a>` : ''}</div></div><div class="mail-r"><time>${absTime(s.observed_at)}</time></div></header>
    ${s.body ? h`<pre class="mail-body">${s.body}</pre>` : ''}</article>`;
}
export function evidenceHTML(ev) {
  const items = ev?.items || [];
  if (!items.length) return h`<div class="hint">没有关联的原始邮件或信号。</div>`;
  return h`<div class="thread">${items.map((it) => (it.type === 'mail' ? mailItem(it) : signalItem(it)))}</div>`;
}
async function loadEvidence(slot, taskId, fallbackSignal) {
  const hit = evCache.get(taskId);
  if (hit && Date.now() - hit.at < 300000) { setHTML(slot, evidenceHTML(hit.data)); return; }
  try {
    let data = await getAuth(`/tasks/${enc(taskId)}/evidence`);
    // Mail-derived signals are served as mails; when the local store has none, show the recorded signal excerpt instead.
    if (!(data.items || []).length && fallbackSignal) data = { ...data, items: [{ type: 'signal', ...fallbackSignal }] };
    evCache.set(taskId, { at: Date.now(), data });
    setHTML(slot, evidenceHTML(data));
    const n = $('.rd-sec-n', slot.closest('.rd-sec'));
    if (n) n.textContent = String((data.items || []).length);
  } catch (e) {
    if (fallbackSignal) { setHTML(slot, evidenceHTML({ items: [{ type: 'signal', ...fallbackSignal }] })); return; }
    setHTML(slot, h`<div class="hint">${e instanceof ApiError && e.status === 404 ? '没有可显示的来源。' : `读取来源失败：${e.message}`}</div>`);
  }
}
ACTS['mail-full'] = async (el) => {
  const ref = el.dataset.ref;
  el.disabled = true;
  try {
    const r = await getAuth(`/mail/read?ref=${enc(ref)}&chars=40000`);
    const text = r.body ?? r.text ?? r.content ?? (r.mail && r.mail.body) ?? '';
    const pre = document.querySelector(`[data-mail-body="${CSS.escape(ref)}"]`);
    if (pre) pre.textContent = text || pre.textContent;
    el.closest('.mail-f')?.remove();
  } finally { el.disabled = false; }
};

/* ------------------------------------------------------------------ approval blocks */
export function payloadView(payload) {
  if (payload == null || payload === '') return '';
  if (payload && typeof payload === 'object' && !Array.isArray(payload)) {
    const entries = Object.entries(payload);
    if (entries.length && entries.length <= 14) {
      return kvList(entries.map(([k, v]) => [k, Array.isArray(v) && v.every((x) => x == null || typeof x !== 'object')
        ? (v.length ? v.map(String).join('、') : h`<span class="muted">（空）</span>`)
        : v != null && typeof v === 'object'
        ? h`<pre class="code inline">${JSON.stringify(v, null, 2)}</pre>`
        : typeof v === 'string' && v.length > 280 ? h`<details><summary>${cut(v, 120)}</summary><pre class="code inline">${v}</pre></details>` : String(v)]), { cls: 'payload' });
    }
  }
  return h`<pre class="code">${jsonText(payload, 12000)}</pre>`;
}

export function optionButtons(a, { terse = false } = {}) {
  const recKey = recommendedKey(a);
  const opts = (a.options || []).filter((o) => o && o.key);
  if (!opts.length) return h`<div class="hint">该事项没有可选项</div>`;
  return h`<div class="opt-grid${terse ? ' terse' : ''}">${opts.map((o) => {
    const rec = o.key === recKey; const rej = isRejectish(o.key);
    return h`<button type="button" class="opt${rec ? ' rec' : ''}${rej ? ' rej' : ''}" data-act="decide" data-id="${a.id}" data-option="${o.key}" data-label="${o.label || o.key}" title="${o.description || ''}">
      <span class="opt-l">${rec ? h`<span class="opt-tag">${icon('sparkles', { size: 12 })}AI 推荐</span>` : ''}<b>${o.label || o.key}</b></span>${o.description && !terse ? h`<span class="opt-d">${o.description}</span>` : ''}</button>`;
  })}</div>`;
}
/** What each option means, in the reading flow (the sticky decision bar only carries the option names). */
function optionDetails(a) {
  const opts = (a.options || []).filter((o) => o && o.key && o.description);
  if (!opts.length) return '';
  const recKey = recommendedKey(a);
  return sec('各选项的含义', h`<ul class="optd">${opts.map((o) => h`<li><b>${o.label || o.key}</b>${o.key === recKey ? h`<span class="optd-rec">${icon('sparkles', { size: 11 })}AI 推荐</span>` : ''}<p>${o.description}</p></li>`)}</ul>`, { icon: 'file-text' });
}
export function decisionBlock(a, { compact = false, terse = false } = {}) {
  if (a.status !== 'pending') {
    return h`<div class="decided">${apvBadge(a.status)}<span>决策：<b>${a.decision || '—'}</b></span>${a.decision_note ? h`<span class="muted">备注：${a.decision_note}</span>` : ''}<span class="muted">${a.decided_via || ''} ${a.decided_at ? absTime(a.decided_at) : ''}</span></div>`;
  }
  return h`<div class="decide" data-approval="${a.id}">
    <label class="field"><span>备注（AI 会记住你写的话）<small class="muted"> 可选 · 修改意见也写在这里，随决策一起交给 AI</small></span><textarea class="textarea" name="note" rows="${compact ? 2 : 3}" data-dirty-key="note-${a.id}" placeholder="例如：把措辞改得更正式一些再发送"></textarea></label>
    ${optionButtons(a, { terse })}</div>`;
}
/* The decision bar is sticky; on a laptop screen it must never swallow the content the user needs to read first.
   It stays compact, and the user can fold it away while reading (the choice is remembered per viewer). */
const DECIDE_KEY = 'awos.decideCollapsed';
function decideCollapsed() { try { return localStorage.getItem(DECIDE_KEY) === '1'; } catch { return false; } }
ACTS['toggle-decide'] = (el) => {
  const bar = el.closest('.rd-decide');
  if (!bar) return;
  const folded = bar.classList.toggle('collapsed');
  el.setAttribute('aria-expanded', String(!folded));
  el.textContent = folded ? '展开选项' : '收起';
  try { localStorage.setItem(DECIDE_KEY, folded ? '1' : '0'); } catch { /* storage unavailable: just not remembered */ }
};
ACTS.decide = async (el) => {
  const a = CACHE.approvals.get(el.dataset.id);
  const card = el.closest('[data-approval]');
  const note = card?.querySelector('textarea[name=note]')?.value.trim() || null;
  if (!a) throw new Error('找不到该审批事项，请刷新');
  const btns = $$('button', card); btns.forEach((b) => { b.disabled = true; });
  try { await decideApproval(a, el.dataset.option, note, el.dataset.label); } finally { btns.forEach((b) => { b.disabled = false; }); }
};

/* ------------------------------------------------------------------ task reader */
function metaRow(t) {
  const due = t.due_at ? parseTs(t.due_at) : null;
  const overdue = due && due.getTime() < Date.now() && !TERMINAL.has(t.status);
  return h`<div class="rd-meta">
    ${statusBadge(t.status)}${levelBadge(t, { full: true })}${t.priority != null ? h`<span class="meta-i">${prioIcon(t.priority)}<span>P${t.priority}</span></span>` : ''}
    ${t.category ? meta('layers', catLabel(t.category)) : ''}${t.kind ? meta('hash', META.kinds[t.kind] || t.kind) : ''}
    <span class="meta-i">${execAvatar(t.executor, { size: 18, tip: false })}<span>${execLabel(t.executor)}</span></span>
    ${t.project ? meta('folder', t.project) : ''}
    ${due ? h`<span class="meta-i${overdue ? ' tone-crit' : ''}" data-tip="${absTime(t.due_at, true)}">${icon('calendar', { size: 13 })}<span>${overdue ? '已逾期 · ' : '截止 '}${absTime(t.due_at)}</span></span>` : ''}
    ${t.cost_usd ? meta('dollar', usd(t.cost_usd)) : ''}${t.attempts ? meta('rotate', `尝试 ${t.attempts}/${t.max_attempts}`) : ''}
    ${t.source ? meta(sourceIcon(t.source), sourceLabel(t.source)) : ''}
    <span class="meta-i" data-tip="创建 ${absTime(t.created_at, true)}">${icon('clock', { size: 13 })}<span>更新 ${absTime(t.updated_at)}</span></span>
    <button type="button" class="meta-i meta-id" data-act="copy" data-text="${t.id}" data-tip="复制任务 ID">${icon('copy', { size: 12 })}<span class="mono">${shortId(t.id)}</span></button></div>`;
}

function treeNode(n) {
  return h`<li><div class="node">${statusIcon(n.status)}<button type="button" class="node-t" data-act="open-task" data-id="${n.id}">${n.headline || n.title}</button>${levelBadge(n)}${execAvatar(n.executor, { size: 18 })}${n.cost_usd ? h`<span class="muted small">${usd(n.cost_usd)}</span>` : ''}
    ${n.deps?.length ? h`<div class="node-deps">依赖 ${n.deps.map((x) => h`<span class="dep${x.status !== 'done' ? ' pending' : ''}" data-tip="${x.title}">${cut(x.title, 18)} · ${META.statusLabel[x.status] || x.status}</span>`)}</div>` : ''}</div>
    ${n.children?.length ? h`<ul class="tree">${n.children.map(treeNode)}</ul>` : ''}</li>`;
}
const depLinks = (list, label) => (list?.length ? h`<div class="dep-line"><span class="muted">${label}：</span>${list.map((x) => h`<button type="button" class="dep${x.status !== 'done' ? ' pending' : ''}" data-act="open-task" data-id="${x.id}">${cut(x.title, 30)} · ${META.statusLabel[x.status] || x.status}</button>`)}</div>` : '');

ACTS['open-task'] = (el) => openTask(el.dataset.id);
ACTS['open-run'] = (el) => openRun(el.dataset.id, el.dataset.back);
ACTS.copy = (el) => copyText(el.dataset.text);
ACTS['edit-task'] = (el) => editTaskDialog(el.dataset.id, CACHE.tasks.get(el.dataset.id));
CHANGES['set-level'] = (el) => patchTask(el.dataset.id, { level: Number(el.value) }, `等级已改为 L${el.value}（已锁定）`);
CHANGES['set-priority'] = (el) => patchTask(el.dataset.id, { priority: Number(el.value) }, `优先级已改为 P${el.value}`);
CHANGES['set-category'] = (el) => patchTask(el.dataset.id, { category: el.value }, '类别已更新');
CHANGES['set-executor'] = (el) => patchTask(el.dataset.id, { executor: el.value }, `执行器已改为 ${el.value}`);

export function taskReader(d, { mode = 'pane' } = {}) {
  const t = d.task; cacheTask(t);
  d.approvals?.forEach(cacheApproval);
  const dg = digestOf(t, 'task');
  const e = effLevel(t);
  const L = META.levels[e] || {};
  const lastErr = (d.runs || []).find((r) => r.error);
  const pendingApv = (d.approvals || []).filter((a) => a.status === 'pending');
  const crumbs = [d.root && ['根任务', d.root], d.parent && ['父任务', d.parent]].filter(Boolean);
  const showSummaryHero = ['review', 'waiting_user', 'done'].includes(t.status) && t.summary;
  const suggested = ['inbox', 'triaged', 'snoozed'].includes(t.status);
  const prog = d.children_progress || { total: 0, done: 0 };
  const arts = d.artifacts || [];
  const firstPrev = arts.findIndex((a) => a.previewable && ['.md', '.markdown', '.txt', '.json'].includes(a.ext));
  const result = t.result && typeof t.result === 'object' ? t.result : null;
  const topic = t.topic || d.topic || CACHE.topicOf.get(t.id) || null;

  return h`<article class="reader" data-reader="task" data-id="${t.id}">
    <header class="rd-head">
      ${crumbs.length ? h`<nav class="rd-crumbs" aria-label="层级">${crumbs.map(([label, p]) => h`<button type="button" data-act="open-task" data-id="${p.id}"><span class="muted">${label}</span> ${cut(p.headline || p.title, 36)}</button>`)}</nav>` : ''}
      <h1 class="rd-title">${dg.headline}</h1>
      ${dg.gist ? h`<p class="rd-gist">${dg.gist}${dg.auto ? h` <span class="auto-tag" data-tip="尚未生成 AI 摘要，此为规则推导">规则摘要</span>` : ''}</p>` : ''}
      ${dg.headline !== t.title ? h`<p class="rd-full">${t.title}</p>` : ''}
      ${metaRow(t)}
      ${dg.keywords.length ? h`<div class="rd-kws">${kwChips(dg.keywords, { max: 8, act: 'kw-filter' })}</div>` : ''}
    </header>
    ${actionBar(t, { shortcuts: mode === 'pane' })}
    <div class="rd-body">
      ${recapCard({ id: t.id, topic, ts: t.created_at, extraIds: pendingApv.map((a) => a.id) })}
      ${suggested ? callout('AI 建议', h`${t.level_reason ? md(t.level_reason) : h`<p>分拣后没有自动启动，等你决定是否接受。</p>`}<p class="callout-s">接受后将按 <b>${levelName(e)}</b> 推进：${L.deliverable || ''}</p>`, { icon: 'sparkles' }) : ''}
      ${t.status === 'failed' ? callout('失败原因', h`${t.status_reason ? h`<p>${t.status_reason}</p>` : ''}${lastErr ? h`<pre class="errbox">${lastErr.error}</pre>` : ''}`, { tone: 'crit', icon: 'circle-x' }) : ''}
      ${pendingApv.length ? sec('等你批准', h`${pendingApv.map((a) => h`<div class="apv-inline"><div class="apv-inline-h">${riskBadge(a.risk)}${badge(META.apvKind[a.kind] || a.kind)}${a.action_type ? h`<code>${a.action_type}</code>` : ''}<b>${a.title}</b></div>
          ${a.recommendation ? callout('AI 建议', md(a.recommendation), { icon: 'sparkles' }) : ''}${a.context_md ? md(a.context_md) : ''}${a.payload != null ? h`<details><summary class="small muted">批准后将执行的内容</summary>${payloadView(a.payload)}</details>` : ''}${decisionBlock(a, { compact: true })}</div>`)}`, { icon: 'shield-alert', count: pendingApv.length, cls: 'sec-attn' }) : ''}
      ${showSummaryHero ? callout('AI 产出 / 摘要', md(t.summary), { tone: 'brand', icon: 'sparkles' }) : t.summary ? sec('AI 产出 / 摘要', md(t.summary), { icon: 'sparkles' }) : ''}
      ${t.description ? sec('描述', md(t.description), { icon: 'file-text' }) : ''}
      ${t.acceptance ? sec('验收标准', md(t.acceptance), { icon: 'check-check' }) : ''}
      ${sec('来源与原文', h`<div data-lazy="evidence" data-task="${t.id}">${h`<div class="skel-block">${skel('45%', 12)}${skel('100%', 10)}${skel('92%', 10)}</div>`}</div>`, { icon: 'mail', count: null, id: `ev-${t.id}`, open: !showSummaryHero })}
      ${arts.length ? sec('交付物', h`<div class="art-list">${arts.map((a, i) => artifactRow(a, { auto: i === firstPrev }))}</div>`, { icon: 'package', count: arts.length }) : ''}
      ${d.children?.length ? sec('子任务', h`<div class="tree-h">${progress(prog.total ? prog.done / prog.total : 0, { label: `${prog.done}/${prog.total} 已完成`, tone: 'good' })}</div>${depLinks(d.deps, '本任务依赖')}${depLinks(d.dependents, '被这些任务依赖')}<ul class="tree">${d.children.map(treeNode)}</ul>`, { icon: 'tasks', count: prog.total }) : (d.deps?.length || d.dependents?.length ? sec('依赖', h`${depLinks(d.deps, '本任务依赖')}${depLinks(d.dependents, '被这些任务依赖')}`, { icon: 'link' }) : '')}
      ${d.runs?.length ? sec('运行记录', h`<div class="run-list">${d.runs.map((r) => h`<button type="button" class="run-row" data-act="open-run" data-id="${r.id}" data-back="${t.id}">${runBadge(r.status)}<span class="run-p">${purposeLabel(r.purpose)} #${r.attempt}</span>${execAvatar(r.executor, { size: 18 })}<span class="muted small">${r.model || r.executor}</span><span class="grow"></span><span class="muted small">${dur(r.duration_s)}</span><span class="muted small num">${usd(r.cost_usd)}</span>${r.error ? h`<span class="run-err" data-tip="${cut(r.error, 400)}">${cut(r.error, 56)}</span>` : ''}</button>`)}</div>`, { icon: 'play', count: d.runs.length, open: t.status === 'running' || t.status === 'failed' }) : ''}
      ${(d.approvals || []).filter((a) => a.status !== 'pending').length ? sec('审批记录', h`${d.approvals.filter((a) => a.status !== 'pending').map((a) => h`<div class="apv-hist"><b>${a.title}</b>${decisionBlock(a)}</div>`)}`, { icon: 'shield-alert', open: false }) : ''}
      ${d.notes?.length ? sec('备注', h`<ul class="notes">${d.notes.slice().reverse().map((n) => h`<li><div class="note-t">${n.text}</div><div class="note-m">${n.by} · ${absTime(n.ts)}</div></li>`)}</ul>`, { icon: 'pencil', count: d.notes.length, open: false }) : ''}
      ${result ? sec('结构化结果', h`${result.verification ? h`<p class="small"><b>验证：</b>${jsonText(result.verification, 1500)}</p>` : ''}<pre class="code">${jsonText(result, 20000)}</pre>`, { icon: 'file', open: false }) : ''}
      ${t.plan ? sec('执行计划', h`<pre class="code">${jsonText(t.plan, 20000)}</pre>`, { icon: 'list', open: false }) : ''}
      ${sec('属性与设置', h`<div class="form-grid props">
        <label class="field"><span>自主等级</span><select class="select" data-change="set-level" data-id="${t.id}">${options(LEVEL_OPTS(), t.level)}</select></label>
        <label class="field"><span>优先级</span><select class="select" data-change="set-priority" data-id="${t.id}">${options(PRIO_OPTS, t.priority)}</select></label>
        <label class="field"><span>类别</span><select class="select" data-change="set-category" data-id="${t.id}">${options(META.categories, t.category)}</select></label>
        <label class="field"><span>执行器</span><select class="select" data-change="set-executor" data-id="${t.id}">${options(Object.fromEntries(META.executors.map((x) => [x, x])), t.executor)}</select></label></div>
        <p class="hint">${levelName(e)}：${L.deliverable || ''}　交接点：${L.handoff || ''}${t.level_cap && t.level_cap < t.level ? h`　<b>策略上限 L${t.level_cap}</b>（评估 L${t.level}）` : ''}</p>
        <div class="row">${btn('编辑标题 / 描述 / 验收标准…', { act: 'edit-task', data: { id: t.id }, size: 'sm', icon: 'pencil' })}${btn('在任务表中查看运行', { href: `#/runs?task_id=${enc(t.id)}`, size: 'sm', variant: 'ghost', icon: 'play' })}</div>
        ${t.workspace ? h`<p class="hint">工作区：<span class="mono">${t.workspace}</span></p>` : ''}`, { icon: 'wrench', open: false })}
      ${sec('时间线', timeline(d.events), { icon: 'history', count: (d.events || []).length, open: false })}
    </div></article>`;
}

/* ------------------------------------------------------------------ approval reader */
export function approvalReader(d, { mode = 'pane' } = {}) {
  const a = d.approval; cacheApproval(a);
  const t = d.task;
  if (t) cacheTask(t);
  const dg = digestOf(a, 'approval');
  const cd = countdown(a.expires_at);
  const pending = a.status === 'pending';
  const recKey = recommendedKey(a);
  const recOpt = (a.options || []).find((o) => o.key === recKey);
  // The model recommends with an option key ("approve", "A：…"): show the option's name instead of the bare key.
  const rc = String(a.recommendation || '').trim();
  let recText = rc;
  if (recOpt) {
    const m = rc.match(/^([^：:\s]+)\s*[：:]\s*([\s\S]*)$/);
    if (m && m[1].toLowerCase() === String(recOpt.key).toLowerCase()) recText = `${recOpt.label || recOpt.key}：${m[2]}`;
    else if (rc.toLowerCase() === String(recOpt.key).toLowerCase()) recText = recOpt.description || `选「${recOpt.label || recOpt.key}」`;
  }
  const topic = a.topic || d.topic || (t && t.topic) || CACHE.topicOf.get(a.id) || null;
  return h`<article class="reader" data-reader="approval" data-id="${a.id}">
    <header class="rd-head">
      ${t ? h`<nav class="rd-crumbs"><button type="button" data-act="open-task" data-id="${t.id}"><span class="muted">任务</span> ${cut(t.headline || t.title, 44)}</button></nav>` : ''}
      <h1 class="rd-title">${dg.headline}</h1>
      ${dg.gist ? h`<p class="rd-gist">${dg.gist}${dg.auto ? h` <span class="auto-tag" data-tip="尚未生成 AI 摘要，此为规则推导">规则摘要</span>` : ''}</p>` : ''}
      ${dg.headline !== a.title ? h`<p class="rd-full">${a.title}</p>` : ''}
      <div class="rd-meta">${apvBadge(a.status)}${riskBadge(a.risk)}${badge(META.apvKind[a.kind] || a.kind, { icon: a.kind === 'decision' ? 'scale' : 'send' })}
        ${a.action_type ? h`<span class="meta-i"><code>${a.action_type}</code></span>` : ''}
        <span class="meta-i" data-tip="${absTime(a.created_at, true)}">${icon('clock', { size: 13 })}<span>创建 ${absTime(a.created_at)}</span></span>
        ${a.expires_at && pending ? h`<span class="meta-i cd-${cd.state}" data-countdown="${a.expires_at}" data-suffix="${a.default_on_expiry ? `（默认：${a.default_on_expiry}）` : ''}">${icon('hourglass', { size: 13 })}<span>${cd.text}${a.default_on_expiry ? `（默认：${a.default_on_expiry}）` : ''}</span></span>` : ''}
        ${a.run_id ? h`<button type="button" class="meta-i" data-act="open-run" data-id="${a.run_id}">${icon('play', { size: 13 })}<span>来源运行</span></button>` : ''}
        <button type="button" class="meta-i meta-id" data-act="copy" data-text="${a.id}" data-tip="复制 ID">${icon('copy', { size: 12 })}<span class="mono">${shortId(a.id)}</span></button></div>
      ${dg.keywords.length ? h`<div class="rd-kws">${kwChips(dg.keywords, { max: 8, act: 'kw-filter' })}</div>` : ''}
    </header>
    <div class="rd-body">
      ${recapCard({ id: a.id, topic, ts: a.created_at })}
      ${a.recommendation ? callout(recOpt ? `AI 建议：${recOpt.label || recOpt.key}` : 'AI 建议', md(recText), { icon: 'sparkles' }) : ''}
      ${pending ? optionDetails(a) : ''}
      ${a.context_md ? sec('背景', md(a.context_md), { icon: 'file-text' }) : ''}
      ${a.payload != null && a.payload !== '' ? sec('批准后将执行的内容', payloadView(a.payload), { icon: 'send', open: a.kind === 'action' }) : ''}
      ${!pending ? sec('处理结果', h`${decisionBlock(a)}${a.result ? h`<pre class="code">${jsonText(a.result, 8000)}</pre>` : ''}`, { icon: 'check-check' }) : ''}
      ${t ? sec('来源与原文', h`<div data-lazy="evidence" data-task="${t.id}">${h`<div class="skel-block">${skel('45%', 12)}${skel('100%', 10)}${skel('92%', 10)}</div>`}</div>`, { icon: 'mail', open: false }) : ''}
      ${sec('时间线', timeline(d.events || []), { icon: 'history', count: (d.events || []).length, open: false })}
    </div>
    ${pending ? h`<section class="rd-decide${decideCollapsed() ? ' collapsed' : ''}"><h3>${icon('shield-alert', { size: 16 })}你的决定${mode === 'pane' ? h`${kbd('E')}<small class="muted">选中 AI 推荐项</small>` : ''}<button type="button" class="rd-decide-toggle" data-act="toggle-decide" aria-expanded="${!decideCollapsed()}">${decideCollapsed() ? '展开选项' : '收起'}</button></h3>${decisionBlock(a, { terse: true })}</section>` : ''}</article>`;
}

/* ------------------------------------------------------------------ mounting (lazy parts) */
export function mountReader(root, d) {
  $$('[data-lazy="evidence"]', root).forEach((slot) => {
    const sig = d?.signal ? { source: d.signal.source, title: d.signal.title, body: d.signal.body, url: d.signal.url, observed_at: d.signal.observed_at } : null;
    loadEvidence(slot, slot.dataset.task, sig);
  });
  $$('[data-autopreview]', root).forEach((slot) => loadPreviewInto(slot, slot.dataset.previewSlot, slot.dataset.autopreview));
  mountRecap(root);
}
export { recommendedKey };
