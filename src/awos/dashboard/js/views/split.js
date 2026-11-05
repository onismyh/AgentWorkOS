// Split reading view shared by 收件箱 and Outbox: grouped list on the left, reader on the right (Linear / Superhuman style).
import { h, raw, setHTML, $, $$, enc } from '../core/html.js';
import { api } from '../core/api.js';
import { ACTS, CHANGES, INPUTS } from '../core/acts.js';
import { S, cacheTask, cacheApproval, CACHE } from '../core/state.js';
import { isUnread, markRead, markUnread, stampOf } from '../core/store.js';
import { setParams, parseHash } from '../core/router.js';
import { when, cut, countdown, parseTs } from '../core/format.js';
import { icon } from '../ui/icons.js';
import { btn, iconBtn, badge, kwChips, kwChip, levelBadge, prioIcon, riskBadge, empty, kbd, skeletonList, errorState, tipAttr } from '../ui/components.js';
import { taskReader, approvalReader, mountReader, taskActionList } from '../ui/reader.js';
import { toast } from '../ui/overlay.js';
import { snapshotUI, restoreUI } from '../core/ui-state.js';
import { META, digestOf, sourceIcon, recommendedKey, isRejectish, effLevel } from '../domain/vocab.js';
import { taskAction, taskActionUndoable, decideApproval } from '../domain/actions.js';
import { skel } from '../ui/components.js';

const SECTION_ICON = { decision: 'scale', approval: 'shield-alert', review: 'circle-dot', handoff: 'hand', suggested: 'sparkles', failed: 'circle-x', pending: 'shield-alert', history: 'check-check' };

export function rowHTML(it, selKey) {
  const unread = isUnread(it.key, it.stamp);
  const cd = it.expires_at && it.kind === 'approval' && it.status === 'pending' ? countdown(it.expires_at) : null;
  const sel = it.key === selKey;
  return h`<a class="srow${sel ? ' sel' : ''}${unread ? ' unread' : ''}" role="option" id="row-${it.dom}" aria-selected="${sel}" data-key="${it.key}" data-act="split-select" href="#/${S.view}?sel=${enc(it.key)}">
    <span class="sr-dot" ${unread ? raw('data-tip="未读"') : ''}></span>
    <span class="sr-ic tone-${it.tone || 'muted'}">${icon(it.kind === 'approval' ? (it.action_type === 'send_email' ? 'mail' : 'shield-alert') : sourceIcon(it.source), { size: 15 })}</span>
    <span class="sr-main">
      <span class="sr-l1"><span class="sr-title">${it.headline}</span><span class="sr-r">${it.kind === 'approval' ? riskBadge(it.risk) : it.level ? levelBadge(it) : ''}<span class="sr-w">${when(it.ts, { short: true })}</span></span></span>
      ${it.gist ? h`<span class="sr-gist">${it.gist}</span>` : ''}
      <span class="sr-l3">${kwChips(it.keywords, { max: 3 })}<span class="grow"></span>${it.priority != null ? prioIcon(it.priority) : ''}${cd ? h`<span class="sr-cd cd-${cd.state}">${icon('hourglass', { size: 12 })}${cd.text}</span>` : ''}</span>
    </span></a>`;
}

export class Split {
  constructor(cfg) {
    this.cfg = cfg;           // {id, title, sub, icon, sections, fetch(), detail(item), emptyPane(items), toolbarExtra(), keysExtra}
    this.items = []; this.extra = null;
    this.f = { q: '', level: '', unread: false };
    this.sel = null; this.detail = new Map(); this.seq = 0; this.lastIdx = 0;
  }

  /* ---------------- filters ---------------- */
  params() { return S.params; }
  visible() {
    const p = this.params(); const kw = p.get('kw') || ''; const sec = p.get('sec') || '';
    const q = this.f.q.toLowerCase();
    return this.items.filter((it) => (!sec || it.section === sec) && (!kw || it.keywords.includes(kw))
      && (!this.f.level || String(it.level) === this.f.level) && (!this.f.unread || isUnread(it.key, it.stamp))
      && (!q || `${it.headline} ${it.gist} ${it.keywords.join(' ')} ${it.title}`.toLowerCase().includes(q)));
  }
  grouped(list) {
    const order = this.cfg.sections.map((s) => s.key);
    const by = new Map(order.map((k) => [k, []]));
    for (const it of list) { if (!by.has(it.section)) by.set(it.section, []); by.get(it.section).push(it); }
    return [...by.entries()].filter(([, v]) => v.length);
  }

