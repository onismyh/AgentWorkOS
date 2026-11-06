// Split reading view shared by 收件箱 and Outbox: grouped list on the left, reader on the right (Linear / Superhuman style).
import { h, raw, setHTML, $, $$, enc } from '../core/html.js';
import { api } from '../core/api.js';
import { ACTS, CHANGES, INPUTS } from '../core/acts.js';
import { S, cacheTask, cacheApproval, CACHE } from '../core/state.js';
import { isUnread, markRead, markUnread, stampOf, pref, touchTopic } from '../core/store.js';
import { setParams, parseHash, go } from '../core/router.js';
import { when } from '../core/format.js';
import { icon } from '../ui/icons.js';
import { btn, iconBtn, empty, kbd, skeletonList, errorState, seg, projectChip } from '../ui/components.js';
import { taskReader, approvalReader, mountReader, taskActionList } from '../ui/reader.js';
import { toast } from '../ui/overlay.js';
import { snapshotUI, restoreUI } from '../core/ui-state.js';
import { META, recommendedKey, isRejectish } from '../domain/vocab.js';
import { taskAction, taskActionUndoable, decideApproval } from '../domain/actions.js';
import { skel } from '../ui/components.js';
import { topicReader, mountTopicReader } from '../ui/topic-reader.js';
import { mountRecap, fetchTopic } from '../ui/recap.js';
import { projectReader } from '../ui/project-reader.js';
import { ageWord, groupByTopic, isTopicKey, oldTone, topicFromSel, topicSel } from '../domain/topics.js';
import { ASK, askVerb, needsText, oneLineState, plainHeadline, rowNote, urgencyOf } from '../domain/readable.js';
import { NO_PROJECT, PROJ, groupByProject, isProjectKey, loadProject, noteTopicProjects, projectFromSel, projectOfTopic, projectSel, projectSlot } from '../domain/projects.js';

const isHeadKey = (k) => isTopicKey(k) || isProjectKey(k);   // a group header (topic / project), as opposed to an item row
const pgKey = (k) => `pj:${k}`;                              // collapse-state key of a project group

const SECTION_ICON = { decision: 'scale', approval: 'shield-alert', review: 'circle-dot', handoff: 'hand', suggested: 'sparkles', failed: 'circle-x', pending: 'shield-alert', history: 'check-check' };

