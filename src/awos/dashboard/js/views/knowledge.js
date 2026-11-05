// 交付物 / 上下文 / 传感器.
import { h, raw, enc, setHTML, $, $$ } from '../core/html.js';
import { api, post, del } from '../core/api.js';
import { ACTS, CHANGES, FORMS } from '../core/acts.js';
import { S, cacheTask } from '../core/state.js';
import { setParams } from '../core/router.js';
import { when, bytes, cut, rel, absTime, dur } from '../core/format.js';
import { md } from '../core/markdown.js';
import { safeUrl } from '../core/html.js';
import { icon } from '../ui/icons.js';
import { btn, iconBtn, badge, card, empty, progress, skeletonList, skel, statusDot, tabs } from '../ui/components.js';
import { table, selectParam, searchParam, filterBar, filterChip } from '../ui/table.js';
import { dialog, toast, confirmDialog } from '../ui/overlay.js';
import { artifactPreview, artifactButtons, artifactIcon } from '../ui/reader.js';
import { META, sourceIcon, sourceLabel, digestOf } from '../domain/vocab.js';
import { factDialog, options } from '../domain/actions.js';
import { REL_KINDS, isProgressNote, loadProject, loadProjects, progressTitle, projectSlot } from '../domain/projects.js';
import { relatedRow } from '../ui/project-card.js';

/* ------------------------------------------------------------------ artifacts */
async function loadArtifacts() { return api('/artifacts?limit=1000'); }
function artifactsRender(d, p) {
  const term = (p.get('q') || '').toLowerCase(); const kind = p.get('kind') || '';
  const kinds = [...new Set(d.artifacts.map((a) => a.kind))].sort();
  const list = d.artifacts.filter((a) => (!kind || a.kind === kind) && (!term || `${a.title} ${a.path || ''} ${a.task_title || ''}`.toLowerCase().includes(term)));
  const grid = list.length ? h`<div class="art-grid">${list.map((a) => h`<article class="art-card"><div class="ac-h"><span class="ac-ic">${icon(artifactIcon(a), { size: 20 })}</span><div class="ac-m"><div class="ac-t">${a.title}</div><div class="ac-s">${badge(a.kind)}<span>${bytes(a.bytes)}</span><span>${when(a.created_at, { short: true })}</span></div></div></div>
    ${a.task_id ? h`<button type="button" class="ac-task" data-act="open-task" data-id="${a.task_id}">${icon('tasks', { size: 13 })}<span>${cut(a.task_title || a.task_id, 48)}</span></button>` : ''}
    ${a.path || a.uri ? h`<div class="ac-path mono" data-tip="${a.path || a.uri}">${cut((a.path || a.uri).split(/[\\/]/).slice(-2).join('/'), 56)}</div>` : ''}
    <div class="ac-a">${a.previewable ? btn('预览', { act: 'preview-artifact', data: { id: a.id, ext: a.ext, title: a.title }, size: 'sm', icon: 'eye', variant: 'secondary' }) : ''}${[a.servable && btn('打开', { href: `/api/artifacts/${enc(a.id)}/content`, external: true, size: 'sm', icon: 'external', variant: 'ghost' }), a.servable && btn('下载', { href: `/api/artifacts/${enc(a.id)}/content?download=1`, size: 'sm', icon: 'download', variant: 'ghost' }), a.uri && btn('链接', { href: safeUrl(a.uri), external: true, size: 'sm', icon: 'link', variant: 'ghost' })].filter(Boolean)}</div></article>`)}</div>` : empty({ icon: 'package', title: '还没有交付物', desc: 'Agent 产出的文件和链接会出现在这里。' });
  return h`${filterBar(searchParam('q', '按标题 / 路径 / 任务过滤'), selectParam('kind', Object.fromEntries(kinds.map((k) => [k, k])), '全部类型'), h`<span class="muted small">${list.length} 个</span>`)}${grid}`;
}
ACTS['preview-artifact'] = async (el) => {
  const body = await artifactPreview(el.dataset.id, el.dataset.ext);
  dialog({ title: el.dataset.title || '预览', body: h`<div class="art-prev">${body}</div>`, wide: true, hideSubmit: true, icon: 'eye' });
};

/* ------------------------------------------------------------------ context */
async function loadContext(p) {
  // the project list adds "关联话题 n · 待处理 m" to the project cards (null on an older server)
  const [d, projects] = await Promise.all([api(`/context?status=${p.get('all') === '1' ? 'all' : 'active,proposed'}`), loadProjects({ force: true }).catch(() => null)]);
  return { ...d, projects };
}

