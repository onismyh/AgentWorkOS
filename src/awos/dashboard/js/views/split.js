// Split reading view shared by 收件箱 and Outbox: grouped list on the left, reader on the right (Linear / Superhuman style).
import { h, raw, setHTML, $, $$, enc } from '../core/html.js';
import { api } from '../core/api.js';
import { ACTS, CHANGES, INPUTS } from '../core/acts.js';
import { S, cacheTask, cacheApproval, CACHE } from '../core/state.js';
import { isUnread, markRead, markUnread, stampOf, pref, touchTopic } from '../core/store.js';
import { setParams, parseHash, go } from '../core/router.js';
import { when, cut, countdown, parseTs } from '../core/format.js';
import { icon } from '../ui/icons.js';
import { btn, iconBtn, badge, kwChips, kwChip, levelBadge, prioIcon, riskBadge, empty, kbd, skeletonList, errorState, tipAttr, seg } from '../ui/components.js';
import { taskReader, approvalReader, mountReader, taskActionList } from '../ui/reader.js';
import { toast } from '../ui/overlay.js';
import { snapshotUI, restoreUI } from '../core/ui-state.js';
import { META, digestOf, sourceIcon, recommendedKey, isRejectish, effLevel } from '../domain/vocab.js';
import { taskAction, taskActionUndoable, decideApproval } from '../domain/actions.js';
import { skel } from '../ui/components.js';
import { topicReader, mountTopicReader, countChips } from '../ui/topic-reader.js';
import { mountRecap, fetchTopic } from '../ui/recap.js';
import { SECTION_VIEW, ageWord, groupByTopic, isTopicKey, oldTone, topicFromSel, topicSel } from '../domain/topics.js';

const SECTION_ICON = { decision: 'scale', approval: 'shield-alert', review: 'circle-dot', handoff: 'hand', suggested: 'sparkles', failed: 'circle-x', pending: 'shield-alert', history: 'check-check' };

export function rowHTML(it, selKey, { inTopic = false, showTopic = false } = {}) {
  const unread = isUnread(it.key, it.stamp);
  const cd = it.expires_at && it.kind === 'approval' && it.status === 'pending' ? countdown(it.expires_at) : null;
  const sel = it.key === selKey;
  const sv = inTopic ? SECTION_VIEW[it.section] : null;
  return h`<a class="srow${sel ? ' sel' : ''}${unread ? ' unread' : ''}${inTopic ? ' in-topic' : ''}" role="option" id="row-${it.dom}" aria-selected="${sel}" data-key="${it.key}" data-act="split-select" href="#/${S.view}?sel=${enc(it.key)}">
    <span class="sr-dot" ${unread ? raw('data-tip="未读"') : ''}></span>
    <span class="sr-ic tone-${it.tone || 'muted'}">${icon(it.kind === 'approval' ? (it.action_type === 'send_email' ? 'mail' : 'shield-alert') : sourceIcon(it.source), { size: 15 })}</span>
    <span class="sr-main">
      <span class="sr-l1"><span class="sr-title">${it.headline}</span><span class="sr-r">${it.kind === 'approval' ? riskBadge(it.risk) : it.level ? levelBadge(it) : ''}<span class="sr-w">${when(it.ts, { short: true })}</span></span></span>
      ${it.gist ? h`<span class="sr-gist">${it.gist}</span>` : ''}
      <span class="sr-l3">${sv ? h`<span class="sr-sec tone-${sv.tone}">${sv.label}</span>` : ''}${showTopic && it.topic_title ? h`<span class="sr-tp" data-tip="话题：${it.topic_title}">${icon('layers', { size: 11 })}<span>${it.topic_title}</span></span>` : ''}${kwChips(it.keywords, { max: sv || showTopic ? 2 : 3 })}<span class="grow"></span>${it.priority != null ? prioIcon(it.priority) : ''}${cd ? h`<span class="sr-cd cd-${cd.state}">${icon('hourglass', { size: 12 })}${cd.text}</span>` : ''}</span>
    </span></a>`;
}

