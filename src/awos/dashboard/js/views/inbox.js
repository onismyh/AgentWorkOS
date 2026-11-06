// 收件箱: items grouped by project (项目, default), topic (话题) or type; a topic reader with the 前情提要 and a reading pane for items.
import { h } from '../core/html.js';
import { api } from '../core/api.js';
import { S, CACHE, cacheTask, cacheApproval } from '../core/state.js';
import { ACTS } from '../core/acts.js';
import { pref } from '../core/store.js';
import { go } from '../core/router.js';
import { enc } from '../core/html.js';
import { empty, skeletonList } from '../ui/components.js';
import { Split, setActiveSplit, splitKeys } from './split.js';
import { digestOf, recommendedKey } from '../domain/vocab.js';
import { recLabel } from '../domain/readable.js';
import { topicKeyOf, topicTitleOf } from '../domain/topics.js';

export const INBOX_SECTIONS = [
  { key: 'decision', title: '需你决定', tone: 'warn', hint: 'AI 已分析，选一项即可' },
  { key: 'approval', title: '需你批准', tone: 'warn', hint: '对外动作、写回、预算' },
  { key: 'review', title: '待验收', tone: 'warn', hint: 'AI 已交付成果' },
  { key: 'handoff', title: '待接手', tone: 'warn', hint: 'AI 做完了它那部分' },
  { key: 'suggested', title: 'AI 的新建议', tone: 'brand', hint: '等你决定做不做' },
  { key: 'failed', title: '失败', tone: 'crit', hint: '重试已耗尽' },
];
const TONE = { decision: 'warn', approval: 'warn', review: 'warn', handoff: 'warn', suggested: 'brand', failed: 'crit' };

/** Build list rows from /api/inbox (flat `feed`, with a fallback that derives it from the bucket arrays). */
export function inboxItems(d) {
  const raw = new Map();
  for (const k of ['decisions', 'actions', 'review', 'handoff', 'suggested', 'failed']) for (const x of d[k] || []) raw.set(x.id, x);
  let feed = d.feed;
  if (!feed) {
    const sect = { decisions: ['decision', '需你决定'], actions: ['approval', '需你批准'], review: ['review', '待验收'], handoff: ['handoff', '待接手'], suggested: ['suggested', 'AI 的新建议'], failed: ['failed', '失败'] };
    feed = [];
    for (const [k, [section, section_title]] of Object.entries(sect)) {
      for (const x of d[k] || []) {
        const approval = k === 'decisions' || k === 'actions';
        feed.push({ section, section_title, topic_key: x.topic_key, topic_title: x.topic_title, kind: approval ? 'approval' : 'task', id: x.id, task_id: approval ? x.task_id : x.id, title: x.title, level: approval ? x.task_level : x.effective_level, priority: x.priority, risk: x.risk, action_type: x.action_type, status: x.status, ts: x.updated_at || x.created_at, expires_at: x.expires_at, headline: x.headline, gist: x.gist, keywords: x.keywords, digest_auto: x.digest_auto });
      }
    }
  }
  return feed.map((f) => {
    const r = raw.get(f.id) || {};
    const dg = digestOf({ ...r, ...f }, f.kind);
    if (f.kind === 'task') cacheTask({ ...r, ...f, id: f.id, headline: dg.headline });
    else cacheApproval({ ...r, id: f.id });
    const tkey = topicKeyOf({ ...r, ...f }); const ttitle = topicTitleOf({ ...r, ...f });
    CACHE.topicOf.set(f.id, { key: tkey, title: ttitle });
    return {
      topic_key: tkey, topic_title: ttitle,
      key: `${f.kind}:${f.id}`, dom: `${f.kind}_${f.id}`, kind: f.kind, id: f.id, task_id: f.task_id, section: f.section, section_title: f.section_title,
      headline: dg.headline, gist: dg.gist, keywords: dg.keywords, auto: dg.auto, title: f.title || r.title || '', level: f.level, priority: f.priority ?? r.priority,
      risk: f.risk, action_type: f.action_type, status: f.status, ts: f.ts, stamp: String(f.ts || ''), expires_at: f.expires_at,
      source: r.source, category: r.category, project: r.project, tone: f.risk === 'high' ? 'crit' : (TONE[f.section] || 'muted'),
      effective_level: f.level, level_cap: r.level_cap, due_at: r.due_at || f.due_at || null,
      rec_label: f.kind === 'approval' ? recLabel(r, recommendedKey) : '',
    };
  });
}

const ctl = new Split({
  id: 'inbox', title: '收件箱', icon: 'inbox', sections: INBOX_SECTIONS, autoSelect: false,
  async fetch() { const d = await api('/inbox'); return { items: inboxItems(d), extra: d.counts, topics: d.topics || [] }; },
  async detail(it) { return it.kind === 'approval' ? api(`/approvals/${enc(it.id)}`) : api(`/tasks/${enc(it.id)}`); },
  emptyAll: () => empty({ icon: 'check-check', title: '收件箱已清空', desc: '新的事项出现时会实时显示在这里。' }),
});

/** `g` then `t`: the topic list (switches to the 按话题 grouping, focuses the list, opens the current / first topic). */
ACTS['go-topics'] = () => {
  pref.set('inbox.group', 'topic');
  if (S.view !== 'inbox') { ctl.focusList = true; go('inbox'); return; }
  ctl.paintToolbar(); ctl.paintList();
  ctl.focusTopics();
};

export default {
  id: 'inbox', title: '收件箱', sub: '按项目、话题归并 · 先看背景和前情提要，再逐项处理', icon: 'inbox', split: true, softParams: ['sel'],
  load: () => ctl.load(),
  render: () => { setActiveSplit(ctl); return ctl.render(); },
  mount: () => { setActiveSplit(ctl); ctl.mount(); },
  refresh: () => ctl.refresh(),
  keys: (e) => splitKeys(e, ctl),
  skeleton: skeletonList,
  controller: ctl,
};
