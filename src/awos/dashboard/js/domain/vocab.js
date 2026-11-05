// Vocabulary: labels, tones, status presentation, digest derivation. Mutable META is filled from /api/meta at boot.
import { cut, TZ } from '../core/format.js';

export const META = {
  statusLabel: {
    inbox: '待分拣', triaged: '建议', ready: '排队中', blocked: '等待子任务', running: '运行中', waiting_approval: '待审批',
    waiting_user: '待接手', review: '待验收', done: '已完成', failed: '失败', cancelled: '已取消', dismissed: '已忽略', snoozed: '已推迟',
  },
  runLabel: { queued: '排队', running: '运行中', succeeded: '成功', failed: '失败', timeout: '超时', cancelled: '已取消', interrupted: '中断' },
  apvLabel: { pending: '待决策', approved: '已批准', rejected: '已拒绝', expired: '已过期', cancelled: '已取消', executing: '执行中', executed: '已执行', failed: '执行失败' },
  apvKind: { action: '动作', decision: '决策', review: '评审', permission: '权限', budget: '预算' },
  categories: {}, kinds: { todo: '待办事项', issue: '潜在问题', opportunity: '项目机会', decision: '需要决策', info: '信息' },
  levels: {}, executors: ['codex', 'claude', 'pi', 'dsh', 'internal'],
  factKinds: { role: '角色', project: '项目', goal: '目标', interest: '兴趣', habit: '习惯', preference: '偏好', person: '人物', deadline: '截止日期', note: '备注' },
  transitions: {}, version: '',
};

export const PURPOSES = { triage: '分拣', plan: '规划', execute: '执行', verify: '验证', sensor: '传感器', context: '上下文', action: '动作', digest: '摘要' };
export const ENTITY = { task: '任务', run: '运行', approval: '审批', signal: '信号', sensor: '传感器', context: '上下文', artifact: '交付物', system: '系统' };
export const EVENT_LABEL = {
  created: '创建', status_changed: '状态变更', updated: '更新', retried: '重试', decided: '决策', rejected: '已删除',
  collected: '采集', failed: '失败', cancel_requested: '请求取消', digests: '摘要生成',
};
export const EV_KIND = { message: '消息', tool_call: '工具调用', tool_result: '工具结果', reasoning: '推理', stderr: 'stderr', error: '错误', status: '状态', usage: '用量' };
export const TERMINAL = new Set(['done', 'failed', 'cancelled', 'dismissed']);
export const SENSITIVE_RE = /(send[_ -]?(mail|email|message)|smtp|post_message|publish|git\s+push|gh\s+(pr|release|issue)\s+(create|merge)|rm\s+-rf|remove-item|\bdel\s+\/|notion[\w.]*(create|update|append)|calendar[\w.]*(create|insert|update)|create_draft|reply_to)/i;

/* ---- status presentation: icon + tone (never colour alone) ---- */
export const STATUS_VIEW = {
  inbox: { icon: 'circle-dashed', tone: 'muted' },
  triaged: { icon: 'circle-dashed', tone: 'brand' },
  ready: { icon: 'circle-half', tone: 'muted' },
  blocked: { icon: 'circle-pause', tone: 'muted' },
  running: { icon: 'loader', tone: 'brand', spin: true },
  waiting_approval: { icon: 'shield-alert', tone: 'warn' },
  waiting_user: { icon: 'hand', tone: 'warn' },
  review: { icon: 'circle-dot', tone: 'warn' },
  done: { icon: 'circle-check', tone: 'good' },
  failed: { icon: 'circle-x', tone: 'crit' },
  cancelled: { icon: 'ban', tone: 'muted' },
  dismissed: { icon: 'ban', tone: 'muted' },
  snoozed: { icon: 'clock', tone: 'muted' },
};
export const RUN_VIEW = {
  queued: { icon: 'circle-dashed', tone: 'muted' }, running: { icon: 'loader', tone: 'brand', spin: true },
  succeeded: { icon: 'circle-check', tone: 'good' }, failed: { icon: 'circle-x', tone: 'crit' },
  timeout: { icon: 'clock', tone: 'crit' }, cancelled: { icon: 'ban', tone: 'muted' }, interrupted: { icon: 'circle-alert', tone: 'crit' },
};
export const APV_VIEW = {
  pending: { icon: 'shield-alert', tone: 'warn' }, approved: { icon: 'circle-check', tone: 'good' }, rejected: { icon: 'ban', tone: 'muted' },
  expired: { icon: 'clock', tone: 'muted' }, cancelled: { icon: 'ban', tone: 'muted' }, executing: { icon: 'loader', tone: 'brand', spin: true },
  executed: { icon: 'circle-check', tone: 'good' }, failed: { icon: 'circle-x', tone: 'crit' },
};

export const PRIORITY = [
  { key: 0, short: 'P0', label: '紧急', icon: 'flag' }, { key: 1, short: 'P1', label: '重要', icon: 'flag' },
  { key: 2, short: 'P2', label: '常规', icon: 'flag' }, { key: 3, short: 'P3', label: '有空再做', icon: 'flag' },
];