const factBadges = (f) => h`${f.pinned ? badge('已确认', { tone: 'good', icon: 'pin', variant: 'soft' }) : ''}${f.status === 'proposed' ? badge('待确认', { tone: 'warn', variant: 'soft' }) : f.status !== 'active' ? badge(f.status) : ''}`;
const factMeta = (f) => h`<span>置信度 ${Math.round((f.confidence || 0) * 100)}%</span><span>来源 ${f.source || '—'}</span><span>更新 ${rel(f.updated_at)}</span>`;
const factActions = (f) => h`${f.status === 'proposed' || !f.pinned ? btn(f.status === 'rejected' ? '恢复' : f.status === 'proposed' ? '确认' : '锁定', { act: 'fact-confirm', data: { id: f.id }, size: 'sm', variant: f.status === 'proposed' ? 'default' : 'secondary', icon: 'pin' }) : ''}${f.status !== 'rejected' ? h`${iconBtn('pencil', { act: 'fact-edit', data: { id: f.id }, tip: '编辑', size: 'icon-sm' })}${iconBtn('trash', { act: 'fact-delete', data: { id: f.id }, tip: '删除', size: 'icon-sm' })}` : ''}`;
const fact = (f) => h`<div class="fact${f.status === 'proposed' ? ' proposed' : ''}"><div class="f-k"><b>${f.key}</b><div class="f-m">${factBadges(f)}</div></div>
    <div class="f-v"><div>${f.value}</div><div class="f-m">${factMeta(f)}</div></div>
    <div class="f-a">${factActions(f)}</div></div>`;

/* ---- the AI's working notes (kind note, key `进展·…`, value usually `（项目名）…`): muted, grouped under their project, never projects ---- */
const normName = (s) => String(s ?? '').toLowerCase().replace(/[^0-9a-z一-鿿]+/g, '');
const LEAD = /^[（(]([^）)]{1,60})[）)]\s*/;
/** The project a working note belongs to: the leading （…） of its value matched against the project names (longest match wins). */
export function noteProject(note, projects) {
  const m = LEAD.exec(String(note.value ?? '').trim());
  const n = normName(m?.[1]);
  if (n.length < 2) return null;
  let best = null; let len = 0;
  for (const p of projects) { const pn = normName(p.key); if (pn.length >= 2 && (pn === n || pn.includes(n) || n.includes(pn)) && pn.length > len) { best = p; len = pn.length; } }
  return best;
}
const progressFact = (f, { stripLead = false } = {}) => h`<div class="fact progress"><div class="f-k"><b>${progressTitle(f.key)}</b></div>
    <div class="f-v"><div>${stripLead ? String(f.value ?? '').replace(LEAD, '') : f.value}</div><div class="f-m">${factMeta(f)}</div></div>
    <div class="f-a">${iconBtn('pencil', { act: 'fact-edit', data: { id: f.id }, tip: '编辑', size: 'icon-sm' })}${iconBtn('trash', { act: 'fact-delete', data: { id: f.id }, tip: '删除', size: 'icon-sm' })}</div></div>`;

function projectCardHTML(f, meta, notes) {
  const m = meta.get(f.key);
  const bits = m ? [m.deadlines ? `${m.deadlines} 个截止日期` : '', m.notes ? `${m.notes} 条笔记` : ''].filter(Boolean).join(' · ') : '';
  return h`<article class="pjc pj-c${projectSlot(f.key)}${f.status === 'proposed' ? ' proposed' : ''}" data-project-card="${f.key}">
    <header class="pjc-h"><span class="pjc-ic">${icon('folder', { size: 16 })}</span><div class="pjc-t"><b class="pjc-name">${f.key}</b><span class="pjc-badges">${factBadges(f)}</span></div><div class="f-a">${factActions(f)}</div></header>
    <p class="pjc-sum">${f.value}</p>
    <div class="f-m">${factMeta(f)}</div>
    ${m ? h`<div class="pjc-links">${m.topic_keys.length
      ? h`<a class="pjc-link" href="#/inbox?project=${enc(f.key)}" data-act="open-project-inbox" data-key="${f.key}">${icon('inbox', { size: 13 })}<span>关联话题 <b>${m.topic_keys.length}</b> · 待处理 <b>${m.open_items}</b></span>${icon('arrow-right', { size: 12 })}</a>`
      : h`<span class="pjc-link none">${icon('inbox', { size: 13 })}<span>暂无关联话题 · 待处理 0</span></span>`}
      ${m.last_activity ? h`<span class="pjc-act">最近动态 ${rel(m.last_activity)}</span>` : ''}</div>` : ''}
    ${m ? h`<details class="pjc-rel" id="pjrel-${f.id}" data-key="${f.key}"><summary data-act="pj-rel-toggle">${icon('chevron-right', { size: 13, cls: 'pjc-ch' })}<span>相关事实</span>${bits ? h`<small>${bits}</small>` : ''}</summary><div class="pjc-rel-b" data-pj-rel-body></div></details>` : ''}
    ${notes.length ? h`<details class="pjc-notes" id="pjn-${f.id}"><summary>${icon('chevron-right', { size: 13, cls: 'pjc-ch' })}<span>工作笔记</span><small>${notes.length} 条 · AI 自动记录</small></summary><div class="facts">${notes.map((n) => progressFact(n, { stripLead: true }))}</div></details>` : ''}
  </article>`;
}

