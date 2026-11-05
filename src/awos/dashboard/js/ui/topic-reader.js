// Topic reader (right pane when a topic header is selected): 现状, 前情提要 timeline, and "现在需要你" - every open item of the
// topic as a compact card with the plain-language question and one-click answers, so a whole topic can be cleared here.
import { h, raw, setHTML, $, $$, enc } from '../core/html.js';
import { api, post } from '../core/api.js';
import { ACTS } from '../core/acts.js';
import { S, CACHE, cacheApproval, cacheTask } from '../core/state.js';
import { absTime, cut, when } from '../core/format.js';
import { plain } from '../core/markdown.js';
import { icon } from './icons.js';
import { btn, badge, empty, levelBadge, riskBadge } from './components.js';
import { dialog, toast } from './overlay.js';
import { optionButtons, taskActionList } from './reader.js';
import { recapTimeline, briefNote, genBtnHTML } from './recap.js';
import { projectCard, mountProjectCards } from './project-card.js';
import { digestOf, recommendedKey } from '../domain/vocab.js';
import { SECTIONS, SECTION_VIEW, ageWord, countSections, dropTopicCache, normalizeEntries, oldTone } from '../domain/topics.js';

/** The little "决定 2 · 批准 1" pills (also used by the list header). */
export function countChips(counts, { cls = '' } = {}) {
  const on = SECTIONS.filter((s) => counts[s.key]);
  if (!on.length) return raw('');
  return h`<span class="tg-counts${cls ? ` ${cls}` : ''}">${on.map((s) => h`<span class="tg-c tone-${s.tone}" data-tip="${s.title} ${counts[s.key]} 项">${s.label} <b>${counts[s.key]}</b></span>`)}</span>`;
}

function needCard(it, ask, ctx) {
  const dg = digestOf(it, it.kind);
  const sec = SECTION_VIEW[it.section] || { title: it.section_title || '', tone: 'muted', icon: 'circle' };
  const key = `${it.kind}:${it.id}`;
  const approval = it.kind === 'approval' ? CACHE.approvals.get(it.id) : null;
  if (it.kind === 'task') cacheTask({ id: it.id, status: it.status, title: it.title, headline: dg.headline });
  const question = ask?.question || dg.headline;
  const about = ask?.question && dg.headline !== question ? dg.headline : '';
  let actions;
  if (it.kind === 'approval') {
    actions = approval?.options
      ? h`<div class="decide compact" data-approval="${it.id}">${optionButtons(approval)}</div>`
      : h`<div class="decide compact" data-approval="${it.id}" data-lazy="need-opts" data-id="${it.id}"><div class="skel-block"><span class="skel" style="width:60%;height:34px"></span></div></div>`;
  } else {
    const acts = taskActionList({ status: it.status }).filter((a) => a.action !== 'cancel').slice(0, 3);
    actions = acts.length ? h`<div class="need-btns">${acts.map((a) => btn(a.label, {
      act: 'task-action', data: { id: it.id, action: a.action, ask: a.ask ? '1' : null }, variant: a.variant === 'default' ? 'default' : 'secondary', size: 'sm', icon: a.icon }))}</div>` : '';
  }
  const href = `#/${ctx.hasItem(key) ? ctx.view : 'inbox'}?sel=${enc(key)}`;
  return h`<article class="need tone-${sec.tone}" data-need="${key}">
    <header class="need-h">${badge(sec.title, { tone: sec.tone, variant: 'soft', icon: sec.icon })}${it.kind === 'approval' ? riskBadge(it.risk) : it.level ? levelBadge(it) : ''}<span class="grow"></span><span class="need-w">${when(it.ts, { short: true })}</span></header>
    <h4 class="need-q">${question}</h4>
    ${about ? h`<p class="need-about">关于：${about}${dg.gist ? ` — ${cut(dg.gist, 80)}` : ''}</p>` : (!ask && dg.gist ? h`<p class="need-about">${dg.gist}</p>` : '')}
    ${ask?.why_now ? h`<p class="need-why">${icon('hourglass', { size: 13 })}<span>${ask.why_now}</span></p>` : ''}
    ${ask?.stakes ? h`<p class="need-stakes"><b>利害</b>${ask.stakes}</p>` : ''}
    ${approval?.recommendation ? h`<p class="need-rec">${icon('sparkles', { size: 13 })}<span>AI 的理由：${plain(approval.recommendation, 120)}</span></p>` : ''}
    ${actions}
    <footer class="need-f"><a class="linkish" href="${href}" data-act="open-item" data-key="${key}" data-view="${ctx.hasItem(key) ? ctx.view : 'inbox'}">查看详情${it.kind === 'approval' ? '、写备注' : ''}${icon('arrow-right', { size: 12 })}</a></footer></article>`;
}

/**
 * `d` is the /api/topics/{key} payload (or a local fallback). ctx: {view, hasItem(selKey)}.
 */
