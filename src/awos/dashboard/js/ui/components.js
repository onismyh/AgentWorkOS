// Component library (shadcn/ui-inspired). Every function returns Safe markup; text args are escaped by h``.
import { h, raw, join, esc, Safe } from '../core/html.js';
import { icon } from './icons.js';
import { META, STATUS_VIEW, RUN_VIEW, APV_VIEW, RISK, effLevel, levelName, catLabel, execSlot } from '../domain/vocab.js';
import { projectSlot } from '../domain/projects.js';

/** data-* attributes from an object, e.g. dataAttrs({id: 'x'}) -> ' data-id="x"'. Keys must be constant identifiers. */
export function dataAttrs(obj = {}) {
  const parts = [];
  for (const [k, v] of Object.entries(obj)) {
    if (v != null && v !== false) parts.push(new Safe(` data-${k.replace(/[^a-z0-9-]/gi, '')}="${esc(v)}"`));
  }
  return join(parts);
}
export const tipAttr = (t) => (t ? h` data-tip="${t}"` : raw(''));

/* ------------------------------------------------------------------ Button */
export function btn(label, o = {}) {
  const { act, data, variant = 'secondary', size = '', icon: ic, tip, kbd: kb, disabled, cls = '', href, type = 'button', aria } = o;
  const klass = `btn btn-${variant}${size ? ` btn-${size}` : ''}${cls ? ` ${cls}` : ''}`;
  const inner = h`${ic ? icon(ic, { size: size === 'sm' ? 14 : 16 }) : ''}${label ? h`<span class="btn-label">${label}</span>` : ''}${kb ? kbd(kb) : ''}`;
  const common = h`class="${klass}"${act ? h` data-act="${act}"` : ''}${dataAttrs(data)}${tipAttr(tip)}${aria ? h` aria-label="${aria}"` : ''}`;
  if (href) return h`<a ${common} href="${href}"${o.external ? raw(' target="_blank" rel="noopener noreferrer"') : ''}>${inner}</a>`;
  return h`<button type="${type}" ${common}${disabled ? raw(' disabled') : ''}>${inner}</button>`;
}
export function iconBtn(ic, o = {}) {
  const { act, data, variant = 'ghost', size = 'icon', tip, aria, cls = '', disabled } = o;
  return h`<button type="button" class="btn btn-${variant} btn-${size}${cls ? ` ${cls}` : ''}"${act ? h` data-act="${act}"` : ''}${dataAttrs(data)}${tipAttr(tip)} aria-label="${aria || tip || ''}"${disabled ? raw(' disabled') : ''}>${icon(ic, { size: size === 'icon-sm' ? 14 : 16 })}</button>`;
}

/* ------------------------------------------------------------------ Badge / Chip */
export function badge(content, { tone = 'muted', icon: ic, variant = 'outline', tip, cls = '' } = {}) {
  return h`<span class="badge badge-${variant} tone-${tone}${cls ? ` ${cls}` : ''}"${tipAttr(tip)}>${ic ? icon(ic, { size: 12 }) : ''}<span>${content}</span></span>`;
}
export function statusIcon(status, view = STATUS_VIEW) {
  const v = view[status] || { icon: 'circle', tone: 'muted' };
  return h`<span class="sico tone-${v.tone}${v.spin ? ' spin' : ''}">${icon(v.icon, { size: 14 })}</span>`;
}
const stBadge = (view, labels) => (s, { tip } = {}) => {
  const v = view[s] || { icon: 'circle', tone: 'muted' };
  return badge(labels()[s] || s, { tone: v.tone, icon: v.icon, tip, cls: v.spin ? 'spin' : '' });
};
export const statusBadge = stBadge(STATUS_VIEW, () => META.statusLabel);
export const runBadge = stBadge(RUN_VIEW, () => META.runLabel);
export const apvBadge = stBadge(APV_VIEW, () => META.apvLabel);
export function riskBadge(r) {
  const v = RISK[r] || RISK.medium;
  return badge(v.label, { tone: v.tone, icon: v.icon, variant: r === 'high' ? 'soft' : 'outline' });
}
export const statusDot = (tone = 'muted', { pulse = false, label = '' } = {}) =>
  h`<span class="sdot tone-${tone}${pulse ? ' pulse' : ''}" ${label ? h`role="img" aria-label="${label}"` : raw('aria-hidden="true"')}></span>`;

