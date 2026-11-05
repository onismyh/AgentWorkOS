// Table + filter-bar helpers shared by the list views.
import { h, raw } from '../core/html.js';
import { ACTS, CHANGES, INPUTS } from '../core/acts.js';
import { S } from '../core/state.js';
import { setParams } from '../core/router.js';
import { icon } from './icons.js';
import { empty } from './components.js';

export function table(cols, rows, { emptyTitle = '暂无数据', emptyDesc = '', cls = '', emptyIcon = 'inbox' } = {}) {
  if (!rows.length) return empty({ icon: emptyIcon, title: emptyTitle, desc: emptyDesc });
  return h`<div class="table-wrap"><table class="t${cls ? ` ${cls}` : ''}"><thead><tr>${cols.map((c) => {
    const col = typeof c === 'string' ? { label: c } : c;
    return h`<th class="${col.cls || ''}"${col.sort ? h` aria-sort="${col.sort}"` : ''}>${col.act ? h`<button type="button" class="th-b" data-act="${col.act}" data-key="${col.key}">${col.label}${col.sort ? icon(col.sort === 'ascending' ? 'chevron-up' : 'chevron-down', { size: 12 }) : ''}</button>` : col.label}</th>`;
  })}</tr></thead><tbody>${rows}</tbody></table></div>`;
}

export function filterChip(key, value, label, count) {
  const cur = new Set((S.params.get(key) || '').split(',').filter(Boolean));
  const on = cur.has(value);
  return h`<button type="button" class="chip-f${on ? ' on' : ''}" aria-pressed="${on}" data-act="toggle-param" data-key="${key}" data-value="${value}">${label}${count != null ? h` <b>${count}</b>` : ''}</button>`;
}
export function presetChip(key, value, label) {
  const on = (S.params.get(key) || '') === value;
  return h`<button type="button" class="chip-f${on ? ' on' : ''}" aria-pressed="${on}" data-act="set-param" data-key="${key}" data-value="${value}">${label}</button>`;
}
export function selectParam(key, options, allLabel) {
  const cur = S.params.get(key) || '';
  return h`<select class="select select-sm" data-change="param" data-key="${key}" aria-label="${allLabel}"><option value="">${allLabel}</option>${Object.entries(options).map(([k, v]) => h`<option value="${k}"${k === cur ? raw(' selected') : ''}>${v}</option>`)}</select>`;
}
export function searchParam(key, placeholder, { id = `f-${key}` } = {}) {
  return h`<div class="search-box">${icon('search', { size: 15 })}<input class="input input-bare" type="search" id="${id}" data-input="param" data-param="${key}" placeholder="${placeholder}" value="${S.params.get(key) || ''}" autocomplete="off" aria-label="${placeholder}"></div>`;
}
export const filterBar = (...parts) => h`<div class="filter-bar">${parts}</div>`;

ACTS['set-param'] = (el) => setParams(el.dataset.key === 'tab' ? { [el.dataset.key]: el.dataset.value, sel: null } : { [el.dataset.key]: el.dataset.value });
ACTS['toggle-param'] = (el) => {
  const cur = new Set((S.params.get(el.dataset.key) || '').split(',').filter(Boolean));
  if (cur.has(el.dataset.value)) cur.delete(el.dataset.value); else cur.add(el.dataset.value);
  setParams({ [el.dataset.key]: [...cur].join(',') });
};
CHANGES.param = (el) => setParams({ [el.dataset.key]: el.value });
const timers = {};
INPUTS.param = (el) => {
  const k = el.dataset.param;
  clearTimeout(timers[k]);
  timers[k] = setTimeout(() => setParams({ [k]: el.value.trim() }, { replace: true }), 350);
};
