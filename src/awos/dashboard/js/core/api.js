// API client + live stream. All mutating calls carry the X-AWOS-Token injected into <meta name="awos-token">.
import { enc } from './html.js';

let token = null;
const getToken = () => {
  if (token == null) token = (typeof document !== 'undefined' && document.querySelector('meta[name="awos-token"]')?.content) || '';
  return token;
};

export class ApiError extends Error {
  constructor(message, status) { super(message); this.status = status; }
}

export async function api(path, { method = 'GET', body, signal, auth = false } = {}) {
  const opt = { method, headers: { Accept: 'application/json' }, signal };
  if (method !== 'GET' || auth) opt.headers['X-AWOS-Token'] = getToken(); // evidence/mail GETs are token-protected too
  if (body !== undefined) { opt.headers['Content-Type'] = 'application/json'; opt.body = JSON.stringify(body); }
  const r = await fetch(`/api${path}`, opt);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) {
    const d = data.detail;
    const msg = typeof d === 'string' ? d : Array.isArray(d) ? d.map((x) => `${(x.loc || []).slice(-1)[0] || ''} ${x.msg || ''}`).join('; ') : `HTTP ${r.status}`;
    throw new ApiError(msg || `HTTP ${r.status}`, r.status);
  }
  return data;
}
export const post = (path, body = {}) => api(path, { method: 'POST', body });
export const patch = (path, body) => api(path, { method: 'PATCH', body });
export const del = (path) => api(path, { method: 'DELETE' });
/** GET for token-protected read endpoints (task evidence, mail body). */
export const getAuth = (path) => api(path, { auth: true });

/** GET that resolves to null when the endpoint is missing (older server) - used for graceful fallbacks. */
export async function apiOpt(path) {
  try { return await api(path); } catch (e) { if (e instanceof ApiError && (e.status === 404 || e.status === 405)) return null; throw e; }
}

export const tq = (obj) => {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(obj)) if (v != null && v !== '') p.set(k, v);
  const s = p.toString();
  return s ? `?${s}` : '';
};

export async function fetchText(path) {
  const r = await fetch(path);
  if (!r.ok) throw new ApiError(`HTTP ${r.status}`, r.status);
  return { text: await r.text(), truncated: !!r.headers.get('X-AWOS-Truncated') };
}

/** Server-sent events with automatic reconnect (the browser handles retry). */
export function connectStream(onEvent, onState) {
  if (typeof EventSource === 'undefined') { onState(false); return null; }
  const es = new EventSource('/api/stream');
  es.onopen = () => onState(true);
  es.onerror = () => onState(false);
  es.onmessage = (m) => {
    let ev;
    try { ev = JSON.parse(m.data); } catch { return; }
    onEvent(ev);
  };
  return es;
}

export const artifactUrl = (id, download = false) => `/api/artifacts/${enc(id)}/content${download ? '?download=1' : ''}`;