  /* ---------------- rendering ---------------- */
  toolbarHTML() {
    const p = this.params(); const kw = p.get('kw') || ''; const sec = p.get('sec') || '';
    const counts = {}; for (const it of this.items) counts[it.section] = (counts[it.section] || 0) + 1;
    const levels = [...new Set(this.items.map((i) => i.level).filter(Boolean))].sort();
    const kwCount = {};
    for (const it of this.items) for (const k of it.keywords) if (!/^L[1-5]$/.test(k)) kwCount[k] = (kwCount[k] || 0) + 1;
    const topKw = Object.entries(kwCount).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0], 'zh')).slice(0, 18);
    if (kw && !kwCount[kw]) topKw.unshift([kw, 0]);
    return h`<div class="sp-tools"><div class="search-box">${icon('search', { size: 15 })}<input class="input input-bare" type="search" id="split-q" data-input="split-q" placeholder="搜索标题、摘要、关键词（/）" value="${this.f.q}" autocomplete="off" aria-label="搜索收件箱">${kbd('/')}</div>
      <div class="sp-chips" role="group" aria-label="分区筛选"><button type="button" class="chip-f${!sec ? ' on' : ''}" data-act="split-sec" data-sec="">全部 <b>${this.items.length}</b></button>${this.cfg.sections.filter((s) => counts[s.key]).map((s) => h`<button type="button" class="chip-f${sec === s.key ? ' on' : ''}" data-act="split-sec" data-sec="${s.key}">${s.title} <b>${counts[s.key]}</b></button>`)}</div>
      <div class="sp-row2">${kw ? h`<button type="button" class="chip-f on" data-act="split-kw-clear" data-tip="清除关键词筛选">${icon('hash', { size: 12 })}${kw}${icon('x', { size: 12 })}</button>` : ''}
        ${topKw.length ? h`<select class="select select-sm" data-change="split-kwsel" aria-label="按关键词筛选"><option value="">全部关键词</option>${topKw.map(([k, n]) => h`<option value="${k}"${k === kw ? raw(' selected') : ''}>${k}${n ? ` (${n})` : ''}</option>`)}</select>` : ''}
        ${levels.length > 1 ? h`<select class="select select-sm" data-change="split-level" aria-label="按等级筛选"><option value="">全部等级</option>${levels.map((l) => h`<option value="${l}"${this.f.level === String(l) ? raw(' selected') : ''}>L${l}</option>`)}</select>` : ''}
        <label class="check small"><input type="checkbox" data-change="split-unread"${this.f.unread ? raw(' checked') : ''}><span>仅未读</span></label><span class="grow"></span>
        ${btn('全部已读', { act: 'split-readall', size: 'sm', variant: 'ghost', icon: 'check-check' })}</div></div>`;
  }
  listBodyHTML() {
    const vis = this.visible();
    if (!vis.length) {
      return this.items.length ? empty({ icon: 'search', title: '没有符合条件的事项', desc: '试试清除筛选条件。', action: btn('清除筛选', { act: 'split-clear', size: 'sm' }) })
        : this.cfg.emptyAll();
    }
    return h`${this.grouped(vis).map(([key, rows]) => {
      const s = this.cfg.sections.find((x) => x.key === key) || { title: key };
      return h`<div class="sgroup" role="group" aria-label="${s.title}"><div class="sgroup-h"><span class="sg-i tone-${s.tone || 'muted'}">${icon(SECTION_ICON[key] || 'circle', { size: 13 })}</span><b>${s.title}</b><span class="sg-n">${rows.length}</span>${s.hint ? h`<span class="sg-hint">${s.hint}</span>` : ''}</div>${rows.map((it) => rowHTML(it, this.sel))}</div>`;
    })}`;
  }
  render() {
    return h`<div class="split${this.sel ? ' has-sel' : ''}" id="split"><section class="sp-list" aria-label="${this.cfg.title}列表">${this.toolbarHTML()}<div class="sp-scroll" id="split-list" role="listbox" tabindex="0" aria-label="${this.cfg.title}" data-scroll="split-list" ${this.sel ? h`aria-activedescendant="row-${this.domOf(this.sel)}"` : ''}>${this.listBodyHTML()}</div></section>
      <section class="sp-pane" id="split-pane" aria-label="阅读窗格" tabindex="-1" data-scroll="split-pane">${this.paneEmpty()}</section></div>`;
  }
  domOf(key) { return String(key).replace(/[^a-zA-Z0-9_-]/g, '_'); }
  paneEmpty() {
    const n = this.visible().length;
    return h`<div class="sp-empty">${icon(this.cfg.icon, { size: 28 })}<h3>${n ? `选择一条来阅读` : `${this.cfg.title}已清空`}</h3><p class="muted">${n ? '点击左侧事项，或用键盘 J / K 浏览。所有内容都会显示在这里。' : '新的事项出现时会实时显示。'}</p>
      <div class="sp-keys">${[['J K', '上一条 / 下一条'], ['E', '接受 / 批准推荐项'], ['X', '忽略'], ['S', '稍后'], ['R', '返工'], ['?', '全部快捷键']].map(([k, t]) => h`<div>${kbd(k)}<span>${t}</span></div>`)}</div></div>`;
  }

  /* ---------------- selection ---------------- */
  find(key) { return this.items.find((i) => i.key === key); }
  visibleKeys() { return this.grouped(this.visible()).flatMap(([, rows]) => rows.map((r) => r.key)); }
  paintList(keepScroll = true) {
    const list = $('#split-list'); if (!list) return;
    const sc = list.scrollTop;
    setHTML(list, this.listBodyHTML());
    if (keepScroll) list.scrollTop = sc;
    this.syncActive();
  }
  paintToolbar() { const t = $('.sp-tools'); if (t) setHTML(t.parentElement.querySelector('.sp-tools'), this.toolbarHTML()); }
  syncActive() {
    $$('.srow', $('#split-list')).forEach((r) => { const on = r.dataset.key === this.sel; r.classList.toggle('sel', on); r.setAttribute('aria-selected', on); });
    const list = $('#split-list');
    if (list) { if (this.sel) list.setAttribute('aria-activedescendant', `row-${this.domOf(this.sel)}`); else list.removeAttribute('aria-activedescendant'); }
    $('#split')?.classList.toggle('has-sel', !!this.sel);
    const cur = $('.srow.sel'); if (cur) cur.scrollIntoView({ block: 'nearest' });
  }
  async select(key, { replace = true, silentPush = false } = {}) {
    const it = key ? this.find(key) : null;
    this.sel = it ? key : null;
    if (it) { this.lastIdx = Math.max(0, this.visibleKeys().indexOf(key)); if (markRead(it.key, it.stamp)) this.syncUnread(it); }
    if (!silentPush) setParams({ sel: this.sel }, { replace });
    this.syncActive();
    await this.loadPane(it);
  }
  syncUnread(it) { const r = $(`.srow[data-key="${CSS.escape(it.key)}"]`); r?.classList.remove('unread'); $('.sr-dot', r)?.removeAttribute('data-tip'); S.refreshBadgesLocal?.(); }
  async loadPane(it, { background = false } = {}) {
    const pane = $('#split-pane'); if (!pane) return;
    if (!it) { setHTML(pane, this.paneEmpty()); return; }
    const mySeq = ++this.seq;
    const cached = this.detail.get(it.key);
    if (cached && !background) { this.paintPane(it, cached.data); }
    else if (!background) setHTML(pane, h`<div class="pane-skel">${skel('40%', 14)}${skel('75%', 24)}${skel('55%', 12)}${skel('100%', 10)}${skel('92%', 10)}${skel('96%', 10)}</div>`);
    try {
      const data = await this.cfg.detail(it);
      if (mySeq !== this.seq) return;
      const hash = JSON.stringify(data);
      if (cached && cached.hash === hash) return;
      this.detail.set(it.key, { data, hash });
      this.paintPane(it, data);
    } catch (e) {
      if (mySeq !== this.seq) return;
      setHTML(pane, errorState(e.message, { retry: false }));
    }
  }
  paintPane(it, data) {
    const pane = $('#split-pane'); if (!pane || this.sel !== it.key) return;
    const snap = pane.dataset.key === it.key ? snapshotUI(pane) : null;
    const back = btn('返回列表', { act: 'split-back', icon: 'arrow-left', size: 'sm', variant: 'ghost', cls: 'only-narrow' });
    setHTML(pane, h`<div class="sp-pane-in">${back}${it.kind === 'approval' ? approvalReader(data, { mode: 'pane' }) : taskReader(data, { mode: 'pane' })}</div>`);
    pane.dataset.key = it.key;
    if (snap) restoreUI(pane, snap); else pane.scrollTop = 0;
    mountReader(pane, data.signal !== undefined ? data : data);
  }

  /* ---------------- data ---------------- */
  async load() {
    const r = await this.cfg.fetch();
    this.items = r.items; this.extra = r.extra;
    const want = S.params.get('sel');
    if (want && this.find(want)) this.sel = want;
    else if (want) this.sel = null;
    else if (this.sel && !this.find(this.sel)) this.sel = null;
    return r;
  }
  mount() {
    if (this.sel) { const it = this.find(this.sel); if (it) { markRead(it.key, it.stamp); this.loadPane(it); this.syncUnread(it); } }
    else if (window.matchMedia('(min-width: 1100px)').matches && this.cfg.autoSelect && this.visible().length) this.select(this.visibleKeys()[0], { replace: true });
    this.syncActive();
  }
  /** SSE refresh: reload the list, keep position, refresh the open reader quietly. */
  async refresh() {
    const prevKeys = this.visibleKeys(); const prevSel = this.sel;
    const idx = prevSel ? prevKeys.indexOf(prevSel) : -1;
    const r = await this.cfg.fetch();
    this.items = r.items; this.extra = r.extra;
    if (prevSel && !this.find(prevSel)) {
      const keys = this.visibleKeys();
      const nxt = keys[Math.min(Math.max(idx, 0), keys.length - 1)] || null;
      this.paintToolbar(); this.sel = nxt; this.paintList();
      if (nxt) await this.select(nxt, { replace: true }); else { setParams({ sel: null }, { replace: true }); await this.loadPane(null); }
      return;
    }
    this.paintToolbar(); this.paintList();
    const it = prevSel ? this.find(prevSel) : null;
    if (it) await this.loadPane(it, { background: true });
  }

  /** The URL's sel param changed (browser back/forward, a link): follow it. */
  onParams() {
    const want = S.params.get('sel') || null;
    if (want === this.sel) return;
    this.sel = want && this.find(want) ? want : null;
    this.syncActive();
    this.loadPane(this.current());
  }

  /* ---------------- keyboard ---------------- */
  move(delta) {
    const keys = this.visibleKeys(); if (!keys.length) return;
    let i = this.sel ? keys.indexOf(this.sel) : -1;
    i = Math.max(0, Math.min(keys.length - 1, i + delta));
    this.select(keys[i], { replace: true });
  }
  neighbourOf(key) { const keys = this.visibleKeys(); const i = keys.indexOf(key); return keys[i + 1] || keys[i - 1] || null; }
  current() { return this.sel ? this.find(this.sel) : null; }
}