function relatedFactsHTML(d) {
  const rows = REL_KINDS.flatMap((k) => (d.related?.[k.key] || []).filter((f) => !isProgressNote(f.key)).map((f) => ({ kind: k.key, key: f.key, value: f.value, progress: false })));
  return rows.length ? h`<ul class="pj-rel roomy">${rows.map((f) => relatedRow(f, { value: 220 }))}</ul>` : h`<p class="pjc-none">还没有相关的截止日期、人物或笔记。</p>`;
}
async function fillRelated(det) {
  const body = $('[data-pj-rel-body]', det); const key = det.dataset.key;
  if (!body || !key) return;
  if (!body.firstChild) setHTML(body, h`<div class="pj-fill-skel">${skel('50%', 11)}${skel('90%', 11)}${skel('72%', 11)}</div>`);
  try {
    const d = await loadProject(key);
    if (!body.isConnected) return;
    setHTML(body, d ? relatedFactsHTML(d) : h`<p class="pjc-none">项目详情暂时不可用。</p>`);
  } catch (e) { if (body.isConnected) setHTML(body, h`<p class="pjc-none err">无法加载：${e.message}</p>`); }
}
ACTS['pj-rel-toggle'] = (el) => { const det = el.closest('details'); if (!det) return; det.open = !det.open; if (det.open) fillRelated(det); };

let lastFocus = '';
function mountContext(el, d, p) {
  $$('details.pjc-rel[open]', el).forEach(fillRelated);   // restored open after a live re-render
  const key = p.get('project') || '';
  if (!key) { lastFocus = ''; return; }
  if (key === lastFocus) return;
  lastFocus = key;
  const card = $$('[data-project-card]', el).find((c) => c.dataset.projectCard === key);
  if (!card) return;
  const det = $('details.pjc-rel', card);
  if (det && !det.open) { det.open = true; fillRelated(det); }
  card.classList.add('focus');
  card.scrollIntoView({ block: 'center' });
  setTimeout(() => card.classList.remove('focus'), 2400);
}

