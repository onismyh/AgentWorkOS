// Human actions on tasks / approvals / context. Every destructive action asks for a reason (stored as learned feedback).
import { h, raw, $, $$ } from '../core/html.js';
import { api, post, patch, del, tq } from '../core/api.js';
import { enc } from '../core/html.js';
import { S } from '../core/state.js';
import { ACTS } from '../core/acts.js';
import { openTask } from '../core/router.js';
import { dialog, confirmDialog, toast, deferred } from '../ui/overlay.js';
import { META, TERMINAL, isRejectish } from './vocab.js';

export const ACTION_DONE = {
  accept: '已接受，进入队列', retry: '已重新排队', rework: '已提交返工意见', complete: '已标记完成',
  cancel: '已取消', dismiss: '已忽略', snooze: '已推迟',
};

const options = (map, cur) => h`${Object.entries(map).map(([k, v]) => h`<option value="${k}"${String(k) === String(cur ?? '') ? raw(' selected') : ''}>${v}</option>`)}`;
export const LEVEL_OPTS = () => Object.fromEntries([1, 2, 3, 4, 5].map((n) => [n, META.levels[n]?.name || `L${n}`]));
export const PRIO_OPTS = { 0: 'P0 紧急', 1: 'P1 重要', 2: 'P2 常规', 3: 'P3 有空再做' };
export { options };

const REASONS = {
  dismiss: ['与当前目标无关', '已经处理过了', '太频繁 / 太吵', '信息不准确', '以后不要再提醒这类事'],
  cancel: ['不需要了', '方向变了', '被别的任务取代', '风险太高'],
};
const reasonBlock = (kind) => h`<div class="reason-chips">${REASONS[kind].map((r) => h`<button type="button" class="chip chip-btn" data-act="fill-reason" data-text="${r}">${r}</button>`)}</div>`;
ACTS['fill-reason'] = (el) => {
  const ta = el.closest('form')?.querySelector('textarea[name=note]');
  if (ta) { ta.value = el.dataset.text; ta.focus(); }
};

export async function postAction(id, action, body = {}) {
  await post(`/tasks/${enc(id)}/actions/${action}`, body);
}

/** Ask whatever the action needs, then perform it. Resolves true when the action was sent. */
export async function taskAction(task, action, { ask = false, silent = false } = {}) {
  const id = typeof task === 'string' ? task : task.id;
  const title = typeof task === 'string' ? '' : (task.headline || task.title || '');
  const body = {};
  if (action === 'rework') {
    const r = await dialog({ title: '返工意见', icon: 'rotate', submitLabel: '提交返工', desc: title,
      body: h`<label class="field">告诉 AI 哪里需要修改（写入任务备注并重新执行）<textarea class="textarea" name="feedback" rows="6" required placeholder="例如：第 3 节的公式推导缺少中间步骤，请补全并保持符号一致"></textarea></label>` });
    if (!r) return false;
    body.feedback = r.feedback;
  } else if (action === 'snooze') {
    const r = await dialog({ title: '稍后处理', icon: 'clock', submitLabel: '推迟', desc: title, size: 'sm',
      body: h`<div class="radio-list">${[[4, '4 小时后'], [24, '明天（24 小时）'], [72, '3 天后'], [168, '1 周后'], [720, '30 天后']].map(([v, l], i) => h`<label class="radio"><input type="radio" name="hours" value="${v}"${i === 1 ? raw(' checked') : ''}><span>${l}</span></label>`)}</div>` });
    if (!r) return false;
    body.hours = Number(r.hours);
  } else if (action === 'cancel') {
    const r = await dialog({ title: '取消任务', icon: 'ban', danger: true, submitLabel: '确认取消', desc: title,
      body: h`<p class="dialog-p">将同时取消运行中的 Agent、全部未完成的子任务和待审批事项。</p>
        <label class="field">原因（可选）<textarea class="textarea" name="note" rows="2" placeholder="你的理由会被记录为偏好反馈，AI 以后会参考"></textarea></label>${reasonBlock('cancel')}` });
    if (!r) return false;
    body.note = r.note || null;
  } else if (action === 'dismiss') {
    const r = await dialog({ title: '忽略这件事', icon: 'x', danger: true, submitLabel: '忽略', desc: title,
      body: h`<label class="field">原因（可选）<textarea class="textarea" name="note" rows="2" placeholder="你的理由会被记录为偏好反馈，帮助 AI 以后少打扰你"></textarea></label>${reasonBlock('dismiss')}` });
    if (!r) return false;
    body.note = r.note || null;
  } else if (ask) {
    const r = await dialog({ title: action === 'retry' ? '交给 AI 继续 / 重试' : '附加说明', submitLabel: '提交', desc: title,
      body: h`<label class="field">给 AI 的补充说明（可选，写入任务备注）<textarea class="textarea" name="note" rows="4"></textarea></label><p class="hint">如需 AI 做得更多，可先在属性里调高自主等级。</p>` });
    if (!r) return false;
    body.note = r.note || null;
  }
  await postAction(id, action, body);
  if (!silent) toast(ACTION_DONE[action] || '已完成');
  await S.refresh(true);
  return true;
}

/** Accept / snooze with an undo window. `apply`/`revert` update the UI optimistically. */
export async function taskActionUndoable(task, action, { hours = 24, apply, revert } = {}) {
  const title = task.headline || task.title || '';
  const label = action === 'accept' ? `已接受「${title.slice(0, 22)}」` : `已推迟「${title.slice(0, 22)}」${hours >= 24 ? ` ${Math.round(hours / 24)} 天` : ` ${hours} 小时`}`;
  apply?.();
  const r = await deferred({
    msg: label, onUndo: () => revert?.(),
    run: async () => { await postAction(task.id, action, action === 'snooze' ? { hours } : {}); },
  });
  if (r === 'done') await S.refresh(true);
  return r === 'done';
}

