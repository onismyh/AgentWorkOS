// AgentWorkOS operator console. Vanilla JS, no build step, no external assets.
// XSS rule: every piece of user/agent-provided text is rendered through h`` (auto-escaping) or md()
// (escape-first markdown). Never concatenate untrusted strings into HTML any other way.

/* ======================================================================== safe HTML */

class Safe { constructor(s) { this.s = s; } toString() { return this.s; } }
const ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;', '`': '&#96;' };
const esc = (v) => String(v ?? '').replace(/[&<>"'`]/g, (c) => ESC[c]);
const unesc = (s) => String(s).replace(/&(amp|lt|gt|quot|#39|#96);/g, (m) => ({ '&amp;': '&', '&lt;': '<', '&gt;': '>', '&quot;': '"', '&#39;': "'", '&#96;': '`' }[m]));
const raw = (s) => new Safe(String(s));
function fmt(v) {
  if (v == null || v === false || v === true) return '';
  if (v instanceof Safe) return v.s;
  if (Array.isArray(v)) return v.map(fmt).join('');
  return esc(v);
}
function h(strings, ...vals) {
  let out = strings[0];
  for (let i = 0; i < vals.length; i++) out += fmt(vals[i]) + strings[i + 1];
  return new Safe(out);
}
function safeUrl(u) {
  const s = String(u ?? '').trim();
  if (/^(https?:|mailto:|file:|notion:|zotero:|obsidian:|vscode:)/i.test(s)) return s;
  if (s.startsWith('/') && !/^\/[/\\]/.test(s)) return s;
  return '#';
}

/* ======================================================================== minimal markdown (escape first) */

function inline(src) {
  const codes = [];
  let t = String(src).replace(/`([^`\n]+)`/g, (_, c) => { codes.push(c); return `\u0000${codes.length - 1}\u0000`; });
  t = esc(t);
  t = t.replace(/\[([^\]\n]+)\]\(([^)\s]+)\)/g, (_, txt, url) =>
    `<a href="${esc(safeUrl(unesc(url)))}" target="_blank" rel="noopener noreferrer">${txt}</a>`);
  t = t.replace(/(^|[\s(（:：])(https?:\/\/[^\s<)）]+)/g, (_, pre, url) =>
    `${pre}<a href="${url}" target="_blank" rel="noopener noreferrer">${url}</a>`);
  t = t.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>').replace(/__([^_\n]+)__/g, '<strong>$1</strong>');
  t = t.replace(/(^|[^*\w])\*([^*\n]+)\*(?!\*)/g, '$1<em>$2</em>');
  t = t.replace(/~~([^~\n]+)~~/g, '<del>$1</del>');
  t = t.replace(/\n/g, '<br>');
  return t.replace(/\u0000(\d+)\u0000/g, (_, n) => `<code>${esc(codes[+n])}</code>`);
}

const LIST_RE = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/;
function mdBlocks(text) {
  const lines = text.split('\n');
  const out = [];
  const para = [];
  const flush = () => { if (para.length) { out.push(`<p>${inline(para.join('\n'))}</p>`); para.length = 0; } };
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    let m = line.match(/^\s*(```|~~~)/);
    if (m) {
      flush();
      const fence = m[1]; const buf = []; i++;
      while (i < lines.length && !lines[i].trim().startsWith(fence)) buf.push(lines[i++]);
      i++;
      out.push(`<pre><code>${esc(buf.join('\n'))}</code></pre>`);
      continue;
    }
    if (!line.trim()) { flush(); i++; continue; }
    m = line.match(/^(#{1,6})\s+(.*)$/);
    if (m) { flush(); out.push(`<h${m[1].length}>${inline(m[2])}</h${m[1].length}>`); i++; continue; }
    if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) { flush(); out.push('<hr>'); i++; continue; }
    if (/^\s*>/.test(line)) {
      flush();
      const buf = [];
      while (i < lines.length && /^\s*>/.test(lines[i])) buf.push(lines[i++].replace(/^\s*>\s?/, ''));
      out.push(`<blockquote>${mdBlocks(buf.join('\n'))}</blockquote>`);
      continue;
    }
    if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < lines.length && /^\s*\|?\s*:?-{2,}/.test(lines[i + 1])) {
      flush();
      const rows = [];
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) rows.push(lines[i++]);
      const cells = (r) => r.trim().replace(/^\||\|$/g, '').split('|').map((c) => c.trim());
      const head = cells(rows[0]);
      const body = rows.slice(2).map(cells);
      out.push(`<table><thead><tr>${head.map((c) => `<th>${inline(c)}</th>`).join('')}</tr></thead><tbody>${
        body.map((r) => `<tr>${r.map((c) => `<td>${inline(c)}</td>`).join('')}</tr>`).join('')}</tbody></table>`);
      continue;
    }
    m = line.match(LIST_RE);
    if (m) {
      flush();
      const tag = /\d/.test(m[2]) ? 'ol' : 'ul';
      const items = [];
      while (i < lines.length) {
        const lm = lines[i].match(LIST_RE);
        if (lm) { items.push(lm[3]); i++; continue; }
        if (items.length && /^\s{2,}\S/.test(lines[i])) { items[items.length - 1] += '\n' + lines[i++].trim(); continue; }
        break;
      }
      out.push(`<${tag}>${items.map((it) => {
        const cb = it.match(/^\[([ xX])\]\s+([\s\S]*)$/);
        return cb ? `<li>${cb[1] === ' ' ? '☐' : '☑'} ${inline(cb[2])}</li>` : `<li>${inline(it)}</li>`;
      }).join('')}</${tag}>`);
      continue;
    }
    para.push(line); i++;
  }
  flush();
  return out.join('');
}
const md = (src) => (src ? raw(`<div class="md">${mdBlocks(String(src).replace(/\r\n?/g, '\n'))}</div>`) : raw(''));

/* ======================================================================== formatting */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
const enc = encodeURIComponent;
const parseTs = (s) => { if (!s) return null; const d = new Date(s); return Number.isNaN(d.getTime()) ? null : d; };
function absTime(s, withYear = false) {
  const d = parseTs(s);
  if (!d) return '—';
  const o = { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false };
  if (withYear || d.getFullYear() !== new Date().getFullYear()) o.year = 'numeric';
  return d.toLocaleString('zh-CN', o);
}
function rel(s) {
  const d = parseTs(s);
  if (!d) return '—';
  const diff = (Date.now() - d.getTime()) / 1000;
  const a = Math.abs(diff);
  const suf = diff >= 0 ? '前' : '后';
  if (a < 45) return diff >= 0 ? '刚刚' : '即将';
  if (a < 3600) return `${Math.round(a / 60)} 分钟${suf}`;
  if (a < 86400) return `${Math.round(a / 3600)} 小时${suf}`;
  if (a < 86400 * 30) return `${Math.round(a / 86400)} 天${suf}`;
  return absTime(s);
}
const when = (s) => (s ? h`<span class="nowrap" title="${absTime(s, true)}">${rel(s)}</span>` : h`<span class="muted">—</span>`);
function dur(sec) {
  if (sec == null) return '—';
  if (sec < 60) return `${sec < 10 ? sec.toFixed(1) : Math.round(sec)} 秒`;
  if (sec < 3600) return `${Math.floor(sec / 60)} 分 ${Math.round(sec % 60)} 秒`;
  return `${Math.floor(sec / 3600)} 时 ${Math.round((sec % 3600) / 60)} 分`;
}
const usd = (v) => (v == null ? '—' : `$${Number(v).toFixed(Number(v) >= 100 ? 0 : 2)}`);
function compact(n) {
  const v = Number(n || 0);
  if (Math.abs(v) >= 1e6) return `${(v / 1e6).toFixed(1).replace(/\.0$/, '')}M`;
  if (Math.abs(v) >= 1e3) return `${(v / 1e3).toFixed(1).replace(/\.0$/, '')}K`;
  return String(Math.round(v * 100) / 100);
}
function bytes(n) {
  if (n == null) return '—';
  if (n < 1024) return `${n} B`;
  if (n < 1 << 20) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1 << 20)).toFixed(1)} MB`;
}
const cut = (s, n) => { const t = String(s ?? ''); return t.length > n ? `${t.slice(0, n)}…` : t; };
const shortId = (id) => String(id || '').replace(/^([a-z]+_)\d{6}/, '$1');
const jsonText = (v, n = 6000) => cut(typeof v === 'string' ? v : JSON.stringify(v, null, 2), n);

/* ======================================================================== vocabulary */

const META = {
  statusLabel: {
    inbox: '待分拣', triaged: '建议', ready: '排队中', blocked: '等待子任务', running: '运行中', waiting_approval: '待审批',
    waiting_user: '待接手', review: '待验收', done: '已完成', failed: '失败', cancelled: '已取消', dismissed: '已驳回', snoozed: '已推迟',
  },
  runLabel: { queued: '排队', running: '运行中', succeeded: '成功', failed: '失败', timeout: '超时', cancelled: '已取消', interrupted: '中断' },
  apvLabel: { pending: '待决策', approved: '已批准', rejected: '已拒绝', expired: '已过期', cancelled: '已取消', executing: '执行中', executed: '已执行', failed: '执行失败' },
  apvKind: { action: '动作', decision: '决策', review: '评审', permission: '权限', budget: '预算' },
  categories: {}, kinds: { todo: '待办事项', issue: '潜在问题', opportunity: '项目机会', decision: '需要决策', info: '信息' },
  levels: {}, executors: ['codex', 'claude', 'pi', 'dsh', 'internal'],
  factKinds: { role: '角色', project: '项目', goal: '目标', interest: '兴趣', habit: '习惯', preference: '偏好', person: '人物', deadline: '截止日期', note: '备注' },
  version: '',
};
const STATUS_TONE = { inbox: 'off', triaged: 'info', ready: 'info', blocked: 'block', running: 'run', waiting_approval: 'human',
  waiting_user: 'human', review: 'human', done: 'good', failed: 'bad', cancelled: 'off', dismissed: 'off', snoozed: 'off' };
const RUN_TONE = { queued: 'off', running: 'run', succeeded: 'good', failed: 'bad', timeout: 'bad', cancelled: 'off', interrupted: 'bad' };
const APV_TONE = { pending: 'human', approved: 'good', rejected: 'off', expired: 'off', cancelled: 'off', executing: 'run', executed: 'good', failed: 'bad' };
const PURPOSES = { triage: '分拣', plan: '规划', execute: '执行', verify: '验证', sensor: '传感器', context: '上下文', action: '动作', digest: '摘要' };
const ENTITY = { task: '任务', run: '运行', approval: '审批', signal: '信号', sensor: '传感器', context: '上下文', artifact: '交付物', system: '系统' };
const EVENT_LABEL = { created: '创建', status_changed: '状态变更', updated: '更新', retried: '重试', decided: '决策', rejected: '已删除',
  collected: '采集', failed: '失败', cancel_requested: '请求取消' };
const EV_KIND = { message: '消息', tool_call: '工具调用', tool_result: '工具结果', reasoning: '推理', stderr: 'stderr', error: '错误', status: '状态', usage: '用量' };
const EXEC_SERIES = { codex: 1, claude: 2, pi: 3, dsh: 4, internal: 5 };
const TERMINAL = new Set(['done', 'failed', 'cancelled', 'dismissed']);
const SENSITIVE_RE = /(send[_ -]?(mail|email|message)|smtp|post_message|publish|git\s+push|gh\s+(pr|release|issue)\s+(create|merge)|rm\s+-rf|remove-item|\bdel\s+\/|notion[\w.]*(create|update|append)|calendar[\w.]*(create|insert|update)|create_draft|reply_to)/i;

const chip = (label, tone) => h`<span class="chip st-${tone}"><span class="dot"></span>${label}</span>`;
const taskStatus = (s) => chip(META.statusLabel[s] || s, STATUS_TONE[s] || 'off');
const runStatus = (s) => chip(META.runLabel[s] || s, RUN_TONE[s] || 'off');
const apvStatus = (s) => chip(META.apvLabel[s] || s, APV_TONE[s] || 'off');
const prio = (p) => h`<span class="prio prio-${p}" title="${['紧急', '重要', '常规', '有空再做'][p] || ''}">P${p}</span>`;
const catLabel = (c) => (META.categories[c] ? META.categories[c].split(/[ /（]/)[0] : c || '—');
const levelName = (n) => META.levels[n]?.name || `L${n}`;
function lvl(t) {
  const e = Number(t.effective_level ?? t.level ?? 1);
  const capped = t.level_cap && Number(t.level_cap) < Number(t.level);
  const tip = `${levelName(e)}${capped ? `（评估 L${t.level}，策略上限 L${t.level_cap}）` : ''}`;
  return h`<span class="lvl lvl-${e}${capped ? ' capped' : ''}" title="${tip}">L${e}</span>`;
}
const risk = (r) => h`<span class="risk risk-${r || 'medium'}">${{ low: '低风险', medium: '中风险', high: '高风险' }[r] || r}</span>`;
const execLabel = (e) => e || '自动路由';
const purposeLabel = (p) => PURPOSES[p] || p;

/* ======================================================================== API */

const TOKEN = typeof document !== 'undefined' ? document.querySelector('meta[name="awos-token"]')?.content || '' : '';
async function api(path, { method = 'GET', body } = {}) {
  const opt = { method, headers: { Accept: 'application/json' } };
  if (method !== 'GET') opt.headers['X-AWOS-Token'] = TOKEN;
  if (body !== undefined) { opt.headers['Content-Type'] = 'application/json'; opt.body = JSON.stringify(body); }
  const r = await fetch(`/api${path}`, opt);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) {
    const d = data.detail;
    const msg = typeof d === 'string' ? d : Array.isArray(d) ? d.map((x) => `${(x.loc || []).slice(-1)[0] || ''} ${x.msg || ''}`).join('; ') : `HTTP ${r.status}`;
    throw new Error(msg || `HTTP ${r.status}`);
  }
  return data;
}
const post = (path, body = {}) => api(path, { method: 'POST', body });

/* ======================================================================== state / router */

const S = {
  view: 'overview', params: new URLSearchParams(), viewKey: '', drawer: null, drawerData: null, drawerTab: { task: 'overview', run: 'events' },
  renderSeq: 0, drawerSeq: 0, live: false, pending: false, changed: new Set(), chartMode: 'usd', cost: null,
  runPoll: null, runFollow: true, evFilter: '', history: null, health: null, overview: null, previews: {},
};
const VIEWS = {
  overview: { title: '总览', sub: '健康状况、队列、预算与最新动态', render: viewOverview },
  inbox: { title: 'Inbox', sub: '需要你看一眼的事项', render: viewInbox },
  tasks: { title: '任务', sub: '统一任务中心', render: viewTasks },
  outbox: { title: 'Outbox', sub: '等待你批准的动作与决策', render: viewOutbox },
  runs: { title: '运行', sub: '每一次 Agent 调用', render: viewRuns },
  tools: { title: '工具调用', sub: '跨运行的工具调用审计', render: viewTools },
  errors: { title: '异常', sub: '失败的运行、任务与传感器', render: viewErrors },
  artifacts: { title: '交付物', sub: 'Agent 产出的文件与链接', render: viewArtifacts },
  context: { title: '上下文', sub: '个人上下文知识库（你编辑的条目会被锁定，Agent 不会覆盖）', render: viewContext },
  sensors: { title: '传感器', sub: '信号来源与最近采集', render: viewSensors },
  history: { title: '历史', sub: '审计事件与通知记录', render: viewHistory, paged: () => (S.history?.pages || 1) > 1 },
};
const DRAWER_KEYS = ['task', 'run', 'back'];

function parseHash() {
  const rawHash = location.hash.replace(/^#\/?/, '');
  const [path, qs] = rawHash.split('?');
  return { view: VIEWS[path] ? path : 'overview', params: new URLSearchParams(qs || '') };
}
function hashFor(view, params) {
  const p = new URLSearchParams();
  for (const [k, v] of params instanceof URLSearchParams ? params.entries() : Object.entries(params)) if (v != null && v !== '') p.set(k, v);
  const qs = p.toString();
  return `#/${view}${qs ? `?${qs}` : ''}`;
}
function setParams(updates, { replace = false } = {}) {
  const { view, params } = parseHash();
  for (const [k, v] of Object.entries(updates)) { if (v == null || v === '') params.delete(k); else params.set(k, v); }
  const next = hashFor(view, params);
  if (replace) { history.replaceState(null, '', next); onRoute(); } else location.hash = next;
}
const openTask = (id) => setParams({ task: id, run: null, back: null });
const openRun = (id, back) => setParams({ run: id, task: null, back: back || null });
const closeDrawer = () => setParams({ task: null, run: null, back: null });

