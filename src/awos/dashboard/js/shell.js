// App shell: sidebar, top bar, router, live refresh (SSE-driven, debounced), health, theme, global shortcuts.
import { h, raw, setHTML, $, $$, esc } from './core/html.js';
import { api, apiOpt, connectStream } from './core/api.js';
import { ACTS, bindDelegation } from './core/acts.js';
import { S } from './core/state.js';
import { lsGet, lsSet, pref } from './core/store.js';
import { parseHash, onRouteChange, DRAWER_KEYS, closeDrawer, go, setParams } from './core/router.js';
import { snapshotUI, restoreUI } from './core/ui-state.js';
import { rel, relShort, dur, usd, countdown, parseTs } from './core/format.js';
import { icon } from './ui/icons.js';
import { btn, iconBtn, kbd, skeletonList, errorState, tipAttr } from './ui/components.js';
import { bindTooltips, bindDrawerKeys, isDialogOpen, toast, closePopover, drawerOpen, hasPendingDeferred } from './ui/overlay.js';
import { bindCharts } from './ui/charts.js';
import { openPalette, paletteOpen } from './ui/palette.js';
import { NAV_GROUPS } from './shell-nav.js';
import { applyMeta, META } from './domain/vocab.js';
import { renderDrawer } from './drawer.js';
import { stopRunPoll } from './ui/rundetail.js';
import overview from './views/overview.js';
import board from './views/board.js';
import inbox from './views/inbox.js';
import outbox from './views/outbox.js';
import { tasksView, runsView, toolsView } from './views/tasks.js';
import { artifactsView, contextView, sensorsView } from './views/knowledge.js';
import { errorsView, historyView } from './views/system.js';

const MOD_KEY = typeof navigator !== 'undefined' && /Mac|iPhone|iPad/.test(navigator.platform || '') ? '⌘ K' : 'Ctrl K';
const VIEWS = { overview, board, inbox, outbox, tasks: tasksView, runs: runsView, tools: toolsView, errors: errorsView, artifacts: artifactsView, context: contextView, sensors: sensorsView, history: historyView };

/* ------------------------------------------------------------------ theme */
const THEMES = ['auto', 'light', 'dark'];
const THEME_META = { auto: ['monitor', '跟随系统'], light: ['sun', '浅色'], dark: ['moon', '深色'] };
function applyTheme(t) {
  if (t === 'auto') delete document.documentElement.dataset.theme; else document.documentElement.dataset.theme = t;
  const [ic, label] = THEME_META[t];
  const b = $('#theme-btn');
  if (b) { setHTML(b, icon(ic, { size: 16 })); b.dataset.tip = `主题：${label}（点击切换）`; b.setAttribute('aria-label', `切换主题，当前${label}`); }
}
const curTheme = () => { const t = lsGet('awos.theme', 'auto'); return THEMES.includes(t) ? t : 'auto'; };
ACTS.theme = () => { const next = THEMES[(THEMES.indexOf(curTheme()) + 1) % THEMES.length]; lsSet('awos.theme', next); applyTheme(next); };

