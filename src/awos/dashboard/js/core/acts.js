// Action registries: views and components register handlers; one set of document-level listeners dispatches.
// Markup uses data-act="name" (click), data-change="name" (change), data-form="name" (submit), data-input="name" (input).
import { toastError } from '../ui/overlay-bus.js';

export const ACTS = {};
export const CHANGES = {};
export const FORMS = {};
export const INPUTS = {};

const run = async (fn, ...args) => {
  try { await fn(...args); } catch (err) { toastError(err); }
};

let bound = false;
export function bindDelegation() {
  if (bound) return;
  bound = true;
  document.addEventListener('click', (e) => {
    const el = e.target.closest('[data-act]');
    if (!el || el.disabled || el.getAttribute('aria-disabled') === 'true') return;
    if (el.matches('input, select, textarea')) return;
    const fn = ACTS[el.dataset.act];
    if (!fn) return;
    // Let modified clicks on real links behave natively (open in new tab etc.)
    if (el.tagName === 'A' && (e.ctrlKey || e.metaKey || e.shiftKey) && el.getAttribute('href') && el.getAttribute('href') !== '#') return;
    e.preventDefault();
    run(fn, el, e);
  });
  document.addEventListener('change', (e) => {
    const el = e.target.closest('[data-change]');
    if (el) run(CHANGES[el.dataset.change] || (() => {}), el, e);
  });
  document.addEventListener('input', (e) => {
    const el = e.target.closest('[data-input]');
    if (el) run(INPUTS[el.dataset.input] || (() => {}), el, e);
  });
  document.addEventListener('submit', (e) => {
    const form = e.target.closest('[data-form]');
    if (!form) return;
    e.preventDefault();
    run(FORMS[form.dataset.form] || (() => {}), form, e);
  });
}