async function onRoute() {
  const { view, params } = parseHash();
  const p2 = new URLSearchParams(params);
  DRAWER_KEYS.forEach((k) => p2.delete(k));
  const key = `${view}?${p2}`;
  if (key !== S.viewKey) {
    const viewChanged = view !== S.view;
    S.viewKey = key; S.view = view; S.params = p2;
    if (viewChanged) { S.history = null; $('#main').scrollTop = 0; }
    renderView(viewChanged);
  }
  const d = params.get('task') ? { type: 'task', id: params.get('task') } : params.get('run') ? { type: 'run', id: params.get('run'), back: params.get('back') } : null;
  if (JSON.stringify(d) !== JSON.stringify(S.drawer)) { S.drawer = d; renderDrawer(true); }
}

async function renderView(initial = false) {
  const v = VIEWS[S.view];
  document.title = `${v.title} · AgentWorkOS`;
  $('#view-title').textContent = v.title;
  $('#view-sub').textContent = v.sub || '';
  $$('#nav a').forEach((a) => a.classList.toggle('active', a.dataset.view === S.view));
  const el = $('#view');
  if (initial) el.innerHTML = '<div class="loading">加载中…</div>';
  const seq = ++S.renderSeq;
  try {
    const out = await v.render(S.params);
    if (seq !== S.renderSeq) return;
    const active = document.activeElement;
    const focusId = active && el.contains(active) ? active.id : null;
    const sel = focusId && 'selectionStart' in active ? [active.selectionStart, active.selectionEnd] : null;
    const dirty = saveDirty(el);
    el.innerHTML = String(out);
    restoreDirty(el, dirty);
    if (focusId) {
      const n = document.getElementById(focusId);
      if (n) { n.focus(); if (sel) try { n.setSelectionRange(sel[0], sel[1]); } catch { /* not a text input */ } }
    }
    afterRender(el);
  } catch (e) {
    if (seq === S.renderSeq) el.innerHTML = String(h`<div class="empty err-text">加载失败：${e.message}</div>`);
  }
  S.pending = false;
  $('#stale-pill').classList.remove('show');
}
// Unsent text (approval notes, context drafts) survives live re-renders.
const dirtyKey = (n) => `${n.closest('[data-approval]')?.dataset.approval || n.closest('[data-form]')?.dataset.form || ''}|${n.name || n.id}`;
function saveDirty(root) {
  const out = {};
  $$('[data-dirty]', root).forEach((n) => { out[dirtyKey(n)] = n.value; });
  return out;
}
function restoreDirty(root, saved) {
  if (!Object.keys(saved).length) return;
  $$('textarea, input', root).forEach((n) => {
    const k = dirtyKey(n);
    if (k in saved) { n.value = saved[k]; n.dataset.dirty = '1'; }
  });
}
function afterRender(root) {
  drawCharts();
  $$('[data-lazy]', root).forEach((n) => LAZY[n.dataset.lazy]?.(n));
}

/* ======================================================================== live refresh */

let refreshTimer = null;
function scheduleRefresh(delay = 700) { clearTimeout(refreshTimer); refreshTimer = setTimeout(() => refreshNow(false), delay); }
function isEditing() {
  const a = document.activeElement;
  if (a && a.matches('input:not([data-filter]), textarea, select:not([data-filter])') && a.closest('#view, #drawer')) return true;
  return $('#modal-wrap').classList.contains('show');
}
async function refreshNow(force = false) {
  refreshBadges();
  const changed = S.changed; S.changed = new Set();
  if (!force && (isEditing() || VIEWS[S.view].paged?.())) {
    S.pending = true; $('#stale-pill').classList.add('show');
    changed.forEach((x) => S.changed.add(x));
    return;
  }
  if (force) S.history = null;
  await renderView(false);
  if (S.drawer) {
    const d = S.drawerData;
    const runRelevant = S.drawer.type !== 'run' || force || !d || changed.has(d.run?.id) || changed.has(d.run?.task_id);
    if (runRelevant) await renderDrawer(false);
  }
}
function setLive(on) {
  S.live = on;
  $('#foot-live').className = `dot-s ${on ? 'ok' : 'warn'}`;
  $('#foot-live-text').textContent = on ? '实时推送 · 已连接' : '实时推送 · 重连中（每 20 秒轮询）';
}
function connectLive() {
  if (!window.EventSource) { setLive(false); return; }
  const es = new EventSource('/api/stream');
  es.onopen = () => setLive(true);
  es.onerror = () => setLive(false);
  es.onmessage = (m) => {
    let ev;
    try { ev = JSON.parse(m.data); } catch { return; }
    if (ev.entity_id) S.changed.add(ev.entity_id);
    scheduleRefresh(ev.entity_type === 'run' ? 1200 : 600);
  };
}
function setBadge(name, n, cls = '') {
  const b = document.querySelector(`[data-badge="${name}"]`);
  if (!b) return;
  b.hidden = !n;
  b.textContent = n > 99 ? '99+' : String(n || '');
  b.className = `badge ${n ? cls : ''}`;
}
async function refreshBadges() {
  try {
    const o = await api('/overview');
    S.overview = o;
    setBadge('inbox', o.inbox.total, 'hot');
    setBadge('outbox', o.approvals_pending, 'hot');
    setBadge('runs', o.runs_running);
    setBadge('errors', o.failures_24h.total, 'alert');
  } catch { /* the footer shows connectivity */ }
}
async function refreshHealth() {
  try { applyHealth(await api('/health')); } catch {
    $('#foot-daemon').className = 'dot-s bad';
    $('#foot-daemon-text').textContent = 'API 无法连接';
  }
}
function daemonState(dm) {
  if (!dm) return ['warn', '未知'];
  if (dm.alive) return ['ok', `守护进程 · 心跳 ${rel(dm.ts)}`];
  if (dm.ts) return ['bad', `守护进程停止 · 最后心跳 ${rel(dm.ts)}`];
  return ['warn', '未检测到守护进程（仅 API）'];
}
function applyHealth(hl) {
  S.health = hl;
  const [cls, text] = daemonState(hl.daemon);
  $('#foot-daemon').className = `dot-s ${cls}`;
  $('#foot-daemon-text').textContent = text;
  $('#brand-version').textContent = `v${hl.version} · 运行 ${dur(hl.uptime_s)}`;
}

/* ======================================================================== shared components */

function emptyState(text) { return h`<div class="empty">${text}</div>`; }
function section(id, title, hint, body, count) {
  return h`<section class="section" id="${id}"><div class="section-h"><h2>${title}</h2>${count != null ? h`<span class="badge">${count}</span>` : ''}<span class="hint">${hint || ''}</span></div>${body}</section>`;
}
function table(headers, rows, emptyText = '暂无数据') {
  if (!rows.length) return emptyState(emptyText);
  return h`<div class="table-wrap"><table class="t"><thead><tr>${headers.map((x) => {
    const [label, cls] = Array.isArray(x) ? x : [x, ''];
    return h`<th class="${cls}">${label}</th>`;
  })}</tr></thead><tbody>${rows}</tbody></table></div>`;
}
function meter(frac) {
  const p = Math.max(0, Math.min(1, Number(frac) || 0));
  const cls = p >= 0.9 ? 'crit' : p >= 0.7 ? 'warn' : '';
  return h`<div class="meter ${cls}" role="meter" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Math.round(p * 100)}"><i style="width:${(p * 100).toFixed(1)}%"></i></div>`;
}
function progress(done, total) {
  if (!total) return '';
  return h`<span class="progress" title="子任务完成 ${done}/${total}"><span class="bar"><i style="width:${((done / total) * 100).toFixed(0)}%"></i></span>${done}/${total}</span>`;
}
function filterChip(key, value, label, count) {
  const cur = new Set((S.params.get(key) || '').split(',').filter(Boolean));
  const on = cur.has(value);
  return h`<button class="chip filter${on ? ' on' : ''}" data-act="toggle-param" data-key="${key}" data-value="${value}">${label}${count != null ? h` <span class="muted">${count}</span>` : ''}</button>`;
}
function selectParam(key, options, allLabel) {
  const cur = S.params.get(key) || '';
  return h`<select class="select" data-filter data-change="param" data-key="${key}"><option value="">${allLabel}</option>${
    Object.entries(options).map(([k, v]) => h`<option value="${k}"${k === cur ? raw(' selected') : ''}>${v}</option>`)}</select>`;
}
function options(map, cur, { auto } = {}) {
  return h`${auto ? h`<option value="">${auto}</option>` : ''}${Object.entries(map).map(([k, v]) =>
    h`<option value="${k}"${String(k) === String(cur ?? '') ? raw(' selected') : ''}>${v}</option>`)}`;
}
const LEVEL_OPTS = () => Object.fromEntries([1, 2, 3, 4, 5].map((n) => [n, levelName(n)]));
const PRIO_OPTS = { 0: 'P0 紧急', 1: 'P1 重要', 2: 'P2 常规', 3: 'P3 有空再做' };

function recommendedKey(a) {
  const rec = String(a.recommendation || '').toLowerCase();
  if (!rec) return null;
  let best = null; let bi = Infinity;
  for (const o of a.options || []) {
    for (const s of [o.key, o.label]) {
      if (!s) continue;
      const i = rec.indexOf(String(s).toLowerCase());
      if (i >= 0 && i < bi) { bi = i; best = o.key; }
    }
  }
  return best;
}
const isRejectish = (k) => /^(reject|no|cancel|deny|skip|拒绝|否)/i.test(String(k));

function approvalCard(a) {
  const recKey = recommendedKey(a);
  const opts = (a.options || []).filter((o) => o && o.key);
  return h`<article class="item-card ${a.risk === 'high' ? 'k-bad' : 'k-human'}" data-approval="${a.id}">
    <div class="head">
      <span class="chip">${META.apvKind[a.kind] || a.kind}</span>${risk(a.risk)}
      ${a.action_type ? h`<code class="muted">${a.action_type}</code>` : ''}
      <span class="title" style="cursor:default">${a.title}</span>
      ${a.status !== 'pending' ? apvStatus(a.status) : ''}
    </div>
    <div class="metas">
      ${a.task_id ? h`<a href="#" data-act="open-task" data-id="${a.task_id}">任务：${cut(a.task_title || a.task_id, 60)}</a>` : ''}
      <span>创建 ${when(a.created_at)}</span>
      ${a.expires_at ? h`<span>过期 ${when(a.expires_at)}${a.default_on_expiry ? `（默认：${a.default_on_expiry}）` : ''}</span>` : ''}
      ${a.run_id ? h`<a href="#" data-act="open-run" data-id="${a.run_id}">来源运行</a>` : ''}
    </div>
    ${a.context_md ? h`<div class="body">${md(a.context_md)}</div>` : ''}
    ${a.recommendation ? h`<div class="rec"><div class="rec-h">AI 建议</div>${md(a.recommendation)}</div>` : ''}
    ${a.payload != null && a.payload !== '' ? h`<details${a.kind === 'action' ? raw(' open') : ''}><summary class="small muted">批准后将执行的内容（payload）</summary><pre class="raw">${jsonText(a.payload)}</pre></details>` : ''}
    ${a.status === 'pending' ? h`
      <textarea class="textarea" name="note" rows="2" placeholder="备注 / 修改意见（可选，会随决策一起交给 AI）"></textarea>
      <div class="actions">${opts.map((o) => h`<button class="btn opt-btn ${o.key === recKey ? 'primary' : isRejectish(o.key) ? 'danger' : ''}"
          data-act="decide" data-id="${a.id}" data-option="${o.key}" data-label="${o.label || o.key}" data-risk="${a.risk}">
          <span>${o.key === recKey ? h`<span class="tag">AI 推荐 · </span>` : ''}${o.label || o.key}</span>
          ${o.description ? h`<small>${o.description}</small>` : ''}</button>`)}
        ${opts.length ? '' : h`<span class="muted small">该事项没有可选项</span>`}
      </div>` : h`<div class="metas"><span>决策：<b>${a.decision || '—'}</b></span>${a.decision_note ? h`<span>备注：${a.decision_note}</span>` : ''}<span>${a.decided_via || ''} ${when(a.decided_at)}</span></div>`}
  </article>`;
}

