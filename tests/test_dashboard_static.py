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
const kinds = [['act', ACTS, [/data-act="([a-z0-9-]+)"/g, /\bact:\s*'([a-z0-9-]+)'/g]], ['change', CHANGES, [/data-change="([a-z0-9-]+)"/g]], ['input', INPUTS, [/data-input="([a-z0-9-]+)"/g]], ['form', FORMS, [/data-form="([a-z0-9-]+)"/g]]];
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