/* ------------------------------------------------------------------ chrome */
function navHTML() {
  return h`${NAV_GROUPS.map((g) => h`<div class="nav-g"><div class="nav-gt">${g.title}</div>${g.items.map((i) => h`<a class="nav-i" href="#/${i.key}" data-view="${i.key}" data-tip-c="${i.title}">${icon(i.icon, { size: 17 })}<span class="nav-l">${i.title}</span>${i.badge ? h`<span class="nav-b" data-badge="${i.badge}" hidden></span>` : ''}</a>`)}</div>`)}`;
}
function bottomNavHTML() {
  const items = [['overview', '总览', 'dashboard'], ['board', '看板', 'kanban'], ['inbox', '收件箱', 'inbox'], ['outbox', 'Outbox', 'send']];
  return h`${items.map(([k, l, ic]) => h`<a class="bn-i" href="#/${k}" data-view="${k}">${icon(ic, { size: 20 })}<span>${l}</span>${k === 'inbox' || k === 'outbox' ? h`<span class="nav-b" data-badge="${k}" hidden></span>` : ''}</a>`)}<button type="button" class="bn-i" data-act="open-nav">${icon('menu', { size: 20 })}<span>更多</span></button>`;
}
function chromeHTML() {
  return h`<div class="brand"><span class="brand-mark">AW</span><div class="brand-t"><b>AgentWorkOS</b><small id="brand-version">个人智能体操作系统</small></div></div>
    <nav class="nav" id="nav" aria-label="主导航">${navHTML()}</nav>
    <div class="side-foot"><button type="button" class="nav-i" data-act="toggle-nav" data-tip="折叠 / 展开侧栏 [">${icon('panel-left', { size: 17 })}<span class="nav-l">收起侧栏</span></button></div>`;
}
function topbarHTML() {
  return h`<button type="button" class="btn btn-ghost btn-icon only-mobile" data-act="open-nav" aria-label="打开导航">${icon('menu', { size: 18 })}</button>
    <div class="tb-title"><h1 id="view-title">总览</h1><span class="tb-sub" id="view-sub"></span></div>
    <button type="button" class="stale-pill" id="stale-pill" data-act="refresh" hidden>${icon('refresh', { size: 13 })}有新动态 · 刷新</button>
    <span class="grow"></span>
    <button type="button" class="search-trigger" data-act="open-palette" aria-label="搜索或跳转（Ctrl K）">${icon('search', { size: 15 })}<span>搜索任务、跳转…</span>${kbd(MOD_KEY)}</button>
    <a class="cost-meter" id="cost-meter" href="#/runs" hidden></a>
    <button type="button" class="pill" id="health-pill" data-act="refresh" aria-live="polite"><i class="sdot tone-muted" id="health-dot"></i><span id="health-text">检查中</span></button>
    <button type="button" class="btn btn-ghost btn-icon" id="refresh-btn" data-act="refresh" data-tip="刷新 [R]" aria-label="刷新">${icon('refresh', { size: 16 })}</button>
    <button type="button" class="btn btn-ghost btn-icon" id="theme-btn" data-act="theme" aria-label="切换主题"></button>
    <button type="button" class="btn btn-default btn-sm new-btn" data-act="new-task">${icon('plus', { size: 15 })}<span class="btn-label">新建任务</span></button>`;
}

function setNavCollapsed(c) {
  $('#app').dataset.collapsed = c ? '1' : '0';
  pref.set('nav.collapsed', c);
  setTimeout(() => moveNavInd(), 480);   // the sidebar glides to its new width first
}

/* ------------------------------------------------------------------ motion: the sliding sidebar highlight, the entrance cascade */
const reducedMotion = () => typeof matchMedia === 'function' && matchMedia('(prefers-reduced-motion: reduce)').matches;
function moveNavInd() {
  const nav = $('#nav'); if (!nav) return;
  let ind = $('.nav-ind', nav);
  const on = $('.nav-i.active', nav);
  if (!ind) {
    ind = document.createElement('i'); ind.className = 'nav-ind no-anim'; ind.setAttribute('aria-hidden', 'true'); nav.prepend(ind);
    requestAnimationFrame(() => requestAnimationFrame(() => ind.classList.remove('no-anim')));   // the first placement must not fly in from the corner
  }
  if (!on || !on.offsetHeight) { ind.style.opacity = '0'; nav.classList.remove('has-ind'); return; }
  nav.classList.add('has-ind');
  ind.style.opacity = '1'; ind.style.width = `${on.offsetWidth}px`; ind.style.height = `${on.offsetHeight}px`;
  ind.style.transform = `translate(${on.offsetLeft}px, ${on.offsetTop}px)`;
}
const FLOW_SEL = '.page-head, .kpi, .wb, .grid12 > *, .ctx-add, .ctx-main > .card, .bhead, .cell, .bcard, .tgroup, .art-card, .sensor, .filter-bar, .toolbar, .table-wrap, .sp-list, .sp-pane, .view-tabs, .view > .card, .tl-bar, .tl-lanes, .tl-main > *';
let flowTimer = 0;
/** Entrance cascade: what is on screen rises in one after another (stagger = --i). Runs on navigation only, never on a live refresh. */
function playFlow(el) {
  if (reducedMotion()) return;
  const limit = innerHeight + 40; let i = 0;
  $$('.fi', el).forEach((n) => n.classList.remove('fi'));
  for (const n of $$(FLOW_SEL, el)) {
    if (i > 30 || n.getBoundingClientRect().top > limit) continue;   // below the fold: no entrance to wait for
    n.style.setProperty('--i', String(Math.min(i, 14))); n.classList.add('fi'); i += 1;
  }
  el.classList.add('flow');
  countUp(el);
  clearTimeout(flowTimer);
  flowTimer = setTimeout(() => { el.classList.remove('flow'); $$('.fi', el).forEach((n) => n.classList.remove('fi')); }, 2200);
}
function countUp(root) {
  for (const n of $$('.kpi-v', root)) {
    const text = n.textContent.trim(); const m = /^(\$?)(\d+(?:\.\d+)?)$/.exec(text);
    if (!m || !parseFloat(m[2])) continue;
    const end = parseFloat(m[2]); const dec = (m[2].split('.')[1] || '').length; const t0 = performance.now();
    const step = (t) => {
      if (!n.isConnected) return;
      const k = Math.min(1, (t - t0) / 950);
      n.textContent = k < 1 ? `${m[1]}${(end * (1 - (1 - k) ** 4)).toFixed(dec)}` : text;
      if (k < 1) requestAnimationFrame(step);
    };
    n.textContent = `${m[1]}${(0).toFixed(dec)}`; requestAnimationFrame(step);
  }
}
ACTS['toggle-nav'] = () => setNavCollapsed($('#app').dataset.collapsed !== '1');
ACTS['open-nav'] = () => document.body.classList.add('nav-open');
ACTS['close-nav'] = () => document.body.classList.remove('nav-open');
ACTS['open-palette'] = () => openPalette();

