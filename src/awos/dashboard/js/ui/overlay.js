// Overlays: toast, dialog, popover, tooltip, drawer shell. Keyboard accessible (focus trap, Esc, focus restore).
import { h, raw, setHTML, clearEl, $, $$, esc } from '../core/html.js';
import { icon } from './icons.js';
import { btn, iconBtn } from './components.js';
import { setToastSink } from './overlay-bus.js';

/* ------------------------------------------------------------------ toast */
const pendingDeferred = new Set();
export const hasPendingDeferred = () => pendingDeferred.size > 0;

export function toast(msg, kind = 'ok', opts = {}) {
  const host = $('#toasts');
  if (!host) return { dismiss() {} };
  const el = document.createElement('div');
  el.className = `toast toast-${kind}`;
  el.setAttribute('role', kind === 'err' ? 'alert' : 'status');
  const ic = kind === 'err' ? 'circle-x' : kind === 'warn' ? 'circle-alert' : kind === 'info' ? 'info' : 'circle-check';
  setHTML(el, h`<span class="toast-ic tone-${kind === 'err' ? 'crit' : kind === 'warn' ? 'warn' : kind === 'info' ? 'brand' : 'good'}">${icon(ic, { size: 16 })}</span>
    <div class="toast-m"><div class="toast-t"></div>${opts.desc ? h`<div class="toast-d"></div>` : ''}</div>
    ${opts.action ? btn(opts.action.label, { variant: 'ghost', size: 'sm', cls: 'toast-act', icon: opts.action.icon }) : ''}
    ${iconBtn('x', { size: 'icon-sm', tip: '关闭', cls: 'toast-x' })}
    ${opts.duration && opts.bar ? h`<i class="toast-bar" style="animation-duration:${opts.duration}ms"></i>` : ''}`);
  $('.toast-t', el).textContent = msg;
  if (opts.desc) $('.toast-d', el).textContent = opts.desc;
  host.appendChild(el);
  let timer = null;
  const api = {
    dismiss() { clearTimeout(timer); el.classList.add('out'); setTimeout(() => el.remove(), 160); },
  };
  $('.toast-x', el).addEventListener('click', () => { opts.onClose?.(); api.dismiss(); });
  if (opts.action) $('.toast-act', el).addEventListener('click', () => { opts.action.fn?.(); api.dismiss(); });
  const duration = opts.duration ?? (kind === 'err' ? 8000 : 3600);
  if (duration > 0) timer = setTimeout(() => { opts.onExpire?.(); api.dismiss(); }, duration);
  while (host.children.length > 4) host.firstChild.remove();
  return api;
}
setToastSink(({ msg, kind }) => toast(msg, kind));

/**
 * Optimistic action with undo window (Gmail-style): `run` fires after `delay` unless undone.
 * Returns a promise resolving 'done' | 'undone' | 'failed'. Flushed immediately when the page is hidden/closed.
 */
export function deferred({ msg, run, onUndo, delay = 5000, undoLabel = '撤销' }) {
  return new Promise((resolve) => {
    let settled = false;
    const entry = { commit: null };
    const finish = async (mode) => {
      if (settled) return;
      settled = true;
      pendingDeferred.delete(entry);
      t.dismiss();
      if (mode === 'undo') { onUndo?.(); resolve('undone'); document.dispatchEvent(new CustomEvent('awos:dialog-closed')); return; }
      try { await run(); resolve('done'); } catch (e) { toast(e.message || String(e), 'err'); onUndo?.(); resolve('failed'); }
    };
    entry.commit = () => finish('commit');
    pendingDeferred.add(entry);
    const t = toast(msg, 'ok', {
      duration: delay, bar: true,
      action: { label: undoLabel, icon: 'undo', fn: () => finish('undo') },
      onExpire: () => finish('commit'), onClose: () => finish('commit'),
    });
  });
}
if (typeof window !== 'undefined') window.addEventListener('pagehide', () => { for (const e of [...pendingDeferred]) e.commit?.(); });

