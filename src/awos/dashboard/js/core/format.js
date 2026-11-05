// Formatting helpers (pure; no DOM access at import time).
import { h, raw } from './html.js';

/** Display timezone from /api/meta (the browser clock may differ from the user's configured zone). */
export const TZ = { name: undefined };

export const parseTs = (s) => { if (!s) return null; const d = new Date(s); return Number.isNaN(d.getTime()) ? null : d; };

export function absTime(s, withYear = false) {
  const d = parseTs(s);
  if (!d) return '—';
  const o = { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false, timeZone: TZ.name };
  if (withYear || d.getFullYear() !== new Date().getFullYear()) o.year = 'numeric';
  return d.toLocaleString('zh-CN', o);
}
export function dayLabel(s) {
  const d = parseTs(s);
  return d ? d.toLocaleDateString('zh-CN', { month: 'numeric', day: 'numeric', timeZone: TZ.name }) : '—';
}

/** Relative time in Chinese: "5 分钟前" / "2 小时后". */
export function rel(s, now = Date.now()) {
  const d = parseTs(s);
  if (!d) return '—';
  const diff = (now - d.getTime()) / 1000;
  const a = Math.abs(diff);
  const suf = diff >= 0 ? '前' : '后';
  if (a < 45) return diff >= 0 ? '刚刚' : '即将';
  if (a < 3600) return `${Math.round(a / 60)} 分钟${suf}`;
  if (a < 86400) return `${Math.round(a / 3600)} 小时${suf}`;
  if (a < 86400 * 30) return `${Math.round(a / 86400)} 天${suf}`;
  return absTime(s);
}
/** Short relative time for dense rows: "5分" / "3时" / "2天". */
export function relShort(s, now = Date.now()) {
  const d = parseTs(s);
  if (!d) return '—';
  const diff = (now - d.getTime()) / 1000;
  const a = Math.abs(diff);
  if (a < 60) return diff >= 0 ? '刚刚' : '即将';
  if (a < 3600) return `${Math.round(a / 60)} 分钟`;
  if (a < 86400) return `${Math.round(a / 3600)} 小时`;
  if (a < 86400 * 30) return `${Math.round(a / 86400)} 天`;
  return absTime(s);
}
/** `<time>` that the 60 s ticker keeps fresh. */
export const when = (s, { short = false } = {}) => (s
  ? h`<time class="when" data-ts="${s}"${short ? raw(' data-short="1"') : ''} title="${absTime(s, true)}">${short ? relShort(s) : rel(s)}</time>`
  : h`<span class="muted">—</span>`);

export function dur(sec) {
  if (sec == null) return '—';
  if (sec < 60) return `${sec < 10 ? sec.toFixed(1) : Math.round(sec)} 秒`;
  if (sec < 3600) return `${Math.floor(sec / 60)} 分 ${Math.round(sec % 60)} 秒`;
  if (sec < 86400) return `${Math.floor(sec / 3600)} 时 ${Math.round((sec % 3600) / 60)} 分`;
  return `${Math.floor(sec / 86400)} 天 ${Math.round((sec % 86400) / 3600)} 时`;
}
export function durShort(sec) {
  if (sec == null) return '—';
  if (sec < 60) return `${Math.round(sec)}秒`;
  if (sec < 3600) return `${Math.round(sec / 60)}分`;
  return `${(sec / 3600).toFixed(1)}时`;
}
export const usd = (v) => (v == null ? '—' : `$${Number(v).toFixed(Number(v) >= 100 ? 0 : 2)}`);
export function compact(n) {
  const v = Number(n || 0);
  if (Math.abs(v) >= 1e6) return `${(v / 1e6).toFixed(1).replace(/\.0$/, '')}M`;
  if (Math.abs(v) >= 1e3) return `${(v / 1e3).toFixed(1).replace(/\.0$/, '')}K`;
  return String(Math.round(v * 100) / 100);
}
export function bytes(n) {
  if (n == null) return '—';
  if (n < 1024) return `${n} B`;
  if (n < 1 << 20) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1 << 20)).toFixed(1)} MB`;
}
export const cut = (s, n) => { const t = String(s ?? ''); return t.length > n ? `${t.slice(0, n)}…` : t; };
export const shortId = (id) => String(id || '').replace(/^([a-z]+_)\d{6}/, '$1');
export const jsonText = (v, n = 6000) => cut(typeof v === 'string' ? v : JSON.stringify(v, null, 2), n);
export const clamp = (x, lo, hi) => Math.max(lo, Math.min(hi, x));

/** "还剩 2 小时" / "已过期 5 分钟" for approval expiry. */
export function countdown(s, now = Date.now()) {
  const d = parseTs(s);
  if (!d) return { text: '', state: 'none' };
  const sec = (d.getTime() - now) / 1000;
  if (sec <= 0) return { text: `已过期 ${relShort(s, now)}`, state: 'expired' };
  const text = sec < 3600 ? `${Math.max(1, Math.round(sec / 60))} 分钟后过期` : sec < 86400 ? `${Math.round(sec / 3600)} 小时后过期` : `${Math.round(sec / 86400)} 天后过期`;
  return { text, state: sec < 6 * 3600 ? 'soon' : 'ok' };
}

export function niceStep(max, n = 4) {
  const rawStep = max / n;
  const p = 10 ** Math.floor(Math.log10(rawStep));
  const m = rawStep / p;
  return (m <= 1 ? 1 : m <= 2 ? 2 : m <= 2.5 ? 2.5 : m <= 5 ? 5 : 10) * p;
}
