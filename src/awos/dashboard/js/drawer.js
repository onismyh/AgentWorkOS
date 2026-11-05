// Slide-over drawer: task reader (same reader as the inbox pane) and run detail.
import { h, setHTML, $, enc } from './core/html.js';
import { api } from './core/api.js';
import { ACTS } from './core/acts.js';
import { S } from './core/state.js';
import { closeDrawer } from './core/router.js';
import { showDrawer, hideDrawer } from './ui/overlay.js';
import { iconBtn, errorState } from './ui/components.js';
import { taskReader, mountReader } from './ui/reader.js';
import { RS, loadRun, paintRun, startRunPoll, stopRunPoll } from './ui/rundetail.js';
import { snapshotUI, restoreUI } from './core/ui-state.js';

let seq = 0;
let lastTaskData = null;
export const drawerData = () => lastTaskData;

function taskChrome(d) {
  return h`<div class="dr-bar"><span class="dr-kicker">任务详情</span><span class="grow"></span>${iconBtn('copy', { act: 'copy-link', tip: '复制链接' })}${iconBtn('x', { act: 'close-drawer', tip: '关闭 Esc', cls: 'dr-x' })}</div>
    <div class="dr-body" id="drawer-body">${taskReader(d, { mode: 'drawer' })}</div>`;
}

export async function renderDrawer(initial = false) {
  const dr = $('#drawer');
  stopRunPoll();
  if (!S.drawer) { hideDrawer(); lastTaskData = null; RS.data = null; return; }
  const { type, id } = S.drawer;
  if (initial) {
    showDrawer(h`<div class="dr-bar"><span class="dr-kicker">${type === 'task' ? '任务详情' : '运行详情'}</span><span class="grow"></span>${iconBtn('x', { act: 'close-drawer', tip: '关闭 Esc', cls: 'dr-x' })}</div><div class="dr-body"><div class="skel-block pad">${h`<span class="skel" style="width:60%;height:18px"></span>`}${h`<span class="skel" style="width:90%;height:12px"></span>`}${h`<span class="skel" style="width:80%;height:12px"></span>`}</div></div>`);
    RS.filter = ''; RS.tab = 'events'; RS.log = null;
  } else showDrawer();
  const mySeq = ++seq;
  const snap = initial ? null : snapshotUI(dr);
  try {
    if (type === 'task') {
      const d = await api(`/tasks/${enc(id)}`);
      if (mySeq !== seq) return;
      lastTaskData = d;
      setHTML(dr, taskChrome(d));
      mountReader(dr, d);
      if (snap) restoreUI(dr, snap);
      else $('#drawer-body')?.focus?.();
    } else {
      const d = await loadRun(id);
      if (mySeq !== seq) return;
      RS.data = d;
      paintRun(dr, S.drawer.back);
      if (snap) restoreUI(dr, snap);
      startRunPoll(dr);
      if (initial && RS.follow && RS.tab === 'events') { const b = $('#run-body'); if (b) b.scrollTop = b.scrollHeight; }
    }
    if (initial) dr.focus({ preventScroll: true });
  } catch (e) {
    if (mySeq !== seq) return;
    setHTML(dr, h`<div class="dr-bar"><span class="dr-kicker">无法加载</span><span class="grow"></span>${iconBtn('x', { act: 'close-drawer', tip: '关闭', cls: 'dr-x' })}</div><div class="dr-body">${errorState(e.message, { retry: false })}</div>`);
  }
}

ACTS['close-drawer'] = () => closeDrawer();
ACTS['copy-link'] = async () => {
  try { await navigator.clipboard.writeText(location.href); const { toast } = await import('./ui/overlay.js'); toast('链接已复制'); } catch { /* ignore */ }
};