export function topicReader(d, ctx = {}) {
  const c = { view: 'inbox', hasItem: () => true, ...ctx };
  const brief = d.brief || null;
  const hist = normalizeEntries(d.history);
  const recap = brief ? normalizeEntries(brief.recap) : [];
  const entries = recap.length ? recap : hist;
  const items = d.items || [];
  const counts = countSections(items);
  const asks = brief?.asks || [];
  const oldest = items.map((i) => i.ts).filter(Boolean).sort()[0] || null;
  const last = hist.slice(-1)[0];
  return h`<article class="reader topic-reader" data-reader="topic" data-key="${d.key}">
    <header class="rd-head tp-head">
      <div class="tp-top"><span class="tp-kicker">${icon('layers', { size: 13 })}话题</span><span class="grow"></span>
        ${items.length ? btn('这个话题都不用管了…', { act: 'topic-dismiss', data: { key: d.key }, size: 'sm', variant: 'ghost-destructive', icon: 'ban' }) : ''}</div>
      <h1 class="rd-title">${d.title}</h1>
      <div class="rd-meta">${countChips(counts, { cls: 'big' })}${oldest ? h`<span class="meta-i tone-${oldTone(oldest)}" data-tip="最早的一项出现于 ${absTime(oldest, true)}"><span class="age-ink">${icon('clock', { size: 13 })}最早 ${ageWord(oldest)}前</span></span>` : ''}
        ${hist.length ? h`<span class="meta-i muted">${icon('history', { size: 13 })}<span>${hist.length} 条记录</span></span>` : ''}</div>
    </header>
    <div class="rd-body">
      ${d.project === undefined ? '' : projectCard(d.project, { key: d.key, title: d.title })}
      <section class="tp-now" aria-labelledby="tp-now-h"><h2 id="tp-now-h">${icon('sparkles', { size: 14 })}现状</h2>
        ${brief?.state_now ? h`<p>${brief.state_now}</p>`
          : last ? h`<p>${last.text}<span class="muted"> · ${when(last.ts)}</span></p>` : h`<p class="muted">这个话题还没有任何记录。</p>`}</section>
      <section class="tp-card" aria-labelledby="tp-rc-h">
        <header class="tp-card-h"><h2 id="tp-rc-h">${icon('history', { size: 15 })}前情提要${entries.length ? h`<span class="rd-sec-n">${entries.length}</span>` : ''}</h2><span class="grow"></span>${d._fallback ? '' : genBtnHTML(d.key, { label: brief ? '重新生成提要' : '生成提要' })}</header>
        <div class="tp-card-b">${briefNote(d.key, brief, { button: false })}
          ${recapTimeline(entries)}
          ${brief && hist.length ? h`<details class="rc-full" id="tpfull-${d.key}"><summary>${icon('chevron-right', { size: 13, cls: 'rc-fch' })}<span>查看完整经过</span><small>${hist.length} 条系统记录</small></summary><div class="rc-full-b">${recapTimeline(hist, { cls: 'compact' })}</div></details>` : ''}</div></section>
      <section class="tp-card tp-needs" aria-labelledby="tp-need-h">
        <header class="tp-card-h"><h2 id="tp-need-h">${icon('hand', { size: 15 })}现在需要你${items.length ? h`<span class="rd-sec-n">${items.length}</span>` : ''}</h2>${items.length > 1 ? h`<span class="tp-hint">在这里就能一次处理完</span>` : ''}</header>
        <div class="tp-card-b">${items.length ? h`<div class="need-list">${items.map((it) => needCard(it, asks.find((a) => a.id === it.id), c))}</div>`
          : empty({ icon: 'check-check', title: '这个话题没有待处理的事项', desc: '有新的问题时会出现在这里。' })}</div></section>
    </div></article>`;
}

/** Fill the option buttons of approvals this pane has not seen full data for (a topic opened without the inbox buckets), and the 所属项目 card. */
export function mountTopicReader(root) {
  mountProjectCards(root);
  $$('[data-lazy="need-opts"]', root).forEach(async (slot) => {
    try {
      const d = await api(`/approvals/${enc(slot.dataset.id)}`);
      const a = cacheApproval(d.approval);
      if (slot.isConnected) { delete slot.dataset.lazy; setHTML(slot, optionButtons(a)); }
    } catch (e) { if (slot.isConnected) setHTML(slot, h`<span class="err-text small">无法加载选项：${e.message}</span>`); }
  });
}

/* ------------------------------------------------------------------ "这个话题都不用管了…" */
const REASONS = ['已经处理完了', '这个项目不用再追踪', '与当前目标无关', '太频繁 / 太吵'];
ACTS['topic-dismiss'] = async (el) => {
  const key = el.dataset.key;
  const card = el.closest('[data-reader="topic"]');
  const title = $('.rd-title', card)?.textContent || '';
  const n = $$('[data-need]', card).length;
  let res = null;
  const r = await dialog({
    title: '这个话题都不用管了？', icon: 'ban', danger: true, submitLabel: `忽略全部 ${n} 项`, desc: title,
    body: h`<p class="dialog-p">会<b>忽略这个话题下 ${n} 项</b>未处理的事项：停止正在运行的工作，撤回正在等你回答的问题。</p>
      <label class="field">原因（必填，至少两个字）<textarea class="textarea" name="note" rows="3" required minlength="2" placeholder="例：论文已投稿，不用再追踪"></textarea></label>
      <div class="reason-chips">${REASONS.map((x) => h`<button type="button" class="chip chip-btn" data-act="fill-reason" data-text="${x}">${x}</button>`)}</div>
      <p class="hint">你写的原因会被记住：AI 以后不会再为这类事情来打扰你。</p>`,
    onSubmit: async (f) => {
      const note = String(f.note || '').trim();
      if (note.length < 2) throw new Error('请至少写两个字的原因');
      res = await post(`/topics/${enc(key)}/dismiss`, { note });
    },
  });
  if (!r) return;
  toast(`已忽略 ${res?.dismissed ?? n} 项，原因已记住`);
  dropTopicCache(key);
  window.dispatchEvent(new CustomEvent('awos:topic-dismissed', { detail: { key } }));
  await S.refresh(true);
};