/** Keyword chip. Clickable when `act` is given. */
export function kwChip(text, { act, data } = {}) {
  return act
    ? h`<button type="button" class="chip chip-btn" data-act="${act}" data-kw="${text}"${dataAttrs(data)}>${text}</button>`
    : h`<span class="chip">${text}</span>`;
}
export function kwChips(list, { max = 4, act } = {}) {
  const items = (list || []).filter(Boolean);
  if (!items.length) return raw('');
  const shown = items.slice(0, max);
  const rest = items.length - shown.length;
  return h`<span class="chips">${shown.map((k) => kwChip(k, { act }))}${rest > 0 ? h`<span class="chip chip-more" data-tip="${items.slice(max).join('、')}">+${rest}</span>` : ''}</span>`;
}

/**
 * Project chip: a folder mark in the project's colour + the project name (the name is always printed, so identity never relies on
 * colour alone). Passive <span> by default; with `act` it is a button. `empty` renders the dashed "未关联项目" chip for no project.
 */
export function projectChip(p, { act, data, cls = '', empty = false, tip = true, label } = {}) {
  if (!p?.key && !empty) return raw('');
  const none = !p?.key;
  const name = none ? (label || '未关联项目') : p.name || p.key;
  const klass = `pj-chip ${none ? 'pj-none' : `pj-c${projectSlot(p.key)}`}${cls ? ` ${cls}` : ''}`;
  const tipText = none ? '这个话题还没有关联项目' : `项目：${name}${p.summary ? `\n${p.summary}` : ''}`;
  const inner = h`${icon(none ? 'circle-dashed' : 'folder', { size: 12 })}<span class="pj-n">${name}</span>`;
  const attrs = h`${act ? h` data-act="${act}"` : ''}${dataAttrs(data)}${tip ? tipAttr(tipText) : ''}`;
  return act ? h`<button type="button" class="${klass}"${attrs}>${inner}</button>` : h`<span class="${klass}"${attrs}>${inner}</span>`;
}

/* ------------------------------------------------------------------ Level / Priority / Executor */
export function levelBadge(t, { full = false } = {}) {
  const e = effLevel(t);
  const lvl = Number(t.level ?? e);
  const capped = t.level_cap && Number(t.level_cap) < lvl;
  const tip = `${levelName(e)}${capped ? `（评估 L${lvl}，策略上限 L${t.level_cap}）` : ''}`;
  return h`<span class="lvl lv-${e}${capped ? ' capped' : ''}"${tipAttr(tip)}>L${e}${full ? h`<span class="lvl-name">${(META.levels[e]?.name || '').replace(/^L\d\s*/, '')}</span>` : ''}</span>`;
}
const PRI_TIP = ['P0 · 紧急', 'P1 · 重要', 'P2 · 常规', 'P3 · 有空再做'];
export function prioIcon(p, { label = false } = {}) {
  if (p == null) return raw('');
  const n = Number(p);
  const bars = n === 0 ? '' : [0, 1, 2].map((i) => `<rect x="${3 + i * 6}" y="${14 - (i + 1) * 4}" width="4" height="${(i + 1) * 4 + 2}" rx="1" ${i < 4 - n ? '' : 'opacity=".25"'}/>`).join('');
  const urgent = '<rect x="2" y="2" width="16" height="16" rx="4"/><path d="M10 6v5M10 13.4v.1" stroke="#fff" stroke-width="2" stroke-linecap="round" fill="none"/>';
  return h`<span class="pri pri-${n}"${tipAttr(PRI_TIP[n] || '')}><svg width="16" height="16" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">${raw(n === 0 ? urgent : bars)}</svg>${label ? h`<span>P${n}</span>` : ''}</span>`;
}
const EX_MARK = { codex: 'Cx', claude: 'Cl', pi: 'π', dsh: 'Ds', internal: '·' };
export function execAvatar(name, { size = 20, tip = true } = {}) {
  const n = name || 'auto';
  if (!name) return h`<span class="exav ex-auto" style="--sz:${size}px"${tipAttr('自动路由')}>${icon('sparkles', { size: Math.round(size * 0.6) })}</span>`;
  return h`<span class="exav ex-${execSlot(name)}" style="--sz:${size}px"${tip ? tipAttr(n) : ''}>${EX_MARK[n] || String(n).slice(0, 2)}</span>`;
}
export const execChip = (name) => h`<span class="exchip">${execAvatar(name, { size: 18, tip: false })}<span>${name || '自动'}</span></span>`;

