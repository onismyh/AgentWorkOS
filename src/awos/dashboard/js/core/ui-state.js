// Keep scroll positions, focus and unsent text across live re-renders.
import { $, $$ } from './html.js';

const keyOf = (n) => n.dataset.dirtyKey || n.name || n.id || '';

export function snapshotUI(root) {
  const scrolls = {};
  $$('[data-scroll]', root).forEach((n) => { scrolls[n.dataset.scroll] = [n.scrollTop, n.scrollLeft]; });
  for (const sel of ['#view', '#drawer-body', '#run-body']) { const n = root.matches?.(sel) ? root : $(sel, root); if (n) scrolls[sel] = n.scrollTop; }
  const dirty = {};
  $$('textarea[data-dirty-key], input[data-dirty-key]', root).forEach((n) => { if (n.value) dirty[keyOf(n)] = n.value; });
  const a = document.activeElement;
  const focus = a && root.contains(a) && (a.id || a.dataset?.focusKey) ? { id: a.id, key: a.dataset?.focusKey, start: a.selectionStart, end: a.selectionEnd } : null;
  const open = {};
  $$('details[id]', root).forEach((n) => { open[n.id] = n.open; });
  return { scrolls, dirty, focus, open };
}

export function restoreUI(root, snap) {
  if (!snap) return;
  $$('details[id]', root).forEach((n) => { if (n.id in snap.open) n.open = snap.open[n.id]; });
  $$('textarea[data-dirty-key], input[data-dirty-key]', root).forEach((n) => { const k = keyOf(n); if (k in snap.dirty) n.value = snap.dirty[k]; });
  $$('[data-scroll]', root).forEach((n) => { if (n.dataset.scroll in snap.scrolls) { [n.scrollTop, n.scrollLeft] = snap.scrolls[n.dataset.scroll]; } });
  for (const sel of ['#view', '#drawer-body', '#run-body']) { const n = root.matches?.(sel) ? root : $(sel, root); if (n && sel in snap.scrolls) n.scrollTop = snap.scrolls[sel]; }
  const f = snap.focus;
  if (f) {
    const n = (f.id && document.getElementById(f.id)) || (f.key && $(`[data-focus-key="${CSS.escape(f.key)}"]`, root));
    if (n) { n.focus({ preventScroll: true }); try { if (f.start != null) n.setSelectionRange(f.start, f.end); } catch { /* not text */ } }
  }
}