/* ------------------------------------------------------------------ focus trap helper */
const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]):not([type=hidden]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';
function trapTab(container, e) {
  if (e.key !== 'Tab') return;
  const items = $$(FOCUSABLE, container).filter((n) => n.offsetParent !== null || n === document.activeElement);
  if (!items.length) { e.preventDefault(); container.focus(); return; }
  const first = items[0]; const last = items[items.length - 1];
  if (e.shiftKey && (document.activeElement === first || document.activeElement === container)) { e.preventDefault(); last.focus(); }
  else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
}

/* ------------------------------------------------------------------ dialog */
let dialogCount = 0;
export const isDialogOpen = () => !!$('#dialog-host .overlay');

export function dialog({ title, desc, body, submitLabel = '确定', cancelLabel = '取消', wide = false, danger = false, onSubmit = null,
  hideSubmit = false, size = '', icon: ic = '', focus = '' }) {
  return new Promise((resolve) => {
    const host = $('#dialog-host');
    const prevFocus = document.activeElement;
    const id = `dlg-${++dialogCount}`;
    setHTML(host, h`<div class="overlay" data-overlay><form class="dialog${wide ? ' wide' : ''}${size ? ` ${size}` : ''}" role="dialog" aria-modal="true" aria-labelledby="${id}-t" novalidate tabindex="-1">
      <header class="dialog-h"><div>${ic ? h`<span class="dialog-ic${danger ? ' danger' : ''}">${icon(ic, { size: 18 })}</span>` : ''}<h2 id="${id}-t">${title}</h2>${desc ? h`<p class="dialog-d">${desc}</p>` : ''}</div>${iconBtn('x', { tip: '关闭 Esc', cls: 'dlg-x' })}</header>
      <div class="dialog-b">${body}<div class="dialog-err" data-dialog-err role="alert"></div></div>
      <footer class="dialog-f"><button type="button" class="btn btn-secondary" data-close>${hideSubmit ? '关闭' : cancelLabel}</button>${hideSubmit ? '' : h`<button type="submit" class="btn ${danger ? 'btn-destructive' : 'btn-default'}">${submitLabel}</button>`}</footer></form></div>`);
    const overlay = $('.overlay', host);
    const form = $('form', overlay);
    let done = false;
    const close = (val) => {
      if (done) return;
      done = true;
      document.removeEventListener('keydown', onKey, true);
      clearEl(host);
      try { prevFocus?.focus?.(); } catch { /* element gone */ }
      resolve(val);
      document.dispatchEvent(new CustomEvent('awos:dialog-closed'));
    };
    const onKey = (e) => {
      if (e.key === 'Escape') { e.stopPropagation(); e.preventDefault(); close(null); return; }
      if (e.key === 'Enter' && (e.ctrlKey || e.metaKey) && !hideSubmit) { e.preventDefault(); form.requestSubmit(); return; }
      trapTab(form, e);
    };
    document.addEventListener('keydown', onKey, true);
    overlay.addEventListener('mousedown', (e) => { if (e.target === overlay) close(null); });
    overlay.addEventListener('click', (e) => { if (e.target.closest('[data-close], .dlg-x')) close(null); });
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const missing = $$('[required]', form).find((n) => !String(n.value || '').trim());
      const err = $('[data-dialog-err]', form);
      if (missing) { missing.focus(); err.textContent = '请填写必填项'; return; }
      const data = Object.fromEntries(new FormData(form).entries());
      $$('input[type=checkbox]', form).forEach((c) => { if (c.name) data[c.name] = c.checked; });
      if (!onSubmit) { close(data); return; }
      const sb = $('[type=submit]', form);
      sb.disabled = true;
      try { await onSubmit(data); close(data); } catch (er) { err.textContent = er.message || String(er); sb.disabled = false; }
    });
    requestAnimationFrame(() => {
      const target = (focus && $(focus, form)) || $('input:not([type=hidden]):not([type=checkbox]), textarea, select', form) || $('[type=submit]', form) || form;
      target.focus();
    });
  });
}
export const confirmDialog = (title, text, { danger = false, submitLabel = '确定', icon: ic = danger ? 'alert' : '' } = {}) =>
  dialog({ title, body: h`<p class="dialog-p">${text}</p>`, danger, submitLabel, size: 'sm', icon: ic });

/* ------------------------------------------------------------------ popover (menus, column chooser) */
let popClose = null;
export function popover(anchor, content, { align = 'start', onClose, cls = '' } = {}) {
  closePopover();
  const pop = document.createElement('div');
  pop.className = `popover${cls ? ` ${cls}` : ''}`;
  pop.setAttribute('role', 'dialog');
  setHTML(pop, content);
  document.body.appendChild(pop);
  const place = () => {
    const r = anchor.getBoundingClientRect();
    const pw = pop.offsetWidth; const ph = pop.offsetHeight;
    let x = align === 'end' ? r.right - pw : r.left;
    x = Math.max(8, Math.min(window.innerWidth - pw - 8, x));
    let y = r.bottom + 6;
    if (y + ph > window.innerHeight - 8) y = Math.max(8, r.top - ph - 6);
    pop.style.left = `${x}px`; pop.style.top = `${y}px`;
  };
  place();
  anchor.setAttribute('aria-expanded', 'true');
  const onDown = (e) => { if (!pop.contains(e.target) && !anchor.contains(e.target)) closePopover(); };
  const onKey = (e) => { if (e.key === 'Escape') { e.stopPropagation(); closePopover(); anchor.focus?.(); } else if (e.key === 'Tab') trapTab(pop, e); };
  setTimeout(() => document.addEventListener('pointerdown', onDown, true), 0);
  document.addEventListener('keydown', onKey, true);
  window.addEventListener('resize', closePopover);
  popClose = () => {
    document.removeEventListener('pointerdown', onDown, true);
    document.removeEventListener('keydown', onKey, true);
    window.removeEventListener('resize', closePopover);
    anchor.removeAttribute('aria-expanded');
    pop.remove();
    popClose = null;
    onClose?.();
  };
  const first = $(FOCUSABLE, pop);
  first?.focus();
  return pop;
}
export function closePopover() { popClose?.(); }

