// Per-viewer conveniences kept in localStorage. Every access is wrapped: storage may be blocked or throw.

export function lsGet(key, def = null) {
  try {
    const v = globalThis.localStorage?.getItem(key);
    return v == null ? def : v;
  } catch { return def; }
}
export function lsSet(key, value) {
  try { globalThis.localStorage?.setItem(key, String(value)); return true; } catch { return false; }
}
export function lsGetJSON(key, def) {
  try {
    const v = lsGet(key, null);
    return v == null ? def : JSON.parse(v);
  } catch { return def; }
}
export function lsSetJSON(key, value) {
  try { return lsSet(key, JSON.stringify(value)); } catch { return false; }
}

/** Preference with a default, persisted as JSON under awos.pref.<name>. */
export const pref = {
  get(name, def) { return lsGetJSON(`awos.pref.${name}`, def); },
  set(name, value) { lsSetJSON(`awos.pref.${name}`, value); },
};

/* ---- read / unread marks for inbox items (per viewer) ---- */
const READ_KEY = 'awos.read';
let readMap = null;
const loadRead = () => { if (!readMap) readMap = lsGetJSON(READ_KEY, {}) || {}; return readMap; };
export const stampOf = (it) => String(it.updated_at || it.ts || it.created_at || '');
export const isUnread = (key, stamp) => loadRead()[key] !== stamp;
export function markRead(key, stamp) {
  const m = loadRead();
  if (m[key] === stamp) return false;
  m[key] = stamp;
  const keys = Object.keys(m);
  if (keys.length > 1200) for (const k of keys.slice(0, keys.length - 1000)) delete m[k];
  lsSetJSON(READ_KEY, m);
  return true;
}
export function markUnread(key) { const m = loadRead(); delete m[key]; lsSetJSON(READ_KEY, m); }