/* ------------------------------------------------------------------ shared actions (the active split view) */
let active = null;
export const setActiveSplit = (s) => { active = s; };
const wide = () => window.matchMedia('(min-width: 1100px)').matches;

Split.prototype.removeLocal = function removeLocal(key) {
  const keys = this.visibleKeys(); const idx = keys.indexOf(key);
  const item = this.find(key);
  this.items = this.items.filter((i) => i.key !== key);
  const next = keys[idx + 1] || keys[idx - 1] || null;
  this.paintToolbar(); this.paintList();
  if (this.sel === key) { if (next && next !== key) this.select(next, { replace: true }); else { this.sel = null; setParams({ sel: null }, { replace: true }); this.loadPane(null); this.syncActive(); } }
  return { item, idx };
};
Split.prototype.restoreLocal = function restoreLocal({ item, idx }) {
  if (!item || this.find(item.key)) return;
  this.items.splice(Math.min(idx, this.items.length), 0, item);
  this.paintToolbar(); this.paintList();
  this.select(item.key, { replace: true });
};

ACTS['split-select'] = (el) => { active?.select(el.dataset.key, { replace: wide() }); };
ACTS['split-sec'] = (el) => setParams({ sec: el.dataset.sec });
ACTS['split-kw-clear'] = () => setParams({ kw: null });
ACTS['split-back'] = () => { if (active) { active.sel = null; setParams({ sel: null }, { replace: false }); active.syncActive(); } };
ACTS['split-readall'] = () => { if (!active) return; active.visible().forEach((it) => markRead(it.key, it.stamp)); active.paintList(); toast('已全部标为已读'); };
ACTS['split-clear'] = () => { if (!active) return; active.f = { q: '', level: '', unread: false }; setParams({ sec: null, kw: null }); };
INPUTS['split-q'] = (el) => { if (!active) return; active.f.q = el.value.trim(); clearTimeout(INPUTS._st); INPUTS._st = setTimeout(() => { active.paintList(); }, 120); };
CHANGES['split-kwsel'] = (el) => setParams({ kw: el.value || null });
CHANGES['split-level'] = (el) => { if (active) { active.f.level = el.value; active.paintList(); } };
CHANGES['split-unread'] = (el) => { if (active) { active.f.unread = el.checked; active.paintList(); } };

