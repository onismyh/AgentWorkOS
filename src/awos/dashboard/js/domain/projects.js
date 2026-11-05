// Projects: the stable things in 上下文 (personal context) that inbox topics belong to. Pure helpers (colour, labels, grouping,
// related-fact picking) plus the cached, de-duplicated loaders for /api/projects and /api/projects/{key}. No polling: callers
// fetch on view load / SSE refresh / user action only.
import { api, ApiError } from '../core/api.js';
import { enc } from '../core/html.js';
import { countSections } from './topics.js';

/** Group key of topics that are not linked to any project. */
export const NO_PROJECT = '__none__';
export const PROJECT_SLOTS = 8;
export const PROGRESS_PREFIX = '进展·';

export const isProjectKey = (k) => typeof k === 'string' && k.startsWith('project:');
export const projectSel = (key) => `project:${key}`;
export const projectFromSel = (sel) => String(sel).slice(8);
/** Working notes the AI writes about a project (kind note, key `进展·…`): shown muted, never as projects. */
export const isProgressNote = (key) => String(key ?? '').startsWith(PROGRESS_PREFIX);
export const progressTitle = (key) => String(key ?? '').slice(PROGRESS_PREFIX.length) || String(key ?? '');

/** Stable colour slot 1..8 from the project key (FNV-1a over UTF-16 units): the colour follows the project, never its rank. */
export function projectSlot(key) {
  let x = 0x811c9dc5;
  const s = String(key ?? '');
  for (let i = 0; i < s.length; i++) { x ^= s.charCodeAt(i); x = Math.imul(x, 0x01000193) >>> 0; }
  return (x % PROJECT_SLOTS) + 1;
}

/** Who made the link. The user's choice is never overwritten by the AI. */
export const LINK_SOURCE = {
  user: { label: '你指定', tip: '你指定的关联，AI 不会改动', icon: 'user' },
  llm: { label: 'AI 推断', tip: 'AI 根据话题内容推断的关联，不对可以更改（更改后会锁定为「你指定」）', icon: 'sparkles' },
  rule: { label: '自动匹配', tip: '按项目名自动匹配的关联，不对可以更改（更改后会锁定为「你指定」）', icon: 'sparkles' },
};
export const linkSource = (p) => LINK_SOURCE[p?.link_source] || LINK_SOURCE.llm;

/* ------------------------------------------------------------------ store */
export const PROJ = {
  list: null,               // last /api/projects `projects` (null until loaded / on an older server)
  at: 0,
  byKey: new Map(),         // project key -> {key, name, summary, ...}
  byTopic: new Map(),       // topic key -> {key, name, summary, link_source?}
};
/** Which topic opened the project card last (set by a project chip; the topic reader pulses the card once). */
export const FOCUS = { topic: null };

/** Remember the project of every topic summary (inbox / topic detail). A topic without `project` (null) is unlinked. */
export function noteTopicProjects(topics) {
  for (const t of topics || []) {
    if (!t || !('project' in t)) continue;
    if (t.project?.key) { PROJ.byTopic.set(t.key, t.project); PROJ.byKey.set(t.project.key, { ...(PROJ.byKey.get(t.project.key) || {}), ...t.project }); }
    else PROJ.byTopic.delete(t.key);
  }
}
export const projectOfTopic = (topicKey) => (topicKey ? PROJ.byTopic.get(topicKey) || null : null);

function adoptList(list) {
  PROJ.list = list;
  PROJ.at = Date.now();
  for (const p of list) {
    PROJ.byKey.set(p.key, { ...(PROJ.byKey.get(p.key) || {}), ...p });
    for (const tk of p.topic_keys || []) PROJ.byTopic.set(tk, { ...(PROJ.byTopic.get(tk) || {}), key: p.key, name: p.name, summary: p.summary });
  }
}

let listFlight = null;
/** `GET /api/projects` -> projects[] (null when the server has no such endpoint). Cached for `maxAge` ms and de-duplicated. */
export async function loadProjects({ force = false, maxAge = 20000 } = {}) {
  if (!force && PROJ.list && Date.now() - PROJ.at < maxAge) return PROJ.list;
  if (listFlight) return listFlight;
  listFlight = (async () => {
    try {
      const d = await api('/projects');
      adoptList(Array.isArray(d.projects) ? d.projects : []);
      return PROJ.list;
    } catch (e) {
      if (e instanceof ApiError && (e.status === 404 || e.status === 405)) { PROJ.list = null; return null; }
      throw e;
    } finally { listFlight = null; }
  })();
  return listFlight;
}

