// 前情提要 (recap): the timeline component, the card at the top of every item reader, and the shared topic loader that
// keeps the recap fresh on SSE-driven refreshes (never by polling).
import { h, raw, setHTML, $, $$ } from '../core/html.js';
import { post } from '../core/api.js';
import { ACTS } from '../core/acts.js';
import { absTime, cut, parseTs, when } from '../core/format.js';
import { touchTopic, topicSeenAt } from '../core/store.js';
import { go, closeDrawer } from '../core/router.js';
import { icon } from './icons.js';
import { btn, empty, skel } from './components.js';
import { toast } from './overlay.js';
import { KIND_VIEW, OLD_MS, GEN, cachedTopic, dropTopicCache, loadTopic, normalizeEntries, topicSel } from '../domain/topics.js';

const GIVE_UP_MS = 4 * 60 * 1000;     // a regeneration normally takes 1-3 minutes; stop showing the spinner after this

/* ------------------------------------------------------------------ timeline */
function entryHTML(e) {
  const k = KIND_VIEW[e.kind];
  const t = parseTs(e.ts);
  return h`<li class="rc-i${k ? ` rc-${e.kind}` : ' rc-plain'}"><span class="rc-n tone-${k ? k.tone : 'muted'}">${k ? icon(k.icon, { size: 12 }) : ''}</span>
    <div class="rc-b"><div class="rc-h">${k ? h`<span class="rc-k">${k.label}</span>` : ''}${t ? h`<time class="rc-t" datetime="${e.ts}">${absTime(e.ts)}</time><span class="rc-dot" aria-hidden="true">·</span>${when(e.ts)}` : ''}</div>
      <p class="rc-x">${e.text}</p></div></li>`;
}
/** Vertical rail with one marker per entry, oldest to newest. `entries` may be raw (the list is normalised here). */
export function recapTimeline(entries, { emptyTitle = '这个话题没有历史记录', cls = '' } = {}) {
  const list = normalizeEntries(entries);
  if (!list.length) return empty({ icon: 'history', title: emptyTitle });
  return h`<ol class="rc-tl${cls ? ` ${cls}` : ''}">${list.map(entryHTML)}</ol>`;
}

/* ------------------------------------------------------------------ topic loading (shared by recap cards and topic readers) */
/** Load a topic; also notices when a regeneration the user asked for has landed (toast once, clear the spinner). */
export async function fetchTopic(key, opts = {}) {
  const rec = await loadTopic(key, opts);
  const g = GEN.get(key);
  const at = rec.data?.brief?.generated_at;
  if (g && at && String(at) > String(g.baseline || '')) { GEN.delete(key); paintGenButtons(key); toast('前情提要已更新'); }
  return rec;
}

/* ------------------------------------------------------------------ regenerate (POST, then wait for the SSE-driven refresh) */
const genBtnHTML = (key, { label = '重新生成提要', size = 'sm', variant = 'ghost' } = {}) => (GEN.has(key)
  ? btn('生成中…', { act: 'topic-refresh', data: { key }, size, variant, icon: 'loader', disabled: true, cls: 'spin gen-btn' })
  : btn(label, { act: 'topic-refresh', data: { key }, size, variant, icon: 'refresh', cls: 'gen-btn' }));
export { genBtnHTML };

function paintGenButtons(key) {
  $$('[data-act="topic-refresh"]').forEach((b) => {
    if (b.dataset.key !== key) return;
    const on = GEN.has(key);
    b.disabled = on;
    b.classList.toggle('spin', on);
    const label = $('.btn-label', b);
    if (label) label.textContent = on ? '生成中…' : (b.dataset.label || '重新生成提要');
    const ic = $('.ic', b);
    if (ic) ic.style.animation = '';
  });
  $$(`[data-gen-note="${CSS.escape(key)}"]`).forEach((n) => { n.hidden = !GEN.has(key); });
}

