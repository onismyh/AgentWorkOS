// 所属项目: the card at the top of a topic reader (project, summary, link source, related facts, counts), the "更改关联…" dialog
// (pick an existing project / create one / unlink) and the small navigation actions shared by every project chip.
import { h, raw, setHTML, $, $$, enc } from '../core/html.js';
import { post } from '../core/api.js';
import { ACTS, CHANGES, INPUTS } from '../core/acts.js';
import { S } from '../core/state.js';
import { go } from '../core/router.js';
import { cut } from '../core/format.js';
import { icon } from './icons.js';
import { btn, badge, skel } from './components.js';
import { dialog, toast } from './overlay.js';
import { dropTopicCache } from '../domain/topics.js';
import { FOCUS, REL_KINDS, cachedProject, dropProjectCache, linkSource, loadProject, loadProjects, noteTopicProjects, pickRelated, progressTitle,
  projectOfTopic, projectSlot } from '../domain/projects.js';

const REL_ICON = Object.fromEntries(REL_KINDS.map((k) => [k.key, k.icon]));

/** One related fact (deadline / person / goal / note): icon, name, value. The AI's working notes are muted. */
export const relatedRow = (f, { value = 120 } = {}) => h`<li class="pj-rel-i${f.progress ? ' progress' : ''}"><span class="pj-rel-ic" data-tip="${f.progress ? '工作笔记' : (REL_KINDS.find((k) => k.key === f.kind)?.label || '')}">${icon(REL_ICON[f.kind] || 'file-text', { size: 13 })}</span>
  <span class="pj-rel-t"><b>${f.progress ? progressTitle(f.key) : f.key}</b><span class="pj-rel-v">${cut(f.value, value)}</span></span></li>`;

/* ------------------------------------------------------------------ the card in the topic reader */
const linkBadge = (project) => {
  const s = linkSource(project);
  return badge(s.label, { tone: project.link_source === 'user' ? 'user' : 'brand', variant: 'soft', icon: s.icon, tip: s.tip, cls: 'pj-src' });
};

/** `project` is the topic's `project` (or null); `topic` {key, title}. The counts / related facts arrive lazily (mountProjectCards). */
export function projectCard(project, topic) {
  const change = (variant, label) => btn(label, { act: 'topic-project', data: { key: topic.key, title: topic.title }, size: 'sm', variant, icon: project?.key ? 'pencil' : 'link', kbd: 'P' });
  if (!project?.key) {
    return h`<section class="pj-card pj-card-none" data-pj-card data-topic="${topic.key}" aria-label="所属项目">
      <div class="pj-h"><span class="pj-tile">${icon('circle-dashed', { size: 18 })}</span>
        <div class="pj-hd"><span class="pj-kicker">所属项目</span><h2 class="pj-name">这个话题还没有关联项目</h2></div><span class="grow"></span>${change('secondary', '关联项目…')}</div>
      <p class="pj-sum">关联后，AI 整理前情提要时会带上项目的背景，你也能在「上下文」里看到它的话题和待处理事项。</p></section>`;
  }
  return h`<section class="pj-card pj-c${projectSlot(project.key)}" data-pj-card data-project="${project.key}" data-topic="${topic.key}" aria-label="所属项目：${project.name}">
    <div class="pj-h"><span class="pj-tile">${icon('folder', { size: 18 })}</span>
      <div class="pj-hd"><span class="pj-kicker">所属项目${linkBadge(project)}</span><h2 class="pj-name">${project.name}</h2></div><span class="grow"></span>${change('secondary', '更改关联…')}</div>
    ${project.summary ? h`<p class="pj-sum">${project.summary}</p>` : ''}
    <div class="pj-fill" data-pj-fill aria-busy="true"><div class="pj-fill-skel">${skel('46%', 11)}${skel('88%', 11)}</div></div></section>`;
}