function updateBadges() {
  const k = S.dash?.kpis; if (!k) return;
  const set = (name, n, cls) => $$(`[data-badge="${name}"]`).forEach((b) => { b.hidden = !n; b.textContent = n > 99 ? '99+' : String(n || ''); b.className = `nav-b${n ? ` ${cls}` : ''}`; });
  set('inbox', (k.inbox || 0) + (k.approvals || 0), 'hot'); set('outbox', k.approvals || 0, 'hot');
  set('runs', k.running || 0, ''); set('errors', k.failures_open ?? k.failures_24h ?? 0, 'alert');
  const cm = $('#cost-meter');
  if (cm && k.budget_daily) {
    const f = k.cost_today / k.budget_daily; const tone = f >= 0.9 ? 'crit' : f >= 0.7 ? 'warn' : 'brand';
    cm.hidden = false; cm.dataset.tip = `今日花费 ${usd(k.cost_today)} / 日预算 ${usd(k.budget_daily)}（${Math.round(f * 100)}%）`;
    setHTML(cm, h`${icon('dollar', { size: 13 })}<span class="cm-v">${usd(k.cost_today)}<small> / ${usd(k.budget_daily)}</small></span><span class="cm-bar tone-${tone}"><i style="width:${Math.min(100, f * 100).toFixed(0)}%"></i></span>`);
  }
}

function daemonState(dm) {
  if (!dm) return ['muted', '状态未知'];
  if (dm.alive) return ['good', '守护进程正常'];
  if (dm.ts) return ['crit', '守护进程已停止'];
  return ['warn', '仅 API（无守护进程）'];
}
function applyHealth(hl) {
  hl._at = Date.now();
  S.health = hl;
  const [tone, text] = daemonState(hl.daemon);
  const dot = $('#health-dot'); if (dot) dot.className = `sdot tone-${tone}${tone === 'good' ? ' pulse' : ''}`;
  const t = $('#health-text'); if (t) t.textContent = text;
  const ex = Object.entries(hl.executors || {}).map(([n, v]) => `${v.ok ? '✓' : '✗'} ${n}`).join('  ');
  const pill = $('#health-pill');
  if (pill) pill.dataset.tip = [`API v${hl.version} · 已运行 ${dur(hl.uptime_s)}`, hl.daemon?.ts ? `守护进程心跳 ${rel(hl.daemon.ts)}` : '未检测到守护进程心跳', ex ? `执行器 ${ex}` : '', `实时推送：${S.live ? '已连接' : '重连中'}`].filter(Boolean).join('\n');
  const v = $('#brand-version'); if (v) v.textContent = `v${hl.version} · 运行 ${dur(hl.uptime_s)}`;
}
async function refreshHealth() {
  try { applyHealth(await api('/health')); } catch {
    const dot = $('#health-dot'); if (dot) dot.className = 'sdot tone-crit'; const t = $('#health-text'); if (t) t.textContent = 'API 无法连接';
  }
}
async function refreshKpis() {
  if (S.view === 'overview') return;   // the overview loads (and stores) the dashboard itself
  try { const d = await apiOpt('/dashboard?days=3'); if (d) S.dash = d; } catch { /* keep the previous KPIs */ }
  updateBadges();
}

