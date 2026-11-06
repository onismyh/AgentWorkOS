// 分配依据: which executors the work was routed to and why (importance x difficulty x trust, see engine/router.py).
import { h, join, raw } from '../core/html.js';
import { icon } from './icons.js';
import { execAvatar } from './components.js';

const TIER = { economy: '经济档', standard: '标准档', premium: '高级档' };
const IMPORTANCE = { high: '重要', normal: '一般', low: '不急' };
const DIFFICULTY = { easy: '简单', medium: '中等', hard: '困难' };
const REASON = { budget: '预算不足', capacity: '预算额度被占用', transient: '临时故障' };
const NAME = { codex: 'Codex', claude: 'Claude', pi: 'Pi', dsh: 'DSH' };
const nm = (x) => NAME[x] || x;

/** The executor to show for a task: who ran it, else who is first in line, else a pinned/suggested one (older tasks). */
/** The executor the USER chose for a task (not a triage suggestion); '' means automatic. */
export const pinnedExec = (t) => (t?.executor && (t.meta?.executor_pinned || t.source === 'manual') ? t.executor : '');
export const executorOf = (t) => t?.meta?.last_executor || t?.meta?.routing?.order?.[0] || t?.executor || '';

/** Collapsible section for the task reader; empty until the task has been routed once. */
export function routingSection(task, { open = false } = {}) {
  const r = task?.meta?.routing;
  if (!r || !(r.order || []).length) return '';
  const ran = task.meta.last_executor;
  const hand = task.meta.handoffs || [];
  const order = join((r.order || []).map((x) => h`<span class="rt-ex${x === ran ? ' ran' : ''}">${execAvatar(x, { size: 18, tip: false })}<span>${nm(x)}</span></span>`), h`<span class="rt-arrow">${icon('chevron-right', { size: 13 })}</span>`);
  const who = ran ? h`<p class="rt-ran">实际由 <b>${nm(ran)}</b> 完成${hand.length ? h`，中途因${hand.map((x) => REASON[x.reason] || '故障').join('、')}从 ${hand.map((x) => nm(x.from)).join('、')} 切换过来` : ''}。</p>` : '';
  return h`<details class="rd-sec rt"${open ? raw(' open') : ''}><summary><span class="rd-sec-t">${icon('cpu', { size: 15 })}<span>分配依据</span><span class="rt-sum">${TIER[r.tier] || ''} · ${IMPORTANCE[r.importance] || ''} · ${DIFFICULTY[r.difficulty] || ''}</span></span>${icon('chevron-down', { size: 15, cls: 'rd-chev' })}</summary>
    <div class="rd-sec-b"><div class="rt-tags"><span class="badge badge-soft tone-${r.importance === 'high' ? 'warn' : 'muted'}">重要性 · ${IMPORTANCE[r.importance] || '-'}</span><span class="badge badge-soft tone-${r.difficulty === 'hard' ? 'crit' : r.difficulty === 'medium' ? 'brand' : 'muted'}">难度 · ${DIFFICULTY[r.difficulty] || '-'}</span><span class="badge badge-soft tone-brand">${TIER[r.tier] || '-'}</span>${r.trusted_only ? h`<span class="badge badge-soft tone-good">${icon('shield-alert', { size: 12 })}只交给 Codex / Claude</span>` : ''}</div>
      <div class="rt-order"><span class="muted small">优先顺序</span>${order}</div>${who}
      <ul class="rt-why">${(r.reasons || []).map((x) => h`<li>${x}</li>`)}</ul></div></details>`;
}