export const SENSOR_ICON = { mailmaster: 'mail', gmail: 'mail', git: 'git', files: 'folder', zotero: 'book', notion: 'file-text', agent_history: 'bot', inbox_drop: 'inbox', github: 'git', gcal: 'calendar' };
export const sourceIcon = (src) => {
  const s = String(src || '').replace(/^sensor:/, '');
  return SENSOR_ICON[s] || (s === 'manual' ? 'user' : s === 'subtask' ? 'tasks' : s === 'system' ? 'cpu' : 'sparkles');
};
export const SOURCE_LABEL = { mailmaster: '邮件', gmail: 'Gmail', git: 'Git', files: '文档', agent_history: 'AI 会话', notion: 'Notion', github: 'GitHub', zotero: 'Zotero', inbox_drop: '投递', manual: '手动', subtask: '子任务', system: '系统', gcal: '日历' };
export const sourceLabel = (src) => {
  const s = String(src || '').replace(/^sensor:/, '');
  return SOURCE_LABEL[s] || s || '—';
};

export const catLabel = (c) => (META.categories[c] ? META.categories[c].split(/[ /（]/)[0] : c || '—');
export const levelName = (n) => META.levels[n]?.name || `L${n}`;
export const effLevel = (t) => Number(t?.effective_level ?? t?.level ?? 1) || 1;
export const execLabel = (e) => e || '自动路由';
export const purposeLabel = (p) => PURPOSES[p] || p;

/** Executors keep one fixed series slot (identity, never rank): codex=1 claude=2 pi=3 dsh=4 internal=5. */
export const EXEC_SLOT = { codex: 1, claude: 2, pi: 3, dsh: 4, internal: 5 };
export const execSlot = (e) => EXEC_SLOT[e] || 5;

/** Headline / gist / keywords with client-side fallbacks when the API has none (older server, missing digest). */
export function digestOf(item, type = 'task') {
  const title = item.title || '';
  const headline = (item.headline && String(item.headline).trim()) || cut(title, 30) || '（无标题）';
  const gist = item.gist != null ? String(item.gist) : '';
  let kws = Array.isArray(item.keywords) ? item.keywords.map((k) => String(k).trim()).filter((k) => k && k !== 'other') : [];
  if (!kws.length) {
    if (type === 'approval') {
      kws = [META.apvKind[item.kind] || item.kind, item.action_type, item.risk ? `风险 ${({ low: '低', medium: '中', high: '高' })[item.risk] || item.risk}` : ''].filter(Boolean);
    } else {
      if (item.category && item.category !== 'other') kws.push(catLabel(item.category));
      if (item.project) kws.push(String(item.project).slice(0, 14));
      kws.push(`L${effLevel(item)}`);
      const s = sourceLabel(item.source);
      if (s && s !== '—' && s !== '手动' && s !== '子任务' && s !== '系统') kws.push(s);
    }
  }
  return { headline, gist, keywords: kws.slice(0, 6), auto: item.digest_auto !== false && !(item.headline && item.keywords?.length) };
}

/** Risk label + tone. */
export const RISK = {
  low: { label: '低风险', tone: 'good', icon: 'circle-check' },
  medium: { label: '中风险', tone: 'warn', icon: 'circle-alert' },
  high: { label: '高风险', tone: 'crit', icon: 'shield-alert' },
};

/** Pick the AI-recommended option of an approval (the recommendation text starts with / mentions an option key). */
export function recommendedKey(a) {
  const rec = String(a.recommendation || '').toLowerCase();
  if (!rec) return null;
  let best = null; let bi = Infinity;
  for (const o of a.options || []) {
    for (const s of [o.key, o.label]) {
      if (!s) continue;
      const i = rec.indexOf(String(s).toLowerCase());
      if (i >= 0 && i < bi) { bi = i; best = o.key; }
    }
  }
  return best;
}
export const isRejectish = (k) => /^(reject|no|cancel|deny|skip|defer|拒绝|否)/i.test(String(k));

export function applyMeta(m) {
  META.categories = m.categories || META.categories;
  META.kinds = m.kinds || META.kinds;
  META.levels = m.levels || META.levels;
  META.executors = m.executors || META.executors;
  META.factKinds = m.fact_kinds || META.factKinds;
  META.transitions = m.transitions || {};
  META.version = m.version || '';
  if (m.timezone) { try { new Intl.DateTimeFormat('zh-CN', { timeZone: m.timezone }); TZ.name = m.timezone; } catch { /* unknown zone: keep the browser's */ } }
  for (const s of m.task_statuses || []) META.statusLabel[s.key] = s.label;
  for (const s of m.run_statuses || []) META.runLabel[s.key] = s.label;
  for (const s of m.approval_statuses || []) META.apvLabel[s.key] = s.label;
  Object.assign(META.apvKind, m.approval_kinds || {});
}