/* ------------------------------------------------------------------ router / rendering */
let seqView = 0; let viewKey = ''; let stale = false;
const viewOf = () => VIEWS[S.view];

function setStale(on) { stale = on; const p = $('#stale-pill'); if (p) p.hidden = !on; }
function isEditing() {
  const a = document.activeElement;
  if (a && a.matches?.('textarea, input:not([type=checkbox]):not([type=search]), select') && !a.hasAttribute('data-filter') && a.closest('#view, #drawer') && (a.value || '').length) return true;
  return isDialogOpen() || paletteOpen();
}

async function renderView(initial = false) {
  const v = viewOf();
  const el = $('#view');
  document.title = `${v.title} · AgentWorkOS`;
  $('#view-title').textContent = v.title;
  $('#view-sub').textContent = v.sub || '';
  $$('[data-view]').forEach((a) => { if (a.matches('a')) { const on = a.dataset.view === S.view; a.classList.toggle('active', on); if (on) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current'); } });
  el.dataset.view = S.view;
  moveNavInd();
  if (initial) setHTML(el, v.skeleton ? v.skeleton() : skeletonList());
  else el.classList.add('refetching');
  const seq = ++seqView;
  try {
    const data = await v.load(S.params);
    if (seq !== seqView) return;
    const snap = initial ? null : snapshotUI(el);
    setHTML(el, v.render(data, S.params));
    if (snap) restoreUI(el, snap); else el.scrollTop = 0;
    v.mount?.(el, data, S.params);
    if (initial) playFlow(el);
    updateBadges();
  } catch (e) {
    if (seq === seqView) setHTML(el, errorState(e.message));
  } finally { if (seq === seqView) el.classList.remove('refetching'); }
  setStale(false);
}

async function onRoute() {
  const { view, params } = parseHash();
  S.view = view;
  const hard = new URLSearchParams(params);
  DRAWER_KEYS.forEach((k) => hard.delete(k));
  S.params = new URLSearchParams(hard);
  (VIEWS[view].softParams || []).forEach((k) => hard.delete(k));
  const key = `${view}?${hard}`;
  if (key !== viewKey) {
    const changed = !viewKey.startsWith(`${view}?`);
    viewKey = key;
    if (changed) stopRunPoll();
    renderView(changed || !$('#view').dataset.view);
  } else VIEWS[view].controller?.onParams?.();
  const d = params.get('task') ? { type: 'task', id: params.get('task') } : params.get('run') ? { type: 'run', id: params.get('run'), back: params.get('back') } : null;
  if (JSON.stringify(d) !== JSON.stringify(S.drawer)) { S.drawer = d; renderDrawer(true); }
  document.body.classList.remove('nav-open');
}

async function refresh(force = false) {
  const v = viewOf();
  if (!force && document.hidden) { setStale(true); return; }
  if (!force && (isEditing() || hasPendingDeferred() || v.busy?.() || v.paged?.())) { setStale(true); return; }
  const btnIc = $('#refresh-btn .ic'); btnIc?.classList.add('spin');
  try {
    await refreshKpis();
    if (v.refresh) await v.refresh(); else await renderView(false);
    if (S.drawer) await renderDrawer(false);
  } catch (e) { console.warn('refresh failed', e); }
  btnIc?.classList.remove('spin');
  S.changed = new Set();
  setStale(false);
}
S.refresh = refresh;
ACTS.refresh = () => refresh(true);

/* ------------------------------------------------------------------ live updates: SSE + debounce (>= 3 s) */
let debounce = null; let firstPending = 0;
function scheduleRefresh() {
  const now = Date.now();
  if (!firstPending) firstPending = now;
  clearTimeout(debounce);
  // trailing debounce of 3 s, but never hold a burst of events back for more than 12 s
  const fireAt = Math.min(now + 3000, firstPending + 12000);
  debounce = setTimeout(() => { firstPending = 0; refresh(false); }, Math.max(0, fireAt - now));
}
function onLiveEvent(ev) {
  if (ev.entity_id) S.changed.add(ev.entity_id);
  if (document.hidden) { setStale(true); return; }
  scheduleRefresh();
}
function setLive(on) {
  S.live = on;
  const pill = $('#health-pill');
  if (pill && S.health) applyHealth(S.health);
  if (!on) $('#health-dot')?.classList.remove('pulse');
}

/* ------------------------------------------------------------------ 60 s ticker: relative times (no network) */
function tickTimes() {
  if (document.hidden) return;
  const now = Date.now();
  $$('time.when[data-ts]').forEach((t) => { t.textContent = t.dataset.short ? relShort(t.dataset.ts, now) : rel(t.dataset.ts, now); });
  $$('[data-elapsed]').forEach((t) => { const s = parseTs(t.dataset.elapsed); if (s) t.textContent = dur((now - s.getTime()) / 1000); });
  $$('[data-countdown]').forEach((t) => { const c = countdown(t.dataset.countdown, now); const sp = $('span', t); if (sp) sp.textContent = c.text + (t.dataset.suffix || ''); t.classList.remove('cd-ok', 'cd-soon', 'cd-expired'); t.classList.add(`cd-${c.state}`); });
}

/* ------------------------------------------------------------------ keyboard */
let pendingG = 0;
const G_MAP = { o: 'overview', b: 'board', i: 'inbox', x: 'outbox', t: 'topics', w: 'tasks', r: 'runs', e: 'errors', a: 'artifacts', c: 'context', s: 'sensors', h: 'history' };
function bindKeys() {
  document.addEventListener('keydown', (e) => {
    if (e.defaultPrevented) return;
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); if (!isDialogOpen()) openPalette(); return; }
    if (isDialogOpen() || paletteOpen()) return;
    const typing = e.target.matches?.('input, textarea, select, [contenteditable]');
    if (e.key === 'Escape') {
      if (typing) { e.target.blur(); return; }
      closePopover();
      if (viewOf().keys?.(e)) return;
      if (document.body.classList.contains('nav-open')) { document.body.classList.remove('nav-open'); return; }
      return;
    }
    if (typing || e.ctrlKey || e.metaKey || e.altKey) return;
    if ((e.key === 'Enter' || e.key === ' ') && e.target.matches?.('tr.click[tabindex], [role=button][tabindex]') && !e.target.closest('button')) { e.preventDefault(); e.target.click(); return; }
    if (e.key === '?') { e.preventDefault(); ACTS.shortcuts(); return; }
    if (drawerOpen()) return;
    if (viewOf().keys?.(e)) return;
    if (pendingG && Date.now() - pendingG < 1200) { pendingG = 0; const t = G_MAP[e.key]; if (t) { e.preventDefault(); if (t === 'topics') ACTS['go-topics']?.(); else go(t); } return; }
    if (e.key === 'g') { pendingG = Date.now(); return; }
    if (e.key === 'n') { e.preventDefault(); ACTS['new-task'](); }
    else if (e.key === 'r') { e.preventDefault(); refresh(true); }
    else if (e.key === '[') { e.preventDefault(); ACTS['toggle-nav'](); }
    else if (e.key === '/') { e.preventDefault(); const s = $('#view input[type=search]'); if (s) s.focus(); else openPalette(); }
  });
  bindDrawerKeys(() => closeDrawer());
}

/* ------------------------------------------------------------------ boot */
export async function boot() {
  applyTheme(curTheme());
  setHTML($('#sidebar'), chromeHTML());
  setHTML($('#topbar'), topbarHTML());
  setHTML($('#bottom-nav'), bottomNavHTML());
  applyTheme(curTheme());
  setNavCollapsed(!!pref.get('nav.collapsed', false));
  bindDelegation(); bindTooltips(); bindCharts(); bindKeys();
  window.addEventListener('resize', () => requestAnimationFrame(moveNavInd));
  $('#scrim')?.addEventListener('click', () => document.body.classList.remove('nav-open'));
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) return;
    tickTimes();
    if (stale) refresh(false);
    if (!S.health || Date.now() - (S.health._at || 0) > 60000) refreshHealth();
  });
  document.addEventListener('focusout', () => { if (stale) setTimeout(() => { if (stale && !isEditing()) refresh(false); }, 500); });
  document.addEventListener('awos:dialog-closed', () => { if (stale) setTimeout(() => refresh(false), 300); });
  onRouteChange(onRoute);
  window.addEventListener('hashchange', onRoute);
  try { applyMeta(await api('/meta')); } catch (e) { toast(`无法读取元数据：${e.message}`, 'err'); }
  await onRoute();
  refreshHealth();
  connectStream(onLiveEvent, setLive);
  let ticks = 0;
  setInterval(() => {
    if (document.hidden) return;
    tickTimes(); ticks += 1;
    if (ticks % 2 === 0) refreshHealth();            // every 2 minutes
    if (!S.live) refresh(false);                     // SSE down: fall back to a slow poll (every minute)
  }, 60000);
}