function taskActions(t, mode) {
  const b = (action, label, cls = '', extra = {}) => h`<button class="btn ${cls}" data-act="task-action" data-id="${t.id}" data-action="${action}"${extra.ask ? raw(' data-ask="1"') : ''}>${label}</button>`;
  const st = t.status;
  const out = [];
  if (mode === 'review' || st === 'review') { out.push(b('complete', '验收通过', 'primary'), b('rework', '返工…')); }
  else if (mode === 'handoff' || st === 'waiting_user') { out.push(b('complete', '我已处理', 'primary'), b('retry', '交给 AI 继续…', '', { ask: true }), b('snooze', '推迟…')); }
  else if (mode === 'suggested' || ['inbox', 'triaged', 'snoozed'].includes(st)) { out.push(b('accept', '接受并排队', 'primary'), b('snooze', '推迟…')); }
  else if (mode === 'failed' || st === 'failed') { out.push(b('retry', '重试', 'primary'), b('retry', '附说明重试…', '', { ask: true })); }
  if (mode === 'detail') {
    if (['done', 'cancelled', 'dismissed'].includes(st)) out.push(b('retry', '重新打开…', '', { ask: true }));
    if (st === 'done') out.push(b('rework', '返工…'));
    if (['running', 'blocked', 'waiting_approval'].includes(st)) out.push(b('retry', '重启…', '', { ask: true }));
    if (['running', 'blocked', 'ready', 'waiting_approval'].includes(st)) out.push(b('complete', '标记完成'));
  }
  if (['inbox', 'triaged', 'waiting_user', 'failed', 'snoozed'].includes(st)) out.push(b('dismiss', '驳回'));
  if (!TERMINAL.has(st)) out.push(b('cancel', '取消', 'danger'));
  return h`<div class="actions btn-group">${out}</div>`;
}

function artifactList(arts) {
  if (!arts?.length) return '';
  return h`<div class="list">${arts.map((a) => h`<div class="li">
    <div class="grow"><div class="t">${a.title}</div><div class="small muted">${a.kind} · ${bytes(a.bytes)} · ${when(a.created_at)}${a.path ? h` · <span class="mono" title="${a.path}">${cut(a.path.split(/[\\/]/).pop(), 50)}</span>` : ''}</div>
      <div data-preview-slot="${a.id}"></div></div>
    <div class="btn-group">${artifactButtons(a)}</div></div>`)}</div>`;
}
function artifactButtons(a) {
  const out = [];
  if (a.previewable) out.push(h`<button class="btn sm" data-act="toggle-preview" data-id="${a.id}" data-ext="${a.ext}">预览</button>`);
  if (a.servable) {
    out.push(h`<a class="btn sm" href="/api/artifacts/${enc(a.id)}/content" target="_blank" rel="noopener">打开</a>`);
    out.push(h`<a class="btn sm" href="/api/artifacts/${enc(a.id)}/content?download=1">下载</a>`);
  }
  if (a.uri) out.push(h`<a class="btn sm" href="${safeUrl(a.uri)}" target="_blank" rel="noopener noreferrer">链接</a>`);
  if (!out.length && a.path) out.push(h`<span class="small muted" title="${a.path}">${a.exists ? '位于 var/ 之外' : '文件不存在'}</span>`);
  return out;
}
async function artifactPreviewHtml(id, ext) {
  if (ext === '.html' || ext === '.htm') return h`<iframe class="preview" sandbox="" src="/api/artifacts/${enc(id)}/content" title="预览"></iframe>`;
  const r = await fetch(`/api/artifacts/${enc(id)}/content`);
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  const text = await r.text();
  const note = r.headers.get('X-AWOS-Truncated') ? h`<div class="small muted">文件较大，仅显示前 4MB</div>` : '';
  if (ext === '.md' || ext === '.markdown') return h`${note}<div class="card">${md(text)}</div>`;
  if (ext === '.json') { let pretty = text; try { pretty = JSON.stringify(JSON.parse(text), null, 2); } catch { /* keep raw */ } return h`${note}<pre class="raw">${pretty}</pre>`; }
  return h`${note}<pre class="raw">${text}</pre>`;
}

/* ======================================================================== view: overview */

async function viewOverview() {
  const [o, hl] = await Promise.all([api('/overview'), api('/health')]);
  S.overview = o; S.cost = o.cost;
  applyHealth(hl);
  const inb = o.inbox; const b = o.budget; const c = o.tasks;
  const [dcls, dtext] = daemonState(hl.daemon);
  const execs = Object.entries(hl.executors || {});
  const sensors = hl.sensors || [];
  const sOn = sensors.filter((s) => s.enabled).length;
  const sErr = sensors.filter((s) => s.last_error);
  const tile = (label, value, foot, go, alert = false) => h`<button class="tile${alert ? ' alert' : ''}" data-go="${go}"><span class="label">${label}</span><span class="value">${value}</span><span class="foot">${foot}</span></button>`;
  const queue = [['running', c.running], ['ready', c.ready], ['blocked', c.blocked], ['waiting_approval', c.waiting_approval],
    ['waiting_user', c.waiting_user], ['review', c.review], ['triaged', c.triaged], ['inbox', c.inbox], ['failed', c.failed], ['snoozed', c.snoozed]];
  return h`
  <div class="card"><div class="health">
    <span class="item"><span class="dot-s ${dcls}"></span>${dtext}</span>
    <span class="item muted">API v${hl.version} · 已运行 ${dur(hl.uptime_s)}</span>
    <span class="item">执行器
      ${execs.length ? execs.map(([n, v]) => h`<span class="chip" title="${v.detail}"><span class="dot-s ${v.ok ? 'ok' : 'bad'}"></span>${n}</span>`) : h`<span class="muted">未注册</span>`}</span>
    <span class="item"><a href="#/sensors">传感器 ${sOn}/${sensors.length} 启用</a>${sErr.length ? h`<span class="chip st-bad" title="${sErr.map((s) => s.name).join(', ')}"><span class="dot"></span>${sErr.length} 个异常</span>` : ''}</span>
    <span class="item">Codex 今日 ${b.codex_runs}/${b.codex_daily_runs} 次</span>
  </div></div>

  <div class="tiles section" style="margin-top:12px">
    ${tile('Inbox', inb.total, `决策 ${inb.decisions} · 验收 ${inb.review} · 接手 ${inb.handoff} · 建议 ${inb.suggested}`, '#/inbox')}
    ${tile('待审批', o.approvals_pending, `动作 ${inb.actions} · 决策 ${inb.decisions}`, '#/outbox')}
    ${tile('运行中', o.runs_running, `排队任务 ${c.ready} · 等待子任务 ${c.blocked}`, '#/runs?status=running,queued')}
    ${tile('今日完成', o.done_today, `累计完成 ${c.done}`, '#/tasks?status=done')}
    ${tile('今日花费', usd(b.today_usd), h`${meter(b.daily_usd ? b.today_usd / b.daily_usd : 0)}<span>预算 ${usd(b.daily_usd)} · ${compact(b.today_tokens)} tokens</span>`, '#/runs')}
    ${tile('24h 失败', o.failures_24h.total, `运行 ${o.failures_24h.runs} · 任务 ${o.failures_24h.tasks}`, '#/errors', o.failures_24h.total > 0)}
  </div>

  <div class="grid grid-2 section">
    <div class="card">
      <div class="card-h"><h2>近 14 天成本</h2><span class="spacer"></span>
        <span class="seg"><button class="${S.chartMode === 'usd' ? 'on' : ''}" data-act="chart-mode" data-mode="usd">花费 USD</button><button class="${S.chartMode === 'runs' ? 'on' : ''}" data-act="chart-mode" data-mode="runs">运行次数</button></span></div>
      ${costLegend(o.cost)}
      <div class="chart" data-chart="cost"></div>
      ${costTable(o.cost)}
    </div>
    <div class="card">
      <div class="card-h"><h2>任务队列</h2><span class="spacer"></span><a class="small" href="#/tasks">全部任务</a></div>
      <div class="list">${queue.map(([st, n]) => h`<div class="li click" data-go="#/tasks?status=${st}"><div class="grow">${taskStatus(st)}</div><b class="num">${n || 0}</b></div>`)}</div>
    </div>
  </div>

  <div class="grid grid-3 section">
    <div class="card"><div class="card-h"><h2>正在运行</h2><span class="spacer"></span><a class="small" href="#/runs">全部运行</a></div>
      ${o.running.length ? h`<div class="list">${o.running.map((r) => h`<div class="li click" data-act="open-run" data-id="${r.id}">
        <div class="grow"><div class="t ellipsis">${r.task_title || purposeLabel(r.purpose)}</div><div class="small muted">${purposeLabel(r.purpose)} · ${r.executor}${r.model ? ` · ${r.model}` : ''}</div></div>
        <div class="right small">${runStatus(r.status)}<div class="muted">${dur(r.duration_s)}</div></div></div>`)}</div>` : emptyState('当前没有运行中的 Agent')}
    </div>
    <div class="card"><div class="card-h"><h2>最新交付物</h2><span class="spacer"></span><a class="small" href="#/artifacts">全部</a></div>
      ${o.deliverables.length ? h`<div class="list">${o.deliverables.map((a) => h`<div class="li">
        <div class="grow"><div class="t ellipsis">${a.title}</div><div class="small muted ellipsis">${a.task_title ? h`<a href="#" data-act="open-task" data-id="${a.task_id}">${cut(a.task_title, 40)}</a> · ` : ''}${when(a.created_at)}</div></div>
        ${a.previewable ? h`<button class="btn sm" data-act="preview-modal" data-id="${a.id}" data-ext="${a.ext}" data-title="${a.title}">预览</button>` : ''}</div>`)}</div>` : emptyState('还没有交付物')}
    </div>
    <div class="card"><div class="card-h"><h2>最近异常</h2><span class="spacer"></span><a class="small" href="#/errors">全部</a></div>
      ${o.recent_errors.length ? h`<div class="list">${o.recent_errors.map((e) => h`<div class="li ${e.type === 'run' ? 'click' : ''}" ${e.type === 'run' ? raw(`data-act="open-run" data-id="${esc(e.id)}"`) : ''}>
        <span class="dot-s bad" style="margin-top:6px"></span><div class="grow"><div class="t ellipsis">${e.title}</div><div class="small muted ellipsis" title="${e.error || ''}">${cut(e.error || e.status, 120)}</div></div>
        <span class="small muted nowrap">${when(e.ts)}</span></div>`)}</div>` : emptyState('最近没有异常')}
    </div>
  </div>`;
}

function seriesVar(e) { return `var(--s${EXEC_SERIES[e] || 5})`; }
function costLegend(cost) {
  if (!cost || cost.executors.length < 2) return '';
  return h`<div class="legend">${cost.executors.map((e) => h`<span><i class="sw" style="background:${seriesVar(e)}"></i>${e}</span>`)}</div>`;
}
function costTable(cost) {
  if (!cost) return '';
  const ex = cost.executors;
  return h`<details class="tbl"><summary>表格视图</summary><div class="table-wrap" style="margin-top:6px"><table class="t"><thead><tr><th>日期</th>${ex.map((e) => h`<th class="num">${e} USD</th><th class="num">${e} 次</th>`)}<th class="num">合计 USD</th><th class="num">合计次数</th></tr></thead><tbody>${
    cost.days.slice().reverse().map((d) => h`<tr><td class="num">${d.day}</td>${ex.map((e) => h`<td class="num">${usd(d.by_executor[e]?.usd || 0)}</td><td class="num">${d.by_executor[e]?.runs || 0}</td>`)}<td class="num">${usd(d.usd)}</td><td class="num">${d.runs}</td></tr>`)}</tbody></table></div></details>`;
}
function niceStep(max, n = 4) {
  const rawStep = max / n;
  const p = 10 ** Math.floor(Math.log10(rawStep));
  const m = rawStep / p;
  return (m <= 1 ? 1 : m <= 2 ? 2 : m <= 2.5 ? 2.5 : m <= 5 ? 5 : 10) * p;
}
function roundTop(x, y, w, hh, r) {
  const rr = Math.min(r, w / 2, hh);
  return `M${x},${y + hh}V${y + rr}A${rr},${rr} 0 0 1 ${x + rr},${y}H${x + w - rr}A${rr},${rr} 0 0 1 ${x + w},${y + rr}V${y + hh}Z`;
}
function drawCharts() {
  const el = document.querySelector('[data-chart="cost"]');
  if (!el || !S.cost) return;
  const mode = S.chartMode;
  const { days, executors } = S.cost;
  const val = (d, e) => (mode === 'usd' ? d.by_executor[e]?.usd || 0 : d.by_executor[e]?.runs || 0);
  const totals = days.map((d) => executors.reduce((s, e) => s + val(d, e), 0));
  const peak = Math.max(0, ...totals);
  if (!peak) { el.innerHTML = String(emptyState(mode === 'usd' ? '近 14 天没有计费花费（订阅制执行器不计费，可切换到“运行次数”）' : '近 14 天没有运行记录')); return; }
  const W = Math.max(280, el.clientWidth); const H = 190; const L = 46; const R = 6; const T = 10; const B = 24;
  let step = niceStep(peak);
  if (mode === 'runs') step = Math.max(1, Math.ceil(step));
  const max = Math.ceil(peak / step) * step;
  const y = (v) => T + (H - T - B) * (1 - v / max);
  const band = (W - L - R) / days.length;
  const bw = Math.min(24, band * 0.62);
  const tickFmt = (v) => (mode === 'usd' ? `$${v < 1 && v > 0 ? v.toFixed(2) : compact(v)}` : compact(v));
  const parts = [];
  for (let v = 0; v <= max + 1e-9; v += step) {
    const yy = Math.round(y(v)) + 0.5;
    parts.push(`<line class="${v === 0 ? 'baseline' : 'grid-line'}" x1="${L}" x2="${W - R}" y1="${yy}" y2="${yy}"/>`);
    parts.push(`<text class="tick" x="${L - 6}" y="${yy + 3.5}" text-anchor="end">${esc(tickFmt(v))}</text>`);
  }
  const every = band < 30 ? 2 : 1;
  days.forEach((d, i) => {
    const cx = L + band * i + band / 2;
    parts.push(`<rect class="hit" data-i="${i}" x="${L + band * i}" y="${T}" width="${band}" height="${H - T - B}"/>`);
    let base = y(0);
    const segs = executors.map((e) => [e, val(d, e)]).filter(([, v]) => v > 0);
    segs.forEach(([e, v], k) => {
      const top = k === segs.length - 1;
      const hh = Math.max(2, (H - T - B) * (v / max));
      const gap = top ? 0 : 2;
      const yTop = base - hh;
      const drawH = Math.max(1, hh - gap);
      const yy = yTop + gap;
      parts.push(top
        ? `<path d="${roundTop(cx - bw / 2, yy, bw, drawH, 4)}" fill="${seriesVar(e)}" pointer-events="none"/>`
        : `<rect x="${cx - bw / 2}" y="${yy}" width="${bw}" height="${drawH}" fill="${seriesVar(e)}" pointer-events="none"/>`);
      base = yTop;
    });
    if (i % every === (days.length - 1) % every) parts.push(`<text class="tick" x="${cx}" y="${H - 7}" text-anchor="middle">${esc(d.day.slice(5).replace('-', '/'))}</text>`);
  });
  el.innerHTML = `<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="img" aria-label="近 14 天${mode === 'usd' ? '花费' : '运行次数'}（按执行器堆叠）">${parts.join('')}</svg><div class="tooltip"></div>`;
  const tip = el.querySelector('.tooltip');
  el.onmouseover = (ev) => {
    const hit = ev.target.closest('.hit');
    if (!hit) return;
    $$('.hit.on', el).forEach((n) => n.classList.remove('on'));
    hit.classList.add('on');
    const i = Number(hit.dataset.i); const d = days[i];
    tip.innerHTML = String(h`<div class="tt-h">${d.day}</div>${executors.map((e) => h`<div class="tt-r"><i class="sw" style="background:${seriesVar(e)};width:9px;height:9px;border-radius:2px;display:inline-block"></i>${e}<span class="v">${usd(d.by_executor[e]?.usd || 0)} · ${d.by_executor[e]?.runs || 0} 次</span></div>`)}<div class="tt-r" style="border-top:1px solid var(--line);margin-top:4px;padding-top:4px"><b>合计</b><span class="v">${usd(d.usd)} · ${d.runs} 次 · ${compact(d.tokens)} tok</span></div>`);
    tip.style.display = 'block';
    const x = L + band * i + band / 2;
    const tw = tip.offsetWidth;
    tip.style.left = `${Math.max(0, Math.min(W - tw, x - tw / 2))}px`;
    tip.style.top = `${Math.max(0, y(totals[i]) - tip.offsetHeight - 8)}px`;
  };
  el.onmouseleave = () => { tip.style.display = 'none'; $$('.hit.on', el).forEach((n) => n.classList.remove('on')); };
}

