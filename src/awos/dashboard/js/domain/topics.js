// Topics: the things the user actually cares about. Several tasks / approvals belong to one topic and share ONE recap
// (前情提要) instead of N separate threads. Pure helpers + the (cached, de-duplicated) loader for /api/topics/{key}.
import { api, ApiError } from '../core/api.js';
import { enc } from '../core/html.js';
import { parseTs } from '../core/format.js';
import { catLabel } from './vocab.js';
import { sortItems } from './readable.js';

/** The open-item sections of the inbox feed, in urgency order, with their short labels (决定 2 · 批准 1 · 验收 1). */
export const SECTIONS = [
  { key: 'decision', label: '决定', title: '需你决定', tone: 'warn', icon: 'scale' },
  { key: 'approval', label: '批准', title: '需你批准', tone: 'warn', icon: 'shield-alert' },
  { key: 'review', label: '验收', title: '待验收', tone: 'warn', icon: 'circle-dot' },
  { key: 'handoff', label: '接手', title: '待接手', tone: 'warn', icon: 'hand' },
  { key: 'suggested', label: '建议', title: 'AI 的新建议', tone: 'brand', icon: 'sparkles' },
  { key: 'failed', label: '失败', title: '失败', tone: 'crit', icon: 'circle-x' },
];
export const SECTION_VIEW = Object.fromEntries(SECTIONS.map((s) => [s.key, s]));

/** What a recap / history entry did, as labelled markers on the timeline. */
export const KIND_VIEW = {
  found: { label: '发现', icon: 'search', tone: 'brand' },
  ai: { label: 'AI 做了', icon: 'bot', tone: 'muted' },
  user: { label: '你决定了', icon: 'user', tone: 'user' },
  deliverable: { label: '交付', icon: 'package', tone: 'good' },
  system: { label: '系统', icon: 'cpu', tone: 'muted' },
};

const DAY = 86400000;
export const OLD_MS = DAY;                    // "a while ago": the recap opens by default beyond this

/* ------------------------------------------------------------------ keys / titles */
export const isTopicKey = (k) => typeof k === 'string' && k.startsWith('topic:');
export const topicSel = (key) => `topic:${key}`;
export const topicFromSel = (sel) => String(sel).slice(6);

/** Server values win; older servers (no topic fields) fall back to project / category so grouping still works. */
export function topicKeyOf(it) {
  if (it?.topic_key) return it.topic_key;
  if (it?.project) return `p:${it.project}`;
  return `c:${it?.category || 'other'}`;
}
export function topicTitleOf(it) {
  if (it?.topic_title) return it.topic_title;
  if (it?.project) return String(it.project);
  const c = it?.category;
  return c && c !== 'other' ? catLabel(c) : '其他';
}

/* ------------------------------------------------------------------ counts / age */
export function countSections(items) {
  const c = { total: 0 };
  for (const s of SECTIONS) c[s.key] = 0;
  for (const it of items) { c[it.section] = (c[it.section] || 0) + 1; c.total += 1; }
  return c;
}
/** "决定 2 · 批准 1 · 验收 1" (plain text, for aria labels / tooltips). */
export const countsText = (c) => SECTIONS.filter((s) => c[s.key]).map((s) => `${s.label} ${c[s.key]}`).join(' · ');

const tsMs = (s) => parseTs(s)?.getTime() ?? null;
export const ageMs = (ts, now = Date.now()) => { const t = tsMs(ts); return t == null ? 0 : Math.max(0, now - t); };
/** Whole-day-ish label for the oldest open item: 刚刚出现 / 3 小时 / 3 天. */
export function ageWord(ts, now = Date.now()) {
  const t = tsMs(ts);
  if (t == null) return '';
  const a = Math.max(0, now - t) / 1000;
  if (a < 3600) return a < 90 ? '刚刚' : `${Math.round(a / 60)} 分钟`;
  if (a < 86400) return `${Math.round(a / 3600)} 小时`;
  return `${Math.round(a / 86400)} 天`;
}
export const oldTone = (ts, now = Date.now()) => (ageMs(ts, now) > 3 * DAY ? 'crit' : ageMs(ts, now) > DAY ? 'warn' : 'muted');

/* ------------------------------------------------------------------ grouping */
const minTs = (list) => list.map((x) => x.ts).filter(Boolean).sort()[0] || null;
const maxTs = (list) => list.map((x) => x.ts).filter(Boolean).sort().slice(-1)[0] || null;

/** Topics with pending decisions first, then pending approvals, then the longest-waiting. Mirrors the server order. */
export const byUrgency = (a, b) => (Number(b.counts.decision > 0) - Number(a.counts.decision > 0))
  || (Number(b.counts.approval > 0) - Number(a.counts.approval > 0))
  || String(a.oldest_ts || '9999').localeCompare(String(b.oldest_ts || '9999'));