function contextRender(d, p) {
  const kinds = { ...META.factKinds, ...d.kinds };
  const order = [...Object.keys(kinds), ...Object.keys(d.groups).filter((k) => !kinds[k])];
  S.facts = Object.values(d.groups).flat();
  // working notes are not facts about the user: they go under their project, or into a muted group of their own
  const notes = d.groups.note || [];
  const progress = notes.filter((f) => isProgressNote(f.key));
  const groups0 = { ...d.groups, note: notes.filter((f) => !isProgressNote(f.key)) };
  const projFacts = d.groups.project || [];
  const byProject = new Map(); const orphan = [];
  for (const n of progress) { const pj = noteProject(n, projFacts); if (pj) byProject.set(pj.key, [...(byProject.get(pj.key) || []), n]); else orphan.push(n); }
  const meta = new Map((d.projects || []).map((x) => [x.key, x]));
  const rank = new Map((d.projects || []).map((x, i) => [x.key, i]));
  groups0.project = [...projFacts].sort((a, b) => (rank.get(a.key) ?? 1e6) - (rank.get(b.key) ?? 1e6));
  const groups = order.filter((k) => groups0[k]?.length);
  const nLinked = projFacts.reduce((s, f) => s + (meta.get(f.key)?.open_items || 0), 0);
  const kindCard = (k) => (k === 'project'
    ? card({ title: kinds[k] || k, desc: d.projects ? `${groups0[k].length} 个项目 · 共 ${nLinked} 项待处理` : k, actions: h`<span class="tab-n">${groups0[k].length}</span>`, flush: true,
      body: h`<div class="pjc-list">${groups0[k].map((f) => projectCardHTML(f, meta, byProject.get(f.key) || []))}</div>` })
    : card({ title: kinds[k] || k, desc: k, actions: h`<span class="tab-n">${groups0[k].length}</span>`, flush: true, body: h`<div class="facts">${groups0[k].map(fact)}</div>` }));
  const orphanCard = orphan.length ? card({ title: '工作笔记', desc: 'AI 自动记录的进展，不是项目', cls: 'ctx-progress', actions: h`<span class="tab-n">${orphan.length}</span>`, flush: true,
    body: h`<details class="ctx-prog" id="ctx-prog"><summary>${icon('chevron-right', { size: 13, cls: 'pjc-ch' })}<span>展开 ${orphan.length} 条</span><small>没有对应到某个项目</small></summary><div class="facts">${orphan.map((n) => progressFact(n))}</div></details>` }) : '';
  return h`<form class="card ctx-add" data-form="add-fact"><header class="card-h"><div class="card-t"><span class="card-ic">${icon('plus', { size: 15 })}</span><h3>添加上下文</h3><p class="card-d">你添加或编辑的条目会被锁定，Agent 的自动刷新不会覆盖</p></div></header>
    <div class="card-b ctx-form"><select class="select" name="kind" aria-label="类别">${options(kinds, 'note')}</select><input class="input" name="key" placeholder="键（如：博士课题 / 导师 / 投稿截止）" required maxlength="200" aria-label="键"><textarea class="textarea" name="value" placeholder="内容" required rows="1" maxlength="8000" aria-label="内容"></textarea>${btn('添加', { variant: 'default', type: 'submit', icon: 'plus' })}</div></form>
    <div class="ctx-grid"><div class="ctx-main">${groups.length || orphan.length ? h`${groups.map(kindCard)}${orphanCard}` : empty({ icon: 'book', title: '还没有上下文条目' })}
      <label class="check small"><input type="checkbox" data-change="toggle-all-facts"${p.get('all') === '1' ? raw(' checked') : ''}><span>显示已删除 / 过期的条目</span></label></div>
      ${card({ title: 'profile.md', icon: 'file-text', desc: d.profile.exists ? `更新 ${rel(d.profile.updated_at)}` : '尚未生成', cls: 'ctx-profile', body: h`<div class="mono small muted pathline">${d.profile.path}</div>${d.profile.exists ? md(d.profile.content) : empty({ icon: 'file', title: '尚未生成', desc: '上下文刷新任务运行后会生成 profile.md' })}` })}</div>`;
}
CHANGES['toggle-all-facts'] = (el) => setParams({ all: el.checked ? '1' : null });
FORMS['add-fact'] = async (form) => {
  const f = Object.fromEntries(new FormData(form).entries());
  if (!f.key.trim() || !f.value.trim()) { toast('请填写键和内容', 'err'); return; }
  await post('/context', { kind: f.kind, key: f.key.trim(), value: f.value.trim() });
  form.reset(); toast('已添加'); await S.refresh(true);
};
ACTS['fact-edit'] = (el) => { const f = (S.facts || []).find((x) => x.id === el.dataset.id); if (f) factDialog(f); };
ACTS['fact-delete'] = async (el) => {
  if (!await confirmDialog('删除上下文条目', '该条目将标记为已删除，不再注入提示词。', { danger: true, submitLabel: '删除', icon: 'trash' })) return;
  await del(`/context/${enc(el.dataset.id)}`); toast('已删除'); await S.refresh(true);
};
ACTS['fact-confirm'] = async (el) => {
  const f = (S.facts || []).find((x) => x.id === el.dataset.id); if (!f) return;
  await post('/context', { kind: f.kind, key: f.key, value: f.value }); toast('已锁定'); await S.refresh(true);
};