/* ======================================================================== view: inbox */

function taskCard(t, mode) {
  const tone = { review: 'k-human', handoff: 'k-human', suggested: 'k-info', failed: 'k-bad' }[mode] || '';
  let body = '';
  if (mode === 'review' || mode === 'handoff') body = t.summary ? md(t.summary) : t.description ? md(t.description) : h`<span class="muted">（没有摘要）</span>`;
  else if (mode === 'suggested') body = h`${t.description ? md(t.description) : ''}${t.level_reason ? h`<div class="small muted">等级理由：${t.level_reason}</div>` : ''}`;
  else if (mode === 'failed') body = h`${t.last_error ? h`<div class="errbox">${t.last_error.error}</div>` : ''}${t.status_reason ? h`<div class="small muted">${t.status_reason}</div>` : ''}`;
  return h`<article class="item-card ${tone}">
    <div class="head">${lvl(t)}${prio(t.priority)}<span class="title" data-act="open-task" data-id="${t.id}">${t.title}</span>
      ${mode === 'suggested' ? h`<select class="select" data-filter data-change="set-level" data-id="${t.id}" title="调整等级（锁定，不再被 AI 改写）">${options(LEVEL_OPTS(), t.level)}</select>` : ''}</div>
    <div class="metas"><span>${catLabel(t.category)}</span><span>${META.kinds[t.kind] || t.kind}</span>
      ${t.parent_title ? h`<span>子任务 · ${cut(t.parent_title, 40)}</span>` : ''}${progress(t.children_done, t.children_total)}
      <span>来源 ${t.source || '—'}</span><span>${when(t.updated_at)}</span>${t.cost_usd ? h`<span>${usd(t.cost_usd)}</span>` : ''}
      ${t.pending_approvals ? h`<span class="chip st-human"><span class="dot"></span>${t.pending_approvals} 个待审批</span>` : ''}</div>
    ${body ? h`<div class="body">${body}</div>` : ''}
    ${t.artifacts?.length ? artifactList(t.artifacts) : ''}
    ${taskActions(t, mode)}
  </article>`;
}

async function viewInbox() {
  const d = await api('/inbox');
  const groups = [
    ['decisions', '需要你决策', 'AI 已分析并给出推荐，选一个选项即可', d.decisions.map(approvalCard)],
    ['actions', '待批准的动作', '对外动作、写回项目、预算等；批准后由 Action runner 执行', d.actions.map(approvalCard)],
    ['review', '待验收', 'AI 已交付成果：验收通过，或附意见返工', d.review.map((t) => taskCard(t, 'review'))],
    ['handoff', '待你接手', 'AI 已完成它那部分（L1/L2 briefing / 方案），剩下的需要你', d.handoff.map((t) => taskCard(t, 'handoff'))],
    ['suggested', '建议任务', '分拣后没有自动启动；可先调整等级再接受', d.suggested.map((t) => taskCard(t, 'suggested'))],
    ['failed', '失败', '重试已耗尽，需要人工判断', d.failed.map((t) => taskCard(t, 'failed'))],
  ];
  const total = groups.reduce((s, g) => s + g[3].length, 0);
  if (!total) return emptyState('Inbox 已清空。新的事项出现时会实时显示在这里。');
  return h`<div class="bucket-nav">${groups.filter((g) => g[3].length).map(([id, title, , items]) =>
    h`<button class="chip filter" data-act="scroll-to" data-target="sec-${id}">${title} <b>${items.length}</b></button>`)}</div>
    ${groups.filter((g) => g[3].length).map(([id, title, hint, items]) => section(`sec-${id}`, title, hint, items, items.length))}`;
}

/* ======================================================================== view: tasks */

const STATUS_PRESETS = {
  attention: ['需要我', 'triaged,waiting_user,review,failed,waiting_approval'],
  active: ['进行中', 'inbox,ready,running,blocked,waiting_approval'],
  closed: ['已结束', 'done,cancelled,dismissed'],
};
async function viewTasks(p) {
  const qs = new URLSearchParams();
  for (const k of ['status', 'level', 'category', 'kind']) if (p.get(k)) qs.set(k, p.get(k));
  if (p.get('q')) qs.set('search', p.get('q'));
  if (p.get('root') === '1') qs.set('root_only', '1');
  qs.set('limit', p.get('limit') || '300');
  const d = await api(`/tasks?${qs}`);
  const counts = d.counts || {};
  const curStatus = p.get('status') || '';
  const rows = d.tasks.map((t) => h`<tr class="click" data-act="open-task" data-id="${t.id}">
    <td>${prio(t.priority)}</td>
    <td class="title-cell"><div>${t.title}</div><div class="sub">${shortId(t.id)}${t.parent_title ? ` · 子任务 · ${cut(t.parent_title, 40)}` : ''}${t.project ? ` · ${t.project}` : ''}
      ${progress(t.children_done, t.children_total)}${t.pending_approvals ? h` <span class="chip st-human"><span class="dot"></span>${t.pending_approvals} 待审批</span>` : ''}</div></td>
    <td>${lvl(t)}</td><td>${taskStatus(t.status)}</td><td class="nowrap">${catLabel(t.category)}</td>
    <td class="nowrap">${execLabel(t.executor)}</td><td>${when(t.updated_at)}</td><td class="num">${t.cost_usd ? usd(t.cost_usd) : ''}</td></tr>`);
  return h`
    <div class="filters">
      <div class="chips">
        <button class="chip filter${!curStatus ? ' on' : ''}" data-act="set-param" data-key="status" data-value="">全部</button>
        ${Object.values(STATUS_PRESETS).map(([label, v]) => h`<button class="chip filter${curStatus === v ? ' on' : ''}" data-act="set-param" data-key="status" data-value="${v}">${label}</button>`)}
      </div>
      <input class="input grow" id="f-search" data-filter data-param="q" type="search" placeholder="搜索标题 / 描述 / 任务 ID（按 / 聚焦）" value="${p.get('q') || ''}">
      ${selectParam('level', LEVEL_OPTS(), '全部等级')}
      ${selectParam('category', Object.fromEntries(Object.keys(META.categories).map((k) => [k, catLabel(k)])), '全部类别')}
      ${selectParam('kind', META.kinds, '全部类型')}
      <label class="check"><input type="checkbox" data-filter data-change="toggle-root"${p.get('root') === '1' ? raw(' checked') : ''}>只看顶层任务</label>
    </div>
    <div class="chips" style="margin-bottom:12px">${Object.keys(META.statusLabel).map((s) => filterChip('status', s, META.statusLabel[s], counts[s] || 0))}</div>
    ${table(['', '标题', '等级', '状态', '类别', '执行器', '更新', ['花费', 'num']], rows, '没有符合条件的任务')}
    <div class="small muted" style="margin-top:8px">显示 ${d.total} 条${d.total >= Number(p.get('limit') || 300) ? h` · <a href="#" data-act="set-param" data-key="limit" data-value="2000">加载更多</a>` : ''}</div>`;
}

/* ======================================================================== drawer: task */

function taskDrawer(d) {
  const t = d.task;
  const tab = S.drawerTab.task;
  const pendingApv = d.approvals.filter((a) => a.status === 'pending').length;
  const tabs = [['overview', '概要'], ['tree', '子任务', d.children_progress.total], ['runs', '运行', d.runs.length],
    ['approvals', '审批', d.approvals.length, pendingApv], ['artifacts', '交付物', d.artifacts.length], ['history', '历史', d.events.length]];
  return h`<div class="drawer-h">
    <div class="top">${lvl(t)}<h2>${t.title}</h2><button class="btn ghost" data-act="close-drawer" title="关闭 (Esc)"><svg class="ico" width="16" height="16"><use href="#i-close"/></svg></button></div>
    <div class="metas">${taskStatus(t.status)}${prio(t.priority)}<span>${catLabel(t.category)}</span><span>${META.kinds[t.kind] || t.kind}</span>
      <span>执行器 ${execLabel(t.executor)}</span><span>花费 ${usd(t.cost_usd)}</span><span>尝试 ${t.attempts}/${t.max_attempts}</span>
      <span>来源 ${t.source || '—'}</span><span>创建 ${when(t.created_at)}</span><span>更新 ${when(t.updated_at)}</span>
      ${t.due_at ? h`<span>截止 ${absTime(t.due_at)}</span>` : ''}${t.project ? h`<span>项目 ${t.project}</span>` : ''}
      <button class="btn ghost sm" data-act="copy" data-text="${t.id}" title="复制任务 ID"><span class="mono">${shortId(t.id)}</span></button></div>
    ${taskActions(t, 'detail')}
    <div class="tabs">${tabs.map(([k, label, n, hot]) => h`<button class="${tab === k ? 'on' : ''}" data-act="drawer-tab" data-tab="${k}">${label}${n ? h`<span class="badge ${hot ? 'hot' : ''}">${n}</span>` : ''}</button>`)}</div>
  </div>
  <div class="drawer-b">${TASK_TABS[tab](d)}</div>`;
}

const TASK_TABS = {
  overview(d) {
    const t = d.task;
    const e = t.effective_level;
    const L = META.levels[e] || {};
    const crumbs = [d.root && ['根任务', d.root], d.parent && ['父任务', d.parent]].filter(Boolean);
    const result = t.result && typeof t.result === 'object' ? t.result : null;
    return h`
      ${crumbs.length ? h`<div class="small" style="margin-bottom:10px">${crumbs.map(([label, p]) => h`<span class="muted">${label}：</span><a href="#" data-act="open-task" data-id="${p.id}">${p.title}</a>　`)}</div>` : ''}
      ${t.status_reason ? h`<div class="small muted" style="margin-bottom:10px">状态说明：${t.status_reason}</div>` : ''}
      <div class="card">
        <div class="form-grid">
          <label class="field">自主等级<select class="select" data-change="set-level" data-id="${t.id}">${options(LEVEL_OPTS(), t.level)}</select></label>
          <label class="field">优先级<select class="select" data-change="set-priority" data-id="${t.id}">${options(PRIO_OPTS, t.priority)}</select></label>
          <label class="field">类别<select class="select" data-change="set-category" data-id="${t.id}">${options(Object.fromEntries(Object.entries(META.categories)), t.category)}</select></label>
          <label class="field">执行器<select class="select" data-change="set-executor" data-id="${t.id}">${options(Object.fromEntries(META.executors.map((x) => [x, x])), t.executor, { auto: '自动路由' })}</select></label>
        </div>
        <div class="small muted" style="margin-top:8px">${levelName(e)}：${L.deliverable || ''}　交接点：${L.handoff || ''}${t.level_cap && t.level_cap < t.level ? h`　<b>策略上限 L${t.level_cap}</b>（评估 L${t.level}）` : ''}${t.level_reason ? h`<br>评估理由：${t.level_reason}` : ''}</div>
        <div style="margin-top:8px"><button class="btn sm" data-act="edit-task" data-id="${t.id}">编辑标题 / 描述 / 验收标准…</button></div>
      </div>
      ${t.summary ? h`<div class="card"><div class="card-h"><h3>AI 产出 / 摘要</h3></div>${md(t.summary)}</div>` : ''}
      <div class="card"><div class="card-h"><h3>描述</h3></div>${t.description ? md(t.description) : h`<span class="muted">（无）</span>`}</div>
      ${t.acceptance ? h`<div class="card"><div class="card-h"><h3>验收标准</h3></div>${md(t.acceptance)}</div>` : ''}
      ${d.notes.length ? h`<div class="card"><div class="card-h"><h3>备注</h3></div><ul class="timeline">${d.notes.slice().reverse().map((n) => h`<li><span class="when">${absTime(n.ts)}</span><span class="what">${n.text}<div class="actor">${n.by}</div></span></li>`)}</ul></div>` : ''}
      ${result ? h`<div class="card"><div class="card-h"><h3>结构化结果</h3><span class="spacer"></span>${result.status ? chip(result.status, result.status === 'succeeded' || result.status === 'done' ? 'good' : 'info') : ''}</div>
        ${result.verification ? h`<div class="small"><b>验证：</b>${jsonText(result.verification, 1500)}</div>` : ''}
        <details><summary class="small muted">完整 JSON</summary><pre class="raw">${jsonText(result, 20000)}</pre></details></div>` : ''}
      ${t.plan ? h`<div class="card"><details><summary><b>执行计划</b></summary><pre class="raw">${jsonText(t.plan, 20000)}</pre></details></div>` : ''}
      ${d.signal ? h`<div class="card"><div class="card-h"><h3>来源信号</h3><span class="spacer"></span><span class="small muted">${d.signal.source} · ${when(d.signal.observed_at)}</span></div>
        <div><b>${d.signal.title}</b>${d.signal.url ? h` · <a href="${safeUrl(d.signal.url)}" target="_blank" rel="noopener noreferrer">${cut(d.signal.url, 80)}</a>` : ''}</div>
        ${d.signal.body ? h`<pre class="raw" style="margin-top:6px;max-height:220px">${d.signal.body}</pre>` : ''}</div>` : ''}
      ${d.workspace || t.workspace ? h`<div class="small muted">工作区：<span class="mono">${t.workspace}</span></div>` : ''}`;
  },
  tree(d) {
    const deps = (list, label) => (list.length ? h`<div class="small" style="margin:6px 0"><span class="muted">${label}：</span>${list.map((x) => h`<a href="#" class="dep ${x.status !== 'done' ? 'pending' : ''}" data-act="open-task" data-id="${x.id}">${cut(x.title, 40)} · ${META.statusLabel[x.status] || x.status}</a> `)}</div>` : '');
    const node = (n) => h`<li><div class="node">${taskStatus(n.status)}${lvl(n)}<span class="t" data-act="open-task" data-id="${n.id}">${n.title}</span>
        <span class="small muted">${execLabel(n.executor)}${n.cost_usd ? ` · ${usd(n.cost_usd)}` : ''}</span>
        ${n.deps?.length ? h`<div class="deps">依赖 ${n.deps.map((x) => h`<span class="dep ${x.status !== 'done' ? 'pending' : ''}" title="${x.title}">${cut(x.title, 24)} · ${META.statusLabel[x.status] || x.status}</span>`)}</div>` : ''}</div>
      ${n.children?.length ? h`<ul class="tree">${n.children.map(node)}</ul>` : ''}</li>`;
    const pr = d.children_progress;
    return h`${deps(d.deps, '本任务依赖')}${deps(d.dependents, '被这些任务依赖')}
      ${d.children.length ? h`<div class="row" style="margin-bottom:8px">${progress(pr.done, pr.total)}<span class="small muted">子任务按 DAG 并行执行；虚线框为依赖，橙色表示尚未完成</span></div><ul class="tree">${d.children.map(node)}</ul>` : emptyState('没有子任务')}`;
  },
  runs(d) {
    return table(['状态', '用途', '执行器', '开始', '时长', ['花费', 'num'], '错误'], d.runs.map((r) => h`<tr class="click" data-act="open-run" data-id="${r.id}" data-back="${d.task.id}">
      <td>${runStatus(r.status)}</td><td>${purposeLabel(r.purpose)} #${r.attempt}</td><td class="nowrap">${r.executor}${r.model ? h`<div class="sub">${r.model}</div>` : ''}</td>
      <td>${when(r.started_at)}</td><td class="nowrap">${dur(r.duration_s)}</td><td class="num">${usd(r.cost_usd)}</td><td class="small">${cut(r.error || '', 140)}</td></tr>`), '还没有运行记录');
  },
  approvals(d) {
    if (!d.approvals.length) return emptyState('没有审批事项');
    return h`${d.approvals.map((a) => approvalCard({ ...a, task_id: null }))}`;
  },
  artifacts(d) { return d.artifacts.length ? h`<div class="card">${artifactList(d.artifacts)}</div>` : emptyState('没有交付物'); },
  history(d) { return timeline(d.events); },
};