export class Split {
  constructor(cfg) {
    this.cfg = cfg;           // {id, title, sub, icon, sections, fetch(), detail(item), emptyAll(), topicMode}
    this.items = []; this.extra = null; this.topics = [];
    this.collapsed = pref.get(`${cfg.id}.topicClosed`, {}) || {};
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
      && (!q || `${it.headline} ${it.gist} ${it.keywords.join(' ')} ${it.title} ${it.topic_title || ''}`.toLowerCase().includes(q)));
  }
  grouped(list) {
    const order = this.cfg.sections.map((s) => s.key);
    const by = new Map(order.map((k) => [k, []]));
    for (const it of list) { if (!by.has(it.section)) by.set(it.section, []); by.get(it.section).push(it); }
    return [...by.entries()].filter(([, v]) => v.length);
  }

  /* ---------------- topics ---------------- */
  hasTopics() { return this.cfg.topicMode !== false && this.items.some((i) => i.topic_key); }
  /** 'topic' (default: one collapsible group per topic) or 'type' (the old section groups). */
  mode() { return this.hasTopics() && pref.get(`${this.cfg.id}.group`, 'topic') !== 'type' ? 'topic' : 'type'; }
  /** Topics over ALL items (not just the filtered ones): the reader of a topic always shows its whole state. */
  allTopics() {
    if (this._tg?.items !== this.items || this._tg?.topics !== this.topics) this._tg = { items: this.items, topics: this.topics, list: groupByTopic(this.items, this.topics) };
    return this._tg.list;
  }
  findTopic(sel) { const k = topicFromSel(sel); return this.allTopics().find((t) => t.key === k) || null; }
  isClosed(key) { return !!this.collapsed[key]; }
  saveClosed() { pref.set(`${this.cfg.id}.topicClosed`, this.collapsed); }
  toggleTopic(key, closed = !this.isClosed(key)) {
    if (closed) this.collapsed[key] = true; else delete this.collapsed[key];
    this.saveClosed(); this.paintList(); this.paintToolbar();
  }
  setAllClosed(closed) {
    this.collapsed = {};
    if (closed) for (const t of this.allTopics()) this.collapsed[t.key] = true;
    this.saveClosed(); this.paintList(); this.paintToolbar();
  }

  /* ---------------- rendering ---------------- */
  /** The content of `.sp-tools` (render() wraps it; paintToolbar() swaps it in place). */
  toolbarHTML() {
    const p = this.params(); const kw = p.get('kw') || ''; const sec = p.get('sec') || '';
    const counts = {}; for (const it of this.items) counts[it.section] = (counts[it.section] || 0) + 1;
    const levels = [...new Set(this.items.map((i) => i.level).filter(Boolean))].sort();
    const kwCount = {};
    for (const it of this.items) for (const k of it.keywords) if (!/^L[1-5]$/.test(k)) kwCount[k] = (kwCount[k] || 0) + 1;
    const topKw = Object.entries(kwCount).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0], 'zh')).slice(0, 18);
    if (kw && !kwCount[kw]) topKw.unshift([kw, 0]);
    const mode = this.mode(); const hasT = this.hasTopics();
    const anyOpen = this.allTopics().some((t) => !this.isClosed(t.key));
    return h`<div class="search-box">${icon('search', { size: 15 })}<input class="input input-bare" type="search" id="split-q" data-input="split-q" placeholder="搜索标题、摘要、关键词、话题（/）" value="${this.f.q}" autocomplete="off" aria-label="搜索${this.cfg.title}">${kbd('/')}</div>
      <div class="sp-row2 sp-modes">${hasT ? seg([{ key: 'topic', label: '按话题', icon: 'layers' }, { key: 'type', label: '按类型', icon: 'list' }], mode, { act: 'split-group', key: 'group', label: '分组方式' }) : ''}
        ${hasT && mode === 'topic' ? iconBtn(anyOpen ? 'chevron-up' : 'chevron-down', { act: 'topic-collapse-all', tip: anyOpen ? '折叠全部话题' : '展开全部话题', size: 'icon-sm', variant: 'ghost' }) : ''}<span class="grow"></span>
        ${btn('全部已读', { act: 'split-readall', size: 'sm', variant: 'ghost', icon: 'check-check' })}</div>
      <div class="sp-chips" role="group" aria-label="分区筛选"><button type="button" class="chip-f${!sec ? ' on' : ''}" data-act="split-sec" data-sec="">全部 <b>${this.items.length}</b></button>${this.cfg.sections.filter((s) => counts[s.key]).map((s) => h`<button type="button" class="chip-f${sec === s.key ? ' on' : ''}" data-act="split-sec" data-sec="${s.key}">${s.title} <b>${counts[s.key]}</b></button>`)}</div>
      <div class="sp-row2">${kw ? h`<button type="button" class="chip-f on" data-act="split-kw-clear" data-tip="清除关键词筛选">${icon('hash', { size: 12 })}${kw}${icon('x', { size: 12 })}</button>` : ''}
        ${topKw.length ? h`<select class="select select-sm" data-change="split-kwsel" aria-label="按关键词筛选"><option value="">全部关键词</option>${topKw.map(([k, n]) => h`<option value="${k}"${k === kw ? raw(' selected') : ''}>${k}${n ? ` (${n})` : ''}</option>`)}</select>` : ''}
        ${levels.length > 1 ? h`<select class="select select-sm" data-change="split-level" aria-label="按等级筛选"><option value="">全部等级</option>${levels.map((l) => h`<option value="${l}"${this.f.level === String(l) ? raw(' selected') : ''}>L${l}</option>`)}</select>` : ''}
        <label class="check small"><input type="checkbox" data-change="split-unread"${this.f.unread ? raw(' checked') : ''}><span>仅未读</span></label></div>`;
  }
  topicGroupHTML(g) {
    const key = topicSel(g.key); const open = !this.isClosed(g.key); const sel = this.sel === key;
    const unread = g.items.some((i) => isUnread(i.key, i.stamp));
    const dom = this.domOf(key);
    const tone = oldTone(g.oldest_ts);
    return h`<div class="tgroup${open ? '' : ' closed'}" role="group" aria-label="${g.title}" data-topic="${g.key}">
      <div class="tg-row${sel ? ' sel' : ''}${unread ? ' unread' : ''}">
        <button type="button" class="tg-tog" tabindex="-1" data-act="topic-toggle" data-topic="${g.key}" aria-expanded="${open}" aria-label="${open ? '折叠' : '展开'}话题：${g.title}">${icon('chevron-down', { size: 14 })}</button>
        <a class="tg-h" role="option" id="row-${dom}" aria-selected="${sel}" data-key="${key}" data-act="split-select" href="#/${S.view}?sel=${enc(key)}">
          <span class="tg-l1"><span class="tg-dot${unread ? ' on' : ''}" ${unread ? raw('data-tip="有未读事项"') : ''}></span><span class="tg-title">${g.title}</span>${g.oldest_ts ? h`<span class="tg-age tone-${tone}">${icon('clock', { size: 11 })}最早 ${ageWord(g.oldest_ts)}前</span>` : ''}</span>
          <span class="tg-l2">${countChips(g.counts)}${g.has_brief ? '' : h`<span class="tg-nb" data-tip="还没有 AI 生成的提要">${icon('sparkles', { size: 11 })}无提要</span>`}</span>
          ${g.state_now ? h`<span class="tg-state">${g.state_now}</span>` : ''}
        </a></div>
      ${open ? g.items.map((it) => rowHTML(it, this.sel, { inTopic: true })) : ''}</div>`;
  }
  listBodyHTML() {
    const vis = this.visible();
    if (!vis.length) {
      return this.items.length ? empty({ icon: 'search', title: '没有符合条件的事项', desc: '试试清除筛选条件。', action: btn('清除筛选', { act: 'split-clear', size: 'sm' }) })
        : this.cfg.emptyAll();
    }
    if (this.mode() === 'topic') return h`${groupByTopic(vis, this.topics).map((g) => this.topicGroupHTML(g))}`;
    return h`${this.grouped(vis).map(([key, rows]) => {
      const s = this.cfg.sections.find((x) => x.key === key) || { title: key };
      return h`<div class="sgroup" role="group" aria-label="${s.title}"><div class="sgroup-h"><span class="sg-i tone-${s.tone || 'muted'}">${icon(SECTION_ICON[key] || 'circle', { size: 13 })}</span><b>${s.title}</b><span class="sg-n">${rows.length}</span>${s.hint ? h`<span class="sg-hint">${s.hint}</span>` : ''}</div>${rows.map((it) => rowHTML(it, this.sel, { showTopic: this.hasTopics() }))}</div>`;
    })}`;
  }
  render() {
    return h`<div class="split${this.sel ? ' has-sel' : ''}" id="split"><section class="sp-list" aria-label="${this.cfg.title}列表"><div class="sp-tools">${this.toolbarHTML()}</div><div class="sp-scroll" id="split-list" role="listbox" tabindex="0" aria-label="${this.cfg.title}" data-scroll="split-list" ${this.sel ? h`aria-activedescendant="row-${this.domOf(this.sel)}"` : ''}>${this.listBodyHTML()}</div></section>
      <section class="sp-pane" id="split-pane" aria-label="阅读窗格" tabindex="-1" data-scroll="split-pane">${this.paneEmpty()}</section></div>`;
  }
  domOf(key) { return String(key).replace(/[^a-zA-Z0-9_-]/g, '_'); }
  paneEmpty() {
    const n = this.visible().length;
    const topic = this.mode() === 'topic';
    return h`<div class="sp-empty">${icon(this.cfg.icon, { size: 28 })}<h3>${n ? (topic ? '选择一个话题或事项来阅读' : '选择一条来阅读') : `${this.cfg.title}已清空`}</h3><p class="muted">${n ? (topic ? '点击话题标题先看前情提要，再逐项处理；也可以用键盘 J / K 浏览。' : '点击左侧事项，或用键盘 J / K 浏览。所有内容都会显示在这里。') : '新的事项出现时会实时显示。'}</p>
      <div class="sp-keys">${[['J K', '上一条 / 下一条'], ...(topic ? [['[ ]', '上一个 / 下一个话题'], ['← →', '折叠 / 展开话题']] : []), ['E', '接受 / 批准推荐项'], ['X', '忽略'], ['S', '稍后'], ['R', '返工'], ['?', '全部快捷键']].map(([k, t]) => h`<div>${kbd(k)}<span>${t}</span></div>`)}</div></div>`;
  }

  /* ---------------- selection ---------------- */
  find(key) { return this.items.find((i) => i.key === key); }
  exists(key) { return isTopicKey(key) ? !!this.findTopic(key) : !!this.find(key); }
  /** Navigable keys in screen order: in topic mode every topic header, followed by its items unless collapsed. */
  visibleKeys() {
    const vis = this.visible();
    if (this.mode() === 'topic') return groupByTopic(vis, this.topics).flatMap((g) => [topicSel(g.key), ...(this.isClosed(g.key) ? [] : g.items.map((r) => r.key))]);
    return this.grouped(vis).flatMap(([, rows]) => rows.map((r) => r.key));
  }
  /** The key to measure j / k from: the selection, or - when its group is collapsed - the topic header above it. */
  anchor(keys = this.visibleKeys()) {
    if (this.sel && keys.includes(this.sel)) return this.sel;
    const it = this.sel ? this.find(this.sel) : null;
    return it && this.mode() === 'topic' && keys.includes(topicSel(it.topic_key)) ? topicSel(it.topic_key) : null;
  }
  paintList(keepScroll = true) {
    const list = $('#split-list'); if (!list) return;
    const sc = list.scrollTop;
    setHTML(list, this.listBodyHTML());
    if (keepScroll) list.scrollTop = sc;
    this.syncActive();
  }
  paintToolbar() {
    const t = $('.sp-tools'); if (!t) return;
    const a = document.activeElement;
    const typing = a && a.id === 'split-q' ? [a.selectionStart, a.selectionEnd] : null;   // keep the caret in the search box
    setHTML(t, this.toolbarHTML());
    if (typing) { const n = $('#split-q'); if (n) { n.focus({ preventScroll: true }); try { n.setSelectionRange(...typing); } catch { /* not text */ } } }
  }
  syncActive() {
    const list = $('#split-list');
    if (!list) return;   // the view is not on screen (yet)
    $$('.srow', list).forEach((r) => { const on = r.dataset.key === this.sel; r.classList.toggle('sel', on); r.setAttribute('aria-selected', on); });
    $$('.tg-h', list).forEach((a) => { const on = a.dataset.key === this.sel; a.parentElement.classList.toggle('sel', on); a.setAttribute('aria-selected', on); });
    if (list) { if (this.sel) list.setAttribute('aria-activedescendant', `row-${this.domOf(this.sel)}`); else list.removeAttribute('aria-activedescendant'); }
    $('#split')?.classList.toggle('has-sel', !!this.sel);
    const cur = $('.srow.sel') || $('.tg-row.sel'); if (cur) cur.scrollIntoView({ block: 'nearest' });
  }
  async select(key, { replace = true, silentPush = false } = {}) {
    if (isTopicKey(key)) return this.selectTopic(key, { replace, silentPush });
    const it = key ? this.find(key) : null;
    this.sel = it ? key : null;
    if (it && this.mode() === 'topic' && this.isClosed(it.topic_key)) { delete this.collapsed[it.topic_key]; this.saveClosed(); this.paintList(); this.paintToolbar(); }
    if (it) { this.lastIdx = Math.max(0, this.visibleKeys().indexOf(key)); if (markRead(it.key, it.stamp)) this.syncUnread(it); }
    if (!silentPush) setParams({ sel: this.sel }, { replace });
    this.syncActive();
    await this.loadPane(it);
  }
  async selectTopic(key, { replace = true, silentPush = false } = {}) {
    const tp = this.findTopic(key);
    this.sel = tp ? key : null;
    if (tp) { this.lastIdx = Math.max(0, this.visibleKeys().indexOf(key)); touchTopic(tp.key); }
    if (!silentPush) setParams({ sel: this.sel }, { replace });
    this.syncActive();
    await this.loadTopicPane(tp);
  }
  syncUnread(it) {
    const r = $(`.srow[data-key="${CSS.escape(it.key)}"]`);
    r?.classList.remove('unread'); $('.sr-dot', r)?.removeAttribute('data-tip');
    const g = r?.closest('.tgroup');
    if (g && !$('.srow.unread', g)) { $('.tg-row', g)?.classList.remove('unread'); $('.tg-dot', g)?.classList.remove('on'); }
    S.refreshBadgesLocal?.();
  }
  paneSkeleton(topic = false) {
    return h`<div class="pane-skel">${skel('30%', 12)}${skel('62%', 24)}${skel('40%', 12)}${topic ? h`${skel('100%', 64)}${skel('100%', 10)}${skel('92%', 10)}${skel('96%', 10)}${skel('100%', 90)}` : h`${skel('100%', 10)}${skel('92%', 10)}${skel('96%', 10)}`}</div>`;
  }
  async loadPane(it, { background = false } = {}) {
    const pane = $('#split-pane'); if (!pane) return;
    if (!it) { setHTML(pane, this.paneEmpty()); return; }
    const mySeq = ++this.seq;
    const cached = this.detail.get(it.key);
    if (cached && !background) { this.paintPane(it, cached.data); }
    else if (!background) setHTML(pane, this.paneSkeleton());
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
    const back = this.mode() === 'topic' && it.topic_key
      ? btn('返回话题', { act: 'split-up', data: { key: topicSel(it.topic_key) }, icon: 'arrow-left', size: 'sm', variant: 'ghost', cls: 'only-narrow' })
      : btn('返回列表', { act: 'split-back', icon: 'arrow-left', size: 'sm', variant: 'ghost', cls: 'only-narrow' });
    setHTML(pane, h`<div class="sp-pane-in">${back}${it.kind === 'approval' ? approvalReader(data, { mode: 'pane' }) : taskReader(data, { mode: 'pane' })}</div>`);
    pane.dataset.key = it.key;
    if (snap) restoreUI(pane, snap); else pane.scrollTop = 0;
    mountReader(pane, data);
  }

  /** The reader of a topic header: state, recap timeline and all open items. Falls back to what the list knows (older server). */
  topicFallback(tp) {
    const items = tp.items.map((r) => ({ kind: r.kind, id: r.id, task_id: r.task_id, section: r.section, section_title: r.section_title, headline: r.headline, gist: r.gist,
      keywords: r.keywords, title: r.title, level: r.level, priority: r.priority, risk: r.risk, status: r.status, ts: r.ts, expires_at: r.expires_at }));
    return { key: tp.key, title: tp.title, brief: null, history: [], items, counts: { total: items.length }, age_days: 0, _fallback: true };
  }
  async loadTopicPane(tp, { background = false } = {}) {
    const pane = $('#split-pane'); if (!pane) return;
    if (!tp) { setHTML(pane, this.paneEmpty()); return; }
    const key = topicSel(tp.key);
    const mySeq = ++this.seq;
    const cached = this.detail.get(key);
    if (cached && !background) this.paintTopicPane(tp, cached.data);
    else if (!background) setHTML(pane, this.paneSkeleton(true));
    try {
      const rec = await fetchTopic(tp.key, { force: background, maxAge: background ? 0 : 5000 });
      if (mySeq !== this.seq) return;
      const data = rec.data || this.topicFallback(tp);
      const hash = JSON.stringify(data);
      if (cached && cached.hash === hash) return;
      this.detail.set(key, { data, hash });
      this.paintTopicPane(tp, data);
    } catch (e) {
      if (mySeq !== this.seq) return;
      setHTML(pane, errorState(e.message, { retry: false }));
    }
  }
  paintTopicPane(tp, data) {
    const pane = $('#split-pane'); const key = topicSel(tp.key);
    if (!pane || this.sel !== key) return;
    const snap = pane.dataset.key === key ? snapshotUI(pane) : null;
    const back = btn('返回列表', { act: 'split-back', icon: 'arrow-left', size: 'sm', variant: 'ghost', cls: 'only-narrow' });
    setHTML(pane, h`<div class="sp-pane-in">${back}${topicReader(data, { view: S.view, hasItem: (k) => !!this.find(k) })}</div>`);
    pane.dataset.key = key;
    if (snap) restoreUI(pane, snap); else pane.scrollTop = 0;
    mountTopicReader(pane);
  }
  /** Reload whatever the pane shows without a loading state (SSE refresh, a finished brief). */
  reloadPane() {
    if (!this.sel) return Promise.resolve();
    if (isTopicKey(this.sel)) return this.loadTopicPane(this.findTopic(this.sel), { background: true });
    const it = this.find(this.sel);
    return it ? this.loadPane(it, { background: true }) : Promise.resolve();
  }

  /* ---------------- data ---------------- */
  adopt(r) { this.items = r.items; this.extra = r.extra; this.topics = r.topics || []; }
  async load() {
    const r = await this.cfg.fetch();
    this.adopt(r);
    const want = S.params.get('sel');
    if (want && this.exists(want)) this.sel = want;
    else if (want) this.sel = null;
    else if (this.sel && !this.exists(this.sel)) this.sel = null;
    return r;
  }
  mount() {
    if (this.sel) {
      if (isTopicKey(this.sel)) { const tp = this.findTopic(this.sel); if (tp) { touchTopic(tp.key); this.loadTopicPane(tp); } }
      else { const it = this.find(this.sel); if (it) { markRead(it.key, it.stamp); this.loadPane(it); this.syncUnread(it); } }
    } else if (window.matchMedia('(min-width: 1100px)').matches && this.cfg.autoSelect && this.visible().length) this.select(this.visibleKeys()[0], { replace: true });
    this.syncActive();
    if (this.focusList) { this.focusList = false; this.focusTopics(); }
  }
  /** After the selected topic / item vanished: prefer the next thing of the same kind, then anything near the old spot. */
  successor(prevKeys, prevSel) {
    const keys = this.visibleKeys(); const set = new Set(keys);
    const idx = prevKeys.indexOf(prevSel);
    const isTopic = isTopicKey(prevSel);
    const pick = (list) => list.find((k) => set.has(k));
    const after = prevKeys.slice(idx + 1); const before = prevKeys.slice(0, Math.max(0, idx)).reverse();
    const same = (k) => isTopicKey(k) === isTopic;
    return pick(after.filter(same)) || pick(before.filter(same)) || pick(after) || pick(before) || keys[0] || null;
  }
  /** SSE refresh: reload the list, keep position, refresh the open reader quietly. */
  async refresh() {
    const prevKeys = this.visibleKeys(); const prevSel = this.sel;
    const r = await this.cfg.fetch();
    this.adopt(r);
    if (prevSel && !this.exists(prevSel)) {
      const nxt = this.successor(prevKeys, prevSel);
      this.paintToolbar(); this.sel = nxt; this.paintList();
      if (nxt) await this.select(nxt, { replace: true }); else { setParams({ sel: null }, { replace: true }); await this.loadPane(null); }
      return;
    }
    this.paintToolbar(); this.paintList();
    await this.reloadPane();
    if (prevSel && !isTopicKey(prevSel)) mountRecap($('#split-pane'), { force: true });   // the recap of the open item follows the stream
  }

  /** The URL's sel param changed (browser back/forward, a link): follow it. */
  onParams() {
    const want = S.params.get('sel') || null;
    if (want === this.sel) return;
    this.sel = want && this.exists(want) ? want : null;
    this.syncActive();
    if (this.sel && isTopicKey(this.sel)) this.loadTopicPane(this.findTopic(this.sel)); else this.loadPane(this.current());
  }

  /* ---------------- keyboard ---------------- */
  move(delta) {
    const keys = this.visibleKeys(); if (!keys.length) return;
    const cur = this.anchor(keys);
    let i = cur ? keys.indexOf(cur) : -1;
    i = Math.max(0, Math.min(keys.length - 1, i + delta));
    this.select(keys[i], { replace: true });
  }
  /** `[` / `]`: previous / next topic header. */
  moveTopic(delta) {
    if (this.mode() !== 'topic') return false;
    const heads = this.visibleKeys().filter(isTopicKey); if (!heads.length) return false;
    const it = this.sel && !isTopicKey(this.sel) ? this.find(this.sel) : null;
    const cur = isTopicKey(this.sel) ? this.sel : it ? topicSel(it.topic_key) : null;
    const i = cur ? heads.indexOf(cur) : -1;
    const from = i < 0 ? (delta > 0 ? -1 : heads.length) : i;
    this.select(heads[Math.max(0, Math.min(heads.length - 1, from + delta))], { replace: true });
    return true;
  }
  /** What to open after `key` was cleared: the next item (then the previous one), only then a topic header. */
  neighbourOf(key) {
    const keys = this.visibleKeys(); const i = keys.indexOf(key);
    const items = keys.map((k, n) => [k, n]).filter(([k]) => !isTopicKey(k));
    const hit = items.find(([, n]) => n > i) || [...items].reverse().find(([, n]) => n < i);
    return hit ? hit[0] : (keys[i + 1] || keys[i - 1] || null);
  }
  /** `g t`: focus the list and (on a wide screen) open the current / first topic. */
  focusTopics() {
    $('#split-list')?.focus();
    if (window.matchMedia('(min-width: 1100px)').matches) this.moveTopic(0);
  }
  current() { return this.sel && !isTopicKey(this.sel) ? this.find(this.sel) : null; }
  currentTopic() { return this.sel && isTopicKey(this.sel) ? this.findTopic(this.sel) : null; }
}