function fillHTML(d, topicKey) {
  const topics = d.topics || [];
  const open = topics.reduce((s, t) => s + (t.counts?.total || 0), 0);
  const sibs = topics.filter((t) => t.key !== topicKey).slice(0, 3);
  const rel = pickRelated(d.related, 3);
  return h`<div class="pj-stats"><span class="pj-count">本项目共 <b>${topics.length}</b> 个话题 · <b>${open}</b> 项待处理</span><span class="grow"></span>
      ${topics.length > 1 ? btn('只看这个项目', { act: 'open-project-inbox', data: { key: d.key }, size: 'sm', variant: 'ghost', icon: 'filter' }) : ''}
      ${btn('在上下文中查看', { act: 'open-context', data: { key: d.key }, size: 'sm', variant: 'ghost', icon: 'book' })}</div>
    ${sibs.length ? h`<div class="pj-sibs"><span class="pj-sibs-l">同项目的其他话题</span>${sibs.map((t) => h`<button type="button" class="pj-sib" data-act="open-topic" data-key="${t.key}" data-tip="${t.title}">${icon('layers', { size: 11 })}<span>${t.title}</span></button>`)}</div>` : ''}
    ${rel.length ? h`<ul class="pj-rel" aria-label="相关事实">${rel.map((f) => relatedRow(f))}</ul>` : ''}`;
}

async function fillCard(card) {
  const slot = $('[data-pj-fill]', card); const key = card.dataset.project;
  if (!slot || !key) return;
  const hit = cachedProject(key);
  if (hit) { setHTML(slot, fillHTML(hit.data, card.dataset.topic)); slot.removeAttribute('aria-busy'); }
  try {
    const d = await loadProject(key);
    if (!slot.isConnected) return;
    if (!d) { slot.remove(); return; }                  // older server / the project was deleted meanwhile
    setHTML(slot, fillHTML(d, card.dataset.topic)); slot.removeAttribute('aria-busy');
  } catch (e) {
    if (slot.isConnected && !hit) { setHTML(slot, h`<p class="pj-err">${icon('circle-alert', { size: 13 })}无法加载项目详情：${e.message}</p>`); slot.removeAttribute('aria-busy'); }
  }
}

/** Fill the lazy parts of every 所属项目 card under `root`; pulse the card once when a project chip sent the user here. */
export function mountProjectCards(root) {
  $$('[data-pj-card]', root).forEach((card) => {
    fillCard(card);
    if (FOCUS.topic && FOCUS.topic === card.dataset.topic) {
      FOCUS.topic = null;
      card.classList.add('flash');
      setTimeout(() => card.classList.remove('flash'), 1800);
    }
  });
}

/* ------------------------------------------------------------------ navigation shared by chips / cards / palette */
ACTS['open-project-inbox'] = (el) => go('inbox', { project: el.dataset.key });
ACTS['open-context'] = (el) => go('context', { project: el.dataset.key });

/* ------------------------------------------------------------------ 更改关联… */
const optionHTML = (p, checked) => h`<label class="pj-opt${checked ? ' cur' : ''}" data-search="${`${p.name} ${p.summary || ''}`.toLowerCase()}">
  <input type="radio" name="choice" value="${`p:${p.key}`}" data-change="pj-choice"${checked ? raw(' checked') : ''}>
  <span class="pj-opt-b"><span class="pj-opt-t"><span class="pj-dot pj-c${projectSlot(p.key)}">${icon('folder', { size: 13 })}</span><b>${p.name}</b>${checked ? h`<span class="pj-cur">当前</span>` : ''}${p.open_items ? h`<small>${p.open_items} 项待处理</small>` : ''}</span>
    ${p.summary ? h`<span class="pj-opt-s">${cut(p.summary, 110)}</span>` : ''}</span></label>`;

INPUTS['pj-filter'] = (el) => {
  const q = el.value.trim().toLowerCase();
  const form = el.closest('form');
  let shown = 0;
  $$('.pj-opt[data-search]', form).forEach((o) => { const on = !q || o.dataset.search.includes(q); o.hidden = !on; if (on) shown += 1; });
  const none = $('[data-pj-nomatch]', form); if (none) none.hidden = shown > 0;
};
CHANGES['pj-choice'] = (el) => {
  const form = el.closest('form');
  const isNew = el.value === '__new__';
  const box = $('[data-pj-new]', form); if (!box) return;
  box.hidden = !isNew;
  if (isNew) $('input[name="new_name"]', box)?.focus();
};

/**
 * Pick the project of a topic: an existing one (searchable), a new one (name + one-line description) or none.
 * POST /api/topics/{key}/project, then the caller's view is refreshed. Resolves true when something was saved.
 */