function describeEvent(e) {
  const dt = e.data && typeof e.data === 'object' ? e.data : {};
  const label = EVENT_LABEL[e.type] || e.type;
  if (e.type === 'status_changed') {
    const map = e.entity_type === 'run' ? META.runLabel : e.entity_type === 'approval' ? META.apvLabel : META.statusLabel;
    return h`<span class="ev-type">${label}</span> ${dt.from ? `${map[dt.from] || dt.from} → ` : ''}${map[dt.to] || dt.to || ''}${dt.reason ? h` <span class="muted">· ${cut(dt.reason, 200)}</span>` : ''}${dt.error ? h`<div class="err-text small">${cut(dt.error, 300)}</div>` : ''}`;
  }
  if (e.type === 'decided') return h`<span class="ev-type">${label}</span> ${dt.decision || ''} <span class="muted">· ${dt.via || ''}</span>${dt.note ? h`<div class="small">${cut(dt.note, 300)}</div>` : ''}`;
  if (e.type === 'updated') {
    const parts = Object.entries(dt).filter(([k]) => !['old', 'new'].includes(k)).map(([k, v]) => `${k} = ${cut(typeof v === 'object' ? JSON.stringify(v) : v, 80)}`);
    if (dt.new != null) parts.push(`${cut(dt.old ?? '', 60)} → ${cut(dt.new, 120)}`);
    return h`<span class="ev-type">${label}</span> <span class="muted">${parts.join('，')}</span>`;
  }
  if (e.type === 'created') return h`<span class="ev-type">${label}</span> <span class="muted">${cut(dt.title || dt.value || dt.purpose || '', 160)}${dt.source ? ` · ${dt.source}` : ''}${dt.executor ? ` · ${dt.executor}` : ''}</span>`;
  const rest = e.data == null ? '' : typeof e.data === 'object' ? JSON.stringify(e.data) : String(e.data);
  return h`<span class="ev-type">${label}</span> <span class="muted">${cut(rest, 240)}</span>`;
}
function timeline(events) {
  if (!events.length) return emptyState('没有历史事件');
  return h`<ul class="timeline">${events.map((e) => h`<li><span class="when" title="${absTime(e.ts, true)}">${absTime(e.ts)}</span><span class="what">
    <span class="chip">${ENTITY[e.entity_type] || e.entity_type}</span> ${describeEvent(e)}<div class="actor">${e.actor}</div></span></li>`)}</ul>`;
}

/* ======================================================================== drawer: run */

const RUN_TABS = ['events', 'log', 'result', 'prompt'];
function evBody(e) {
  const dt = e.data;
  if (dt == null) return '';
  if (typeof dt === 'string') return dt;
  if (e.kind === 'tool_call') return jsonText(dt.input ?? dt.arguments ?? dt.args ?? dt.command ?? dt, 4000);
  for (const k of ['text', 'content', 'message', 'output', 'line']) if (typeof dt[k] === 'string') return dt[k];
  return jsonText(dt, 4000);
}
function runEventHtml(e) {
  const body = evBody(e);
  const sensitive = e.kind === 'tool_call' && SENSITIVE_RE.test(`${e.name || ''} ${body}`);
  return h`<div class="ev k-${e.kind}${sensitive ? ' sensitive' : ''}" data-kind="${e.kind}">
    <div class="ev-h"><span class="k">${EV_KIND[e.kind] || e.kind}</span>${e.name ? h`<code>${e.name}</code>` : ''}${sensitive ? h`<span class="chip st-bad"><span class="dot"></span>疑似对外动作</span>` : ''}<span class="spacer"></span><span title="${absTime(e.ts, true)}">${absTime(e.ts)}</span><span>#${e.id}</span></div>
    ${body ? h`<div class="ev-b${e.kind === 'message' ? '' : ' mono'}">${e.kind === 'message' ? md(body) : body}</div>` : ''}</div>`;
}
function runDrawer(d) {
  const r = d.run;
  const tab = S.drawerTab.run;
  const live = ['running', 'queued'].includes(r.status);
  const backId = S.drawer?.back;
  return h`<div class="drawer-h">
    <div class="top">${runStatus(r.status)}<h2>${purposeLabel(r.purpose)} · ${r.executor}${r.model ? ` · ${r.model}` : ''}</h2>
      ${d.cancellable ? h`<button class="btn danger" data-act="cancel-run" data-id="${r.id}">取消运行</button>` : ''}
      <button class="btn ghost" data-act="close-drawer" title="关闭 (Esc)"><svg class="ico" width="16" height="16"><use href="#i-close"/></svg></button></div>
    <div class="metas">
      ${backId ? h`<a href="#" data-act="open-task" data-id="${backId}">← 返回任务</a>` : ''}
      ${d.task ? h`<span>任务 <a href="#" data-act="open-task" data-id="${d.task.id}">${cut(d.task.title, 60)}</a></span>` : ''}
      <span>第 ${r.attempt} 次</span><span>开始 ${when(r.started_at)}</span><span>时长 ${dur(r.duration_s)}</span>
      <span>花费 ${usd(r.cost_usd)}</span><span>tokens ${compact(r.tokens_in)} / ${compact(r.tokens_out)}</span>
      ${r.pid ? h`<span>pid ${r.pid}</span>` : ''}${r.exit_code != null ? h`<span>exit ${r.exit_code}</span>` : ''}
      ${r.error_class ? h`<span class="chip st-bad"><span class="dot"></span>${r.error_class}</span>` : ''}
      <button class="btn ghost sm" data-act="copy" data-text="${r.id}" title="复制运行 ID"><span class="mono">${shortId(r.id)}</span></button></div>
    ${r.error ? h`<div class="errbox" style="margin-bottom:10px">${r.error}</div>` : ''}
    <div class="tabs">${RUN_TABS.map((k) => h`<button class="${tab === k ? 'on' : ''}" data-act="drawer-tab" data-tab="${k}">${{ events: '事件流', log: '原始日志', result: '结果', prompt: 'Prompt' }[k]}${k === 'events' ? h`<span class="badge" id="run-ev-count">${d.events.length}</span>` : ''}</button>`)}
      ${live ? h`<span class="live-dot" style="margin-left:auto"><span class="dot-s ok"></span>实时</span>` : ''}</div>
  </div>
  <div class="drawer-b" id="run-body">${RUN_TAB_RENDER[tab](d)}</div>`;
}
const RUN_TAB_RENDER = {
  events(d) {
    const kinds = Object.keys(d.event_counts || {});
    const f = S.evFilter;
    const evs = f ? d.events.filter((e) => e.kind === f) : d.events;
    return h`<div class="filters">
        <button class="chip filter${!f ? ' on' : ''}" data-act="ev-filter" data-kind="">全部 ${d.events.length}</button>
        ${kinds.map((k) => h`<button class="chip filter${f === k ? ' on' : ''}" data-act="ev-filter" data-kind="${k}">${EV_KIND[k] || k} ${d.event_counts[k]}</button>`)}
        <span class="spacer"></span>
        <label class="check"><input type="checkbox" data-filter data-change="run-follow"${S.runFollow ? raw(' checked') : ''}>自动滚动到底部</label></div>
      <div class="events" id="run-events">${evs.length ? evs.map(runEventHtml) : emptyState('还没有事件')}</div>`;
  },
  log() { return h`<div data-lazy="run-log"><div class="loading">读取日志…</div></div>`; },
  result(d) {
    const r = d.run;
    return h`${r.final_text ? h`<div class="card"><div class="card-h"><h3>最终消息</h3></div>${md(r.final_text)}</div>` : ''}
      ${r.structured ? h`<div class="card"><div class="card-h"><h3>结构化输出</h3></div><pre class="raw">${jsonText(r.structured, 40000)}</pre></div>` : ''}
      ${d.artifacts?.length ? h`<div class="card"><div class="card-h"><h3>交付物</h3></div>${artifactList(d.artifacts)}</div>` : ''}
      ${r.command ? h`<div class="card"><details><summary class="small muted">命令行</summary><pre class="raw">${jsonText(r.command)}</pre></details><div class="small muted">cwd：<span class="mono">${r.cwd || '—'}</span></div></div>` : ''}
      ${!r.final_text && !r.structured ? emptyState('暂无结果') : ''}`;
  },
  prompt() { return h`<div data-lazy="run-prompt"><div class="loading">读取 prompt…</div></div>`; },
};
const LAZY = {
  async 'run-log'(n) {
    const d = S.drawerData;
    if (!d?.run) return;
    try {
      const lg = d.log || (d.log = await api(`/runs/${enc(d.run.id)}/log?tail=300`));
      if (!lg.available) { n.innerHTML = String(emptyState('没有日志目录（运行尚未开始或已被清理）')); return; }
      const block = (f, title) => h`<div class="card"><div class="card-h"><h3>${title}</h3><span class="spacer"></span><span class="small muted">${f?.exists ? `${bytes(f.bytes)}${f.truncated ? ' · 仅显示末尾' : ''}` : '不存在'}</span></div>${f?.lines?.length ? h`<pre class="raw">${f.lines.join('\n')}</pre>` : h`<span class="muted small">（空）</span>`}</div>`;
      n.innerHTML = String(h`<div class="row" style="margin-bottom:8px"><span class="small muted mono">${lg.log_dir}</span><span class="spacer"></span><button class="btn sm" data-act="reload-log">刷新日志</button></div>
        ${block(lg.stdout, 'stdout.jsonl')}${block(lg.stderr, 'stderr.log')}
        ${lg.last_message ? h`<div class="card"><div class="card-h"><h3>last_message.txt</h3></div><pre class="raw">${lg.last_message}</pre></div>` : ''}
        <div class="small muted">文件：${lg.files.map((f) => `${f.name} (${bytes(f.bytes)})`).join('，') || '—'}</div>`);
    } catch (e) { n.innerHTML = String(h`<div class="empty err-text">读取失败：${e.message}</div>`); }
  },
  async 'run-prompt'(n) {
    const d = S.drawerData;
    if (!d?.run) return;
    try {
      const lg = d.log || (d.log = await api(`/runs/${enc(d.run.id)}/log?tail=300`));
      n.innerHTML = String(lg.prompt ? h`<pre class="raw" style="max-height:none">${lg.prompt}</pre>` : emptyState('没有 prompt 文件'));
    } catch (e) { n.innerHTML = String(h`<div class="empty err-text">读取失败：${e.message}</div>`); }
  },
};

function stopRunPoll() { if (S.runPoll) { clearInterval(S.runPoll); S.runPoll = null; } }
function startRunPoll() {
  stopRunPoll();
  const d = S.drawerData;
  if (!d?.run || !['running', 'queued'].includes(d.run.status)) return;
  S.runPoll = setInterval(pollRun, 2000);
}
async function pollRun() {
  const d = S.drawerData;
  if (!d?.run || S.drawer?.type !== 'run') { stopRunPoll(); return; }
  let ev;
  try { ev = await api(`/runs/${enc(d.run.id)}/events?after=${d.lastId}&limit=1000`); } catch { return; }
  if (S.drawerData !== d) return;
  if (ev.events.length) {
    d.events.push(...ev.events);
    d.lastId = ev.last_id;
    for (const e of ev.events) d.event_counts[e.kind] = (d.event_counts[e.kind] || 0) + 1;
    const cnt = $('#run-ev-count');
    if (cnt) cnt.textContent = String(d.events.length);
    const box = $('#run-events');
    if (box && S.drawerTab.run === 'events') {
      const fresh = S.evFilter ? ev.events.filter((e) => e.kind === S.evFilter) : ev.events;
      if (fresh.length) {
        box.querySelector('.empty')?.remove();
        box.insertAdjacentHTML('beforeend', fresh.map((e) => String(runEventHtml(e))).join(''));
        if (S.runFollow) { const b = $('#run-body'); if (b) b.scrollTop = b.scrollHeight; }
      }
    }
  }
  if (ev.run_status !== d.run.status) { stopRunPoll(); renderDrawer(false); }
}

