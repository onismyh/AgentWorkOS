"""Static / structural tests for the dashboard front-end (no browser needed; Node is used when available).

Checks: same-origin assets only, no external URLs, every JS module parses and links, the CSP in api/app.py is satisfied
(no inline scripts / handlers / eval), no raw innerHTML assignments of dynamic data, the XSS-safe rendering helpers
behave, the served module graph resolves, and a few pure-logic helpers (drop rules, digests, inbox rows) are correct.
"""
from __future__ import annotations

import json
import posixpath
import re
import shutil
import subprocess
from pathlib import Path

import pytest

DASH = Path(__file__).resolve().parents[1] / "src" / "awos" / "dashboard"
JS_FILES = sorted(DASH.rglob("*.js"))
TEXT_FILES = sorted(p for p in DASH.rglob("*") if p.is_file() and p.suffix in {".js", ".css", ".html"})
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")

INDEX = (DASH / "index.html").read_text(encoding="utf-8")
# Files allowed to build markup from template literals through raw()/new Safe(): they escape what they interpolate.
RAW_TEMPLATE_OK = {"core/markdown.js", "ui/icons.js"}
SAFE_CTOR_OK = {"core/html.js", "ui/components.js"}
INNERHTML_OK = {"core/html.js"}


def rel(p: Path) -> str:
    return p.relative_to(DASH).as_posix()