export async function changeProjectDialog({ key, title = '' }) {
  let list;
  try { list = await loadProjects({ force: true, maxAge: 3000 }); } catch (e) { toast(`无法读取项目列表：${e.message}`, 'err'); return false; }
  if (list == null) { toast('服务器还不支持项目关联', 'warn'); return false; }
  const cur = projectOfTopic(key);
  const sorted = [...list].sort((a, b) => Number(b.key === cur?.key) - Number(a.key === cur?.key));
  let res = null; let sent = null;
  const pending = dialog({
    title: cur ? '更改所属项目' : '关联项目', icon: 'folder', submitLabel: '保存', desc: title ? `话题：${title}` : '',
    body: h`<div class="pj-pick">
      <div class="search-box">${icon('search', { size: 15 })}<input class="input input-bare" type="search" data-input="pj-filter" placeholder="搜索项目…" autocomplete="off" aria-label="搜索项目"></div>
      <div class="pj-opts" role="radiogroup" aria-label="已有项目">${sorted.length ? sorted.map((p) => optionHTML(p, p.key === cur?.key)) : h`<p class="pj-none-yet">还没有项目，可以在下面新建一个。</p>`}<p class="pj-nomatch" data-pj-nomatch hidden>没有匹配的项目</p></div>
      <div class="pj-extra">
        <label class="pj-opt"><input type="radio" name="choice" value="__new__" data-change="pj-choice"><span class="pj-opt-b"><span class="pj-opt-t"><span class="pj-dot pj-new-dot">${icon('plus', { size: 13 })}</span><b>新建项目…</b></span></span></label>
        <div class="pj-new" data-pj-new hidden>
          <label class="field">项目名称<input class="input" name="new_name" maxlength="60" placeholder="例：学位论文 thuthesis" autocomplete="off"></label>
          <label class="field">一句话说明<input class="input" name="new_summary" maxlength="200" placeholder="这是什么、现在做到哪一步" autocomplete="off"></label></div>
        <label class="pj-opt"><input type="radio" name="choice" value="__none__" data-change="pj-choice"><span class="pj-opt-b"><span class="pj-opt-t"><span class="pj-dot pj-none-dot">${icon('ban', { size: 13 })}</span><b>不属于任何项目</b></span></span></label></div>
      <p class="hint">你指定的关联会被记住：AI 以后不会再改动。</p></div>`,
    onSubmit: async (f) => {
      const choice = String(f.choice || '');
      if (!choice) throw new Error('请选择一个项目，或选择「不属于任何项目」');
      const body = { project_key: null, new_name: null, new_summary: null };
      if (choice === '__new__') {
        const name = String(f.new_name || '').trim();
        if (name.length < 2) throw new Error('请填写新项目的名称（至少两个字）');
        body.new_name = name; body.new_summary = String(f.new_summary || '').trim() || null;
      } else if (choice.startsWith('p:')) body.project_key = choice.slice(2);
      sent = body;
      res = await post(`/topics/${enc(key)}/project`, body);
    },
  });
  // Enter in the search box picks the first match instead of submitting
  const form = $('#dialog-host form');
  form?.addEventListener('keydown', (e) => {
    if (e.key !== 'Enter' || !e.target.matches?.('[data-input="pj-filter"]')) return;
    e.preventDefault();
    const first = $$('.pj-opt[data-search]:not([hidden]) input', form)[0];
    if (first) { first.checked = true; first.dispatchEvent(new Event('change', { bubbles: true })); first.focus(); }
  });
  const ok = await pending;
  if (!ok || !res) return false;
  const p = res.project || null;
  toast(p ? (sent?.new_name ? `已新建项目「${p.name}」并关联` : `已关联到「${p.name}」`) : '已设为「不属于任何项目」');
  noteTopicProjects([{ key, project: p }]);
  dropTopicCache(key); dropProjectCache();
  loadProjects({ force: true }).catch(() => {});
  window.dispatchEvent(new CustomEvent('awos:project-linked', { detail: { key } }));
  await S.refresh(true);
  return true;
}

ACTS['topic-project'] = async (el) => { await changeProjectDialog({ key: el.dataset.key, title: el.dataset.title || '' }); };