/* ------------------------------------------------------------------ shared actions (the active split view) */
let active = null;
export const setActiveSplit = (s) => { active = s; };
const wide = () => window.matchMedia('(min-width: 1100px)').matches;

Split.prototype.removeLocal = function removeLocal(key) {
  const keys = this.visibleKeys(); const idx = keys.indexOf(key);
  const item = this.find(key);
  const next = this.neighbourOf(key);
  this.items = this.items.filter((i) => i.key !== key);
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
ACTS['split-up'] = (el) => { active?.select(el.dataset.key, { replace: false }); };   // narrow screens: from an item back to its topic
ACTS['topic-toggle'] = (el, e) => { e?.stopPropagation?.(); active?.toggleTopic(el.dataset.topic); };
ACTS['topic-collapse-all'] = () => { if (active) active.setAllClosed(active.allTopics().some((t) => !active.isClosed(t.key))); };
ACTS['split-group'] = (el) => { if (!active) return; pref.set(`${active.cfg.id}.group`, el.dataset.value); active.paintToolbar(); active.paintList(); if (!active.sel) active.loadPane(null); };
// opening an item from a topic reader: in this view when it lives here, otherwise in the inbox
ACTS['open-item'] = (el) => { if (el.dataset.view === S.view) ACTS['split-select'](el); else go(el.dataset.view || 'inbox', { sel: el.dataset.key }); };
// a regenerated brief landed (or the 20 s look): reload the topic pane without a loading state
if (typeof window !== 'undefined') window.addEventListener('awos:topic-updated', (e) => { if (active && active.sel === topicSel(e.detail?.key)) active.reloadPane(); });
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
  const tp = ctl.currentTopic();
  const k = e.key;
  const pane = $('#split-pane');
  const inPane = !!e.target.closest?.('#split-pane');
  if (k === 'j' || k === 'ArrowDown') { ctl.move(1); return true; }
  if (k === 'k' || k === 'ArrowUp') { ctl.move(-1); return true; }
  if ((k === '[' || k === ']') && ctl.mode() === 'topic') { e.preventDefault(); ctl.moveTopic(k === ']' ? 1 : -1); return true; }
  if ((k === 'ArrowLeft' || k === 'ArrowRight') && ctl.mode() === 'topic' && !inPane && (tp || it)) {
    e.preventDefault();
    if (k === 'ArrowLeft') { if (tp) { if (!ctl.isClosed(tp.key)) ctl.toggleTopic(tp.key, true); } else ctl.select(topicSel(it.topic_key), { replace: true }); }   // on an item: up to its topic
    else if (tp && ctl.isClosed(tp.key)) ctl.toggleTopic(tp.key, false);
    return true;
  }
  if (k === 'Enter' && (it || tp) && !inPane) {
    const target = $('textarea[name=note]', pane) || $('.opt.rec', pane) || $('.rd-actions .btn-default', pane) || $('.need .opt, .need .btn-default', pane) || pane;
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