/** j/k/Enter/Esc/e/x/s/r/#/u// for a split view. Returns true when the key was handled. */
export function splitKeys(e, ctl) {
  const typing = e.target.matches?.('input, textarea, select, [contenteditable]');
  if (typing || e.ctrlKey || e.metaKey || e.altKey) return false;
  const it = ctl.current();
  const k = e.key;
  const pane = $('#split-pane');
  if (k === 'j' || k === 'ArrowDown') { ctl.move(1); return true; }
  if (k === 'k' || k === 'ArrowUp') { ctl.move(-1); return true; }
  if (k === 'Enter' && it && !e.target.closest?.('#split-pane')) {
    const target = $('textarea[name=note]', pane) || $('.opt.rec', pane) || $('.rd-actions .btn-default', pane) || pane;
    target?.focus(); return true;
  }
  if (k === 'Escape') {
    if (pane?.contains(e.target) && e.target !== pane) { $('#split-list')?.focus(); return true; }
    if (!wide() && ctl.sel) { ACTS['split-back'](); return true; }
    return false;
  }
  if (k === '/') { $('#split-q')?.focus(); return true; }
  if (k === 'u' && it) { markUnread(it.key); ctl.paintList(); return true; }
  if (!it) return false;
  const run = (fn) => { e.preventDefault(); Promise.resolve(fn()).catch((err) => toast(err.message || String(err), 'err')); return true; };
  const task = it.kind === 'task' ? (CACHE.tasks.get(it.id) || { id: it.id, status: it.status, headline: it.headline, title: it.title }) : null;
  if (k === 'e') return run(() => primaryAction(ctl, it, task));
  if (k === 'x') {
    if (task) return run(async () => { const next = ctl.neighbourOf(it.key); const ok = await taskAction(task, 'dismiss'); if (ok && next) ctl.nextHint = next; });
    return run(() => approvalQuick(ctl, it, 'reject'));
  }
  if (!task) return false;
  if (k === 's') return run(() => taskAction(task, 'snooze'));
  if (k === 'r') return run(() => taskAction(task, task.status === 'review' || task.status === 'done' ? 'rework' : 'rework'));
  if (k === '#') return run(() => taskAction(task, 'cancel'));
  return false;
}