ACTS['topic-refresh'] = async (el) => {
  const key = el.dataset.key;
  if (!key || GEN.has(key)) return;
  GEN.set(key, { since: Date.now(), baseline: cachedTopic(key)?.data?.brief?.generated_at || '' });
  paintGenButtons(key);
  try {
    await post(`/topics/${encodeURIComponent(key)}/refresh`);
  } catch (e) { GEN.delete(key); paintGenButtons(key); throw e; }
  toast('已开始重新生成，通常需要 1–3 分钟，完成后会自动更新');
  // one look after 20 s (cheap runs finish quickly), then only the SSE stream or the give-up timer touches it again
  setTimeout(() => { if (GEN.has(key)) refreshTopicViews(key); }, 20000);
  setTimeout(() => { if (GEN.has(key)) { GEN.delete(key); paintGenButtons(key); refreshTopicViews(key); } }, GIVE_UP_MS);
};

/** Re-fetch one topic and repaint every recap card / topic reader showing it. */
export function refreshTopicViews(key) {
  dropTopicCache(key);
  mountRecap(document, { force: true, only: key });
  window.dispatchEvent(new CustomEvent('awos:topic-updated', { detail: { key } }));
}

/* ------------------------------------------------------------------ the recap card at the top of an item reader */
const FIRST = new Set();   // card ids already given their automatic open / seen treatment in this page session

/** Expanded by default when the item is older than 24 h or the viewer has not opened this topic for more than 24 h. */
export function defaultOpen({ ts, topicKey }, now = Date.now()) {
  const t = parseTs(ts);
  if (t && now - t.getTime() > OLD_MS) return true;
  const seen = topicSeenAt(topicKey);
  return !seen || now - seen > OLD_MS;
}

export function recapCard({ id, topic, ts, extraIds = [] }) {
  if (!topic?.key) return raw('');
  const open = defaultOpen({ ts, topicKey: topic.key });
  return h`<details class="recap-card" id="recap-${id}" data-lazy="recap" data-topic="${topic.key}" data-id="${id}" data-extra="${extraIds.join(',')}"${open ? raw(' open') : ''}>
    <summary data-act="recap-toggle"><span class="rc-ic">${icon('history', { size: 15 })}</span><span class="rc-title">前情提要</span>
      <button type="button" class="rc-topic" data-act="open-topic" data-key="${topic.key}" data-tip="查看这个话题的全部事项">${icon('layers', { size: 12 })}<span>${topic.title}</span></button>
      <span class="rc-line" data-rc-line></span>${icon('chevron-down', { size: 15, cls: 'rd-chev' })}</summary>
    <div class="rc-body" data-rc-body><div class="rc-skel" aria-busy="true" aria-label="正在加载前情提要">${skel('38%', 11)}${skel('94%', 13)}${skel('82%', 13)}${skel('90%', 11)}${skel('70%', 11)}</div></div></details>`;
}

ACTS['recap-toggle'] = (el) => { const d = el.closest('details'); if (d) { d.open = !d.open; d.dataset.touched = '1'; } };
ACTS['open-topic'] = (el, e) => {
  e?.stopPropagation?.();
  closeDrawer();
  go('inbox', { sel: topicSel(el.dataset.key) });
};
ACTS['recap-retry'] = (el) => { const card = el.closest('[data-lazy="recap"]'); if (card) fillRecap(card, { force: true }); };

function whyNow(ask) {
  if (!ask || !(ask.question || ask.why_now || ask.stakes)) return '';
  return h`<section class="rc-why"><span class="rc-why-ic">${icon('hourglass', { size: 15 })}</span><div class="rc-why-b"><h4>为什么现在找你</h4>
    ${ask.question ? h`<p class="rc-q">${ask.question}</p>` : ''}${ask.why_now ? h`<p>${ask.why_now}</p>` : ''}${ask.stakes ? h`<p class="rc-stakes"><b>利害</b>${ask.stakes}</p>` : ''}</div></section>`;
}