/* ------------------------------------------------------------------ sensors */
async function loadSensors(p) {
  const src = p.get('source') || '';
  const [d, sg] = await Promise.all([api('/sensors'), api(`/signals?limit=150${src ? `&source=${enc(src)}` : ''}`)]);
  sg.signals.forEach((x) => { if (x.task_id) cacheTask({ id: x.task_id }); });
  return { d, sg };
}
const SIG_TONE = { new: 'brand', triaged: 'good', ignored: 'muted', error: 'crit' };
function sensorsRender({ d, sg }, p) {
  const src = p.get('source') || '';
  const cards = d.sensors.map((s) => {
    const tone = !s.enabled ? 'muted' : s.last_error ? 'crit' : 'good';
    return h`<article class="sensor tone-${tone}"><header><span class="sn-ic">${icon(sourceIcon(s.name), { size: 18 })}</span><div class="sn-t"><b>${s.name}</b><span class="muted small">${s.registered ? '' : '未注册（模块缺失）'}</span></div>
      ${badge(s.enabled ? (s.last_error ? '异常' : '正常') : s.config_enabled ? '已停用' : '未启用', { tone, icon: s.last_error ? 'circle-x' : s.enabled ? 'circle-check' : 'ban' })}</header>
      <dl class="kv sn-kv"><dt>间隔</dt><dd>${s.interval_minutes != null ? `${s.interval_minutes} 分钟` : '—'}${s.active_hours ? ` · ${s.active_hours.join('–')}` : ''}</dd><dt>上次成功</dt><dd>${when(s.last_ok_at)}</dd><dt>下次运行</dt><dd>${when(s.next_run_at)}</dd>
      <dt>上次新增</dt><dd>${s.last_count}</dd><dt>累计信号</dt><dd><button type="button" class="linkish" data-act="set-param" data-key="source" data-value="${s.name}">${s.signals_total}</button></dd></dl>
      ${s.last_error ? h`<pre class="errbox">连续失败 ${s.consecutive_failures} 次\n${cut(s.last_error, 300)}</pre>` : ''}
      <footer>${s.registered ? btn('立即运行', { act: 'run-sensor', data: { name: s.name }, size: 'sm', icon: 'play', variant: 'secondary' }) : ''}</footer></article>`;
  });
  const rows = sg.signals.map((x) => h`<tr><td class="nowrap">${when(x.observed_at)}</td><td><span class="exchip">${icon(sourceIcon(x.source), { size: 14 })}<span>${sourceLabel(x.source)}</span></span></td>
    <td class="title-cell"><div class="tc-t">${x.url ? h`<a href="${safeUrl(x.url)}" target="_blank" rel="noopener noreferrer">${x.title}</a>` : x.title}</div>${x.body ? h`<div class="tc-s" data-tip="${cut(x.body, 600)}">${cut(x.body, 140)}</div>` : ''}</td>
    <td>${badge(x.status, { tone: SIG_TONE[x.status] || 'muted' })}</td><td>${x.task_id ? h`<button type="button" class="linkish" data-act="open-task" data-id="${x.task_id}">${cut(x.task_title || x.task_id, 36)}</button>` : '—'}</td><td class="small muted">${cut(x.triage_note || '', 120)}</td></tr>`);
  return h`<div class="sensor-grid">${cards.length ? cards : empty({ icon: 'activity', title: '没有配置传感器' })}</div>
    <section class="sec-block"><div class="sec-block-h"><h3>最近信号</h3><span class="muted small">${src ? h`来源：${src} · <button type="button" class="linkish" data-act="set-param" data-key="source" data-value="">全部来源</button>` : '传感器产生的原始观测，经分拣后成为任务'}</span></div>
    ${table(['时间', '来源', '标题', '状态', '任务', '分拣说明'], rows, { emptyTitle: '还没有信号', emptyIcon: 'sparkles' })}</section>`;
}
ACTS['run-sensor'] = async (el) => {
  el.disabled = true;
  await post(`/sensors/${enc(el.dataset.name)}/run`);
  toast(`传感器 ${el.dataset.name} 已在后台运行`);
  setTimeout(() => S.refresh(true), 2500);
};

const mk = (id, title, sub, ic, load, render) => ({ id, title, sub, icon: ic, load: () => load(S.params), render: (d) => render(d, S.params), skeleton: skeletonList });
export const artifactsView = mk('artifacts', '交付物', 'Agent 产出的文件与链接', 'package', loadArtifacts, artifactsRender);
export const contextView = { ...mk('context', '上下文', '个人上下文知识库（你编辑的条目会被锁定，Agent 不会覆盖）', 'book', loadContext, contextRender), mount: (el, d, p) => mountContext(el, d, p) };
export const sensorsView = mk('sensors', '传感器', '信号来源与最近采集', 'activity', loadSensors, sensorsRender);