/* ------------------------------------------------------------------ tooltip */
let tipEl = null; let tipTimer = null; let tipFor = null;
function ensureTip() {
  if (!tipEl) {
    tipEl = document.createElement('div');
    tipEl.id = 'tooltip'; tipEl.className = 'tooltip'; tipEl.setAttribute('role', 'tooltip');
    document.body.appendChild(tipEl);
  }
  return tipEl;
}
/** Show a tooltip at viewport point (x,y). `spec` = string | {title, rows:[[label,value,colorVar?]], foot}. DOM-built with textContent. */
export function tipShow(x, y, spec, { below = false } = {}) {
  const t = ensureTip();
  clearEl(t);
  const add = (cls, text, parent = t) => { const d = document.createElement('div'); d.className = cls; d.textContent = text; parent.appendChild(d); return d; };
  if (typeof spec === 'string') { for (const line of spec.split('\n')) add('tip-l', line); }
  else {
    if (spec.title) add('tip-h', spec.title);
    for (const [label, value, color] of spec.rows || []) {
      const r = document.createElement('div'); r.className = 'tip-r';
      if (color) { const k = document.createElement('i'); k.className = 'tip-k'; k.style.background = color; r.appendChild(k); }
      const l = document.createElement('span'); l.className = 'tip-rl'; l.textContent = label; r.appendChild(l);
      const v = document.createElement('b'); v.className = 'tip-rv'; v.textContent = value; r.appendChild(v);
      t.appendChild(r);
    }
    if (spec.foot) add('tip-f', spec.foot);
  }
  t.classList.add('show');
  const w = t.offsetWidth; const hh = t.offsetHeight;
  let left = Math.max(8, Math.min(window.innerWidth - w - 8, x - w / 2));
  let top = below ? y + 14 : y - hh - 10;
  if (top < 8) top = y + 18;
  if (top + hh > window.innerHeight - 8) top = Math.max(8, y - hh - 10);
  t.style.left = `${left}px`; t.style.top = `${top}px`;
}
export function tipHide() { clearTimeout(tipTimer); tipFor = null; tipEl?.classList.remove('show'); }

export function bindTooltips() {
  const navCollapsed = () => document.getElementById('app')?.dataset.collapsed === '1' || window.matchMedia('(max-width: 1100px) and (min-width: 769px)').matches;
  const show = (el, immediate) => {
    const text = el.dataset.tip || (el.dataset.tipC && navCollapsed() ? el.dataset.tipC : '');
    if (!text) return;
    tipFor = el;
    clearTimeout(tipTimer);
    tipTimer = setTimeout(() => {
      if (tipFor !== el || !el.isConnected) return;
      const r = el.getBoundingClientRect();
      tipShow(r.left + r.width / 2, r.top, text, { below: r.top < 60 });
    }, immediate ? 0 : 380);
  };
  document.addEventListener('pointerover', (e) => {
    if (e.pointerType === 'touch') return;
    const el = e.target.closest?.('[data-tip], [data-tip-c]');
    if (el && el !== tipFor) show(el, false);
    else if (!el) tipHide();
  });
  document.addEventListener('focusin', (e) => { const el = e.target.closest?.('[data-tip], [data-tip-c]'); if (el) show(el, true); });
  document.addEventListener('focusout', tipHide);
  document.addEventListener('pointerdown', tipHide);
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') tipHide(); });
  document.addEventListener('scroll', tipHide, true);
}

/* ------------------------------------------------------------------ drawer (sheet) */
let drawerPrevFocus = null;
export function drawerOpen() { return $('#drawer')?.classList.contains('show'); }
export function showDrawer(contentSafe) {
  const dr = $('#drawer'); const bd = $('#backdrop');
  if (!dr.classList.contains('show')) drawerPrevFocus = document.activeElement;
  if (contentSafe) setHTML(dr, contentSafe);
  dr.classList.add('show'); bd.classList.add('show');
  dr.removeAttribute('inert'); dr.removeAttribute('aria-hidden');
}
export function hideDrawer() {
  const dr = $('#drawer'); const bd = $('#backdrop');
  if (!dr.classList.contains('show')) return;
  dr.classList.remove('show'); bd.classList.remove('show');
  dr.setAttribute('inert', ''); dr.setAttribute('aria-hidden', 'true');
  try { drawerPrevFocus?.focus?.(); } catch { /* gone */ }
  drawerPrevFocus = null;
}
export function bindDrawerKeys(onEsc) {
  document.addEventListener('keydown', (e) => {
    const dr = $('#drawer');
    if (!dr?.classList.contains('show') || isDialogOpen()) return;
    if (e.key === 'Tab') trapTab(dr, e);
    if (e.key === 'Escape' && !e.defaultPrevented) {
      const a = document.activeElement;
      if (a && a.matches('input, textarea, select') && !a.matches('[data-esc-close]')) { a.blur(); return; }
      onEsc();
    }
  });
}
export { esc, raw };