async function approvalQuick(ctl, it, which) {
  const cached = ctl.detail.get(it.key)?.data;
  const a = cached?.approval || (await api(`/approvals/${enc(it.id)}`)).approval;
  cacheApproval(a);
  const opts = (a.options || []).filter((o) => o && o.key);
  let opt;
  if (which === 'reject') opt = opts.find((o) => isRejectish(o.key));
  else { const rk = recommendedKey(a); opt = opts.find((o) => o.key === rk); }
  if (!opt) { toast(which === 'reject' ? '这个事项没有「拒绝」类选项，请在右侧手动选择' : '没有 AI 推荐项，请在右侧手动选择', 'warn'); return; }
  const note = $('textarea[name=note]', $('#split-pane'))?.value.trim() || null;
  await decideApproval(a, opt.key, note, opt.label);
}

async function primaryAction(ctl, it, task) {
  if (it.kind === 'approval') return approvalQuick(ctl, it, 'rec');
  const st = task.status;
  if (['inbox', 'triaged', 'snoozed'].includes(st)) {
    return taskActionUndoable(task, 'accept', { apply: () => { ctl._undo = ctl.removeLocal(it.key); }, revert: () => ctl.restoreLocal(ctl._undo) });
  }
  if (st === 'review' || st === 'waiting_user') return taskAction(task, 'complete');
  if (st === 'failed') return taskAction(task, 'retry');
  toast('这个状态没有默认操作', 'info');
  return null;
}
