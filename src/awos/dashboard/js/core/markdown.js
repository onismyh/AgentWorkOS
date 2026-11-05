// Escape-first Markdown renderer. Input is untrusted (mail bodies, web pages, agent output).
// The whole source is HTML-escaped before any tag is produced; links go through safeUrl().
import { esc, unesc, raw, safeUrl } from './html.js';

export function inline(src) {
  const codes = [];
  let t = String(src).replace(/`([^`\n]+)`/g, (_, c) => { codes.push(c); return `\u0000${codes.length - 1}\u0000`; });
  t = esc(t);
  t = t.replace(/\[([^\]\n]+)\]\(([^)\s]+)\)/g, (_, txt, url) =>
    `<a href="${esc(safeUrl(unesc(url)))}" target="_blank" rel="noopener noreferrer">${txt}</a>`);
  t = t.replace(/(^|[\s(（:：])(https?:\/\/[^\s<)）]+)/g, (_, pre, url) =>
    `${pre}<a href="${url}" target="_blank" rel="noopener noreferrer">${url}</a>`);
  t = t.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>').replace(/__([^_\n]+)__/g, '<strong>$1</strong>');
  t = t.replace(/(^|[^*\w])\*([^*\n]+)\*(?!\*)/g, '$1<em>$2</em>');
  t = t.replace(/~~([^~\n]+)~~/g, '<del>$1</del>');
  t = t.replace(/\n/g, '<br>');
  return t.replace(/\u0000(\d+)\u0000/g, (_, n) => `<code>${esc(codes[+n])}</code>`);
}

const LIST_RE = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/;
const cells = (r) => r.trim().replace(/^\||\|$/g, '').split('|').map((c) => c.trim());

function listBlock(lines, i) {
  // Collect consecutive list lines (with indentation-based nesting) starting at lines[i].
  const items = [];
  while (i < lines.length) {
    const lm = lines[i].match(LIST_RE);
    if (lm) { items.push({ indent: lm[1].replace(/\t/g, '  ').length, ordered: /\d/.test(lm[2]), text: lm[3], more: [] }); i++; continue; }
    if (items.length && lines[i].trim() && /^\s{2,}\S/.test(lines[i])) { items[items.length - 1].more.push(lines[i].trim()); i++; continue; }
    break;
  }
  const build = (from, indent) => {
    let html = ''; let k = from;
    const ordered = items[k].ordered;
    html += ordered ? '<ol>' : '<ul>';
    while (k < items.length && items[k].indent >= indent) {
      if (items[k].indent > indent) { // deeper item without a parent: treat as sibling
        k++; continue;
      }
      const it = items[k];
      let body = it.text + (it.more.length ? `\n${it.more.join('\n')}` : '');
      const cb = body.match(/^\[([ xX])\]\s+([\s\S]*)$/);
      let lead = '';
      if (cb) { lead = `<span class="md-cb${cb[1] === ' ' ? '' : ' on'}" aria-hidden="true"></span>`; body = cb[2]; }
      k++;
      let child = '';
      if (k < items.length && items[k].indent > indent) {
        const sub = build(k, items[k].indent);
        child = sub.html; k = sub.next;
      }
      html += `<li${cb ? ' class="md-task"' : ''}>${lead}${inline(body)}${child}</li>`;
    }
    html += ordered ? '</ol>' : '</ul>';
    return { html, next: k };
  };
  return { html: build(0, items[0].indent).html, next: i };
}

export function mdBlocks(text) {
  const lines = text.split('\n');
  const out = [];
  const para = [];
  const flush = () => { if (para.length) { out.push(`<p>${inline(para.join('\n'))}</p>`); para.length = 0; } };
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    let m = line.match(/^\s*(```|~~~)\s*([\w+-]*)/);
    if (m) {
      flush();
      const fence = m[1]; const lang = m[2]; const buf = []; i++;
      while (i < lines.length && !lines[i].trim().startsWith(fence)) buf.push(lines[i++]);
      i++;
      out.push(`<pre${lang ? ` data-lang="${esc(lang)}"` : ''}><code>${esc(buf.join('\n'))}</code></pre>`);
      continue;
    }
    if (!line.trim()) { flush(); i++; continue; }
    m = line.match(/^(#{1,6})\s+(.*)$/);
    if (m) { flush(); out.push(`<h${m[1].length}>${inline(m[2])}</h${m[1].length}>`); i++; continue; }
    if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) { flush(); out.push('<hr>'); i++; continue; }
    if (/^\s*>/.test(line)) {
      flush();
      const buf = [];
      while (i < lines.length && /^\s*>/.test(lines[i])) buf.push(lines[i++].replace(/^\s*>\s?/, ''));
      out.push(`<blockquote>${mdBlocks(buf.join('\n'))}</blockquote>`);
      continue;
    }
    if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < lines.length && /^\s*\|?\s*:?-{2,}/.test(lines[i + 1])) {
      flush();
      const rows = [];
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) rows.push(lines[i++]);
      const head = cells(rows[0]);
      const align = cells(rows[1]).map((c) => (/^:-+:$/.test(c) ? 'center' : /-:$/.test(c) ? 'right' : ''));
      const td = (tag, c, k) => `<${tag}${align[k] ? ` style="text-align:${align[k]}"` : ''}>${inline(c)}</${tag}>`;
      const body = rows.slice(2).map(cells);
      out.push(`<div class="md-table"><table><thead><tr>${head.map((c, k) => td('th', c, k)).join('')}</tr></thead><tbody>${
        body.map((r) => `<tr>${r.map((c, k) => td('td', c, k)).join('')}</tr>`).join('')}</tbody></table></div>`);
      continue;
    }
    if (LIST_RE.test(line)) {
      flush();
      const r = listBlock(lines, i);
      out.push(r.html); i = r.next;
      continue;
    }
    para.push(line); i++;
  }
  flush();
  return out.join('');
}

/** Render markdown to a Safe `<div class="md">`. */
export const md = (src) => (src ? raw(`<div class="md">${mdBlocks(String(src).replace(/\r\n?/g, '\n'))}</div>`) : raw(''));

/** Strip markdown to a single plain-text line (for gists / snippets). Plain string: pass through h`` to render. */
export function plain(src, n = 140) {
  const s = String(src ?? '')
    .replace(/```[\s\S]*?```/g, ' ')
    .replace(/!?\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/[#>*_`~|]/g, ' ')
    .replace(/^\s*[-+]\s+/gm, '')
    .replace(/\s+/g, ' ')
    .trim();
  return s.length > n ? `${s.slice(0, n)}…` : s;
}