/* ------------------------------------------------------------------ Card / Tabs / Segmented */
export function card({ title, desc, actions, body, cls = '', flush = false, icon: ic, id } = {}) {
  return h`<section class="card${cls ? ` ${cls}` : ''}"${id ? h` id="${id}"` : ''}>${title || actions ? h`<header class="card-h"><div class="card-t">${ic ? h`<span class="card-ic">${icon(ic, { size: 15 })}</span>` : ''}<h3>${title}</h3>${desc ? h`<p class="card-d">${desc}</p>` : ''}</div>${actions ? h`<div class="card-a">${actions}</div>` : ''}</header>` : ''}<div class="card-b${flush ? ' flush' : ''}">${body}</div></section>`;
}

export function tabs(items, current, { act = 'set-param', key = 'tab', label = '' } = {}) {
  return h`<div class="tabs" role="tablist"${label ? h` aria-label="${label}"` : ''}>${items.map((it) => h`<button type="button" role="tab" class="tab${it.key === current ? ' on' : ''}" aria-selected="${it.key === current}" data-act="${act}" data-key="${key}" data-value="${it.key}">${it.label}${it.count != null ? h`<span class="tab-n${it.hot ? ' hot' : ''}">${it.count}</span>` : ''}</button>`)}</div>`;
}
export function seg(items, current, { act, key, label = '' } = {}) {
  return h`<div class="seg" role="group"${label ? h` aria-label="${label}"` : ''}>${items.map((it) => h`<button type="button" class="${it.key === current ? 'on' : ''}" aria-pressed="${it.key === current}" data-act="${act}" data-key="${key}" data-value="${it.key}"${tipAttr(it.tip)}>${it.icon ? icon(it.icon, { size: 14 }) : ''}${it.label ? h`<span>${it.label}</span>` : ''}</button>`)}</div>`;
}

export function progress(frac, { tone = 'good', label = '', tip } = {}) {
  const p = Math.max(0, Math.min(1, Number(frac) || 0));
  return h`<span class="progress tone-${tone}"${tipAttr(tip)}><span class="progress-bar" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Math.round(p * 100)}"><i style="width:${(p * 100).toFixed(1)}%"></i></span>${label ? h`<span class="progress-l">${label}</span>` : ''}</span>`;
}

export const kbd = (keys) => h`<span class="kbds">${String(keys).split(' ').map((k) => h`<kbd class="kbd">${k}</kbd>`)}</span>`;

export function empty({ icon: ic = 'inbox', title, desc, action } = {}) {
  return h`<div class="empty"><span class="empty-ic">${icon(ic, { size: 22 })}</span><div class="empty-t">${title}</div>${desc ? h`<div class="empty-d">${desc}</div>` : ''}${action ? h`<div class="empty-a">${action}</div>` : ''}</div>`;
}
export function errorState(msg, { retry = true } = {}) {
  return empty({ icon: 'circle-alert', title: '加载失败', desc: msg, action: retry ? btn('重试', { act: 'refresh', icon: 'refresh', size: 'sm' }) : '' });
}

/** Skeleton blocks. */
export const skel = (w = '100%', hh = 12, cls = '') => h`<span class="skel ${cls}" style="width:${w};height:${hh}px"></span>`;
export function skeletonList(n = 6) {
  return h`<div class="skel-list" aria-busy="true" aria-label="加载中">${Array.from({ length: n }, (_, i) => h`<div class="skel-row"><span class="skel skel-dot"></span><div class="skel-col">${skel(`${60 + ((i * 17) % 30)}%`, 12)}${skel(`${35 + ((i * 11) % 35)}%`, 10)}</div></div>`)}</div>`;
}
export function skeletonPage() {
  return h`<div class="skel-page" aria-busy="true" aria-label="加载中"><div class="skel-kpis">${Array.from({ length: 5 }, () => h`<div class="card skel-card">${skel('40%', 10)}${skel('55%', 26)}${skel('70%', 10)}</div>`)}</div><div class="skel-grid"><div class="card skel-card tall">${skel('30%', 12)}${skel('100%', 10)}${skel('92%', 10)}${skel('96%', 10)}${skel('80%', 10)}</div><div class="card skel-card tall">${skel('30%', 12)}${skel('100%', 90)}</div></div></div>`;
}

/** key / value definition list (payload previews, properties). */
export function kvList(rows, { cls = '' } = {}) {
  return h`<dl class="kv${cls ? ` ${cls}` : ''}">${rows.filter((r) => r && r[1] != null && r[1] !== '').map(([k, v]) => h`<dt>${k}</dt><dd>${v}</dd>`)}</dl>`;
}

/** A meta row item: icon + text. */
export const meta = (ic, content, tip) => h`<span class="meta-i"${tipAttr(tip)}>${icon(ic, { size: 13 })}${content}</span>`;
export const sep = () => raw('<span class="meta-sep" aria-hidden="true">·</span>');
export { join, catLabel, icon };
