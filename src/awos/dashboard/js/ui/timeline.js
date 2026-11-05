// Event timeline rendering (task history, audit log).
import { h } from '../core/html.js';
import { absTime, cut } from '../core/format.js';
import { META, ENTITY, EVENT_LABEL } from '../domain/vocab.js';
import { empty, kwChip } from './components.js';

export function describeEvent(e) {
  const dt = e.data && typeof e.data === 'object' ? e.data : {};
  const label = EVENT_LABEL[e.type] || e.type;
  if (e.type === 'status_changed') {
    const map = e.entity_type === 'run' ? META.runLabel : e.entity_type === 'approval' ? META.apvLabel : META.statusLabel;
    return h`<span class="ev-type">${label}</span> ${dt.from ? `${map[dt.from] || dt.from} → ` : ''}${map[dt.to] || dt.to || ''}${dt.reason ? h` <span class="muted">· ${cut(dt.reason, 200)}</span>` : ''}${dt.error ? h`<div class="err-text small">${cut(dt.error, 300)}</div>` : ''}`;
  }
  if (e.type === 'decided') return h`<span class="ev-type">${label}</span> ${dt.decision || ''} <span class="muted">· ${dt.via || ''}</span>${dt.note ? h`<div class="small">${cut(dt.note, 300)}</div>` : ''}`;
  if (e.type === 'updated') {
    const parts = Object.entries(dt).filter(([k]) => !['old', 'new'].includes(k)).map(([k, v]) => `${k} = ${cut(typeof v === 'object' ? JSON.stringify(v) : v, 80)}`);
    if (dt.new != null) parts.push(`${cut(dt.old ?? '', 60)} → ${cut(dt.new, 120)}`);
    return h`<span class="ev-type">${label}</span> <span class="muted">${parts.join('，')}</span>`;
  }
  if (e.type === 'created') return h`<span class="ev-type">${label}</span> <span class="muted">${cut(dt.title || dt.value || dt.purpose || '', 160)}${dt.source ? ` · ${dt.source}` : ''}${dt.executor ? ` · ${dt.executor}` : ''}</span>`;
  const rest = e.data == null ? '' : typeof e.data === 'object' ? JSON.stringify(e.data) : String(e.data);
  return h`<span class="ev-type">${label}</span> <span class="muted">${cut(rest, 240)}</span>`;
}

export function timeline(events) {
  if (!events?.length) return empty({ icon: 'history', title: '没有历史事件' });
  return h`<ol class="timeline">${events.map((e) => h`<li><span class="tl-dot"></span><div class="tl-b"><div class="tl-h"><span class="tl-ent">${ENTITY[e.entity_type] || e.entity_type}</span><time title="${absTime(e.ts, true)}">${absTime(e.ts)}</time><span class="tl-actor">${e.actor}</span></div><div class="tl-t">${describeEvent(e)}</div></div></li>`)}</ol>`;
}
export { kwChip };
