// State shared by the shell, views and actions.
export const S = {
  view: 'overview',
  params: new URLSearchParams(),
  live: false,
  health: null,
  dash: null,           // last /api/dashboard payload (KPIs for badges + cost meter)
  changed: new Set(),   // entity ids announced over SSE since the last refresh
  refresh: async () => {},   // assigned by the shell: refresh(force?)
  drawer: null,         // {type:'task'|'run', id, back}
  facts: [],
};
export const VIEW_SCROLL = {};

/** Lightweight caches so delegated handlers can find the object behind a data-id. */
export const CACHE = { tasks: new Map(), approvals: new Map(), topicOf: new Map() };   // topicOf: task / approval id -> {key, title}
export function cacheTask(t) { if (t?.id) CACHE.tasks.set(t.id, { ...(CACHE.tasks.get(t.id) || {}), ...t }); return t; }
export function cacheApproval(a) { if (a?.id) CACHE.approvals.set(a.id, { ...(CACHE.approvals.get(a.id) || {}), ...a }); return a; }