/** Note + action under the recap: "提要由 AI 生成于 …，可能已过期" / "尚未生成". */
export function briefNote(key, brief, { inline = false } = {}) {
  const gen = GEN.has(key);
  const genNote = h`<span class="rc-gen" data-gen-note="${key}"${gen ? '' : raw(' hidden')}>${icon('loader', { size: 12, cls: 'spin' })}正在重新生成，完成后会自动更新…</span>`;
  if (!brief) return h`<p class="rc-note${inline ? ' inline' : ''}">${icon('sparkles', { size: 13 })}<span>还没有 AI 生成的提要，下面是系统记录的经过。</span>${genBtnHTML(key, { label: '生成提要' })}${genNote}</p>`;
  if (!brief.stale && !gen) return '';
  return h`<p class="rc-note${inline ? ' inline' : ''}">${icon('clock', { size: 13 })}<span>提要由 AI 生成于 ${absTime(brief.generated_at, true)}，可能已过期。</span>${genBtnHTML(key)}${genNote}</p>`;
}

function bodyHTML(el, data) {
  const brief = data.brief || null;
  const hist = normalizeEntries(data.history);
  const recap = brief ? normalizeEntries(brief.recap) : [];
  const entries = recap.length ? recap : hist;
  const shown = entries.slice(-5);
  const earlier = entries.length - shown.length;
  const stateNow = brief?.state_now || '';
  const asks = brief?.asks || [];
  const ids = [el.dataset.id, ...String(el.dataset.extra || '').split(',').filter(Boolean)];
  const ask = ids.map((i) => asks.find((a) => a.id === i)).find(Boolean) || null;
  const fullOpen = !!$('details.rc-full[open]', el);
  const fullCount = hist.length;
  const canFull = fullCount > 0 && (earlier > 0 || (recap.length > 0 && fullCount > 0) || fullCount > shown.length);
  return h`${stateNow ? h`<section class="rc-now"><h4>现状</h4><p>${stateNow}</p></section>` : ''}
    <section class="rc-sec"><h4>来龙去脉${earlier > 0 ? h`<small>最近 ${shown.length} 条 · 更早还有 ${earlier} 条</small>` : ''}</h4>${recapTimeline(shown, { cls: 'compact' })}</section>
    ${whyNow(ask)}
    ${canFull ? h`<details class="rc-full" id="recapfull-${el.dataset.id}"${fullOpen ? raw(' open') : ''}><summary>${icon('chevron-right', { size: 13, cls: 'rc-fch' })}<span>查看完整经过</span><small>${fullCount} 条记录</small></summary><div class="rc-full-b">${recapTimeline(hist, { cls: 'compact' })}</div></details>` : ''}
    ${briefNote(data.key, brief, { inline: true })}`;
}

function paintRecap(el, data) {
  const body = $('[data-rc-body]', el);
  if (!body) return;
  setHTML(body, bodyHTML(el, data));
  const line = $('[data-rc-line]', el);
  const brief = data.brief;
  if (line) line.textContent = cut(brief?.state_now || (data.history || []).slice(-1)[0]?.text || '', 96);
  el.dataset.painted = '1';
  if (!FIRST.has(el.id)) {
    FIRST.add(el.id);
    if (!el.open && !el.dataset.touched && Number(data.age_days) > 1) el.open = true;   // the topic itself is old: give the background
    touchTopic(el.dataset.topic);
  }
}

async function fillRecap(el, { force = false } = {}) {
  const key = el.dataset.topic;
  const cached = cachedTopic(key);
  if (cached?.data && !el.dataset.painted) paintRecap(el, cached.data);
  if (cached && !force && Date.now() - cached.at < 15000) return;
  try {
    const rec = await fetchTopic(key, { force, maxAge: force ? 0 : 15000 });
    if (!el.isConnected) return;
    if (rec.missing) { el.hidden = true; return; }   // older server: no recap for this item
    if (rec.changed || !el.dataset.painted) paintRecap(el, rec.data);
  } catch (e) {
    if (!el.isConnected || el.dataset.painted) return;
    setHTML($('[data-rc-body]', el), h`<div class="rc-err"><span>${icon('circle-alert', { size: 14 })}无法加载前情提要：${e.message}</span>${btn('重试', { act: 'recap-retry', size: 'sm', icon: 'refresh' })}</div>`);
  }
}

/** Fill every recap card under `root`. `force` skips the freshness window (used on SSE-driven refreshes). */
export function mountRecap(root = document, { force = false, only = null } = {}) {
  $$('[data-lazy="recap"]', root).forEach((el) => { if (!only || el.dataset.topic === only) fillRecap(el, { force }); });
}