def run_node(code: str, *, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run([NODE, "--input-type=module", "-e", code], capture_output=True, text=True, timeout=timeout, encoding="utf-8")


# --------------------------------------------------------------------------- assets


def test_dashboard_has_expected_files():
    assert (DASH / "index.html").is_file() and (DASH / "app.js").is_file() and (DASH / "style.css").is_file()
    assert len(JS_FILES) >= 20, "the dashboard is split into ES modules under js/"


def test_index_references_only_same_origin_assets():
    refs = re.findall(r'(?:src|href|action)="([^"]*)"', INDEX)
    assert refs, "index.html references assets"
    for ref in refs:
        assert not re.match(r"^(?:[a-z][a-z0-9+.-]*:)?//", ref, re.I) or ref.startswith("data:"), f"external reference: {ref}"
        assert ref.startswith(("/static/", "#", "data:")), f"unexpected reference {ref!r}"
    assert INDEX.count("__AWOS_TOKEN__") == 1, "the token placeholder is injected exactly once"
    assert '<script type="module" src="/static/app.js">' in INDEX


def test_no_external_urls_in_any_dashboard_file():
    for p in TEXT_FILES:
        text = p.read_text(encoding="utf-8")
        for m in re.finditer(r"(?:https?:)?//[A-Za-z0-9.-]+\.[A-Za-z]{2,}[^\s'\"`)]*", text):
            url = m.group(0)
            if url.startswith("http://www.w3.org/2000/svg") or url.startswith("//www.w3.org/2000/svg"):
                continue  # SVG namespace inside inline data: URIs
            if p.suffix == ".js" and re.search(r"^\s*(//|\*)", text[: m.start()].rsplit("\n", 1)[-1] + "//"):
                pass
            pytest.fail(f"{rel(p)} mentions external URL {url!r} (everything must work offline)")


def test_no_web_fonts_or_cdn_imports():
    for p in TEXT_FILES:
        text = p.read_text(encoding="utf-8")
        assert "@font-face" not in text, f"{rel(p)}: web fonts are not allowed"
        for m in re.finditer(r"@import\s+url\(([^)]+)\)", text):
            target = m.group(1).strip("'\" ")
            assert not re.match(r"^(https?:)?//", target), f"{rel(p)} imports {target}"
            assert (p.parent / target).resolve().is_file(), f"{rel(p)}: @import {target} does not exist"


# --------------------------------------------------------------------------- JS modules


@needs_node
@pytest.mark.parametrize("path", JS_FILES, ids=[rel(p) for p in JS_FILES])
def test_js_module_parses(path):
    r = subprocess.run([NODE, "--input-type=module", "--check"], input=path.read_text(encoding="utf-8"), capture_output=True,
                       text=True, timeout=30, encoding="utf-8")
    assert r.returncode == 0, f"{rel(path)}: {r.stderr[:600]}"


def test_relative_imports_resolve():
    imp = re.compile(r"""(?:import|export)\s[^'"`;]*?from\s*['"](\.{1,2}/[^'"]+)['"]|import\(\s*['"](\.{1,2}/[^'"]+)['"]\s*\)""")
    for p in JS_FILES:
        for m in imp.finditer(p.read_text(encoding="utf-8")):
            target = m.group(1) or m.group(2)
            assert (p.parent / target).resolve().is_file(), f"{rel(p)} imports missing module {target}"


@needs_node
def test_module_graph_links_in_node():
    """Importing the shell resolves every static import and every named export (SyntaxError otherwise)."""
    r = run_node(f"import {{ pathToFileURL }} from 'node:url'; await import(pathToFileURL({json.dumps(str(DASH / 'js' / 'shell.js'))}).href); console.log('linked');")
    assert r.returncode == 0 and "linked" in r.stdout, r.stderr[:800]


# --------------------------------------------------------------------------- CSP / injection hygiene


def _csp() -> str:
    pytest.importorskip("fastapi")
    from awos.api import app as app_mod
    return app_mod.DASHBOARD_CSP


def test_csp_is_satisfied_by_the_markup():
    csp = _csp()
    assert "script-src 'self'" in csp and "'unsafe-inline'" not in csp.split("script-src")[1].split(";")[0]
    # no inline <script> blocks, no inline event handlers, no javascript: URLs, no eval in index.html or any JS template
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", INDEX), "inline <script> would be blocked by the CSP"
    for p in TEXT_FILES:
        text = p.read_text(encoding="utf-8")
        assert not re.search(r"""\son(?:click|change|input|submit|load|error|focus|blur|key\w+|mouse\w+|pointer\w+|drag\w*|drop)\s*=\s*["'{]""", text, re.I), \
            f"{rel(p)} contains an inline event handler attribute"
        assert "javascript:" not in text.replace("javascript:alert", "").replace("'javascript:", "'"), f"{rel(p)} uses javascript: URLs" \
            if p.suffix != ".js" else True
        if p.suffix == ".js":
            assert not re.search(r"\beval\s*\(|new\s+Function\s*\(|setTimeout\s*\(\s*['\"`]", text), f"{rel(p)} uses eval-like constructs"
    # connect-src / frame-src 'self': iframes and fetches only target same-origin paths
    for p in JS_FILES:
        text = p.read_text(encoding="utf-8")
        for m in re.finditer(r"""fetch\(\s*([`'"])([^`'"]*)""", text):
            assert not re.match(r"^(https?:)?//", m.group(2)), f"{rel(p)} fetches {m.group(2)}"
        for m in re.finditer(r"<iframe[^>]*>", text):
            assert 'sandbox=""' in m.group(0), f"{rel(p)}: iframes must be sandboxed (artifact previews)"
            assert "src=" in m.group(0)


def test_no_unescaped_dynamic_html_sinks():
    """All markup goes through h`` / setHTML(); raw()/new Safe()/innerHTML never take interpolated untrusted text."""
    for p in JS_FILES:
        r = rel(p).removeprefix("js/")
        text = p.read_text(encoding="utf-8")
        if r not in INNERHTML_OK:
            assert not re.search(r"\.(?:innerHTML|outerHTML)\s*[+]?=", text), f"{r}: assign markup with setHTML(el, h`...`), not innerHTML"
            assert "insertAdjacentHTML" not in text and "document.write" not in text, f"{r}: use appendHTML()"
        if r not in RAW_TEMPLATE_OK:
            for m in re.finditer(r"\braw\(\s*`([^`]*)`", text):
                assert "${" not in m.group(1), f"{r}: raw() with an interpolated template literal: {m.group(0)[:80]}"
            for m in re.finditer(r"\braw\(([^()]*(?:\([^()]*\)[^()]*)*)\)", text):
                arg = m.group(1).strip()
                ok = arg == "" or arg[0] in "'\"" or arg in {"i.href ? 'a' : 'span'", "n === 0 ? urgent : bars", "String(s)", "s"}
                assert ok, f"{r}: unvetted raw() argument: raw({arg[:60]})"
        if r not in SAFE_CTOR_OK:
            assert "new Safe(" not in text, f"{r}: only html.js / components.js construct Safe directly"
    # setHTML must refuse plain strings
    html_js = (DASH / "js" / "core" / "html.js").read_text(encoding="utf-8")
    assert "instanceof Safe" in html_js and "throw new TypeError" in html_js


@needs_node
def test_escaping_and_markdown_are_xss_safe():
    code = f"""
import {{ pathToFileURL }} from 'node:url';
const base = {json.dumps((DASH / 'js' / 'core').as_posix())};
const {{ h, esc, raw, safeUrl, setHTML, join }} = await import(pathToFileURL(base + '/html.js').href);
const {{ md, inline, plain }} = await import(pathToFileURL(base + '/markdown.js').href);
const bad = [];
const must = (name, cond) => {{ if (!cond) bad.push(name); }};
const evil = '<img src=x onerror=alert(1)><script>alert(2)</script>"\\'`&';
must('h escapes', !String(h`<p>${{evil}}</p>`).includes('<img') && !String(h`<p>${{evil}}</p>`).includes('<script'));
must('h attr escapes', !String(h`<a title="${{evil}}">`).includes('"<'));
must('nested Safe is not double-escaped', String(h`<b>${{h`<i>x</i>`}}</b>`) === '<b><i>x</i></b>');
must('arrays join', String(h`${{['<', 'a']}}`) === '&lt;a');
must('setHTML rejects strings', (() => {{ try {{ setHTML({{}}, '<b>'); return false; }} catch (e) {{ return e instanceof TypeError; }} }})());
must('safeUrl blocks javascript:', safeUrl('javascript:alert(1)') === '#' && safeUrl(' JaVaScRiPt:alert(1)') === '#' && safeUrl('data:text/html,x') === '#' && safeUrl('//evil.example') === '#');
must('safeUrl allows http / paths', safeUrl('https://a.b/c') === 'https://a.b/c' && safeUrl('/api/x') === '/api/x');
const out = String(md('# T <script>alert(1)</script>\\n\\n[click](javascript:alert(1)) [ok](https://example.com/?a=1&b=2)\\n\\n<img src=x onerror=alert(1)>\\n\\n| a | b |\\n|---|---|\\n| <b>x</b> | `<i>` |\\n\\n```html\\n<script>alert(3)</script>\\n```\\n\\n- [ ] todo <u>x</u>\\n  - nested\\n\\n> quote <svg onload=alert(1)>'));
must('md has no raw script', !/<script/i.test(out));
must('md has no raw img/svg', !/<img|<svg|<u>|<b>x|<i>/i.test(out));
must('md blocks javascript: links', !/href="javascript:/i.test(out) && out.includes('href="#"'));
must('md keeps safe links', out.includes('href="https://example.com/?a=1&amp;b=2"') && out.includes('rel="noopener noreferrer"'));
must('md renders structure', out.includes('<h1>') && out.includes('<table>') && out.includes('<pre') && out.includes('<blockquote>') && out.includes('md-cb'));
must('md nested lists', out.includes('<ul><li class="md-task"><span class="md-cb') && out.includes('<ul><li>nested'));
must('inline code escaped', inline('`<b>`').includes('<code>&lt;b&gt;</code>'));
must('plain strips markdown', plain('## Hi **there** [x](http://y)') === 'Hi there x');
if (bad.length) {{ console.log('FAILED: ' + bad.join(', ')); process.exit(1); }}
console.log('ok');
"""
    r = run_node(code)
    assert r.returncode == 0, (r.stdout + r.stderr)[:1200]


@needs_node
def test_pure_logic_helpers():
    """Digest fallbacks, kanban drop rules, inbox row normalisation, relative time, countdown."""
    root = json.dumps(DASH.joinpath("js").as_posix())
    code = f"""
import {{ pathToFileURL }} from 'node:url';
const u = (p) => pathToFileURL({root} + '/' + p).href;
const bad = []; const must = (n, c) => {{ if (!c) bad.push(n); }};
const vocab = await import(u('domain/vocab.js'));
const board = await import(u('views/board.js'));
const inbox = await import(u('views/inbox.js'));
const fmt = await import(u('core/format.js'));
const reader = await import(u('ui/reader.js'));
vocab.META.categories = {{ writing: '论文 / 文档写作与修改' }};
// digest: server values win, client fallback fills the gaps
const full = vocab.digestOf({{ title: 'T'.repeat(80), headline: '回复邮件', gist: 'g', keywords: ['a', 'b', 'c'], digest_auto: false }});
must('digest keeps server headline', full.headline === '回复邮件' && full.keywords.length === 3 && full.auto === false);
const fb = vocab.digestOf({{ title: '修订论文第三章并统一图表风格的所有细节说明', category: 'writing', project: 'thuthesis', level: 3, source: 'sensor:mailmaster' }});
must('digest fallback headline <= 30', fb.headline.length <= 31 && fb.headline.length > 0);
must('digest fallback keywords', fb.keywords.includes('论文') && fb.keywords.includes('thuthesis') && fb.keywords.includes('L3') && fb.keywords.includes('邮件'));
must('digest drops "other"', !vocab.digestOf({{ title: 'x', keywords: ['other', 'k'] }}, 'approval').keywords.includes('other'));
const ap = vocab.digestOf({{ title: 'x', kind: 'action', action_type: 'send_email', risk: 'high' }}, 'approval');
must('approval fallback keywords', ap.keywords.includes('send_email') && ap.keywords.some((k) => k.includes('高')));
// recommended option
must('recommendedKey', vocab.recommendedKey({{ recommendation: 'approve: ok', options: [{{ key: 'reject' }}, {{ key: 'approve' }}] }}) === 'approve');
must('isRejectish', vocab.isRejectish('reject') && vocab.isRejectish('defer') && !vocab.isRejectish('approve'));
// kanban drop rules mirror the API/state machine
const T = (status) => ({{ status }});
must('accept: suggested -> queued', board.dropRule(T('triaged'), 'queued', 'suggested').action === 'accept');
must('snoozed -> queued accepts', board.dropRule(T('snoozed'), 'queued', 'suggested').action === 'accept');
must('failed -> queued retries', board.dropRule(T('failed'), 'queued', 'failed').action === 'retry');
must('dismiss from suggested / queued', board.dropRule(T('triaged'), 'dismissed', 'suggested').action === 'dismiss' && board.dropRule(T('ready'), 'dismissed', 'queued').action === 'dismiss');
must('running cannot be dismissed (explained)', !board.dropRule(T('running'), 'dismissed', 'running').ok && /取消/.test(board.dropRule(T('running'), 'dismissed', 'running').why));
must('running -> done refused with a reason', !board.dropRule(T('running'), 'done', 'running').ok && board.dropRule(T('running'), 'done', 'running').why.length > 10);
must('same column is a silent no-op', board.dropRule(T('triaged'), 'suggested', 'suggested').silent === true);
// inbox rows from the lead's flat feed
const feed = [{{ section: 'decision', section_title: '需你决定', kind: 'approval', id: 'apv_1', task_id: 't1', headline: 'H', gist: 'G', keywords: ['k1', 'k2', 'k3'], digest_auto: false, title: 'Title', level: 3, priority: null, risk: 'high', status: 'pending', ts: '2026-11-05T08:00:00Z', expires_at: null }},
  {{ section: 'suggested', section_title: 'AI 的新建议', kind: 'task', id: 'tsk_1', task_id: 'tsk_1', title: 'No digest yet', level: 2, priority: 1, status: 'triaged', ts: '2026-11-05T09:00:00Z' }}];
const rows = inbox.inboxItems({{ feed, decisions: [], actions: [], review: [], handoff: [], suggested: [{{ id: 'tsk_1', category: 'writing', project: 'P', source: 'manual', level: 2 }}], failed: [] }});
must('inbox rows keep order and keys', rows.length === 2 && rows[0].key === 'approval:apv_1' && rows[1].key === 'task:tsk_1');
must('inbox row without digest falls back', rows[1].headline === 'No digest yet' && rows[1].keywords.length >= 2);
must('high risk tone', rows[0].tone === 'crit');
// time helpers
const now = Date.parse('2026-11-05T12:00:00Z');
must('rel minutes', fmt.rel('2026-11-05T11:55:00Z', now) === '5 分钟前');
must('rel future', fmt.rel('2026-11-05T14:00:00Z', now) === '2 小时后');
must('countdown soon / expired', fmt.countdown('2026-11-05T14:00:00Z', now).state === 'soon' && fmt.countdown('2026-11-05T11:00:00Z', now).state === 'expired');
must('dur', fmt.dur(125) === '2 分 5 秒');
// action bar per status
const acts = (s) => reader.taskActionList({{ status: s }}).map((a) => a.action);
must('review actions', acts('review').includes('complete') && acts('review').includes('rework') && acts('review').includes('cancel'));
must('suggested actions', acts('triaged').includes('accept') && acts('triaged').includes('dismiss') && acts('triaged').includes('snooze'));
must('failed actions', acts('failed').includes('retry') && acts('failed').includes('dismiss'));
must('done has no cancel', !acts('done').includes('cancel') && acts('done').includes('rework'));
if (bad.length) {{ console.log('FAILED: ' + bad.join(', ')); process.exit(1); }}
console.log('ok');
"""
    r = run_node(code)
    assert r.returncode == 0, (r.stdout + r.stderr)[:1500]


@needs_node
def test_topic_grouping_recap_and_reader_logic():
    """Topics (group / order / counts), recap entries (order, kind guess), the open-by-default rule, escaping, list navigation."""
    root = json.dumps(DASH.joinpath("js").as_posix())
    code = f"""
import {{ pathToFileURL }} from 'node:url';
const store = new Map();
globalThis.localStorage = {{ getItem: (k) => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, String(v)) }};
const u = (p) => pathToFileURL({root} + '/' + p).href;
const bad = []; const must = (n, c) => {{ if (!c) bad.push(n); }};
const T = await import(u('domain/topics.js'));
const R = await import(u('ui/recap.js'));
const TR = await import(u('ui/topic-reader.js'));
const St = await import(u('core/store.js'));
const S = await import(u('core/state.js'));
const inbox = await import(u('views/inbox.js'));
const split = await import(u('views/split.js'));
const day = 86400000; const now = Date.now();
const iso = (ms) => new Date(ms).toISOString().replace(/\\.\\d{{3}}Z$/, 'Z');

// ---- grouping: pending decisions first, then approvals, then the longest-waiting; the server order wins when given
const items = [
  {{ key: 'a', section: 'suggested', ts: '2026-11-01T00:00:00Z', topic_key: 't2', topic_title: 'T2' }},
  {{ key: 'b', section: 'decision', ts: '2026-11-03T00:00:00Z', topic_key: 't1', topic_title: 'T1' }},
  {{ key: 'c', section: 'approval', ts: '2026-11-02T00:00:00Z', project: 'thuthesis' }},
  {{ key: 'd', section: 'failed', ts: '2026-11-04T00:00:00Z', category: 'writing' }},
];
const g0 = T.groupByTopic(items, []);
must('urgency order', g0.map((g) => g.key).join() === 't1,p:thuthesis,t2,c:writing');
must('fallback topic from project', g0[1].title === 'thuthesis' && g0[1].items[0].key === 'c');
must('counts per section', g0[0].counts.decision === 1 && g0[0].counts.total === 1 && g0[2].counts.suggested === 1);
must('oldest per topic', g0[2].oldest_ts === '2026-11-01T00:00:00Z');
const g1 = T.groupByTopic(items, [{{ key: 't2', title: 'Server T2', state_now: 'now', has_brief: true, brief_stale: true }}, {{ key: 't1', title: 'Server T1' }}]);
must('server order and fields win', g1.map((g) => g.key).slice(0, 2).join() === 't2,t1' && g1[0].title === 'Server T2' && g1[0].state_now === 'now' && g1[0].brief_stale === true);
must('countsText', T.countsText({{ decision: 2, approval: 1, review: 0 }}) === '决定 2 · 批准 1');
must('ageWord', T.ageWord('2026-11-02T12:00:00Z', Date.parse('2026-11-05T12:00:00Z')) === '3 天' && T.ageWord('2026-11-05T09:00:00Z', Date.parse('2026-11-05T12:00:00Z')) === '3 小时');
must('topic selection keys', T.isTopicKey('topic:p:x') && T.topicFromSel('topic:p:x') === 'p:x' && !T.isTopicKey('task:abc'));

// ---- recap entries: oldest first, empty dropped, kinds validated or guessed
const ents = T.normalizeEntries([
  {{ ts: '2026-11-03T00:00:00Z', text: 'AI 补全了推导，产出修订稿', kind: 'bogus' }},
  {{ ts: '2026-11-01T00:00:00Z', text: '收到导师的邮件' }},
  {{ ts: '2026-11-02T00:00:00Z', text: '你选择了「稍后再定」', kind: 'user' }},
  {{ ts: '2026-11-04T00:00:00Z', text: '   ' }},
]);
must('entries sorted + filtered', ents.length === 3 && ents[0].text.startsWith('收到') && ents[2].text.startsWith('AI'));
must('kinds guessed', ents[0].kind === 'found' && ents[1].kind === 'user' && ents[2].kind === 'deliverable');
must('guess: ai / none', T.guessKind('AI 起草了邮件，附上修订稿') === 'ai' && T.guessKind('随便一句话') === null);

// ---- recap card default: open when the item or the topic is old (> 24 h) or not opened for > 24 h
St.touchTopic('seen', now); St.touchTopic('stale', now - 2 * day);
must('young item + seen topic is collapsed', R.defaultOpen({{ ts: iso(now - 3600e3), topicKey: 'seen' }}) === false);
must('old item opens', R.defaultOpen({{ ts: iso(now - 3 * day), topicKey: 'seen' }}) === true);
must('topic not opened for > 24 h opens', R.defaultOpen({{ ts: iso(now - 3600e3), topicKey: 'stale' }}) === true);
must('never-seen topic opens', R.defaultOpen({{ ts: iso(now - 3600e3), topicKey: 'new' }}) === true);
must('card needs a topic', String(R.recapCard({{ id: 'x', topic: null }})) === '');
const card = String(R.recapCard({{ id: 'tsk_1', topic: {{ key: 'p:a', title: '<b>论文</b>' }}, ts: iso(now - 3 * day) }}));
must('card markup', card.includes('data-lazy="recap"') && card.includes('data-topic="p:a"') && card.includes(' open') && card.includes('前情提要') && !card.includes('<b>论文'));

// ---- escaping: model-written recap / ask text never becomes markup
const evil = '<img src=x onerror=alert(1)><script>alert(2)</script>';
const tl = String(R.recapTimeline([{{ ts: '2026-11-01T00:00:00Z', text: evil, kind: 'ai' }}]));
must('timeline escapes', !tl.includes('<img') && !tl.includes('<script') && tl.includes('&lt;img') && tl.includes('rc-tl') && tl.includes('rc-ai'));
must('timeline empty state', String(R.recapTimeline([])).includes('这个话题没有历史记录'));
S.CACHE.approvals.set('apv_1', {{ id: 'apv_1', status: 'pending', risk: 'low', options: [{{ key: 'yes', label: '好' }}, {{ key: 'no', label: '不' }}], recommendation: 'yes: ' + evil }});
const rd = String(TR.topicReader({{ key: 'k', title: evil, brief: {{ state_now: evil, recap: [{{ ts: '2026-11-01T00:00:00Z', text: evil }}], asks: [{{ id: 'apv_1', question: evil, why_now: evil, stakes: evil }}], generated_at: '2026-11-05T00:00:00Z', stale: true }},
  history: [{{ ts: '2026-11-01T00:00:00Z', kind: 'found', text: evil }}], items: [{{ kind: 'approval', id: 'apv_1', section: 'decision', title: evil, headline: evil, gist: evil, ts: '2026-11-01T00:00:00Z', risk: 'low' }},
  {{ kind: 'task', id: 'tsk_1', section: 'review', title: 'x', headline: 'x', status: 'review', ts: '2026-11-02T00:00:00Z' }}], counts: {{}}, age_days: 4 }}));
must('topic reader escapes', !rd.includes('<img') && !rd.includes('<script') && rd.includes('&lt;script'));
must('topic reader structure', rd.includes('data-reader="topic"') && rd.includes('现在的状况') && rd.includes('前情提要') && rd.includes('现在需要你') && rd.includes('data-act="topic-dismiss"') && rd.includes('data-act="topic-refresh"'));
must('topic reader reuses the decision buttons', rd.includes('data-act="decide"') && rd.includes('data-option="yes"') && rd.includes('data-act="task-action"') && rd.includes('可能已过期'));
must('topic reader empty items', String(TR.topicReader({{ key: 'k', title: 'T', brief: null, history: [], items: [], counts: {{}} }})).includes('这个话题没有历史记录'));

// ---- inbox rows carry the topic (server fields first, project / category fallback)
const rows = inbox.inboxItems({{ feed: [
  {{ section: 'decision', section_title: '需你决定', kind: 'approval', id: 'apv_9', task_id: 't9', headline: 'H', gist: 'G', keywords: ['k'], title: 'T', status: 'pending', ts: '2026-11-05T08:00:00Z', topic_key: 'thesis', topic_title: '论文' }},
  {{ section: 'suggested', section_title: 'AI 的新建议', kind: 'task', id: 'tsk_9', task_id: 'tsk_9', title: 'No topic', status: 'triaged', ts: '2026-11-05T09:00:00Z' }}],
  decisions: [], actions: [], review: [], handoff: [], suggested: [{{ id: 'tsk_9', project: 'proj', category: 'dev' }}], failed: [] }});
must('rows keep server topic', rows[0].topic_key === 'thesis' && rows[0].topic_title === '论文');
must('rows fall back to project', rows[1].topic_key === 'p:proj' && S.CACHE.topicOf.get('tsk_9').key === 'p:proj');

// ---- list navigation over topic headers and their items
const ctl = new split.Split({{ id: 'unit', title: '收件箱', icon: 'inbox', sections: [], emptyAll: () => '' }});
const mk = (key, topic, section, ts) => ({{ key, dom: key, kind: 'task', id: key, section, headline: key, gist: '', keywords: [], title: key, topic_key: topic, topic_title: topic, ts, stamp: ts, level: 1 }});
ctl.adopt({{ items: [mk('task:a1', 'A', 'suggested', '2026-11-01T00:00:00Z'), mk('task:b1', 'B', 'decision', '2026-11-03T00:00:00Z'), mk('task:b2', 'B', 'review', '2026-11-03T01:00:00Z'), mk('task:a2', 'A', 'failed', '2026-11-02T00:00:00Z')], extra: null, topics: [] }});
must('mode defaults to topic', ctl.mode() === 'topic' && ctl.hasTopics());
must('keys: header then items (what needs you most first: a failure before a mere suggestion), urgent topic first', ctl.visibleKeys().join() === 'topic:B,task:b1,task:b2,topic:A,task:a2,task:a1');
ctl.collapsed = {{ B: true }};
must('collapsed topic keeps only its header', ctl.visibleKeys().join() === 'topic:B,topic:A,task:a2,task:a1');
ctl.collapsed = {{}};
must('find topic by selection key', ctl.findTopic('topic:A').items.length === 2 && ctl.exists('topic:B') && ctl.exists('task:a1') && !ctl.exists('topic:zzz'));
must('after clearing the last item of a topic, the next item is opened first', ctl.neighbourOf('task:b2') === 'task:a2' && ctl.neighbourOf('task:a2') === 'task:a1');
const prev = ctl.visibleKeys();
ctl.adopt({{ items: ctl.items.filter((i) => i.topic_key !== 'B'), extra: null, topics: [] }});
must('successor of a vanished topic is the next topic', ctl.successor(prev, 'topic:B') === 'topic:A');
must('successor of a vanished item stays an item', ctl.successor(prev, 'task:b2') === 'task:a2');
if (bad.length) {{ console.log('FAILED: ' + bad.join(', ')); process.exit(1); }}
console.log('ok');
"""
    r = run_node(code)
    assert r.returncode == 0, (r.stdout + r.stderr)[:2000]


@needs_node
def test_topic_actions_and_modules_are_registered():
    """The new handlers exist; `g t` is the topic list now (tasks moved to `g w`)."""
    root = json.dumps((DASH / "js").as_posix())
    code = f"""
import {{ pathToFileURL }} from 'node:url';
const root = {root};
await import(pathToFileURL(root + '/shell.js').href);
const {{ ACTS }} = await import(pathToFileURL(root + '/core/acts.js').href);
const need = ['topic-toggle', 'topic-collapse-all', 'split-group', 'open-item', 'recap-toggle', 'open-topic', 'recap-retry', 'topic-refresh', 'topic-dismiss', 'go-topics', 'topic-project', 'open-project-inbox', 'open-context', 'split-project-clear', 'pj-rel-toggle'];
const {{ CHANGES, INPUTS }} = await import(pathToFileURL(root + '/core/acts.js').href);
const missing = need.filter((k) => typeof ACTS[k] !== 'function');
if (typeof CHANGES['pj-choice'] !== 'function') missing.push('change:pj-choice');
if (typeof INPUTS['pj-filter'] !== 'function') missing.push('input:pj-filter');
if (missing.length) {{ console.log('MISSING ' + missing.join(', ')); process.exit(1); }}
console.log('ok');
"""
    r = run_node(code)
    assert r.returncode == 0, (r.stdout + r.stderr)[:800]
    shell = (DASH / "js" / "shell.js").read_text(encoding="utf-8")
    assert "t: 'topics'" in shell and "w: 'tasks'" in shell, "g t = topic list, g w = tasks"


@needs_node
def test_every_data_act_in_templates_has_a_registered_handler():
    """data-act / data-change / data-input / data-form names used in markup must exist in the action registries."""
    root = json.dumps((DASH / "js").as_posix())
    code = f"""
import {{ readFileSync, readdirSync }} from 'node:fs';
import {{ pathToFileURL }} from 'node:url';
const root = {root};
await import(pathToFileURL(root + '/shell.js').href);
const {{ ACTS, CHANGES, INPUTS, FORMS }} = await import(pathToFileURL(root + '/core/acts.js').href);
const files = []; const walk = (d) => {{ for (const e of readdirSync(d, {{ withFileTypes: true }})) {{ const p = d + '/' + e.name; if (e.isDirectory()) walk(p); else if (p.endsWith('.js') && !p.endsWith('core/acts.js')) files.push(p); }} }}; walk(root);
const kinds = [['act', ACTS, [/data-act="([a-z0-9-]+)"/g, /\\bact:\\s*'([a-z0-9-]+)'/g]], ['change', CHANGES, [/data-change="([a-z0-9-]+)"/g]], ['input', INPUTS, [/data-input="([a-z0-9-]+)"/g]], ['form', FORMS, [/data-form="([a-z0-9-]+)"/g]]];
const missing = [];
for (const f of files) {{ const t = readFileSync(f, 'utf8'); for (const [kind, reg, res] of kinds) for (const re of res) for (const m of t.matchAll(re)) if (!reg[m[1]]) missing.push(kind + ':' + m[1] + ' (' + f.split('/js/')[1] + ')'); }}
if (missing.length) {{ console.log('MISSING ' + [...new Set(missing)].join(', ')); process.exit(1); }}
console.log('ok');
"""
    r = run_node(code)
    assert r.returncode == 0, (r.stdout + r.stderr)[:800]


# --------------------------------------------------------------------------- served by the real app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    var = tmp_path / "var"
    cfg = tmp_path / "test.toml"
    cfg.write_text(f"""
[server]
host = "127.0.0.1"
port = 8765
[paths]
var = "{var.as_posix()}"
workspaces = "{(var / 'workspaces').as_posix()}"
artifacts = "{(var / 'artifacts').as_posix()}"
logs = "{(var / 'logs').as_posix()}"
runs = "{(var / 'runs').as_posix()}"
backups = "{(var / 'backups').as_posix()}"
context = "{(var / 'context').as_posix()}"
inbox_drop = "{(var / 'inbox-drop').as_posix()}"
[executors.codex]
enabled = false
[executors.claude]
enabled = false
[notify]
digest_times = []
""", encoding="utf-8")
    monkeypatch.setenv("AWOS_CONFIG", str(cfg))
    monkeypatch.setenv("AWOS_DB", str(tmp_path / "awos.db"))
    monkeypatch.setenv("AWOS_NOTIFY_DISABLE", "1")
    from awos.config import reset_config_cache
    reset_config_cache()
    from fastapi.testclient import TestClient
    import awos.api.app as app_mod
    with TestClient(app_mod.create_app(token="t0ken")) as c:
        yield c
    reset_config_cache()


def test_served_module_graph_is_complete(client):
    """Every module reachable from /static/app.js (and the stylesheet imports) is served with the right MIME type."""
    r = client.get("/")
    assert r.status_code == 200 and 'content="t0ken"' in r.text and "__AWOS_TOKEN__" not in r.text
    css = client.get("/static/style.css")
    assert css.status_code == 200 and css.headers["content-type"].startswith("text/css")
    for target in re.findall(r"@import\s+url\(\"([^\"]+)\"\)", css.text):
        sub = client.get(f"/static/{target}")
        assert sub.status_code == 200 and sub.headers["content-type"].startswith("text/css"), target
    seen: set[str] = set()
    queue = ["app.js"]
    while queue:
        path = queue.pop()
        if path in seen:
            continue
        seen.add(path)
        resp = client.get(f"/static/{path}")
        assert resp.status_code == 200, path
        assert "javascript" in resp.headers["content-type"], (path, resp.headers["content-type"])
        base = posixpath.dirname(path)
        for rel_import in re.findall(r"""from\s*['"](\.{1,2}/[^'"]+)['"]|import\(\s*['"](\.{1,2}/[^'"]+)['"]""", resp.text):
            target = next(x for x in rel_import if x)
            queue.append(posixpath.normpath(posixpath.join(base, target)))
    assert len(seen) >= 20, sorted(seen)
    assert {"js/domain/topics.js", "js/ui/recap.js", "js/ui/topic-reader.js", "js/domain/projects.js", "js/ui/project-card.js", "js/ui/project-reader.js"} <= seen, sorted(seen)
    assert "css/topics.css" in css.text and "css/projects.css" in css.text, "the topics / projects stylesheets are imported"


def test_new_api_contract_is_what_the_ui_expects(client):
    """The endpoints the dashboard builds on exist and have the fields the UI reads (graceful fallbacks cover the rest)."""
    board = client.get("/api/board").json()
    assert [c["key"] for c in board["columns"]][:5] == ["suggested", "queued", "running", "needs_you", "done"]
    dash = client.get("/api/dashboard?days=14").json()
    for k in ("kpis", "series", "levels", "funnel", "executors", "sensors", "keywords", "activity"):
        assert k in dash, k
    assert {"inbox", "approvals", "running", "done_today", "failures_24h", "cost_today", "budget_daily"} <= set(dash["kpis"])
    inbox = client.get("/api/inbox").json()
    assert "feed" in inbox and "counts" in inbox
    meta = client.get("/api/meta").json()
    assert "levels" in meta and "transitions" in meta


def test_topics_api_contract_the_ui_reads(client):
    """Topics: /api/inbox carries `topics` + per-entry topic fields, /api/topics/{key} has the recap inputs, topic mutations need the token."""
    from urllib.parse import quote

    from awos import repo
    tid = repo.create_task("检查 thuthesis 仓库未提交改动", status="triaged", project="thuthesis")
    aid = repo.create_approval(title="选哪个方案？", kind="decision", task_id=tid, options=[{"key": "a", "label": "A"}, {"key": "b", "label": "B"}])
    inbox = client.get("/api/inbox").json()
    assert inbox["topics"], "inbox returns a topic list"
    for t in inbox["topics"]:
        assert {"key", "title", "counts", "oldest_ts", "newest_ts", "age_days", "state_now", "has_brief", "brief_stale"} <= set(t), t
        assert {"decisions", "approvals", "review", "handoff", "suggested", "failed", "total"} <= set(t["counts"])
    assert inbox["feed"] and all(f.get("topic_key") and f.get("topic_title") for f in inbox["feed"]), "every feed entry has a topic"
    key = next(f["topic_key"] for f in inbox["feed"] if f["id"] == aid)
    enc = quote(key, safe="")
    d = client.get(f"/api/topics/{enc}").json()
    assert {"key", "title", "brief", "history", "items", "counts", "age_days"} <= set(d)
    assert isinstance(d["history"], list) and d["brief"] is None
    assert {i["id"] for i in d["items"]} >= {tid, aid}, "ask ids equal the feed ids"
    assert client.get(f"/api/approvals/{aid}").json()["approval"]["topic"]["key"] == key
    assert client.get(f"/api/tasks/{tid}").json()["task"]["topic"]["key"] == key
    assert client.get("/api/topics/nope-not-a-topic").status_code == 404, "unknown topic -> 404 (the UI falls back to the list data)"
    for path, body in ((f"/api/topics/{enc}/refresh", {}), (f"/api/topics/{enc}/dismiss", {"note": "不用管了"})):
        assert client.post(path, json=body).status_code == 401, "mutating topic calls need the token"
    H = {"X-AWOS-Token": "t0ken"}
    assert client.post(f"/api/topics/{enc}/dismiss", json={"note": "好"}, headers=H).status_code == 422, "a reason of 2+ characters is required"
    ok = client.post(f"/api/topics/{enc}/dismiss", json={"note": "这个仓库不用再追踪了"}, headers=H).json()
    assert ok["ok"] is True and ok["dismissed"] >= 1
    assert not [t for t in client.get("/api/inbox").json()["topics"] if t["key"] == key], "a dismissed topic leaves the inbox"

@needs_node
def test_projects_grouping_cards_and_list_logic():
    """Projects: colour slot, grouping (按项目 default, 未关联项目 last), chips / 所属项目 card / project reader (escaping), the list keys over
    project headers, ?project= filter, older-server fallback, and the 上下文 project cards with the AI's working notes muted."""
    root = json.dumps(DASH.joinpath("js").as_posix())
    code = r"""
import { pathToFileURL } from 'node:url';
const store = new Map();
globalThis.localStorage = { getItem: (k) => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, String(v)) };
const u = (p) => pathToFileURL(__ROOT__ + '/' + p).href;
const bad = []; const must = (n, c) => { if (!c) bad.push(n); };
const P = await import(u('domain/projects.js'));
const T = await import(u('domain/topics.js'));
const C = await import(u('ui/components.js'));
const PC = await import(u('ui/project-card.js'));
const PR = await import(u('ui/project-reader.js'));
const TR = await import(u('ui/topic-reader.js'));
const R = await import(u('ui/recap.js'));
const K = await import(u('views/knowledge.js'));
const split = await import(u('views/split.js'));
const St = await import(u('core/state.js'));
const evil = '<img src=x onerror=alert(1)><script>alert(2)</script>';
const clean = (s) => !s.includes('<img') && !s.includes('<script');

// ---- colour slot: from the key, stable, 1..8
const names = ['学位论文 thuthesis', 'MoE 路由论文', '实验室组会', '海外博后申请', 'AgentWorkOS', '开题报告归档'];
const slots = names.map(P.projectSlot);
must('slot range', slots.every((s) => Number.isInteger(s) && s >= 1 && s <= 8));
must('slot is stable and spread', P.projectSlot('x') === P.projectSlot('x') && new Set(slots).size >= 3);
must('link source labels', P.linkSource({ link_source: 'user' }).label === '你指定' && P.linkSource({ link_source: 'llm' }).label === 'AI 推断' && P.linkSource(null).label === 'AI 推断');
must('progress notes', P.isProgressNote('进展·abc') && !P.isProgressNote('导师') && P.progressTitle('进展·abc') === 'abc');
const rel = { deadline: [{ key: 'd1', value: 'v' }], person: [{ key: 'p1', value: 'v' }], goal: [{ key: 'g1', value: 'v' }], note: [{ key: '进展·x', value: 'v' }, { key: 'n1', value: 'v' }] };
const pick = P.pickRelated(rel, 99);
must('related order: deadline, person, notes, goal, working notes last', pick.map((f) => f.key).join() === 'd1,p1,n1,g1,进展·x');
must('related max + working-note flag', P.pickRelated(rel, 3).length === 3 && pick[4].progress === true && pick[3].progress === false && P.relatedTotal(rel) === 5);
must('server counts -> section keys', P.sectionCounts({ decisions: 2, approvals: 1, review: 3, total: 6 }).decision === 2 && P.sectionCounts({ approvals: 1 }).approval === 1);

// ---- working notes -> their project (leading （…） of the value)
const projs = [{ key: '学位论文 thuthesis' }, { key: 'MoE 路由论文' }, { key: 'AgentWorkOS' }];
must('note -> project by （name）', K.noteProject({ value: '（学位论文 thuthesis）仓库有 7 个未提交文件' }, projs)?.key === '学位论文 thuthesis');
must('partial names match', K.noteProject({ value: '（thuthesis）x' }, projs)?.key === '学位论文 thuthesis');
must('no leading （） or unknown project -> none', K.noteProject({ value: 'GPU 服务器占用 91%' }, projs) === null && K.noteProject({ value: '（海外博后）x' }, projs) === null);

// ---- grouping: topics -> projects (first seen order, unlinked last, aggregated)
const mk = (key, topic, section, ts) => ({ key, dom: key, kind: 'task', id: key, section, headline: key, gist: '', keywords: [], title: key, topic_key: topic, topic_title: topic, ts, stamp: ts, level: 1 });
const topics = [
  { key: 'tA', title: 'A', project: { key: 'P1', name: 'P1', summary: 's', link_source: 'user' }, state_now: 'now A', oldest_ts: '2026-11-02T00:00:00Z' },
  { key: 'tC', title: 'C', project: { key: 'P2', name: 'P2' }, state_now: 'now C' },
  { key: 'tB', title: 'B', project: { key: 'P1', name: 'P1' } },
  { key: 'tD', title: 'D', project: null },
];
const items = [mk('task:a1', 'tA', 'decision', '2026-11-03T00:00:00Z'), mk('task:b1', 'tB', 'review', '2026-11-01T00:00:00Z'), mk('task:c1', 'tC', 'suggested', '2026-11-04T00:00:00Z'), mk('task:d1', 'tD', 'failed', '2026-11-05T00:00:00Z')];
const tg = T.groupByTopic(items, topics);
must('topic groups carry their project', tg.find((g) => g.key === 'tA').project.key === 'P1' && tg.find((g) => g.key === 'tD').project === null);
const pg = P.groupByProject(tg);
must('project order: first seen, 未关联项目 last', pg.map((g) => g.key).join() === 'P1,P2,__none__');
must('a project holds all its topics', pg[0].topics.map((t) => t.key).join() === 'tA,tB' && pg[0].items.length === 2);
must('aggregated counts / oldest / state', pg[0].counts.decision === 1 && pg[0].counts.review === 1 && pg[0].counts.total === 2 && pg[0].oldest_ts === '2026-11-01T00:00:00Z' && pg[0].state_now === 'now A');
must('unlinked group', pg[2].name === '未关联项目' && pg[2].project === null && pg[2].items.length === 1);

// ---- the chip
const chip = String(C.projectChip({ key: 'k', name: evil, summary: evil }));
must('chip escapes + has a colour slot', clean(chip) && chip.includes('pj-chip') && /pj-c[1-8]/.test(chip) && chip.includes('&lt;img'));
const btnChip = String(C.projectChip({ key: 'k', name: 'n' }, { act: 'open-topic', data: { key: 't', focus: 'project' } }));
must('chip is a button when it has an action', btnChip.startsWith('<button') && btnChip.includes('data-act="open-topic"') && btnChip.includes('data-focus="project"'));
must('chip without a project', String(C.projectChip(null)) === '' && String(C.projectChip(null, { empty: true })).includes('pj-none'));

// ---- 所属项目 card + readers
const card = String(PC.projectCard({ key: 'P1', name: evil, summary: evil, link_source: 'user' }, { key: 'tA', title: evil }));
must('card escapes', clean(card));
must('card parts', card.includes('data-pj-card') && card.includes('所属项目') && card.includes('你指定') && card.includes('data-act="topic-project"') && card.includes('data-pj-fill') && card.includes('更改关联'));
must('AI badge', String(PC.projectCard({ key: 'P1', name: 'n', summary: '', link_source: 'llm' }, { key: 't', title: 'T' })).includes('AI 推断'));
const none = String(PC.projectCard(null, { key: 't', title: 'T' }));
must('empty card', none.includes('这个话题还没有关联项目') && none.includes('data-act="topic-project"') && none.includes('关联项目…') && !none.includes('data-pj-fill'));
const rd = String(TR.topicReader({ key: 'k', title: 'T', project: { key: 'P', name: evil, summary: evil, link_source: 'llm' }, brief: null, history: [], items: [], counts: {} }));
must('topic reader: 现在需要你 first, then 现在的状况, the project card last, escaped', clean(rd) && rd.indexOf('tp-needs') > 0 && rd.indexOf('tp-needs') < rd.indexOf('tp-now') && rd.indexOf('tp-now') < rd.indexOf('data-pj-card'));
must('older server (no project field): no card', !String(TR.topicReader({ key: 'k', title: 'T', brief: null, history: [], items: [], counts: {} })).includes('data-pj-card'));
must('project null: the empty card', String(TR.topicReader({ key: 'k', title: 'T', project: null, brief: null, history: [], items: [], counts: {} })).includes('pj-card-none'));
const grp = { key: 'P1', project: { key: 'P1', name: evil, summary: evil }, name: evil, topics: [{ key: 'tA', title: evil, counts: { decision: 1, total: 1 }, state_now: evil, oldest_ts: '2026-11-01T00:00:00Z' }], counts: { decision: 1, total: 1 }, oldest_ts: '2026-11-01T00:00:00Z' };
const prd = String(PR.projectReader({ key: 'P1', name: evil, summary: evil, related: { deadline: [{ key: evil, value: evil }], person: [], note: [{ key: '进展·x', value: '（P1）' + evil }], goal: [] },
  recent_tasks: [{ id: 'tsk_1', title: evil, headline: evil, status: 'done', updated_at: '2026-11-01T00:00:00Z' }] }, grp, { view: 'inbox' }));
must('project reader escapes', clean(prd) && prd.includes('data-reader="project"'));
must('project reader actions', prd.includes('data-act="open-project-inbox"') && prd.includes('data-act="open-context"') && prd.includes('data-act="split-select"') && prd.includes('data-act="open-task"'));
must('project reader works without the API detail', String(PR.projectReader({ _fallback: true, key: 'P1', name: 'n', summary: '' }, { ...grp, name: 'n', project: { key: 'P1', name: 'n' } }, {})).includes('这个项目还没有简介'));
P.noteTopicProjects([{ key: 'tA', project: { key: 'P1', name: 'P1' } }, { key: 'tZ', project: null }]);
const rc = String(R.recapCard({ id: 'tsk_1', topic: { key: 'tA', title: 'A' }, ts: '2026-11-01T00:00:00Z' }));
must('recap header shows the project chip (click -> the topic reader)', rc.includes('data-rc-project') && rc.includes('pj-chip') && rc.includes('data-focus="project"'));
must('recap header: unlinked topic has no chip yet', !String(R.recapCard({ id: 'x', topic: { key: 'tZ', title: 'Z' }, ts: '2026-11-01T00:00:00Z' })).includes('pj-chip'));

// ---- the inbox list grouped by project
const ctl = new split.Split({ id: 'unit-pj', title: '收件箱', icon: 'inbox', sections: [], emptyAll: () => '' });
ctl.adopt({ items, extra: null, topics });
must('default grouping is 按项目', ctl.mode() === 'project' && ctl.hasProjects());
must('keys: project header only for projects with several topics; 未关联项目 has none', ctl.visibleKeys().join() === 'project:P1,topic:tA,task:a1,topic:tB,task:b1,topic:tC,task:c1,topic:tD,task:d1', ctl.visibleKeys().join());
ctl.collapsed = { 'pj:P1': true };
must('collapsed project keeps its header only', ctl.visibleKeys().join() === 'project:P1,topic:tC,task:c1,topic:tD,task:d1');
ctl.sel = 'task:a1';
must('j / k anchor of an item inside a collapsed project is the project header', ctl.anchor() === 'project:P1' && ctl.headerOf('task:a1') === 'project:P1');
ctl.collapsed = {}; ctl.sel = null;
must('header of an item is its topic', ctl.headerOf('task:a1') === 'topic:tA' && ctl.headerOf('topic:tA') === 'project:P1');
ctl.collapsed = { tC: true };
must('a collapsed single-topic project keeps its header (= the topic)', ctl.visibleKeys().includes('topic:tC') && !ctl.visibleKeys().includes('task:c1'));
ctl.collapsed = {};
must('project / topic selections exist', ctl.exists('project:P1') && ctl.exists('topic:tC') && !ctl.exists('project:__none__') && !ctl.exists('project:nope'));
must('project group aggregates the whole inbox', ctl.findProject('project:P1').topics.length === 2 && ctl.projectOf('tD') === null && ctl.projectOf('tA').key === 'P1');
ctl.paintList = () => {}; ctl.paintToolbar = () => {};
ctl.collapsed = { 'pj:P1': true, tA: true };
must('reveal opens the groups around a selection', ctl.reveal('task:a1') === true && !ctl.collapsed['pj:P1'] && !ctl.collapsed.tA);
must('successor of a vanished project header is the next header', (() => { const prev = ctl.visibleKeys(); ctl.adopt({ items: items.filter((i) => i.topic_key !== 'tA' && i.topic_key !== 'tB'), extra: null, topics }); const nxt = ctl.successor(prev, 'project:P1'); ctl.adopt({ items, extra: null, topics }); return nxt === 'topic:tC'; })());
const html = String(ctl.listBodyHTML());
must('list markup: project groups, sub-topics, 未关联项目', html.includes('data-project="P1"') && html.includes('tgroup sub') && html.includes('data-project="__none__"') && html.includes('未关联项目') && html.includes('by-project'));
const noProj = new split.Split({ id: 'unit-pj2', title: '收件箱', icon: 'inbox', sections: [], emptyAll: () => '' });
noProj.adopt({ items, extra: null, topics: topics.map(({ project, ...t }) => t) });
must('older server (no project field): falls back to 按话题', noProj.mode() === 'topic' && !noProj.hasProjects());
St.S.params = new URLSearchParams('project=P2');
must('?project= filters the list', ctl.visible().map((i) => i.key).join() === 'task:c1');
St.S.params = new URLSearchParams('project=__none__');
must('?project=__none__ shows the unlinked topics', ctl.visible().map((i) => i.key).join() === 'task:d1');
const bar = String(ctl.toolbarHTML());
must('toolbar: 按项目 / 按话题 / 按类型 + the filter chip', bar.includes('按项目') && bar.includes('按话题') && bar.includes('按类型') && bar.includes('data-act="split-project-clear"'));
St.S.params = new URLSearchParams('');
const store2 = await import(u('core/store.js'));
store2.pref.set('unit-pj.group', 'topic');
must('按话题 headers carry the project chip', String(ctl.listBodyHTML()).includes('tg-pj') && ctl.mode() === 'topic');
store2.pref.set('unit-pj.group', 'type');
const rows = String(ctl.listBodyHTML());
must('按类型 rows say which project they belong to (plain text, no chip)', ctl.mode() === 'type' && rows.includes('sr-where') && !rows.includes('in-row'));
const rowEvil = String(split.rowHTML(mk('task:e', 'tA', 'review', '2026-11-01T00:00:00Z'), null, { showTopic: true, project: { key: 'P', name: evil } }));
must('row project name escapes', clean(rowEvil) && rowEvil.includes('sr-where'));

// ---- 上下文: projects as cards, working notes muted under a project or in a group of their own
const fact = (id, kind, key, value, extra = {}) => ({ id, kind, key, value, source: 'ctx', confidence: 0.6, status: 'active', pinned: 1, updated_at: '2026-11-01T00:00:00Z', ...extra });
const ctx = K.contextView.render({ kinds: { project: '项目', note: '备注' }, groups: {
  project: [fact('c1', 'project', '学位论文 thuthesis', '修订第四章'), fact('c2', 'project', evil, evil)],
  note: [fact('n1', 'note', '编译命令', 'make thesis'), fact('n2', 'note', '进展·a', '（学位论文 thuthesis）7 个未提交文件', { source: 'agent' }), fact('n3', 'note', '进展·b', 'GPU 磁盘 91%', { source: 'agent' })] },
  profile: { exists: false, path: 'p' }, count: 5,
  projects: [{ key: '学位论文 thuthesis', name: '学位论文 thuthesis', summary: 's', open_items: 4, topic_keys: ['t1', 't2'], deadlines: 1, notes: 2, last_activity: '2026-11-04T00:00:00Z' }] });
const ctxHtml = String(ctx);
must('context: one card per project with the link to the filtered inbox', ctxHtml.includes('data-project-card="学位论文 thuthesis"') && ctxHtml.includes('href="#/inbox?project=') && ctxHtml.includes('关联话题') && ctxHtml.includes('待处理'));
must('context: edit / pin / delete stay on the project cards', ctxHtml.includes('data-act="fact-edit"') && ctxHtml.includes('data-act="fact-delete"'));
must('context: 相关事实 is collapsed and loads lazily', ctxHtml.includes('data-act="pj-rel-toggle"') && !ctxHtml.includes('pjc-rel" id="pjrel-c1" data-key="学位论文 thuthesis" open'));
must('context: matched working notes sit under their project, the rest in 工作笔记', ctxHtml.includes('pjc-notes') && ctxHtml.includes('ctx-progress') && ctxHtml.includes('7 个未提交文件') && ctxHtml.includes('GPU 磁盘 91%'));
const noteCard = ctxHtml.slice(ctxHtml.indexOf('<h3>备注</h3>'), ctxHtml.indexOf('</section>', ctxHtml.indexOf('<h3>备注</h3>')));
must('context: working notes are not in 备注', noteCard.includes('make thesis') && !noteCard.includes('进展·') && !noteCard.includes('GPU 磁盘'));
must('context escapes', clean(ctxHtml));
if (bad.length) { console.log('FAILED: ' + bad.join(', ')); process.exit(1); }
console.log('ok');
"""
    r = run_node(code.replace("__ROOT__", root))
    assert r.returncode == 0, (r.stdout + r.stderr)[:2500]


def test_project_tokens_are_defined_for_both_themes():
    """The 8 identity colours exist in the light theme and in both dark scopes (OS preference + the manual toggle); projects.css is imported."""
    tokens = (DASH / "css" / "tokens.css").read_text(encoding="utf-8")
    for i in range(1, 9):
        assert tokens.count(f"--pj{i}:") == 3, f"--pj{i} must be set for light, dark (media) and dark (data-theme)"
    assert 'css/projects.css' in (DASH / "style.css").read_text(encoding="utf-8")
    css = (DASH / "css" / "projects.css").read_text(encoding="utf-8")
    for i in range(1, 9):
        assert f".pj-c{i} {{ --pjc: var(--pj{i}); }}" in css
    assert "@media (prefers-reduced-motion: reduce)" in css and "@media (max-width: 480px)" in css, "motion + phone rules"


def test_projects_api_contract_the_ui_reads(client):
    """Projects: topics carry `project`, /api/projects + /api/projects/{key} have the fields the cards read, and linking needs the token."""
    from urllib.parse import quote

    from awos import repo
    name = "学位论文 thuthesis"
    repo.upsert_fact("project", name, "thuthesis 在 revision 分支上修订第四章", source="ctx", actor="user", pinned=True)
    repo.upsert_fact("deadline", "预答辩（学位论文 thuthesis）", "2026-12-05 14:00", source="ctx", actor="user", pinned=True)
    repo.upsert_fact("note", "进展·thuthesis-commit-state", "（学位论文 thuthesis）7 个未提交文件", source="agent", actor="agent")
    tid = repo.create_task("检查 thuthesis 仓库未提交改动", status="triaged", project="thuthesis")
    inbox = client.get("/api/inbox").json()
    assert inbox["topics"] and all("project" in t for t in inbox["topics"]), "every topic summary carries `project` (null when unlinked)"
    key = next(f["topic_key"] for f in inbox["feed"] if f["id"] == tid)
    enc = quote(key, safe="")
    H = {"X-AWOS-Token": "t0ken"}
    assert client.post(f"/api/topics/{enc}/project", json={"project_key": name}).status_code == 401, "linking needs the token"
    assert client.post("/api/topics/nope-not-a-topic/project", json={"project_key": name}, headers=H).status_code == 404
    assert client.post(f"/api/topics/{enc}/project", json={"project_key": "没有这个项目"}, headers=H).status_code == 422
    ok = client.post(f"/api/topics/{enc}/project", json={"project_key": name}, headers=H).json()
    assert ok["ok"] is True and {"key", "name", "summary", "link_source"} <= set(ok["project"]) and ok["project"]["link_source"] == "user"
    topic = client.get(f"/api/topics/{enc}").json()
    assert topic["project"]["key"] == name, "the topic reader gets the project with the topic"
    assert next(t for t in client.get("/api/inbox").json()["topics"] if t["key"] == key)["project"]["name"] == name
    lst = client.get("/api/projects").json()["projects"]
    p = next(x for x in lst if x["key"] == name)
    assert {"key", "name", "summary", "open_items", "topic_keys", "deadlines", "notes", "last_activity"} <= set(p)
    assert key in p["topic_keys"] and p["open_items"] >= 1 and p["deadlines"] >= 1
    d = client.get(f"/api/projects/{quote(name, safe='')}").json()
    assert {"key", "name", "summary", "pinned", "fact_id", "related", "topics", "recent_tasks", "role"} <= set(d), "keys with spaces / CJK are URL-encoded"
    assert {"deadline", "person", "note", "goal"} <= set(d["related"]) and d["related"]["deadline"][0]["key"].startswith("预答辩")
    assert any(f["key"].startswith("进展·") for f in d["related"]["note"]), "working notes arrive as related notes (the UI mutes them)"
    assert [t["key"] for t in d["topics"]] == [key] and d["recent_tasks"][0]["id"] == tid and {"headline", "status", "updated_at"} <= set(d["recent_tasks"][0])
    assert client.get("/api/projects/nope").status_code == 404
    made = client.post(f"/api/topics/{enc}/project", json={"new_name": "新项目", "new_summary": "一句话"}, headers=H).json()
    assert made["project"]["name"] == "新项目" and any(x["key"] == "新项目" for x in client.get("/api/projects").json()["projects"])
    gone = client.post(f"/api/topics/{enc}/project", json={"project_key": None}, headers=H).json()
    assert gone["ok"] is True and gone["project"] is None and client.get(f"/api/topics/{enc}").json()["project"] is None, "unlink = 不属于任何项目"


def test_glass_layer_is_loaded_last_and_respects_accessibility_preferences():
    """The macOS-style material/motion layer overrides the base system, so it must come last; it needs solid fallbacks."""
    imports = re.findall(r'@import url\("([^"]+)"\)', (DASH / "style.css").read_text(encoding="utf-8"))
    assert imports[-1] == "css/glass.css" and (DASH / imports[-1]).is_file()
    css = (DASH / "css" / "glass.css").read_text(encoding="utf-8")
    assert "prefers-reduced-transparency" in css and "prefers-reduced-motion" in css and "@supports not" in css
    assert "backdrop-filter" in css and "@keyframes flow-in" in css


def test_overview_shows_the_whiteboard_and_cascade_runs_only_on_navigation():
    overview = (DASH / "js" / "views" / "overview.js").read_text(encoding="utf-8")
    assert "/whiteboard" in overview and "add-rule" in overview and "whiteboardCard(wb)" in overview
    shell = (DASH / "js" / "shell.js").read_text(encoding="utf-8")
    assert "if (initial) playFlow(el);" in shell        # a live (SSE) refresh must not replay the entrance animation
