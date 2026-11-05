// Navigation model shared by the shell and the command palette.
export const NAV_GROUPS = [
  { title: '工作台', items: [
    { key: 'overview', title: '总览', icon: 'dashboard' }, { key: 'board', title: '看板', icon: 'kanban' },
    { key: 'inbox', title: '收件箱', icon: 'inbox', badge: 'inbox' }, { key: 'outbox', title: 'Outbox', icon: 'send', badge: 'outbox' }] },
  { title: '执行', items: [
    { key: 'tasks', title: '任务', icon: 'tasks' }, { key: 'runs', title: '运行', icon: 'play', badge: 'runs' },
    { key: 'tools', title: '工具调用', icon: 'wrench' }, { key: 'errors', title: '异常', icon: 'alert', badge: 'errors' }] },
  { title: '知识', items: [
    { key: 'artifacts', title: '交付物', icon: 'package' }, { key: 'context', title: '上下文', icon: 'book' }, { key: 'sensors', title: '传感器', icon: 'activity' }] },
  { title: '系统', items: [{ key: 'history', title: '历史', icon: 'history' }] },
];
export const NAV_ITEMS = NAV_GROUPS.flatMap((g) => g.items.map((i) => ({ ...i, group: g.title })));
