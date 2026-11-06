// Outbox: approvals reader (risk, payload, expiry, options with the AI recommendation emphasised) + decided history.
// Pending approvals are grouped by topic like the inbox (topic names come from the inbox feed: /api/approvals has none).
import { h, enc } from '../core/html.js';
import { api } from '../core/api.js';
import { S, CACHE, cacheApproval } from '../core/state.js';
import { empty, skeletonList, tabs } from '../ui/components.js';
import { Split, setActiveSplit, splitKeys } from './split.js';
import { digestOf, META, recommendedKey } from '../domain/vocab.js';
import { recLabel } from '../domain/readable.js';
import { parseTs } from '../core/format.js';

const DECIDED = 'approved,rejected,expired,cancelled,executing,executed,failed';
const tabParam = () => S.params.get('tab') || 'pending';

function toItems(list, tmap = null) {
  return list.map((a) => {
    cacheApproval(a);
    const dg = digestOf(a, 'approval');
    const decided = a.status !== 'pending';
    const f = tmap?.get(a.id);
    const topic = decided || !tmap ? {} : { topic_key: f?.topic_key || 'c:other', topic_title: f?.topic_title || '其他' };
    if (topic.topic_key) CACHE.topicOf.set(a.id, { key: topic.topic_key, title: topic.topic_title });
    return {
      ...topic,
      key: `approval:${a.id}`, dom: `approval_${a.id}`, kind: 'approval', id: a.id, task_id: a.task_id, section: decided ? 'history' : (a.kind === 'decision' ? 'decision' : 'approval'),
      headline: dg.headline, gist: dg.gist || (decided ? `${META.apvLabel[a.status] || a.status}${a.decision ? ` · ${a.decision}` : ''}` : ''), keywords: dg.keywords, auto: dg.auto,
      title: a.title, level: a.task_level, risk: a.risk, action_type: a.action_type, status: a.status, ts: decided ? (a.decided_at || a.created_at) : a.created_at,
      stamp: `${a.status}|${a.decided_at || a.created_at}`, expires_at: a.expires_at, tone: a.risk === 'high' ? 'crit' : decided ? 'muted' : 'warn',
      rec_label: decided ? '' : recLabel(a, recommendedKey),
    };
  });
}

const ctl = new Split({
  id: 'outbox', title: 'Outbox', icon: 'send', autoSelect: false,
  sections: [
    { key: 'decision', title: '需你决定', tone: 'warn', hint: '选一个选项' },
    { key: 'approval', title: '需你批准', tone: 'warn', hint: '动作 / 权限 / 预算 / 评审' },
    { key: 'history', title: '已处理', tone: 'muted', hint: '最近的决策记录' },
  ],
  async fetch() {
    const pending = tabParam() === 'pending';
    const [d, inb] = await Promise.all([api(`/approvals?status=${pending ? 'pending' : DECIDED}&limit=${pending ? 500 : 200}`),
      pending ? api('/inbox').catch(() => null) : null]);   // the feed knows which topic each pending approval belongs to
    const tmap = inb ? new Map((inb.feed || []).filter((f) => f.kind === 'approval').map((f) => [f.id, f])) : null;
    let items = toItems(d.approvals, tmap);
    if (pending) {
      const risk = { high: 0, medium: 1, low: 2 };
      items.sort((a, b) => (risk[a.risk] ?? 1) - (risk[b.risk] ?? 1) || String(parseTs(a.expires_at)?.getTime() ?? 9e15).localeCompare(String(parseTs(b.expires_at)?.getTime() ?? 9e15)));
    }
    return { items, extra: null, topics: inb?.topics || [] };
  },
  detail: (it) => api(`/approvals/${enc(it.id)}`),
  emptyAll: () => empty({ icon: 'send', title: tabParam() === 'pending' ? '没有待决策的事项' : '还没有处理记录', desc: tabParam() === 'pending' ? 'AI 提议的对外动作和需要你拍板的决策会出现在这里。' : '' }),
});

const tabsHTML = () => h`<div class="view-tabs">${tabs([{ key: 'pending', label: '待决策' }, { key: 'history', label: '已处理' }], tabParam(), { act: 'set-param', key: 'tab', label: 'Outbox 分类' })}</div>`;

export default {
  id: 'outbox', title: 'Outbox', sub: '等待你批准的动作与决策', icon: 'send', split: true, softParams: ['sel'],
  load: () => ctl.load(),
  render: () => { setActiveSplit(ctl); return h`${tabsHTML()}${ctl.render()}`; },
  mount: () => { setActiveSplit(ctl); ctl.mount(); },
  refresh: () => ctl.refresh(),
  keys: (e) => splitKeys(e, ctl),
  skeleton: skeletonList,
  controller: ctl,
};