const CACHE_P = new Map();     // key -> {at, data}
const FLIGHT_P = new Map();
export const cachedProject = (key) => CACHE_P.get(key) || null;
export function dropProjectCache(key) { if (key) CACHE_P.delete(key); else CACHE_P.clear(); PROJ.at = 0; }

/** `GET /api/projects/{key}` -> detail or null (404 / older server). `force` skips the cached copy. */
export async function loadProject(key, { force = false, maxAge = 15000 } = {}) {
  const hit = CACHE_P.get(key);
  if (hit && !force && Date.now() - hit.at < maxAge) return hit.data;
  if (FLIGHT_P.has(key)) return FLIGHT_P.get(key);
  const p = (async () => {
    try {
      const data = await api(`/projects/${enc(key)}`);
      CACHE_P.set(key, { at: Date.now(), data });
      return data;
    } catch (e) {
      if (e instanceof ApiError && (e.status === 404 || e.status === 405)) { CACHE_P.delete(key); return null; }
      throw e;
    } finally { FLIGHT_P.delete(key); }
  })();
  FLIGHT_P.set(key, p);
  return p;
}

/* ------------------------------------------------------------------ grouping */
const minOf = (xs) => xs.filter(Boolean).sort()[0] || null;
const maxOf = (xs) => xs.filter(Boolean).sort().slice(-1)[0] || null;

/**
 * Topic groups (groupByTopic output, already in urgency order) -> project groups. A project group aggregates the counts, the
 * oldest open item and the state of the most urgent topic; topics without a project form the last group (key NO_PROJECT).
 */
export function groupByProject(topicGroups) {
  const by = new Map(); const order = [];
  for (const g of topicGroups) {
    const key = g.project?.key || NO_PROJECT;
    let pg = by.get(key);
    if (!pg) { pg = { key, project: g.project || null, name: g.project?.name || '未关联项目', topics: [], items: [] }; by.set(key, pg); order.push(pg); }
    pg.topics.push(g);
    pg.items.push(...g.items);
  }
  const out = order.map((pg) => ({
    ...pg, counts: countSections(pg.items),
    oldest_ts: minOf(pg.topics.map((t) => t.oldest_ts)), newest_ts: maxOf(pg.topics.map((t) => t.newest_ts)),
    state_now: pg.topics.find((t) => t.state_now)?.state_now || '',
  }));
  return [...out.filter((g) => g.key !== NO_PROJECT), ...out.filter((g) => g.key === NO_PROJECT)];
}

/** Server topic-summary counts ({decisions, approvals, ...}) -> the section keys the UI uses ({decision, approval, ...}). */
export function sectionCounts(c = {}) {
  return { decision: c.decisions || 0, approval: c.approvals || 0, review: c.review || 0, handoff: c.handoff || 0, suggested: c.suggested || 0, failed: c.failed || 0, total: c.total || 0 };
}

/* ------------------------------------------------------------------ related facts */
export const REL_KINDS = [
  { key: 'deadline', label: '截止日期', icon: 'calendar' },
  { key: 'person', label: '人物', icon: 'user' },
  { key: 'goal', label: '目标', icon: 'flag' },
  { key: 'note', label: '笔记', icon: 'file-text' },
];
/** Up to `max` related facts to preview: deadlines first, then people, goals, notes; the AI's working notes last. */
export function pickRelated(related, max = 3) {
  const out = [];
  const add = (kind, list) => { for (const f of list || []) out.push({ kind, key: f.key, value: f.value, progress: kind === 'note' && isProgressNote(f.key) }); };
  add('deadline', related?.deadline); add('person', related?.person); add('goal', related?.goal);
  add('note', (related?.note || []).filter((f) => !isProgressNote(f.key)));
  add('note', (related?.note || []).filter((f) => isProgressNote(f.key)));
  return out.slice(0, max);
}
export const relatedTotal = (related) => REL_KINDS.reduce((s, k) => s + ((related?.[k.key] || []).length), 0);