async function renderDrawer(initial = false) {
  const dr = $('#drawer'); const bd = $('#backdrop');
  stopRunPoll();
  if (!S.drawer) {
    dr.classList.remove('show'); bd.classList.remove('show'); dr.setAttribute('aria-hidden', 'true');
    S.drawerData = null;
    return;
  }
  dr.classList.add('show'); bd.classList.add('show'); dr.removeAttribute('aria-hidden');
  const { type, id } = S.drawer;
  if (initial) { dr.innerHTML = '<div class="drawer-b"><div class="loading">加载中…</div></div>'; S.evFilter = ''; }
  const seq = ++S.drawerSeq;
  const prevScroll = $('.drawer-b', dr)?.scrollTop || 0;
  try {
    if (type === 'task') {
      const d = await api(`/tasks/${enc(id)}`);
      if (seq !== S.drawerSeq) return;
      S.drawerData = d;
    } else {
      const [d, ev] = await Promise.all([api(`/runs/${enc(id)}`), api(`/runs/${enc(id)}/events?after=0&limit=5000`)]);
      if (seq !== S.drawerSeq) return;
      S.drawerData = { ...d, events: ev.events, lastId: ev.last_id };
    }
    paintDrawer();
    if (!initial) { const b = $('.drawer-b', dr); if (b) b.scrollTop = prevScroll; }
    if (type === 'run') {
      startRunPoll();
      if (initial && S.runFollow && S.drawerTab.run === 'events') { const b = $('#run-body'); if (b) b.scrollTop = b.scrollHeight; }
    }
  } catch (e) {
    if (seq !== S.drawerSeq) return;
    dr.innerHTML = String(h`<div class="drawer-h"><div class="top"><h2>无法加载</h2><button class="btn ghost" data-act="close-drawer"><svg class="ico" width="16" height="16"><use href="#i-close"/></svg></button></div></div><div class="drawer-b"><div class="empty err-text">${e.message}</div></div>`);
  }
}
function paintDrawer() {
  const dr = $('#drawer');
  if (!S.drawer || !S.drawerData) return;
  const dirty = saveDirty(dr);
  dr.innerHTML = String(S.drawer.type === 'task' ? taskDrawer(S.drawerData) : runDrawer(S.drawerData));
  restoreDirty(dr, dirty);
  afterRender(dr);
}

/* ======================================================================== view: outbox */

async function viewOutbox(p) {
  const tab = p.get('tab') || 'pending';
  const tabs = h`<div class="tabs" style="margin-bottom:12px">
    <button class="${tab === 'pending' ? 'on' : ''}" data-act="set-param" data-key="tab" data-value="">待决策</button>
    <button class="${tab === 'history' ? 'on' : ''}" data-act="set-param" data-key="tab" data-value="history">已处理</button></div>`;
  if (tab === 'pending') {
    const d = await api('/approvals?status=pending&limit=500');
    return h`${tabs}${d.approvals.length ? d.approvals.map(approvalCard) : emptyState('没有待决策的事项。AI 提议的对外动作和需要你拍板的决策会出现在这里。')}`;
  }
  const st = p.get('status') || 'approved,rejected,expired,cancelled,executing,executed,failed';
  const d = await api(`/approvals?status=${enc(st)}&limit=500`);
  const rows = d.approvals.map((a) => h`<tr>
    <td>${when(a.decided_at || a.created_at)}</td><td class="title-cell"><div>${a.title}</div>${a.task_id ? h`<div class="sub"><a href="#" data-act="open-task" data-id="${a.task_id}">${cut(a.task_title || a.task_id, 60)}</a></div>` : ''}</td>
    <td>${META.apvKind[a.kind] || a.kind}${a.action_type ? h`<div class="sub mono">${a.action_type}</div>` : ''}</td><td>${risk(a.risk)}</td><td>${apvStatus(a.status)}</td>
    <td>${a.decision || '—'}</td><td class="small">${cut(a.decision_note || '', 160)}</td><td>${a.decided_via || '—'}</td>
    <td class="small">${a.result ? h`<details><summary>结果</summary><pre class="raw">${jsonText(a.result, 4000)}</pre></details>` : ''}</td></tr>`);
  return h`${tabs}<div class="chips" style="margin-bottom:12px">${['approved', 'rejected', 'executed', 'failed', 'expired', 'cancelled', 'executing'].map((s) => filterChip('status', s, META.apvLabel[s]))}</div>
    ${table(['时间', '事项', '类型', '风险', '状态', '决策', '备注', '渠道', ''], rows, '暂无历史')}`;
}

/* ======================================================================== view: runs */