export async function patchTask(id, fields, msg = '已更新') {
  await patch(`/tasks/${enc(id)}`, fields);
  toast(msg);
  await S.refresh(true);
}

export async function decideApproval(a, option, note, label) {
  const lab = label || option;
  if (a.risk === 'high' && !isRejectish(option)) {
    const ok = await confirmDialog('确认高风险决策', h`你将选择「<b>${lab}</b>」。这是一个<b>高风险</b>事项（${a.action_type || a.kind}），批准后会由 Action runner 执行对应动作。`, { submitLabel: '确认', icon: 'shield-alert' });
    if (!ok) return false;
  }
  await post(`/approvals/${enc(a.id)}/decide`, { option, note: note || null });
  toast(`已提交决策：${lab}`);
  await S.refresh(true);
  return true;
}

/* ------------------------------------------------------------------ dialogs: new task, edit task, facts */
export function newTaskDialog() {
  const execOpts = Object.fromEntries(META.executors.filter((x) => x !== 'internal').map((x) => [x, x]));
  const withAuto = (map, auto) => h`<option value="">${auto}</option>${options(map, '')}`;
  return dialog({
    title: '新建任务', icon: 'plus', submitLabel: '创建', wide: true,
    body: h`<div class="form-grid">
      <label class="field full">标题 *<input class="input" name="title" required maxlength="300" placeholder="一句话说明要做什么"></label>
      <label class="field full">描述<textarea class="textarea" name="description" rows="5" placeholder="背景、材料位置、期望产出……（支持 Markdown）"></textarea></label>
      <label class="field">自主等级<select class="select" name="level">${withAuto(LEVEL_OPTS(), '由 AI 评估')}</select></label>
      <label class="field">类别<select class="select" name="category">${withAuto(META.categories, '由 AI 判断')}</select></label>
      <label class="field">优先级<select class="select" name="priority">${options(PRIO_OPTS, 2)}</select></label>
      <label class="field">类型<select class="select" name="kind">${options(META.kinds, 'todo')}</select></label>
      <label class="field">执行器<select class="select" name="executor">${withAuto(execOpts, '自动路由')}</select></label>
      <label class="field">截止日期<input class="input" type="datetime-local" name="due_at"></label>
      <label class="field full">项目<input class="input" name="project" placeholder="可选：关联的项目 / 仓库路径"></label>
      <label class="check full"><input type="checkbox" name="triage" checked><span>先经过 AI 分拣（评估类别、等级与优先级）；不勾选则按上面的设置直接排队执行</span></label></div>`,
    onSubmit: async (f) => {
      const body = { title: f.title.trim(), description: f.description || null, priority: Number(f.priority), kind: f.kind, triage: !!f.triage };
      if (f.level) body.level = Number(f.level);
      if (f.category) body.category = f.category;
      if (f.executor) body.executor = f.executor;
      if (f.project) body.project = f.project;
      if (f.due_at) body.due_at = new Date(f.due_at).toISOString().replace(/\.\d{3}Z$/, 'Z');
      const r = await post('/tasks', body);
      toast('任务已创建');
      await S.refresh(true);
      openTask(r.id);
    },
  });
}

export async function editTaskDialog(id, cached) {
  const t = cached?.id === id ? cached : (await api(`/tasks/${enc(id)}`)).task;
  const local = t.due_at ? new Date(new Date(t.due_at).getTime() - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 16) : '';
  await dialog({
    title: '编辑任务', icon: 'pencil', submitLabel: '保存', wide: true,
    body: h`<div class="form-grid">
      <label class="field full">标题<input class="input" name="title" required maxlength="300" value="${t.title}"></label>
      <label class="field full">描述<textarea class="textarea" name="description" rows="8">${t.description || ''}</textarea></label>
      <label class="field full">验收标准<textarea class="textarea" name="acceptance" rows="4">${t.acceptance || ''}</textarea></label>
      <label class="field">项目<input class="input" name="project" value="${t.project || ''}"></label>
      <label class="field">截止<input class="input" type="datetime-local" name="due_at" value="${local}"></label></div>`,
    onSubmit: async (f) => {
      const body = {};
      for (const k of ['title', 'description', 'acceptance', 'project']) if ((f[k] || '') !== (t[k] || '')) body[k] = f[k];
      if (f.due_at && f.due_at !== local) body.due_at = new Date(f.due_at).toISOString().replace(/\.\d{3}Z$/, 'Z');
      if (!Object.keys(body).length) return;
      await patchTask(id, body, '任务已保存');
    },
  });
}

export async function factDialog(f) {
  const kinds = META.factKinds;
  await dialog({
    title: '编辑上下文', icon: 'pencil', submitLabel: '保存（锁定）',
    body: h`<div class="form-grid"><label class="field">类别<select class="select" name="kind">${options({ ...kinds, [f.kind]: kinds[f.kind] || f.kind }, f.kind)}</select></label>
      <label class="field">键<input class="input" name="key" required maxlength="200" value="${f.key}"></label>
      <label class="field full">内容<textarea class="textarea" name="value" rows="5" required maxlength="8000">${f.value}</textarea></label></div>`,
    onSubmit: async (v) => {
      await post('/context', { kind: v.kind, key: v.key.trim(), value: v.value.trim() });
      if (v.kind !== f.kind || v.key.trim() !== f.key) await del(`/context/${enc(f.id)}`);
      toast('已保存，并锁定为用户确认');
      await S.refresh(true);
    },
  });
}

export async function copyText(text) {
  try { await navigator.clipboard.writeText(text); toast('已复制'); } catch { toast(text, 'info'); }
}
export { TERMINAL };