/** `bare` leaves the verb out (the 按类型 group header above already says it). One list row = one plain-language line ("要你决定：…") + one muted line (the AI's recommendation or the summary) + when / how urgent. */
export function rowHTML(it, selKey, { inTopic = false, showTopic = false, project = null, bare = false } = {}) {
  const unread = isUnread(it.key, it.stamp);
  const sel = it.key === selKey;
  const urg = urgencyOf(it);
  const note = rowNote(it);
  const where = showTopic ? (project?.name || it.topic_title || '') : '';
  const tone = (ASK[it.section] || {}).tone || 'muted';
  return h`<a class="srow${sel ? ' sel' : ''}${unread ? ' unread' : ''}${inTopic ? ' in-topic' : ''}" role="option" id="row-${it.dom}" aria-selected="${sel}" data-key="${it.key}" data-act="split-select" href="#/${S.view}?sel=${enc(it.key)}">
    <span class="sr-dot" ${unread ? raw('data-tip="未读"') : ''}></span>
    <span class="sr-main">
      <span class="sr-title">${bare ? '' : h`<b class="sr-ask tone-${tone}">${askVerb(it, META.apvLabel)}</b>`}${plainHeadline(it.section, it.headline)}</span>
      ${note || where ? h`<span class="sr-note">${note}${where ? h`<span class="sr-where">${note ? ' · ' : ''}${where}</span>` : ''}</span>` : ''}
    </span>
    <span class="sr-side"><span class="sr-w">${when(it.ts, { short: true })}</span>${urg ? h`<span class="sr-flag tone-${urg.tone}">${urg.text}</span>` : ''}</span></a>`;
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
    const q = this.f.q.toLowerCase(); const pf = p.get('project') || '';
    return this.items.filter((it) => (!sec || it.section === sec) && (!kw || it.keywords.includes(kw)) && (!pf || (this.projectOf(it.topic_key)?.key || NO_PROJECT) === pf)
      && (!this.f.level || String(it.level) === this.f.level) && (!this.f.unread || isUnread(it.key, it.stamp))
      && (!q || `${it.headline} ${it.gist} ${it.keywords.join(' ')} ${it.title} ${it.topic_title || ''}`.toLowerCase().includes(q)));
  }
  grouped(list) {
    const order = this.cfg.sections.map((s) => s.key);
    const by = new Map(order.map((k) => [k, []]));
    for (const it of list) { if (!by.has(it.section)) by.set(it.section, []); by.get(it.section).push(it); }
    return [...by.entries()].filter(([, v]) => v.length);
  }

  /* ---------------- topics / projects ---------------- */
  hasTopics() { return this.cfg.topicMode !== false && this.items.some((i) => i.topic_key); }
  /** Do the server's topic summaries carry `project` at all? (older servers do not: 按项目 then falls back to 按话题.) */
  hasProjects() { return this.cfg.projectMode !== false && this.topics.some((t) => t && 'project' in t); }
  /** 'project' (default: groups are projects, topics are sub-headers), 'topic' (one group per topic) or 'type' (the old section groups). */
  mode() {
    if (!this.hasTopics()) return 'type';
    const m = pref.get(`${this.cfg.id}.group`, 'project');
    if (m === 'type') return 'type';
    return m === 'project' && this.hasProjects() ? 'project' : 'topic';
  }
  topicMeta() {
    if (this._tm?.src !== this.topics) this._tm = { src: this.topics, map: new Map((this.topics || []).map((t) => [t.key, t])) };
    return this._tm.map;
  }
  /** The project of a topic: the inbox summary's `project` when it has one (null = unlinked), else what was seen elsewhere. */
  projectOf(topicKey) {
    if (!topicKey) return null;
    const m = this.topicMeta().get(topicKey);
    return m && 'project' in m ? (m.project || null) : projectOfTopic(topicKey);
  }
  projectName(key) {
    if (key === NO_PROJECT) return '未关联项目';
    return (this.topics.find((t) => t.project?.key === key)?.project?.name) || PROJ.byKey.get(key)?.name || key;
  }
  /** Topics over ALL items (not just the filtered ones): the reader of a topic always shows its whole state. */
  allTopics() {
    if (this._tg?.items !== this.items || this._tg?.topics !== this.topics) this._tg = { items: this.items, topics: this.topics, list: groupByTopic(this.items, this.topics) };
    return this._tg.list;
  }
  allProjects() {
    const topics = this.allTopics();
    if (this._pg?.topics !== topics) this._pg = { topics, list: groupByProject(topics) };
    return this._pg.list;
  }
  findTopic(sel) { const k = topicFromSel(sel); return this.allTopics().find((t) => t.key === k) || null; }
  findProject(sel) { const k = projectFromSel(sel); return k === NO_PROJECT ? null : this.allProjects().find((p) => p.key === k) || null; }
  isClosed(key) { return !!this.collapsed[key]; }
  saveClosed() { pref.set(`${this.cfg.id}.topicClosed`, this.collapsed); }
  /** Everything that can be collapsed: each topic, plus (按项目) the groups that hold several topics and 未关联项目. */
  collapsibleKeys() {
    const ks = this.allTopics().map((t) => t.key);
    if (this.mode() === 'project') for (const pg of this.allProjects()) if (pg.key === NO_PROJECT || pg.topics.length > 1) ks.push(pgKey(pg.key));
    return ks;
  }
  toggleTopic(key, closed = !this.isClosed(key)) {
    if (closed) this.collapsed[key] = true; else delete this.collapsed[key];
    this.saveClosed(); this.paintList(); this.paintToolbar();
  }
  setAllClosed(closed) {
    this.collapsed = {};
    if (closed) for (const k of this.collapsibleKeys()) this.collapsed[k] = true;
    this.saveClosed(); this.paintList(); this.paintToolbar();
  }
  /** Open the groups that hold `sel`, so a selection never sits inside a collapsed group. */
  reveal(sel = this.sel) {
    if (!sel || this.mode() === 'type' || isProjectKey(sel)) return false;
    const it = isTopicKey(sel) ? null : this.find(sel);
    const tkey = it ? it.topic_key : topicFromSel(sel);
    const open = [it ? tkey : null, this.mode() === 'project' ? pgKey(this.projectOf(tkey)?.key || NO_PROJECT) : null];
    let changed = false;
    for (const k of open) if (k && this.collapsed[k]) { delete this.collapsed[k]; changed = true; }
    if (changed) { this.saveClosed(); this.paintList(); this.paintToolbar(); }
    return changed;
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
    const mode = this.mode(); const hasT = this.hasTopics(); const pf = p.get('project') || '';
    const anyOpen = this.collapsibleKeys().some((k) => !this.isClosed(k));
    return h`<div class="search-box">${icon('search', { size: 15 })}<input class="input input-bare" type="search" id="split-q" data-input="split-q" placeholder="搜索标题、摘要、关键词、话题（/）" value="${this.f.q}" autocomplete="off" aria-label="搜索${this.cfg.title}">${kbd('/')}</div>
      <div class="sp-row2 sp-modes">${hasT ? seg([...(this.hasProjects() ? [{ key: 'project', label: '按项目', icon: 'folder' }] : []), { key: 'topic', label: '按话题', icon: 'layers' }, { key: 'type', label: '按类型', icon: 'list' }], mode, { act: 'split-group', key: 'group', label: '分组方式' }) : ''}
        ${hasT && mode !== 'type' ? iconBtn(anyOpen ? 'chevron-up' : 'chevron-down', { act: 'topic-collapse-all', tip: anyOpen ? '折叠全部话题' : '展开全部话题', size: 'icon-sm', variant: 'ghost' }) : ''}<span class="grow"></span>
        ${btn('全部已读', { act: 'split-readall', size: 'sm', variant: 'ghost', icon: 'check-check' })}</div>
      <div class="sp-chips" role="group" aria-label="分区筛选"><button type="button" class="chip-f${!sec ? ' on' : ''}" data-act="split-sec" data-sec="">全部 <b>${this.items.length}</b></button>${this.cfg.sections.filter((s) => counts[s.key]).map((s) => h`<button type="button" class="chip-f${sec === s.key ? ' on' : ''}" data-act="split-sec" data-sec="${s.key}">${s.title} <b>${counts[s.key]}</b></button>`)}</div>
      <div class="sp-row2">${pf ? h`<button type="button" class="chip-f on chip-pj" data-act="split-project-clear" data-tip="清除项目筛选">${icon('folder', { size: 12 })}<span class="chip-pj-n">${this.projectName(pf)}</span>${icon('x', { size: 12 })}</button>` : ''}${kw ? h`<button type="button" class="chip-f on" data-act="split-kw-clear" data-tip="清除关键词筛选">${icon('hash', { size: 12 })}${kw}${icon('x', { size: 12 })}</button>` : ''}
        <label class="check small"><input type="checkbox" data-change="split-unread"${this.f.unread ? raw(' checked') : ''}><span>仅看未读</span></label><span class="grow"></span>
        ${topKw.length || levels.length > 1 ? h`<details class="sp-more"${kw || this.f.level ? raw(' open') : ''}><summary>${icon('filter', { size: 13 })}<span>更多筛选</span></summary><div class="sp-more-b">
          ${topKw.length ? h`<select class="select select-sm" data-change="split-kwsel" aria-label="按关键词筛选"><option value="">全部关键词</option>${topKw.map(([k, n]) => h`<option value="${k}"${k === kw ? raw(' selected') : ''}>${k}${n ? ` (${n})` : ''}</option>`)}</select>` : ''}
          ${levels.length > 1 ? h`<select class="select select-sm" data-change="split-level" aria-label="按自主等级筛选"><option value="">全部自主等级</option>${levels.map((l) => h`<option value="${l}"${this.f.level === String(l) ? raw(' selected') : ''}>L${l}</option>`)}</select>` : ''}</div></details>` : ''}</div>`;
  }
  /** A topic group. `as` = the project it stands for (按项目 with a single topic: the header IS the project and carries its one-line state);
      `sub` = a quiet sub-header of a topic inside a multi-topic project. */
  topicGroupHTML(g, { as = null, sub = false, showProject = false } = {}) {
    const key = topicSel(g.key); const open = !this.isClosed(g.key); const sel = this.sel === key;
    const unread = g.items.some((i) => isUnread(i.key, i.stamp));
    const dom = this.domOf(key);
    const label = as ? as.name : g.title;
    const state = sub ? '' : oneLineState(g.state_now || as?.summary || '');
    const late = !sub && oldTone(g.oldest_ts) !== 'muted' ? { tone: oldTone(g.oldest_ts), text: `已等 ${ageWord(g.oldest_ts)}` } : null;
    return h`<div class="tgroup${open ? '' : ' closed'}${sub ? ' sub' : ''}${as ? ` by-project pj-c${projectSlot(as.key)}` : ''}" role="group" aria-label="${label}" data-topic="${g.key}">
      <div class="tg-row${sel ? ' sel' : ''}${unread ? ' unread' : ''}">
        <button type="button" class="tg-tog" tabindex="-1" data-act="topic-toggle" data-topic="${g.key}" aria-expanded="${open}" aria-label="${open ? '折叠' : '展开'}${as ? '项目' : '话题'}：${label}">${icon('chevron-down', { size: 14 })}</button>
        <a class="tg-h" role="option" id="row-${dom}" aria-selected="${sel}" data-key="${key}" data-act="split-select" href="#/${S.view}?sel=${enc(key)}">
          <span class="tg-l1"><span class="tg-dot${unread ? ' on' : ''}" ${unread ? raw('data-tip="有未读事项"') : ''}></span>${as ? h`<span class="tg-pic">${icon('folder', { size: 14 })}</span>` : ''}<span class="tg-title">${label}</span><span class="tg-need">${sub ? `${g.counts.total} 项` : needsText(g.counts)}</span>${late ? h`<span class="tg-age tone-${late.tone}">${late.text}</span>` : ''}</span>
          ${state ? h`<span class="tg-state">${state}</span>` : ''}
          ${showProject && g.project ? h`<span class="tg-l2">${projectChip(g.project, { empty: true, cls: 'tg-pj' })}</span>` : ''}
        </a></div>
      ${open ? g.items.map((it) => rowHTML(it, this.sel, { inTopic: true })) : ''}</div>`;
  }
  /** 按项目: a project (header = the project; its topics are quiet sub-headers when there are several) or the last group 未关联项目. */
  projectGroupHTML(pg) {
    if (pg.key !== NO_PROJECT && pg.topics.length === 1) return this.topicGroupHTML(pg.topics[0], { as: pg.project });
    const ck = pgKey(pg.key); const open = !this.isClosed(ck);
    const tog = h`<button type="button" class="tg-tog" tabindex="-1" data-act="topic-toggle" data-topic="${ck}" aria-expanded="${open}" aria-label="${open ? '折叠' : '展开'}：${pg.name}">${icon('chevron-down', { size: 14 })}</button>`;
    const subs = open ? h`<div class="pg-topics">${pg.topics.map((t) => this.topicGroupHTML(t, { sub: true }))}</div>` : '';
    if (pg.key === NO_PROJECT) {
      return h`<div class="tgroup pgroup pg-none${open ? '' : ' closed'}" role="group" aria-label="未关联项目" data-project="${NO_PROJECT}">
        <div class="tg-row pg-row">${tog}<div class="tg-h pg-h"><span class="tg-l1"><span class="tg-pic">${icon('circle-dashed', { size: 14 })}</span><span class="tg-title">未关联项目</span><span class="tg-need">${pg.items.length} 项</span></span>
          <span class="tg-state">还没归到项目里的话题；选中话题后按 P 归类</span></div></div>${subs}</div>`;
    }
    const key = projectSel(pg.key); const sel = this.sel === key;
    const unread = pg.items.some((i) => isUnread(i.key, i.stamp));
    const state = oneLineState(pg.state_now || pg.project?.summary || '');
    const late = oldTone(pg.oldest_ts) !== 'muted' ? { tone: oldTone(pg.oldest_ts), text: `已等 ${ageWord(pg.oldest_ts)}` } : null;
    return h`<div class="tgroup pgroup pj-c${projectSlot(pg.key)}${open ? '' : ' closed'}" role="group" aria-label="${pg.name}" data-project="${pg.key}">
      <div class="tg-row pg-row${sel ? ' sel' : ''}${unread ? ' unread' : ''}">${tog}
        <a class="tg-h" role="option" id="row-${this.domOf(key)}" aria-selected="${sel}" data-key="${key}" data-act="split-select" href="#/${S.view}?sel=${enc(key)}">
          <span class="tg-l1"><span class="tg-dot${unread ? ' on' : ''}" ${unread ? raw('data-tip="有未读事项"') : ''}></span><span class="tg-pic">${icon('folder', { size: 14 })}</span><span class="tg-title">${pg.name}</span><span class="tg-need">${needsText(pg.counts)}</span>${late ? h`<span class="tg-age tone-${late.tone}">${late.text}</span>` : ''}</span>
          ${state ? h`<span class="tg-state">${state}</span>` : ''}
        </a></div>${subs}</div>`;
  }
  listBodyHTML() {
    const vis = this.visible();
    if (!vis.length) {
      const pf = this.params().get('project') || '';
      if (pf && this.items.length && !this.items.some((it) => (this.projectOf(it.topic_key)?.key || NO_PROJECT) === pf)) {
        return empty({ icon: 'folder', title: `${this.projectName(pf)}：没有待处理的事项`, desc: '这个项目的事项都处理完了，或者还没有话题关联到它。', action: btn('清除筛选', { act: 'split-clear', size: 'sm' }) });
      }
      return this.items.length ? empty({ icon: 'search', title: '没有符合条件的事项', desc: '试试清除筛选条件。', action: btn('清除筛选', { act: 'split-clear', size: 'sm' }) })
        : this.cfg.emptyAll();
    }
    const mode = this.mode();
    if (mode === 'project') return h`${groupByProject(groupByTopic(vis, this.topics)).map((pg) => this.projectGroupHTML(pg))}`;
    if (mode === 'topic') return h`${groupByTopic(vis, this.topics).map((g) => this.topicGroupHTML(g, { showProject: this.hasProjects() }))}`;
    return h`${this.grouped(vis).map(([key, rows]) => {
      const s = this.cfg.sections.find((x) => x.key === key) || { title: key };
      return h`<div class="sgroup" role="group" aria-label="${s.title}"><div class="sgroup-h"><span class="sg-i tone-${s.tone || 'muted'}">${icon(SECTION_ICON[key] || 'circle', { size: 13 })}</span><b>${s.title}</b><span class="sg-n">${rows.length}</span>${s.hint ? h`<span class="sg-hint">${s.hint}</span>` : ''}</div>${rows.map((it) => rowHTML(it, this.sel, { showTopic: this.hasTopics(), project: this.projectOf(it.topic_key), bare: it.section !== 'history' }))}</div>`;
    })}`;
  }
  render() {
    return h`<div class="split${this.sel ? ' has-sel' : ''}" id="split"><section class="sp-list" aria-label="${this.cfg.title}列表"><div class="sp-tools">${this.toolbarHTML()}</div><div class="sp-scroll" id="split-list" role="listbox" tabindex="0" aria-label="${this.cfg.title}" data-scroll="split-list" ${this.sel ? h`aria-activedescendant="row-${this.domOf(this.sel)}"` : ''}>${this.listBodyHTML()}</div></section>
      <section class="sp-pane" id="split-pane" aria-label="阅读窗格" tabindex="-1" data-scroll="split-pane">${this.paneEmpty()}</section></div>`;
  }
  domOf(key) { return String(key).replace(/[^a-zA-Z0-9_-]/g, '_'); }
  paneEmpty() {
    const n = this.visible().length;
    const mode = this.mode(); const topic = mode !== 'type';
    return h`<div class="sp-empty">${icon(this.cfg.icon, { size: 28 })}<h3>${n ? (mode === 'project' ? '选择一个项目、话题或事项来阅读' : topic ? '选择一个话题或事项来阅读' : '选择一条来阅读') : `${this.cfg.title}已清空`}</h3><p class="muted">${n ? (topic ? '点击项目或话题标题先看背景和前情提要，再逐项处理；也可以用键盘 J / K 浏览。' : '点击左侧事项，或用键盘 J / K 浏览。所有内容都会显示在这里。') : '新的事项出现时会实时显示。'}</p>
      <div class="sp-keys">${[['J K', '上一条 / 下一条'], ...(topic ? [['[ ]', '上一个 / 下一个话题'], ['← →', '折叠 / 展开'], ['P', '更改话题所属项目']] : []), ['E', '接受 / 批准推荐项'], ['X', '忽略'], ['S', '稍后'], ['R', '返工'], ['?', '全部快捷键']].map(([k, t]) => h`<div>${kbd(k)}<span>${t}</span></div>`)}</div></div>`;
  }

  /* ---------------- selection ---------------- */
  find(key) { return this.items.find((i) => i.key === key); }
  exists(key) { return isProjectKey(key) ? !!this.findProject(key) : isTopicKey(key) ? !!this.findTopic(key) : !!this.find(key); }
  /** Navigable keys in screen order: every group header (project / topic) followed by its items unless collapsed. */
  visibleKeys() {
    const vis = this.visible(); const mode = this.mode();
    const topicKeys = (g) => [topicSel(g.key), ...(this.isClosed(g.key) ? [] : g.items.map((r) => r.key))];
    if (mode === 'project') {
      const out = [];
      for (const pg of groupByProject(groupByTopic(vis, this.topics))) {
        const single = pg.key !== NO_PROJECT && pg.topics.length === 1;   // its header is the topic header
        if (!single && pg.key !== NO_PROJECT) out.push(projectSel(pg.key));
        if (!single && this.isClosed(pgKey(pg.key))) continue;
        for (const g of pg.topics) out.push(...topicKeys(g));
      }
      return out;
    }
    if (mode === 'topic') return groupByTopic(vis, this.topics).flatMap(topicKeys);
    return this.grouped(vis).flatMap(([, rows]) => rows.map((r) => r.key));
  }
  /** The visible group header an item / topic belongs to (its topic header, else - inside a collapsed group - its project header). */
  headerOf(sel, keys = this.visibleKeys()) {
    if (this.mode() === 'type' || !sel) return null;
    const it = isHeadKey(sel) ? null : this.find(sel);
    const tkey = it ? it.topic_key : isTopicKey(sel) ? topicFromSel(sel) : null;
    if (tkey && keys.includes(topicSel(tkey)) && sel !== topicSel(tkey)) return topicSel(tkey);
    const pk = tkey ? this.projectOf(tkey)?.key : null;
    return pk && keys.includes(projectSel(pk)) ? projectSel(pk) : null;
  }
  /** The key to measure j / k from: the selection, or - when its group is collapsed - the header above it. */
  anchor(keys = this.visibleKeys()) {
    if (this.sel && keys.includes(this.sel)) return this.sel;
    return this.headerOf(this.sel, keys);
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
    if (isProjectKey(key)) return this.selectProject(key, { replace, silentPush });
    if (isTopicKey(key)) return this.selectTopic(key, { replace, silentPush });
    const it = key ? this.find(key) : null;
    this.sel = it ? key : null;
    if (it) this.reveal(key);
    if (it) { this.lastIdx = Math.max(0, this.visibleKeys().indexOf(key)); if (markRead(it.key, it.stamp)) this.syncUnread(it); }
    if (!silentPush) setParams({ sel: this.sel }, { replace });
    this.syncActive();
    await this.loadPane(it);
  }
  async selectTopic(key, { replace = true, silentPush = false } = {}) {
    const tp = this.findTopic(key);
    this.sel = tp ? key : null;
    if (tp) this.reveal(key);
    if (tp) { this.lastIdx = Math.max(0, this.visibleKeys().indexOf(key)); touchTopic(tp.key); }
    if (!silentPush) setParams({ sel: this.sel }, { replace });
    this.syncActive();
    await this.loadTopicPane(tp);
  }
  async selectProject(key, { replace = true, silentPush = false } = {}) {
    const pg = this.findProject(key);
    this.sel = pg ? key : null;
    if (pg) this.lastIdx = Math.max(0, this.visibleKeys().indexOf(key));
    if (!silentPush) setParams({ sel: this.sel }, { replace });
    this.syncActive();
    await this.loadProjectPane(pg);
  }
  syncUnread(it) {
    const r = $(`.srow[data-key="${CSS.escape(it.key)}"]`);
    r?.classList.remove('unread'); $('.sr-dot', r)?.removeAttribute('data-tip');
    for (const g of [r?.closest('.tgroup'), r?.closest('.pgroup')]) {
      if (g && !$('.srow.unread', g)) { $('.tg-row', g)?.classList.remove('unread'); $('.tg-dot', g)?.classList.remove('on'); }
    }
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
    const back = this.mode() !== 'type' && it.topic_key
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
    const meta = this.topicMeta().get(tp.key);   // an older server has no `project` at all: then there is no 所属项目 card
    return { key: tp.key, title: tp.title, project: meta && 'project' in meta ? meta.project : undefined, brief: null, history: [], items, counts: { total: items.length }, age_days: 0, _fallback: true };
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
  /** The reader of a project header (several topics): summary, topics, related facts, recent tasks. Falls back to what the list knows. */
  async loadProjectPane(pg, { background = false } = {}) {
    const pane = $('#split-pane'); if (!pane) return;
    if (!pg) { setHTML(pane, this.paneEmpty()); return; }
    const key = projectSel(pg.key);
    const mySeq = ++this.seq;
    const cached = this.detail.get(key);
    if (cached && !background) this.paintProjectPane(pg, cached.data);
    else if (!background) setHTML(pane, this.paneSkeleton(true));
    try {
      const data = (await loadProject(pg.key, { force: background, maxAge: background ? 0 : 5000 })) || { _fallback: true, key: pg.key, name: pg.name, summary: pg.project?.summary || '' };
      if (mySeq !== this.seq) return;
      const hash = JSON.stringify([data, pg.topics.map((t) => [t.key, t.counts, t.state_now])]);
      if (cached && cached.hash === hash) return;
      this.detail.set(key, { data, hash });
      this.paintProjectPane(pg, data);
    } catch (e) {
      if (mySeq !== this.seq) return;
      setHTML(pane, errorState(e.message, { retry: false }));
    }
  }
  paintProjectPane(pg, data) {
    const pane = $('#split-pane'); const key = projectSel(pg.key);
    if (!pane || this.sel !== key) return;
    const snap = pane.dataset.key === key ? snapshotUI(pane) : null;
    const back = btn('返回列表', { act: 'split-back', icon: 'arrow-left', size: 'sm', variant: 'ghost', cls: 'only-narrow' });
    setHTML(pane, h`<div class="sp-pane-in">${back}${projectReader(data, pg, { view: S.view })}</div>`);
    pane.dataset.key = key;
    if (snap) restoreUI(pane, snap); else pane.scrollTop = 0;
  }
  /** Reload whatever the pane shows without a loading state (SSE refresh, a finished brief). */
  reloadPane() {
    if (!this.sel) return Promise.resolve();
    if (isProjectKey(this.sel)) return this.loadProjectPane(this.findProject(this.sel), { background: true });
    if (isTopicKey(this.sel)) return this.loadTopicPane(this.findTopic(this.sel), { background: true });
    const it = this.find(this.sel);
    return it ? this.loadPane(it, { background: true }) : Promise.resolve();
  }

  /* ---------------- data ---------------- */
  adopt(r) { this.items = r.items; this.extra = r.extra; this.topics = r.topics || []; noteTopicProjects(this.topics); }
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
      if (isProjectKey(this.sel)) { const pg = this.findProject(this.sel); if (pg) this.loadProjectPane(pg); }
      else if (isTopicKey(this.sel)) { const tp = this.findTopic(this.sel); if (tp) { touchTopic(tp.key); this.loadTopicPane(tp); } }
      else { const it = this.find(this.sel); if (it) { markRead(it.key, it.stamp); this.loadPane(it); this.syncUnread(it); } }
    } else if (window.matchMedia('(min-width: 1100px)').matches && this.cfg.autoSelect && this.visible().length) this.select(this.visibleKeys()[0], { replace: true });
    this.syncActive();
    if (this.focusList) { this.focusList = false; this.focusTopics(); }
  }
  /** After the selected topic / item vanished: prefer the next thing of the same kind, then anything near the old spot. */
  successor(prevKeys, prevSel) {
    const keys = this.visibleKeys(); const set = new Set(keys);
    const idx = prevKeys.indexOf(prevSel);
    const isHead = isHeadKey(prevSel);
    const pick = (list) => list.find((k) => set.has(k));
    const after = prevKeys.slice(idx + 1); const before = prevKeys.slice(0, Math.max(0, idx)).reverse();
    const same = (k) => isHeadKey(k) === isHead;
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
    if (prevSel && !isHeadKey(prevSel)) mountRecap($('#split-pane'), { force: true });   // the recap of the open item follows the stream
  }

  /** The URL's sel param changed (browser back/forward, a link): follow it. */
  onParams() {
    const want = S.params.get('sel') || null;
    if (want === this.sel) return;
    this.sel = want && this.exists(want) ? want : null;
    if (this.sel) this.reveal(this.sel);
    this.syncActive();
    if (this.sel && isProjectKey(this.sel)) this.loadProjectPane(this.findProject(this.sel));
    else if (this.sel && isTopicKey(this.sel)) this.loadTopicPane(this.findTopic(this.sel)); else this.loadPane(this.current());
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
    if (this.mode() === 'type') return false;
    const keys = this.visibleKeys(); const heads = keys.filter(isHeadKey); if (!heads.length) return false;
    const cur = isHeadKey(this.sel) && heads.includes(this.sel) ? this.sel : this.headerOf(this.sel, keys);
    const i = cur ? heads.indexOf(cur) : -1;
    const from = i < 0 ? (delta > 0 ? -1 : heads.length) : i;
    this.select(heads[Math.max(0, Math.min(heads.length - 1, from + delta))], { replace: true });
    return true;
  }
  /** What to open after `key` was cleared: the next item (then the previous one), only then a topic header. */
  neighbourOf(key) {
    const keys = this.visibleKeys(); const i = keys.indexOf(key);
    const items = keys.map((k, n) => [k, n]).filter(([k]) => !isHeadKey(k));
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
  currentProject() { return this.sel && isProjectKey(this.sel) ? this.findProject(this.sel) : null; }
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
ACTS['topic-collapse-all'] = () => { if (active) active.setAllClosed(active.collapsibleKeys().some((k) => !active.isClosed(k))); };
ACTS['split-group'] = (el) => { if (!active) return; pref.set(`${active.cfg.id}.group`, el.dataset.value); active.paintToolbar(); active.paintList(); if (!active.sel) active.loadPane(null); };
// opening an item from a topic reader: in this view when it lives here, otherwise in the inbox
ACTS['open-item'] = (el) => { if (el.dataset.view === S.view) ACTS['split-select'](el); else go(el.dataset.view || 'inbox', { sel: el.dataset.key }); };
// a regenerated brief landed (or the 20 s look): reload the topic pane without a loading state
if (typeof window !== 'undefined') window.addEventListener('awos:topic-updated', (e) => { if (active && active.sel === topicSel(e.detail?.key)) active.reloadPane(); });
// a topic was re-linked (the list is already refreshed): make sure the selection is not inside a collapsed group
if (typeof window !== 'undefined') window.addEventListener('awos:project-linked', () => { active?.reveal(); });
ACTS['split-sec'] = (el) => setParams({ sec: el.dataset.sec });
ACTS['split-kw-clear'] = () => setParams({ kw: null });
ACTS['split-project-clear'] = () => setParams({ project: null });
ACTS['split-back'] = () => { if (active) { active.sel = null; setParams({ sel: null }, { replace: false }); active.syncActive(); } };
ACTS['split-readall'] = () => { if (!active) return; active.visible().forEach((it) => markRead(it.key, it.stamp)); active.paintList(); toast('已全部标为已读'); };
ACTS['split-clear'] = () => { if (!active) return; active.f = { q: '', level: '', unread: false }; setParams({ sec: null, kw: null, project: null }); };
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
  const pj = ctl.currentProject();
  const k = e.key;
  const pane = $('#split-pane');
  const inPane = !!e.target.closest?.('#split-pane');
  if (k === 'j' || k === 'ArrowDown') { ctl.move(1); return true; }
  if (k === 'k' || k === 'ArrowUp') { ctl.move(-1); return true; }
  if ((k === '[' || k === ']') && ctl.mode() !== 'type') { e.preventDefault(); ctl.moveTopic(k === ']' ? 1 : -1); return true; }
  if ((k === 'ArrowLeft' || k === 'ArrowRight') && ctl.mode() !== 'type' && !inPane && (tp || pj || it)) {
    e.preventDefault();
    const head = tp ? tp.key : pj ? pgKey(pj.key) : null;   // collapse-state key of the selected header
    if (k === 'ArrowLeft') {
      if (head) {
        if (!ctl.isClosed(head)) ctl.toggleTopic(head, true);
        else if (tp) { const up = ctl.headerOf(ctl.sel); if (up) ctl.select(up, { replace: true }); }   // a collapsed topic: up to its project
      } else ctl.select(topicSel(it.topic_key), { replace: true });   // on an item: up to its topic
    } else if (head && ctl.isClosed(head)) ctl.toggleTopic(head, false);
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
  if (k === 'p' && ctl.mode() !== 'type' && (tp || it)) {   // 更改关联: the project of the open topic (or of the open item's topic)
    e.preventDefault();
    const key = tp ? tp.key : it.topic_key; const title = tp ? tp.title : it.topic_title || '';
    Promise.resolve(ACTS['topic-project']?.({ dataset: { key, title } })).catch((err) => toast(err.message || String(err), 'err'));
    return true;
  }
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