async function viewRuns(p) {
  const qs = new URLSearchParams({ limit: '300' });
  for (const k of ['status', 'purpose', 'executor', 'task_id']) if (p.get(k)) qs.set(k, p.get(k));
  const d = await api(`/runs?${qs}`);
  const rows = d.runs.map((r) => h`<tr class="click" data-act="open-run" data-id="${r.id}">
    <td>${runStatus(r.status)}</td><td>${purposeLabel(r.purpose)}${r.attempt > 1 ? ` #${r.attempt}` : ''}</td>
    <td class="nowrap">${r.executor}${r.model ? h`<div class="sub">${r.model}</div>` : ''}</td>
    <td class="title-cell">${r.task_title ? h`<div class="ellipsis">${r.task_title}</div>` : h`<span class="muted">—</span>`}<div class="sub">${shortId(r.id)}</div></td>
    <td>${when(r.started_at)}</td><td class="nowrap num">${dur(r.duration_s)}</td><td class="num">${usd(r.cost_usd)}</td>
    <td class="num">${compact((r.tokens_in || 0) + (r.tokens_out || 0))}</td><td class="small err-text">${cut(r.error || '', 120)}</td></tr>`);
  return h`<div class="filters">
      <div class="chips">${Object.keys(META.runLabel).map((s) => filterChip('status', s, META.runLabel[s]))}</div>
      ${selectParam('purpose', PURPOSES, '全部用途')}${selectParam('executor', Object.fromEntries(META.executors.map((x) => [x, x])), '全部执行器')}
      ${p.get('task_id') ? h`<button class="chip filter on" data-act="set-param" data-key="task_id" data-value="">任务 ${shortId(p.get('task_id'))} ✕</button>` : ''}</div>
    ${table(['状态', '用途', '执行器', '任务', '开始', ['时长', 'num'], ['花费', 'num'], ['Tokens', 'num'], '错误'], rows, '没有运行记录')}`;
}

/* ======================================================================== view: tool calls */

async function viewTools(p) {
  const d = await api(`/tool-calls?limit=${enc(p.get('limit') || '500')}`);
  const term = (p.get('q') || '').toLowerCase();
  const onlySensitive = p.get('sensitive') === '1';
  const items = d.tool_calls.map((c) => {
    const args = evBody({ kind: 'tool_call', data: c.data });
    return { c, args, sensitive: SENSITIVE_RE.test(`${c.name || ''} ${args}`) };
  }).filter(({ c, args, sensitive }) => (!term || `${c.name} ${args}`.toLowerCase().includes(term)) && (!onlySensitive || sensitive));
  const nSens = items.filter((x) => x.sensitive).length;
  const rows = items.map(({ c, args, sensitive }) => h`<tr class="click${sensitive ? ' hl-warn' : ''}" data-act="open-run" data-id="${c.run_id}">
    <td>${when(c.ts)}</td><td class="nowrap"><code>${c.name || '—'}</code>${sensitive ? h`<div><span class="chip st-bad"><span class="dot"></span>疑似对外动作</span></div>` : ''}</td>
    <td class="title-cell"><div class="mono small" title="${cut(args, 2000)}">${cut(args.replace(/\s+/g, ' '), 220)}</div></td>
    <td>${c.executor || '—'}</td><td class="small">${c.task_title ? cut(c.task_title, 50) : '—'}</td><td class="mono small">${shortId(c.run_id)}</td></tr>`);
  return h`<div class="filters">
      <input class="input grow" id="f-tool" data-filter data-param="q" type="search" placeholder="按工具名或参数过滤" value="${p.get('q') || ''}">
      <label class="check"><input type="checkbox" data-filter data-change="toggle-sensitive"${onlySensitive ? raw(' checked') : ''}>只看疑似对外动作</label>
      <span class="small muted">最近 ${d.tool_calls.length} 次调用 · ${nSens} 次疑似对外动作（Agent 运行时不应直接执行对外动作）</span></div>
    ${table(['时间', '工具', '参数', '执行器', '任务', '运行'], rows, '没有工具调用记录')}`;
}

/* ======================================================================== view: errors */

async function viewErrors() {
  const d = await api('/errors');
  const total = d.runs.length + d.tasks.length + d.sensors.length + d.approvals.length + d.notifications.length;
  if (!total) return emptyState('一切正常：没有失败的运行、任务或传感器。');
  const runRows = d.runs.map((r) => h`<tr class="click" data-act="open-run" data-id="${r.id}">
    <td>${when(r.ended_at || r.started_at)}</td><td>${runStatus(r.status)}</td><td>${purposeLabel(r.purpose)} · ${r.executor}</td>
    <td class="small">${r.task_title ? cut(r.task_title, 50) : '—'}</td><td>${r.error_class || '—'}</td><td class="small err-text">${cut(r.error || '', 300)}</td></tr>`);
  const sensorRows = d.sensors.map((s) => h`<tr><td><b>${s.name}</b></td><td>${when(s.last_run_at)}</td><td class="num">${s.consecutive_failures}</td>
    <td class="small err-text">${cut(s.last_error, 400)}</td><td>${when(s.next_run_at)}</td><td><button class="btn sm" data-act="run-sensor" data-name="${s.name}">立即运行</button></td></tr>`);
  return h`
    ${d.tasks.length ? section('err-tasks', '失败的任务', '重试已耗尽', d.tasks.map((t) => taskCard(t, 'failed')), d.tasks.length) : ''}
    ${d.runs.length ? section('err-runs', '失败 / 超时 / 中断的运行', '', table(['时间', '状态', '运行', '任务', '错误类别', '错误'], runRows), d.runs.length) : ''}
    ${d.sensors.length ? section('err-sensors', '传感器异常', '失败后按指数退避重试', table(['传感器', '上次运行', ['连续失败', 'num'], '错误', '下次运行', ''], sensorRows), d.sensors.length) : ''}
    ${d.approvals.length ? section('err-apv', '执行失败的审批', '已批准但 Action runner 执行失败', d.approvals.map(approvalCard), d.approvals.length) : ''}
    ${d.notifications.length ? section('err-ntf', '通知发送失败', '', table(['时间', '渠道', '标题', '错误'], d.notifications.map((n) => h`<tr><td>${when(n.created_at)}</td><td>${n.channel}</td><td>${n.title}</td><td class="small err-text">${cut(n.error || '', 300)}</td></tr>`)), d.notifications.length) : ''}`;
}

/* ======================================================================== view: artifacts */

async function viewArtifacts(p) {
  const d = await api('/artifacts?limit=1000');
  const term = (p.get('q') || '').toLowerCase();
  const kind = p.get('kind') || '';
  const kinds = [...new Set(d.artifacts.map((a) => a.kind))].sort();
  const list = d.artifacts.filter((a) => (!kind || a.kind === kind) && (!term || `${a.title} ${a.path || ''} ${a.task_title || ''}`.toLowerCase().includes(term)));
  const rows = list.map((a) => h`<tr>
    <td class="title-cell"><div>${a.title}</div><div class="sub mono" title="${a.path || a.uri || ''}">${cut(a.path || a.uri || '', 90)}</div><div data-preview-slot="${a.id}"></div></td>
    <td>${a.kind}</td><td class="small">${a.task_id ? h`<a href="#" data-act="open-task" data-id="${a.task_id}">${cut(a.task_title || a.task_id, 50)}</a>` : '—'}</td>
    <td class="num">${bytes(a.bytes)}</td><td>${when(a.created_at)}</td><td><div class="btn-group">${artifactButtons(a)}</div></td></tr>`);
  return h`<div class="filters"><input class="input grow" id="f-art" data-filter data-param="q" type="search" placeholder="按标题 / 路径 / 任务过滤" value="${p.get('q') || ''}">
      ${selectParam('kind', Object.fromEntries(kinds.map((k) => [k, k])), '全部类型')}<span class="small muted">${list.length} 个</span></div>
    ${table(['交付物', '类型', '任务', ['大小', 'num'], '时间', ''], rows, '还没有交付物')}`;
}

/* ======================================================================== view: context */

async function viewContext(p) {
  const showAll = p.get('all') === '1';
  const d = await api(`/context?status=${showAll ? 'all' : 'active,proposed'}`);
  const kinds = { ...META.factKinds, ...d.kinds };
  const order = [...Object.keys(kinds), ...Object.keys(d.groups).filter((k) => !kinds[k])];
  const groups = order.filter((k) => d.groups[k]?.length);
  const factRow = (f) => h`<div class="fact ${f.status === 'proposed' ? 'proposed' : ''}">
    <div><div class="k">${f.key}</div><div class="m">${f.pinned ? '已确认 · ' : ''}${f.status !== 'active' ? `${f.status} · ` : ''}置信度 ${Math.round((f.confidence || 0) * 100)}%</div></div>
    <div><div class="v">${f.value}</div><div class="m">来源 ${f.source || '—'} · 更新 ${rel(f.updated_at)}</div></div>
    <div class="btn-group">${f.status === 'proposed' ? h`<button class="btn sm primary" data-act="fact-confirm" data-id="${f.id}">确认</button>` : ''}
      ${f.status !== 'rejected' ? h`<button class="btn sm" data-act="fact-edit" data-id="${f.id}">编辑</button><button class="btn sm danger" data-act="fact-delete" data-id="${f.id}">删除</button>` : h`<button class="btn sm" data-act="fact-confirm" data-id="${f.id}">恢复</button>`}</div></div>`;
  S.facts = Object.values(d.groups).flat();
  return h`
    <form class="card" data-form="add-fact">
      <div class="card-h"><h2>添加上下文</h2><span class="hint small muted">你添加或编辑的条目会被锁定（pinned），Agent 的自动刷新不会覆盖</span></div>
      <div class="row wrap" style="align-items:flex-start">
        <select class="select" name="kind">${options(kinds, 'note')}</select>
        <input class="input" name="key" placeholder="键（如：博士课题 / 导师 / 投稿截止）" required style="flex:1 1 200px" maxlength="200">
        <textarea class="textarea" name="value" placeholder="内容" required rows="1" style="flex:2 1 320px;min-height:34px" maxlength="8000"></textarea>
        <button class="btn primary" type="submit">添加</button></div>
    </form>
    <div class="grid grid-2 section">
      <div>${groups.length ? groups.map((k) => h`<div class="card"><div class="card-h"><h3>${kinds[k] || k}</h3><span class="badge">${d.groups[k].length}</span><span class="muted small">${k}</span></div>${d.groups[k].map(factRow)}</div>`) : emptyState('还没有上下文条目')}
        <label class="check" style="margin-top:10px"><input type="checkbox" data-filter data-change="toggle-all-facts"${showAll ? raw(' checked') : ''}>显示已删除 / 过期的条目</label></div>
      <div class="card"><div class="card-h"><h3>profile.md</h3><span class="spacer"></span><span class="small muted">${d.profile.exists ? `更新 ${rel(d.profile.updated_at)}` : '尚未生成'}</span></div>
        <div class="small muted mono" style="margin-bottom:8px">${d.profile.path}</div>
        ${d.profile.exists ? md(d.profile.content) : emptyState('上下文刷新任务运行后会生成 profile.md')}</div>
    </div>`;
}

/* ======================================================================== view: sensors */

async function viewSensors(p) {
  const src = p.get('source') || '';
  const [d, sg] = await Promise.all([api('/sensors'), api(`/signals?limit=150${src ? `&source=${enc(src)}` : ''}`)]);
  const rows = d.sensors.map((s) => h`<tr>
    <td><b>${s.name}</b>${s.registered ? '' : h`<div class="sub">未注册（模块缺失）</div>`}</td>
    <td>${s.enabled ? chip('启用', 'good') : chip(s.config_enabled ? '已停用' : '未启用', 'off')}</td>
    <td class="num">${s.interval_minutes != null ? `${s.interval_minutes} 分钟` : '—'}${s.active_hours ? h`<div class="sub">${s.active_hours.join('–')}</div>` : ''}</td>
    <td>${when(s.last_run_at)}</td><td>${when(s.last_ok_at)}</td><td>${when(s.next_run_at)}</td>
    <td class="num">${s.last_count}</td><td class="num"><a href="#" data-act="set-param" data-key="source" data-value="${s.name}">${s.signals_total}</a></td>
    <td class="num">${s.consecutive_failures ? h`<b class="err-text">${s.consecutive_failures}</b>` : 0}</td>
    <td class="small err-text">${cut(s.last_error || '', 200)}</td>
    <td>${s.registered ? h`<button class="btn sm" data-act="run-sensor" data-name="${s.name}">立即运行</button>` : ''}</td></tr>`);
  const sigRows = sg.signals.map((x) => h`<tr><td>${when(x.observed_at)}</td><td>${x.source}</td>
    <td class="title-cell"><div>${x.url ? h`<a href="${safeUrl(x.url)}" target="_blank" rel="noopener noreferrer">${x.title}</a>` : x.title}</div>${x.body ? h`<div class="sub ellipsis" title="${cut(x.body, 1500)}">${cut(x.body, 160)}</div>` : ''}</td>
    <td>${chip(x.status, { new: 'info', triaged: 'good', ignored: 'off', error: 'bad' }[x.status] || 'off')}</td>
    <td class="small">${x.task_id ? h`<a href="#" data-act="open-task" data-id="${x.task_id}">${cut(x.task_title || x.task_id, 40)}</a>` : '—'}</td>
    <td class="small muted">${cut(x.triage_note || '', 160)}</td></tr>`);
  return h`${table(['传感器', '状态', ['间隔', 'num'], '上次运行', '上次成功', '下次运行', ['上次新增', 'num'], ['累计信号', 'num'], ['连续失败', 'num'], '最近错误', ''], rows, '没有配置传感器')}
    ${section('signals', '最近信号', src ? h`来源：${src} · <a href="#" data-act="set-param" data-key="source" data-value="">全部来源</a>` : '传感器产生的原始观测，经分拣后成为任务',
      table(['时间', '来源', '标题', '状态', '任务', '分拣说明'], sigRows, '还没有信号'), sg.signals.length)}`;
}

/* ======================================================================== view: history */

async function viewHistory(p) {
  const tab = p.get('tab') || 'events';
  const tabs = h`<div class="tabs" style="margin-bottom:12px">
    <button class="${tab === 'events' ? 'on' : ''}" data-act="set-param" data-key="tab" data-value="">审计事件</button>
    <button class="${tab === 'notifications' ? 'on' : ''}" data-act="set-param" data-key="tab" data-value="notifications">通知记录</button></div>`;
  if (tab === 'notifications') {
    const d = await api('/notifications?limit=300');
    return h`${tabs}${table(['时间', '渠道', '标题', '内容', '状态', '关联', '错误'], d.notifications.map((n) => h`<tr><td>${when(n.created_at)}</td><td>${n.channel}</td><td>${n.title}</td>
      <td class="small">${cut(n.body || '', 200)}</td><td>${chip(n.status, { sent: 'good', failed: 'bad', pending: 'info', suppressed: 'off' }[n.status] || 'off')}</td>
      <td class="small">${n.ref_type === 'task' ? h`<a href="#" data-act="open-task" data-id="${n.ref_id}">任务</a>` : n.ref_type ? `${n.ref_type} ${shortId(n.ref_id)}` : '—'}</td><td class="small err-text">${cut(n.error || '', 200)}</td></tr>`), '没有通知记录')}`;
  }
  const qs = new URLSearchParams({ limit: '100' });
  for (const k of ['entity_type', 'entity_id', 'type', 'actor']) if (p.get(k)) qs.set(k, p.get(k));
  const d = await api(`/events?${qs}`);
  S.history = { qs: qs.toString(), rows: d.events, next: d.next_before, pages: 1 };
  return h`${tabs}<div class="filters">
      ${selectParam('entity_type', ENTITY, '全部对象')}
      <input class="input" id="f-ev-type" data-filter data-param="type" placeholder="事件类型（如 status_changed）" value="${p.get('type') || ''}">
      <input class="input" id="f-ev-actor" data-filter data-param="actor" placeholder="操作者前缀（user / system / sensor:）" value="${p.get('actor') || ''}">
      <input class="input grow" id="f-ev-id" data-filter data-param="entity_id" placeholder="对象 ID" value="${p.get('entity_id') || ''}"></div>
    <div id="history-table">${historyTable(S.history.rows)}</div>
    <div class="row" style="margin-top:10px" id="history-more">${historyMore()}</div>`;
}
function entityLink(e) {
  const title = e.entity_title || shortId(e.entity_id) || '—';
  if (e.entity_type === 'task' && e.entity_id) return h`<a href="#" data-act="open-task" data-id="${e.entity_id}">${cut(title, 60)}</a>`;
  if (e.entity_type === 'run' && e.entity_id) return h`<a href="#" data-act="open-run" data-id="${e.entity_id}">${cut(title, 60)}</a>`;
  return h`<span>${cut(title, 60)}</span>`;
}
function historyRow(e) {
  return h`<tr><td class="nowrap num" title="${absTime(e.ts, true)}">${absTime(e.ts)}</td><td class="small">${e.actor}</td>
    <td><span class="chip">${ENTITY[e.entity_type] || e.entity_type}</span> ${entityLink(e)}</td><td>${describeEvent(e)}</td><td class="mono small muted">#${e.id}</td></tr>`;
}
function historyTable(rows) { return table(['时间', '操作者', '对象', '事件', ''], rows.map(historyRow), '没有符合条件的事件'); }
function historyMore() {
  const hs = S.history;
  if (!hs) return '';
  return h`<span class="small muted">已显示 ${hs.rows.length} 条</span>${hs.next ? h`<button class="btn" data-act="history-more">加载更早的事件</button>` : h`<span class="small muted">· 已到最早</span>`}`;
}

/* ======================================================================== modals / toasts */

function toast(msg, kind = 'ok') {
  const el = document.createElement('div');
  el.className = `toast ${kind === 'err' ? 'err' : ''}`;
  el.textContent = msg;
  $('#toasts').appendChild(el);
  setTimeout(() => el.remove(), kind === 'err' ? 7000 : 3200);
}
function modal({ title, body, submitLabel = '确定', wide = false, danger = false, onSubmit = null, hideSubmit = false }) {
  return new Promise((resolve) => {
    const wrap = $('#modal-wrap');
    wrap.innerHTML = String(h`<form class="modal${wide ? ' wide' : ''}" novalidate>
      <div class="modal-h"><h3>${title}</h3><button type="button" class="btn ghost" data-close><svg class="ico" width="16" height="16"><use href="#i-close"/></svg></button></div>
      <div class="modal-b">${body}<div class="err-text small" data-modal-err></div></div>
      <div class="modal-f"><button type="button" class="btn" data-close>${hideSubmit ? '关闭' : '取消'}</button>${hideSubmit ? '' : h`<button type="submit" class="btn ${danger ? 'danger' : 'primary'}">${submitLabel}</button>`}</div></form>`);
    wrap.classList.add('show');
    const form = wrap.querySelector('form');
    let done = false;
    const close = (val) => {
      if (done) return;
      done = true;
      wrap.classList.remove('show'); wrap.innerHTML = '';
      document.removeEventListener('keydown', onKey, true);
      resolve(val);
      if (S.pending) scheduleRefresh(300);
    };
    const onKey = (e) => { if (e.key === 'Escape') { e.stopPropagation(); close(null); } };
    document.addEventListener('keydown', onKey, true);
    wrap.onclick = (e) => { if (e.target === wrap || e.target.closest('[data-close]')) close(null); };
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const missing = $$('[required]', form).find((n) => !String(n.value || '').trim());
      if (missing) { missing.focus(); form.querySelector('[data-modal-err]').textContent = '请填写必填项'; return; }
      const data = Object.fromEntries(new FormData(form).entries());
      $$('input[type=checkbox]', form).forEach((c) => { if (c.name) data[c.name] = c.checked; });
      if (!onSubmit) { close(data); return; }
      const btn = form.querySelector('[type=submit]');
      btn.disabled = true;
      try { await onSubmit(data); close(data); } catch (err) { form.querySelector('[data-modal-err]').textContent = err.message; btn.disabled = false; }
    });
    setTimeout(() => form.querySelector('input:not([type=hidden]):not([type=checkbox]), textarea, select')?.focus(), 30);
  });
}
const confirmModal = (title, text, { danger = false, submitLabel = '确定' } = {}) =>
  modal({ title, body: h`<div>${text}</div>`, danger, submitLabel });

function newTaskModal() {
  const execOpts = Object.fromEntries(META.executors.filter((x) => x !== 'internal').map((x) => [x, x]));
  return modal({
    title: '新建任务', submitLabel: '创建',
    body: h`<div class="form-grid">
      <label class="field full">标题 *<input class="input" name="title" required maxlength="300" placeholder="一句话说明要做什么"></label>
      <label class="field full">描述<textarea class="textarea" name="description" rows="5" placeholder="背景、材料位置、期望产出……（支持 Markdown）"></textarea></label>
      <label class="field">自主等级<select class="select" name="level">${options(LEVEL_OPTS(), '', { auto: '由 AI 评估' })}</select></label>
      <label class="field">类别<select class="select" name="category">${options(META.categories, '', { auto: '由 AI 判断' })}</select></label>
      <label class="field">优先级<select class="select" name="priority">${options(PRIO_OPTS, 2)}</select></label>
      <label class="field">类型<select class="select" name="kind">${options(META.kinds, 'todo')}</select></label>
      <label class="field">执行器<select class="select" name="executor">${options(execOpts, '', { auto: '自动路由' })}</select></label>
      <label class="field">截止日期<input class="input" type="datetime-local" name="due_at"></label>
      <label class="field full">项目<input class="input" name="project" placeholder="可选：关联的项目 / 仓库路径"></label>
      <label class="check full"><input type="checkbox" name="triage" checked>先经过 AI 分拣（评估类别、等级与优先级）；不勾选则按上面的设置直接排队执行</label></div>`,
    onSubmit: async (f) => {
      const body = { title: f.title.trim(), description: f.description || null, priority: Number(f.priority), kind: f.kind, triage: !!f.triage };
      if (f.level) body.level = Number(f.level);
      if (f.category) body.category = f.category;
      if (f.executor) body.executor = f.executor;
      if (f.project) body.project = f.project;
      if (f.due_at) body.due_at = new Date(f.due_at).toISOString().replace(/\.\d{3}Z$/, 'Z');
      const r = await post('/tasks', body);
      toast('任务已创建');
      openTask(r.id);
    },
  });
}

/* ======================================================================== actions */

