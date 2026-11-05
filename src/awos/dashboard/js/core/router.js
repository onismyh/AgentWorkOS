// Hash router: #/view?param=...  Drawer params (task, run, back) are layered on top of any view.
import { S } from './state.js';

export const VIEW_NAMES = ['overview', 'board', 'inbox', 'outbox', 'tasks', 'runs', 'tools', 'errors', 'artifacts', 'context', 'sensors', 'history'];
export const DRAWER_KEYS = ['task', 'run', 'back'];

export function parseHash() {
  const rawHash = location.hash.replace(/^#\/?/, '');
  const [path, qs] = rawHash.split('?');
  return { view: VIEW_NAMES.includes(path) ? path : 'overview', params: new URLSearchParams(qs || '') };
}
export function hashFor(view, params) {
  const p = new URLSearchParams();
  for (const [k, v] of params instanceof URLSearchParams ? params.entries() : Object.entries(params)) if (v != null && v !== '') p.set(k, v);
  const qs = p.toString();
  return `#/${view}${qs ? `?${qs}` : ''}`;
}
let routeHandler = () => {};
export function onRouteChange(fn) { routeHandler = fn; }

/** Update query params of the current view. replace=true keeps history clean (typing in filters, j/k). */
export function setParams(updates, { replace = false } = {}) {
  const { view, params } = parseHash();
  for (const [k, v] of Object.entries(updates)) { if (v == null || v === '') params.delete(k); else params.set(k, v); }
  const next = hashFor(view, params);
  if (next === (location.hash || '#/')) return;
  if (replace) { history.replaceState(null, '', next); routeHandler(); } else location.hash = next;
}
export function go(view, params = {}) { location.hash = hashFor(view, params); }
export const openTask = (id) => setParams({ task: id, run: null, back: null });
export const openRun = (id, back) => setParams({ run: id, task: null, back: back || null });
export const closeDrawer = () => setParams({ task: null, run: null, back: null });
export const viewParams = () => S.params;
