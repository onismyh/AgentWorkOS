// Plain-language helpers for the inbox / Outbox: what each item asks of you, how urgent it really is, and the one line that
// states the current state. Pure functions (no DOM), so the list rows, the group headers and the readers all say the same thing.
import { countdown, parseTs } from '../core/format.js';

const DAY = 86400000;

/** What a section asks of the user: `verb` leads a list row ("要你决定：…"), `todo` is the first thing the reader says. */
export const ASK = {
  decision: { verb: '要你决定', todo: '在下面选一个方案，AI 推荐的已标出。', tone: 'warn', icon: 'scale' },
  approval: { verb: '要你批准', todo: '核对「批准后将执行的内容」，再批准或拒绝。', tone: 'warn', icon: 'shield-alert' },
  review: { verb: '待你验收', todo: '看一眼 AI 的成果：满意就通过，要改就写明哪里再返工。', tone: 'warn', icon: 'circle-dot' },
  handoff: { verb: '需要你接手', todo: 'AI 做完了它能做的部分，剩下的要你来；做完点「我已处理」。', tone: 'warn', icon: 'hand' },
  suggested: { verb: 'AI 建议新做', todo: '值得做就接受并排队，不想做就忽略。', tone: 'brand', icon: 'sparkles' },
  failed: { verb: '任务失败', todo: '先看失败原因，再决定重试还是放弃。', tone: 'crit', icon: 'circle-x' },
  history: { verb: '已处理', todo: '', tone: 'muted', icon: 'check-check' },
};
/** Most pressing first. */
export const SECTION_RANK = { decision: 0, approval: 1, review: 2, handoff: 3, failed: 4, suggested: 5, history: 6 };

/** The label that leads a row: the section's verb, or - for a handled approval - what happened to it. */
export function askVerb(it, statusLabel = {}) {
  if (it.section === 'history') return statusLabel[it.status] || ASK.history.verb;
  return (ASK[it.section] || { verb: it.section_title || '' }).verb;
}

// A headline that already starts with the verb the label says ("决定…", "确认…" under 要你决定) would read twice.
const LEAD = { decision: /^(请)?(决定|确认|确定|选择)\s*/, approval: /^(请)?(批准|审批|同意)\s*/, review: /^(请)?(验收|确认验收)\s*/ };
export function plainHeadline(section, headline) {
  const text = String(headline || '').trim();
  const re = LEAD[section];
  if (!re) return text;
  const out = text.replace(re, '').replace(/^[：:，,\s]+/, '');
  return out.length >= 4 ? out : text;
}

/** The AI's recommended option as words, when the item is an approval with a recommendation. */
export function recLabel(approval, recommendedKey) {
  if (!approval || !approval.recommendation) return '';
  const key = recommendedKey(approval);
  const opt = (approval.options || []).find((o) => o && o.key === key);
  return opt ? String(opt.label || opt.key) : '';
}

/** The second line of a row: the AI's recommendation, else the one-line summary - never a repeat of the headline. */
export function rowNote(it) {
  if (it.rec_label) return `AI 建议：${it.rec_label}`;
  const gist = String(it.gist || '').trim();
  const head = String(it.headline || '').trim();
  if (!gist || gist === head || (head && gist.includes(head))) return '';
  return gist;
}

/**
 * The ONE marker a row may carry, and only when it matters: expired / overdue / about to expire / high risk / waiting for days.
 * Returns {tone, text, rank} (lower rank = more urgent) or null.
 */
export function urgencyOf(it, now = Date.now()) {
  const open = it.section !== 'history';
  if (open && it.kind === 'approval' && it.status === 'pending' && it.expires_at) {
    const cd = countdown(it.expires_at, now);
    if (cd.state === 'expired') return { tone: 'crit', text: '已过期', rank: 0 };
    const left = (parseTs(it.expires_at)?.getTime() ?? Infinity) - now;
    if (left <= DAY) return { tone: left < 6 * 3600000 ? 'crit' : 'warn', text: cd.text, rank: 2 };
  }
  const due = it.due_at ? parseTs(it.due_at)?.getTime() : null;
  if (open && due && due < now) return { tone: 'crit', text: '已逾期', rank: 1 };
  if (open && it.risk === 'high' && it.kind === 'approval') return { tone: 'crit', text: '高风险', rank: 3 };
  const t = it.ts ? parseTs(it.ts)?.getTime() : null;
  if (open && t && now - t > 3 * DAY && it.section !== 'suggested') return { tone: 'warn', text: `已等 ${Math.round((now - t) / DAY)} 天`, rank: 4 };
  return null;
}

/** What needs you most first: section, then urgency, then the soonest expiry, then the longest wait. Stable; does not mutate. */
export function sortItems(items, now = Date.now()) {
  const exp = (it) => (it.expires_at ? parseTs(it.expires_at)?.getTime() ?? Infinity : Infinity);
  const age = (it) => (it.ts ? parseTs(it.ts)?.getTime() ?? Infinity : Infinity);
  return items.map((it, i) => ({ it, i, u: urgencyOf(it, now)?.rank ?? 9 }))
    .sort((a, b) => (SECTION_RANK[a.it.section] ?? 9) - (SECTION_RANK[b.it.section] ?? 9) || a.u - b.u || exp(a.it) - exp(b.it) || age(a.it) - age(b.it) || a.i - b.i)
    .map((x) => x.it);
}

/**
 * One sentence of "where this stands" out of a long AI summary: the latest sentence that talks about the present
 * (现在 / 目前 / 卡在 / 等 / 下一步 ...), else the last one; cut at the first ；and clamped.
 */
export function oneLineState(text, max = 54) {
  const t = String(text || '').replace(/\s+/g, ' ').trim();
  if (!t) return '';
  const sentences = t.split(/(?<=[。！？!?])/).map((s) => s.trim()).filter(Boolean);
  const now = [...sentences].reverse().find((s) => /(现在|目前|当前|卡在|等待|等你|待你|下一步|还差|尚未|暂缓|需要你)/.test(s));
  let s = (now || sentences[sentences.length - 1] || t).replace(/[。！？!?]+$/, '');
  if (s.length > max && s.includes('；')) s = s.slice(0, s.indexOf('；'));
  return s.length > max ? `${s.slice(0, max - 1)}…` : s;
}

/** "3 项要你处理" (suggestions are not "needs you"; they are counted on their own). */
export function needsText(counts) {
  const c = counts || {};
  const need = (c.total || 0) - (c.suggested || 0);
  const sug = c.suggested || 0;
  if (need && sug) return `${need} 项要你处理 · ${sug} 条 AI 建议`;
  if (need) return `${need} 项要你处理`;
  if (sug) return `${sug} 条 AI 建议`;
  return '没有待处理';
}