/**
 * Group items (anything with topic_key / topic_title / section / ts) by topic.
 * `serverTopics` (from /api/inbox) supply the order, `state_now` and brief flags; the counts are always recomputed from
 * the items given, so a filtered list shows matching numbers.
 */
export function groupByTopic(items, serverTopics = []) {
  const by = new Map();
  for (const it of items) {
    const key = topicKeyOf(it);
    let g = by.get(key);
    if (!g) by.set(key, g = { key, title: topicTitleOf(it), items: [] });
    g.items.push(it);
  }
  const meta = new Map((serverTopics || []).map((t, i) => [t.key, { ...t, rank: i }]));
  const out = [...by.values()].map((g) => {
    const m = meta.get(g.key) || {};
    return {
      key: g.key, title: m.title || g.title, items: sortItems(g.items), counts: countSections(g.items), rank: m.rank ?? null,
      oldest_ts: m.oldest_ts || minTs(g.items), newest_ts: m.newest_ts || maxTs(g.items),
      state_now: m.state_now || '', has_brief: !!m.has_brief, brief_stale: !!m.brief_stale, project: m.project || null,
    };
  });
  out.sort((a, b) => (a.rank != null && b.rank != null ? a.rank - b.rank : a.rank != null ? -1 : b.rank != null ? 1 : byUrgency(a, b)));
  return out;
}

/* ------------------------------------------------------------------ recap entries */
// The AI recap (brief.recap) carries no `kind`; guess it from the first words so the timeline can still show markers.
const GUESS = [
  [/^(你|用户)/, 'user'],
  [/^(系统)/, 'system'],
  [/^(收到|发现|检测到|监测到|出现|导师|编辑|对方).*/, 'found'],
  [/^(AI|智能体|Agent|助手)/i, 'ai'],
];
export function guessKind(text) {
  const t = String(text || '').trim();
  for (const [re, kind] of GUESS) if (re.test(t)) return /(产出|交付了|生成了.{0,8}(文档|报告|稿))/.test(t) && kind === 'ai' ? 'deliverable' : kind;
  return null;
}
/** Clean + order recap / history entries (oldest first); `kind` is only ever one of KIND_VIEW or null. */
export function normalizeEntries(list) {
  const rows = (Array.isArray(list) ? list : [])
    .map((e, i) => ({ i, ts: e?.ts || '', text: String(e?.text ?? '').trim(), kind: KIND_VIEW[e?.kind] ? e.kind : null }))
    .filter((e) => e.text);
  const allDated = rows.every((e) => tsMs(e.ts) != null);
  if (allDated) rows.sort((a, b) => tsMs(a.ts) - tsMs(b.ts) || a.i - b.i);
  return rows.map((e) => ({ ts: e.ts, text: e.text, kind: e.kind || guessKind(e.text) }));
}

/* ------------------------------------------------------------------ loader: cached + de-duplicated, 404 tolerated */
const CACHE_T = new Map();       // key -> {at, data, hash, changed}
const INFLIGHT = new Map();
const MISSING = new Map();       // key -> when a 404 was seen (older server / topic without rows): do not hammer it
export const GEN = new Map();    // key -> {since, baseline}: a brief regeneration the user asked for

export const cachedTopic = (key) => CACHE_T.get(key) || null;
export function dropTopicCache(key) { if (key) { CACHE_T.delete(key); MISSING.delete(key); } else { CACHE_T.clear(); MISSING.clear(); } }

/** Resolves {data, at, changed, missing}. `maxAge` serves a recent copy; `force` always asks the server. */
export async function loadTopic(key, { force = false, maxAge = 15000 } = {}) {
  const hit = CACHE_T.get(key);
  if (hit && !force && Date.now() - hit.at < maxAge) return hit;
  const miss = MISSING.get(key);
  if (miss && !force && Date.now() - miss < 60000) return { at: miss, data: null, missing: true };
  if (INFLIGHT.has(key)) return INFLIGHT.get(key);
  const p = (async () => {
    try {
      const data = await api(`/topics/${enc(key)}`);
      const hash = JSON.stringify(data);
      const prev = CACHE_T.get(key);
      const rec = { at: Date.now(), data, hash, changed: !prev || prev.hash !== hash };
      CACHE_T.set(key, rec); MISSING.delete(key);
      return rec;
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) { MISSING.set(key, Date.now()); CACHE_T.delete(key); return { at: Date.now(), data: null, missing: true }; }
      throw e;
    } finally { INFLIGHT.delete(key); }
  })();
  INFLIGHT.set(key, p);
  return p;
}
