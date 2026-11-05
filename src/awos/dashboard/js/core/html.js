// Safe HTML primitives. Everything that reaches the DOM as markup goes through here.
//
// XSS rule (agent/user text comes from e-mails and web pages):
//   * h`...` escapes every interpolated value unless it is already a Safe (nested h``, md(), icon()).
//   * setHTML()/appendHTML() accept ONLY Safe instances - a plain string throws. No other module assigns innerHTML.
//   * raw() is the escape hatch for constant markup (icons, markdown output that was escaped first). Never feed it
//     interpolated untrusted text.

export class Safe {
  constructor(s) { this.s = s; }
  toString() { return this.s; }
}

const ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;', '`': '&#96;' };
export const esc = (v) => String(v ?? '').replace(/[&<>"'`]/g, (c) => ESC[c]);
export const unesc = (s) => String(s).replace(/&(amp|lt|gt|quot|#39|#96);/g, (m) => ({
  '&amp;': '&', '&lt;': '<', '&gt;': '>', '&quot;': '"', '&#39;': "'", '&#96;': '`',
}[m]));
export const raw = (s) => new Safe(String(s));

export function fmt(v) {
  if (v == null || v === false || v === true) return '';
  if (v instanceof Safe) return v.s;
  if (Array.isArray(v)) return v.map(fmt).join('');
  return esc(v);
}

export function h(strings, ...vals) {
  let out = strings[0];
  for (let i = 0; i < vals.length; i++) out += fmt(vals[i]) + strings[i + 1];
  return new Safe(out);
}

/** Join Safe/array fragments. */
export const join = (items, sep = '') => new Safe(items.map(fmt).join(sep));

export function safeUrl(u) {
  const s = String(u ?? '').trim();
  if (/^(https?:|mailto:|file:|notion:|zotero:|obsidian:|vscode:)/i.test(s)) return s;
  if (s.startsWith('/') && !/^\/[/\\]/.test(s)) return s;
  return '#';
}

function need(html) {
  if (!(html instanceof Safe)) throw new TypeError('setHTML/appendHTML expects a Safe value (use h`` or raw())');
  return html.s;
}
export function setHTML(el, html) { el.innerHTML = need(html); return el; }
export function appendHTML(el, html) { el.insertAdjacentHTML('beforeend', need(html)); return el; }
export function clearEl(el) { el.replaceChildren(); return el; }

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
export const enc = encodeURIComponent;