const ACTION_DONE = { accept: '已接受，进入队列', retry: '已重新排队', rework: '已提交返工意见', complete: '已标记完成', cancel: '已取消', dismiss: '已驳回', snooze: '已推迟' };
async function taskAction(id, action, ask) {
  const body = {};
  if (action === 'rework') {
    const r = await modal({ title: '返工意见', submitLabel: '提交返工', body: h`<label class="field">告诉 AI 哪里需要修改（会写入任务备注并重新执行）<textarea class="textarea" name="feedback" rows="6" required></textarea></label>` });
    if (!r) return;
    body.feedback = r.feedback;
  } else if (action === 'snooze') {
    const r = await modal({ title: '推迟任务', submitLabel: '推迟', body: h`<label class="field">推迟多久<select class="select" name="hours">${options({ 4: '4 小时', 24: '明天（24 小时）', 72: '3 天', 168: '1 周', 720: '30 天' }, 24)}</select></label>` });
    if (!r) return;
    body.hours = Number(r.hours);
  } else if (action === 'cancel') {
    const r = await modal({ title: '取消任务', submitLabel: '确认取消', danger: true, body: h`<div class="small">将同时取消运行中的 Agent、全部未完成的子任务和待审批事项。</div><label class="field">原因（可选）<textarea class="textarea" name="note" rows="2"></textarea></label>` });
    if (!r) return;
    body.note = r.note || null;
  } else if (ask) {
    const r = await modal({ title: action === 'retry' ? '交给 AI 继续 / 重试' : '附加说明', submitLabel: '提交', body: h`<label class="field">给 AI 的补充说明（可选，会写入任务备注）<textarea class="textarea" name="note" rows="4"></textarea></label><div class="small muted">提示：如需 AI 做得更多，可先在任务详情里调高自主等级。</div>` });
    if (!r) return;
    body.note = r.note || null;
  }
  await post(`/tasks/${enc(id)}/actions/${action}`, body);
  toast(ACTION_DONE[action] || '已完成');
  await refreshNow(true);
}
async function patchTask(id, fields, msg = '已更新') {
  await api(`/tasks/${enc(id)}`, { method: 'PATCH', body: fields });
  toast(msg);
  await refreshNow(true);
}
async function editTaskModal(id) {
  const t = S.drawerData?.task?.id === id ? S.drawerData.task : (await api(`/tasks/${enc(id)}`)).task;
  const local = t.due_at ? new Date(new Date(t.due_at).getTime() - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 16) : '';
  await modal({
    title: '编辑任务', submitLabel: '保存', wide: true,
    body: h`<div class="form-grid">
      <label class="field full">标题<input class="input" name="title" required maxlength="300" value="${t.title}"></label>
      <label class="field full">描述<textarea class="textarea" name="description" rows="8">${t.description || ''}</textarea></label>
      <label class="field full">验收标准<textarea class="textarea" name="acceptance" rows="4">${t.acceptance || ''}</textarea></label>
      <label class="field">项目<input class="input" name="project" value="${t.project || ''}"></label>
      <label class="field">截止<input class="input" type="datetime-local" name="due_at" value="${local}"></label></div>`,
    onSubmit: async (f) => {
      const body = {};
      for (const k of ['title', 'description', 'acceptance', 'project']) if ((f[k] || '') !== (t[k] || '')) body[k] = f[k];
      if (f.due_at && f.due_at !== local) body.due_at = new Date(f.due_at).toISOString().replace(/\.\d{3}Z$/, 'Z');
      if (!Object.keys(body).length) return;
      await patchTask(id, body, '任务已保存');
    },
  });
}
async function factModal(f) {
  const kinds = META.factKinds;
  await modal({
    title: '编辑上下文', submitLabel: '保存（锁定）',
    body: h`<div class="form-grid"><label class="field">类别<select class="select" name="kind">${options({ ...kinds, [f.kind]: kinds[f.kind] || f.kind }, f.kind)}</select></label>
      <label class="field">键<input class="input" name="key" required maxlength="200" value="${f.key}"></label>
      <label class="field full">内容<textarea class="textarea" name="value" rows="5" required maxlength="8000">${f.value}</textarea></label></div>`,
    onSubmit: async (v) => {
      await post('/context', { kind: v.kind, key: v.key.trim(), value: v.value.trim() });
      if (v.kind !== f.kind || v.key.trim() !== f.key) await api(`/context/${enc(f.id)}`, { method: 'DELETE' });
      toast('已保存，并锁定为用户确认');
      await refreshNow(true);
    },
  });
}

const ACTS = {
  refresh: () => refreshNow(true),
  'new-task': () => newTaskModal(),
  theme: () => cycleTheme(),
  'close-drawer': () => closeDrawer(),
  'open-task': (el) => openTask(el.dataset.id),
  'open-run': (el) => openRun(el.dataset.id, el.dataset.back),
  'task-action': (el) => taskAction(el.dataset.id, el.dataset.action, !!el.dataset.ask),
  'edit-task': (el) => editTaskModal(el.dataset.id),
  async decide(el) {
    const card = el.closest('[data-approval]');
    const note = card?.querySelector('textarea[name=note]')?.value.trim() || null;
    const label = el.dataset.label || el.dataset.option;
    if (el.dataset.risk === 'high' && !isRejectish(el.dataset.option)) {
      const ok = await confirmModal('确认高风险决策', h`你将选择「<b>${label}</b>」。这是一个<b>高风险</b>事项，批准后会由 Action runner 执行对应动作。`, { submitLabel: '确认' });
      if (!ok) return;
    }
    $$('button', card || el.parentElement).forEach((b) => { b.disabled = true; });
    try {
      await post(`/approvals/${enc(el.dataset.id)}/decide`, { option: el.dataset.option, note });
      card?.querySelectorAll('[data-dirty]').forEach((n) => delete n.dataset.dirty);
      toast(`已提交决策：${label}`);
      await refreshNow(true);
    } finally { $$('button', card || el.parentElement).forEach((b) => { b.disabled = false; }); }
  },
  'drawer-tab': (el) => { S.drawerTab[S.drawer.type] = el.dataset.tab; paintDrawer(); if (S.drawer.type === 'run' && el.dataset.tab === 'events' && S.runFollow) { const b = $('#run-body'); if (b) b.scrollTop = b.scrollHeight; } },
  'ev-filter': (el) => { S.evFilter = el.dataset.kind; paintDrawer(); },
  'reload-log': () => { if (S.drawerData) S.drawerData.log = null; paintDrawer(); },
  async 'cancel-run'(el) {
    if (!await confirmModal('取消运行', '将向执行器发送取消信号并结束整个进程树。', { danger: true, submitLabel: '取消运行' })) return;
    const r = await post(`/runs/${enc(el.dataset.id)}/cancel`);
    toast(r.ok ? '已发送取消信号' : r.detail, r.ok ? 'ok' : 'err');
    await refreshNow(true);
  },
  async 'run-sensor'(el) {
    el.disabled = true;
    await post(`/sensors/${enc(el.dataset.name)}/run`);
    toast(`传感器 ${el.dataset.name} 已在后台运行`);
    setTimeout(() => refreshNow(true), 2500);
  },
  'chart-mode': (el) => { S.chartMode = el.dataset.mode; $$('.seg button').forEach((b) => b.classList.toggle('on', b.dataset.mode === S.chartMode)); drawCharts(); },
  'toggle-param': (el) => {
    const cur = new Set((S.params.get(el.dataset.key) || '').split(',').filter(Boolean));
    if (cur.has(el.dataset.value)) cur.delete(el.dataset.value); else cur.add(el.dataset.value);
    setParams({ [el.dataset.key]: [...cur].join(',') });
  },
  'set-param': (el) => setParams({ [el.dataset.key]: el.dataset.value }),
  'scroll-to': (el) => document.getElementById(el.dataset.target)?.scrollIntoView({ behavior: 'smooth', block: 'start' }),
  async 'toggle-preview'(el) {
    const slot = document.querySelector(`[data-preview-slot="${CSS.escape(el.dataset.id)}"]`);
    if (!slot) return;
    if (slot.innerHTML) { slot.innerHTML = ''; el.textContent = '预览'; return; }
    slot.innerHTML = '<div class="loading">加载中…</div>';
    try { slot.innerHTML = String(h`<div style="margin-top:8px">${await artifactPreviewHtml(el.dataset.id, el.dataset.ext)}</div>`); el.textContent = '收起'; } catch (e) { slot.innerHTML = String(h`<div class="err-text small">${e.message}</div>`); }
  },
  async 'preview-modal'(el) {
    const body = await artifactPreviewHtml(el.dataset.id, el.dataset.ext);
    modal({ title: el.dataset.title || '预览', body, wide: true, hideSubmit: true });
  },
  copy: async (el) => { try { await navigator.clipboard.writeText(el.dataset.text); toast('已复制'); } catch { toast(el.dataset.text); } },
  async 'fact-delete'(el) {
    if (!await confirmModal('删除上下文条目', '该条目将标记为已删除，不再注入提示词。', { danger: true, submitLabel: '删除' })) return;
    await api(`/context/${enc(el.dataset.id)}`, { method: 'DELETE' });
    toast('已删除');
    await refreshNow(true);
  },
  'fact-edit': (el) => { const f = (S.facts || []).find((x) => x.id === el.dataset.id); if (f) factModal(f); },
  async 'fact-confirm'(el) {
    const f = (S.facts || []).find((x) => x.id === el.dataset.id);
    if (!f) return;
    await post('/context', { kind: f.kind, key: f.key, value: f.value });
    toast('已确认');
    await refreshNow(true);
  },
  async 'history-more'(el) {
    const hs = S.history;
    if (!hs?.next) return;
    el.disabled = true;
    const qs = new URLSearchParams(hs.qs); qs.set('before', hs.next);
    const d = await api(`/events?${qs}`);
    hs.rows.push(...d.events); hs.next = d.next_before; hs.pages += 1;
    $('#history-table').innerHTML = String(historyTable(hs.rows));
    $('#history-more').innerHTML = String(historyMore());
  },
};

const CHANGES = {
  param: (el) => setParams({ [el.dataset.key]: el.value }),
  'set-level': (el) => patchTask(el.dataset.id, { level: Number(el.value) }, `等级已改为 L${el.value}（已锁定）`),
  'set-priority': (el) => patchTask(el.dataset.id, { priority: Number(el.value) }, `优先级已改为 P${el.value}`),
  'set-category': (el) => patchTask(el.dataset.id, { category: el.value }, '类别已更新'),
  'set-executor': (el) => (el.value ? patchTask(el.dataset.id, { executor: el.value }, `执行器已改为 ${el.value}`) : toast('如需恢复自动路由，请在 CLI 中清除 executor 字段', 'err')),
  'toggle-root': (el) => setParams({ root: el.checked ? '1' : null }),
  'toggle-sensitive': (el) => setParams({ sensitive: el.checked ? '1' : null }),
  'toggle-all-facts': (el) => setParams({ all: el.checked ? '1' : null }),
  'run-follow': (el) => { S.runFollow = el.checked; },
};

const FORMS = {
  async 'add-fact'(form) {
    const f = Object.fromEntries(new FormData(form).entries());
    if (!f.key.trim() || !f.value.trim()) { toast('请填写键和内容', 'err'); return; }
    await post('/context', { kind: f.kind, key: f.key.trim(), value: f.value.trim() });
    $$('[data-dirty]', form).forEach((n) => delete n.dataset.dirty);
    form.reset();
    toast('已添加');
    await refreshNow(true);
  },
};

/* ======================================================================== theme */

const THEMES = ['auto', 'light', 'dark'];
const THEME_LABEL = { auto: '跟随系统', light: '浅色', dark: '深色' };
function applyTheme(t) {
  if (t === 'auto') delete document.documentElement.dataset.theme; else document.documentElement.dataset.theme = t;
  $('#theme-label').textContent = THEME_LABEL[t];
}
function cycleTheme() {
  let cur = 'auto';
  try { cur = localStorage.getItem('awos.theme') || 'auto'; } catch { /* storage blocked */ }
  const next = THEMES[(THEMES.indexOf(cur) + 1) % THEMES.length];
  try { localStorage.setItem('awos.theme', next); } catch { /* storage blocked */ }
  applyTheme(next);
  drawCharts();
}

/* ======================================================================== boot */

function bindEvents() {
  document.addEventListener('click', async (e) => {
    const goEl = e.target.closest('[data-go]');
    if (goEl && !e.target.closest('[data-act], a[href]:not([href="#"])')) { e.preventDefault(); location.hash = goEl.dataset.go; return; }
    const el = e.target.closest('[data-act]');
    if (!el) return;
    if (el.matches('input, select, textarea')) return;
    const fn = ACTS[el.dataset.act];
    if (!fn) return;
    e.preventDefault();
    try { await fn(el, e); } catch (err) { toast(err.message || String(err), 'err'); }
  });
  document.addEventListener('change', async (e) => {
    const el = e.target.closest('[data-change]');
    if (!el) return;
    try { await CHANGES[el.dataset.change]?.(el, e); } catch (err) { toast(err.message || String(err), 'err'); refreshNow(true); }
  });
  const filterTimers = {};
  document.addEventListener('input', (e) => {
    const el = e.target;
    if (el.dataset.param) {
      clearTimeout(filterTimers[el.dataset.param]);
      filterTimers[el.dataset.param] = setTimeout(() => setParams({ [el.dataset.param]: el.value.trim() }, { replace: true }), 350);
      return;
    }
    if (el.matches('textarea, input') && !el.hasAttribute('data-filter') && el.closest('#view, #drawer')) {
      if (el.value) el.dataset.dirty = '1'; else delete el.dataset.dirty;
    }
  });
  document.addEventListener('submit', async (e) => {
    const form = e.target.closest('[data-form]');
    if (!form) return;
    e.preventDefault();
    try { await FORMS[form.dataset.form]?.(form); } catch (err) { toast(err.message || String(err), 'err'); }
  });
  document.addEventListener('focusout', () => { if (S.pending) setTimeout(() => { if (S.pending && !isEditing()) refreshNow(false); }, 400); });
  document.addEventListener('keydown', (e) => {
    if ($('#modal-wrap').classList.contains('show')) return;
    const typing = e.target.matches('input, textarea, select');
    if (e.key === 'Escape') { if (typing) e.target.blur(); else if (S.drawer) closeDrawer(); return; }
    if (typing || e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.key === 'n') { e.preventDefault(); newTaskModal(); }
    else if (e.key === 'r') { e.preventDefault(); refreshNow(true); }
    else if (e.key === '/') { const s = $('#f-search') || $('#view input[type=search]'); if (s) { e.preventDefault(); s.focus(); } }
  });
  let resizeTimer = null;
  window.addEventListener('resize', () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(drawCharts, 150); });
  window.addEventListener('hashchange', onRoute);
}

async function boot() {
  let theme = 'auto';
  try { theme = localStorage.getItem('awos.theme') || 'auto'; } catch { /* storage blocked */ }
  applyTheme(THEMES.includes(theme) ? theme : 'auto');
  bindEvents();
  try {
    const m = await api('/meta');
    META.categories = m.categories; META.kinds = m.kinds; META.levels = m.levels; META.executors = m.executors;
    META.factKinds = m.fact_kinds || META.factKinds; META.version = m.version;
    for (const s of m.task_statuses) META.statusLabel[s.key] = s.label;
    for (const s of m.run_statuses) META.runLabel[s.key] = s.label;
    for (const s of m.approval_statuses) META.apvLabel[s.key] = s.label;
    Object.assign(META.apvKind, m.approval_kinds || {});
  } catch (e) { toast(`无法读取元数据：${e.message}`, 'err'); }
  await onRoute();
  refreshBadges();
  refreshHealth();
  connectLive();
  setInterval(() => { refreshHealth(); if (S.live) refreshBadges(); else refreshNow(false); }, 20000);
}

if (typeof window !== 'undefined' && typeof document !== 'undefined') boot();
else globalThis.__awosTest = { h, raw, esc, md, inline, safeUrl, recommendedKey, niceStep, rel, dur };
