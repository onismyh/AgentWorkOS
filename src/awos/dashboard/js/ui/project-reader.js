// Project reader (right pane when a project header with several topics is selected): what the project is, its topics, the facts
// from 上下文 that belong to it and the latest tasks. The data is GET /api/projects/{key} plus the list's own topic groups.
import { h, enc } from '../core/html.js';
import { icon } from './icons.js';
import { btn, empty, statusBadge } from './components.js';
import { countChips } from './topic-reader.js';
import { relatedRow } from './project-card.js';
import { when } from '../core/format.js';
import { ageWord, oldTone, topicSel } from '../domain/topics.js';
import { pickRelated, projectSlot } from '../domain/projects.js';

/**
 * `d` = the /api/projects/{key} payload (or a local fallback {_fallback, key, name, summary}); `pg` = the list's project group
 * ({key, project, name, topics, counts, oldest_ts}); ctx {view}.
 */
export function projectReader(d, pg, ctx = {}) {
  const view = ctx.view || 'inbox';
  const name = d?.name || pg.name;
  const summary = d?.summary || pg.project?.summary || '';
  const related = pickRelated(d?.related, 99);
  const tasks = d?.recent_tasks || [];
  const topicRow = (t) => h`<a class="pj-trow" href="#/${view}?sel=${enc(topicSel(t.key))}" data-act="split-select" data-key="${topicSel(t.key)}">
    <span class="pj-trow-h"><span class="pj-trow-t">${t.title}</span>${t.oldest_ts ? h`<span class="tg-age tone-${oldTone(t.oldest_ts)}">${icon('clock', { size: 11 })}最早 ${ageWord(t.oldest_ts)}前</span>` : ''}</span>
    <span class="pj-trow-c">${countChips(t.counts)}</span>
    ${t.state_now ? h`<span class="pj-trow-s">${t.state_now}</span>` : ''}</a>`;
  return h`<article class="reader topic-reader project-reader pj-c${projectSlot(pg.key)}" data-reader="project" data-key="${pg.key}">
    <header class="rd-head tp-head">
      <div class="tp-top"><span class="tp-kicker pj-kick">${icon('folder', { size: 13 })}项目</span><span class="grow"></span>
        ${btn('只看这个项目', { act: 'open-project-inbox', data: { key: pg.key }, size: 'sm', variant: 'ghost', icon: 'filter' })}
        ${btn('在上下文中查看', { act: 'open-context', data: { key: pg.key }, size: 'sm', variant: 'secondary', icon: 'book' })}</div>
      <h1 class="rd-title">${name}</h1>
      <div class="rd-meta">${countChips(pg.counts, { cls: 'big' })}<span class="meta-i muted">${icon('layers', { size: 13 })}<span>${pg.topics.length} 个话题</span></span>
        ${pg.oldest_ts ? h`<span class="meta-i tone-${oldTone(pg.oldest_ts)}" data-tip="这个项目里等得最久的一项"><span class="age-ink">${icon('clock', { size: 13 })}最早 ${ageWord(pg.oldest_ts)}前</span></span>` : ''}</div>
    </header>
    <div class="rd-body">
      <section class="tp-now pj-now" aria-labelledby="pj-now-h"><h2 id="pj-now-h">${icon('folder', { size: 14 })}项目简介</h2>
        ${summary ? h`<p>${summary}</p>` : h`<p class="muted">这个项目还没有简介。可以在「上下文」里补充。</p>`}</section>
      <section class="tp-card" aria-labelledby="pj-topics-h">
        <header class="tp-card-h"><h2 id="pj-topics-h">${icon('layers', { size: 15 })}话题<span class="rd-sec-n">${pg.topics.length}</span></h2><span class="tp-hint">点开一个话题，先看前情提要</span></header>
        <div class="tp-card-b pj-topics">${pg.topics.map(topicRow)}</div></section>
      ${related.length ? h`<section class="tp-card" aria-labelledby="pj-rel-h">
        <header class="tp-card-h"><h2 id="pj-rel-h">${icon('book', { size: 15 })}相关事实<span class="rd-sec-n">${related.length}</span></h2><span class="tp-hint">来自你的上下文</span></header>
        <div class="tp-card-b"><ul class="pj-rel roomy">${related.map((f) => relatedRow(f, { value: 220 }))}</ul></div></section>` : ''}
      ${tasks.length ? h`<section class="tp-card" aria-labelledby="pj-tasks-h">
        <header class="tp-card-h"><h2 id="pj-tasks-h">${icon('tasks', { size: 15 })}最近的任务<span class="rd-sec-n">${tasks.length}</span></h2></header>
        <div class="tp-card-b"><ul class="pj-tasks">${tasks.map((t) => h`<li><button type="button" class="pj-task" data-act="open-task" data-id="${t.id}"><span class="pj-task-t">${t.headline || t.title}</span><span class="pj-task-r">${statusBadge(t.status)}<span class="pj-task-w">${when(t.updated_at, { short: true })}</span></span></button></li>`)}</ul></div></section>`
        : (d?._fallback ? '' : empty({ icon: 'tasks', title: '还没有任务记录' }))}
    </div></article>`;
}
