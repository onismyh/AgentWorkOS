"""Inbox / Outbox readability: every item says what it asks of you in plain words, urgency is shown only when it matters, and the
technical fields stay out of the way (pure helpers + the markup of the list rows and the readers, exercised through Node)."""
from __future__ import annotations

import json

from tests.test_dashboard_static import DASH, needs_node, run_node


@needs_node
def test_readable_helpers():
    root = json.dumps((DASH / "js").as_posix())
    code = f"""
import {{ pathToFileURL }} from 'node:url';
const R = await import(pathToFileURL({root} + '/domain/readable.js').href);
const bad = []; const must = (n, c) => {{ if (!c) bad.push(n); }};
const now = Date.parse('2026-11-06T12:00:00Z'); const iso = (ms) => new Date(ms).toISOString();
const H = 3600000; const D = 24 * H;

// headlines never repeat the verb the label already says
must('decision: 决定/确认/确定 stripped', R.plainHeadline('decision', '决定电厂稿件顺序规划如何处理') === '电厂稿件顺序规划如何处理' && R.plainHeadline('decision', '确认图3走向：沿用v6') === '图3走向：沿用v6' && R.plainHeadline('decision', '确定Fig5以四情景版本推进') === 'Fig5以四情景版本推进');
must('approval / review verbs stripped', R.plainHeadline('approval', '批准发送邮件给导师') === '发送邮件给导师' && R.plainHeadline('review', '验收第五章图件') === '第五章图件');
must('other sections and short results keep the text', R.plainHeadline('handoff', '先确认主线副本，再决定22个文件') === '先确认主线副本，再决定22个文件' && R.plainHeadline('decision', '确认备份') === '确认备份');
must('verbs', R.askVerb({{ section: 'decision' }}) === '要你决定' && R.askVerb({{ section: 'review' }}) === '待你验收' && R.askVerb({{ section: 'handoff' }}) === '需要你接手'
  && R.askVerb({{ section: 'history', status: 'approved' }}, {{ approved: '已批准' }}) === '已批准');

// the second line: the AI's recommendation, else a summary that is not a repeat of the headline
must('note: recommendation wins', R.rowNote({{ rec_label: '修复并重跑', gist: 'x', headline: 'h' }}) === 'AI 建议：修复并重跑');
must('note: gist, but never a repeat', R.rowNote({{ gist: '依据不足，建议暂缓', headline: 'h' }}) === '依据不足，建议暂缓' && R.rowNote({{ gist: '确认备份是否有意', headline: '确认备份是否有意' }}) === '' && R.rowNote({{ gist: '' , headline: 'h' }}) === '');
must('recLabel', R.recLabel({{ recommendation: 'a：因为…', options: [{{ key: 'a', label: '方案甲' }}] }}, () => 'a') === '方案甲' && R.recLabel({{ options: [] }}, () => 'a') === '');

// ONE marker, and only when it matters
const ap = (extra) => ({{ kind: 'approval', status: 'pending', section: 'decision', ts: iso(now - H), ...extra }});
must('plenty of time: no marker', R.urgencyOf(ap({{ expires_at: iso(now + 3 * D) }}), now) === null);
must('expires within a day: warn, within 6 hours: crit', R.urgencyOf(ap({{ expires_at: iso(now + 10 * H) }}), now)?.tone === 'warn' && R.urgencyOf(ap({{ expires_at: iso(now + 2 * H) }}), now)?.tone === 'crit');
must('expired', R.urgencyOf(ap({{ expires_at: iso(now - H) }}), now)?.text === '已过期');
must('overdue task', R.urgencyOf({{ kind: 'task', section: 'handoff', due_at: iso(now - D), ts: iso(now - H) }}, now)?.text === '已逾期');
must('high risk', R.urgencyOf(ap({{ risk: 'high', expires_at: iso(now + 3 * D) }}), now)?.text === '高风险');
must('waiting for days', R.urgencyOf(ap({{ ts: iso(now - 5 * D), expires_at: iso(now + 3 * D) }}), now)?.text === '已等 5 天');
must('suggestions and handled items never nag', R.urgencyOf({{ kind: 'task', section: 'suggested', ts: iso(now - 9 * D) }}, now) === null && R.urgencyOf(ap({{ section: 'history', expires_at: iso(now - H) }}), now) === null);
must('only one: expiry beats high risk', R.urgencyOf(ap({{ risk: 'high', expires_at: iso(now + H) }}), now)?.text !== '高风险');

// what needs you most first
const mk = (key, section, extra = {{}}) => ({{ key, section, kind: 'task', ts: iso(now - H), ...extra }});
const src = [mk('sug', 'suggested'), mk('rev', 'review'), mk('apv2', 'approval', {{ kind: 'approval', status: 'pending', expires_at: iso(now + 3 * D) }}), mk('apv1', 'approval', {{ kind: 'approval', status: 'pending', expires_at: iso(now + 5 * H) }}), mk('dec', 'decision', {{ kind: 'approval', status: 'pending', expires_at: iso(now + 3 * D) }}), mk('fail', 'failed')];
const copy = src.map((x) => x.key).join();
must('order: decision, approval (soonest expiry first), review, failed, suggested', R.sortItems(src, now).map((x) => x.key).join() === 'dec,apv1,apv2,rev,fail,sug');
must('sortItems does not mutate', src.map((x) => x.key).join() === copy);

// one sentence of "where it stands"
const long = '这是电厂改造研究的代码和稿件整理，关系到你博士阶段研究成果的可靠性。79 个历史提交已备份到远端。现在卡在稿件结论：所谓顺序规划比集成规划贵约20%，存在管网成本重复计费，最新稿件修订暂缓提交；另有20项任务临时文件等待清理批准。';
const one = R.oneLineState(long, 40);
must('state: the present-tense sentence, clamped', one.startsWith('现在卡在稿件结论') && one.length <= 40 && !one.includes('79 个'));
must('state: cut at the first ；when too long', R.oneLineState('现在卡在结论：A 成立，B 待核实；另有三项文件等清理', 20) === '现在卡在结论：A 成立，B 待核实');
must('state: no present-tense sentence -> the last one; empty -> empty', R.oneLineState('第一句。第二句。') === '第二句' && R.oneLineState('') === '' && R.oneLineState(null) === '');
must('needsText', R.needsText({{ total: 3, suggested: 0 }}) === '3 项要你处理' && R.needsText({{ total: 3, suggested: 1 }}) === '2 项要你处理 · 1 条 AI 建议' && R.needsText({{ total: 1, suggested: 1 }}) === '1 条 AI 建议' && R.needsText({{ total: 0 }}) === '没有待处理');
if (bad.length) {{ console.log('FAILED: ' + bad.join(', ')); process.exit(1); }}
console.log('ok');
"""
    r = run_node(code)
    assert r.returncode == 0, (r.stdout + r.stderr)[:2500]


@needs_node
def test_rows_and_readers_lead_with_the_ask_and_hide_the_jargon():
    root = json.dumps((DASH / "js").as_posix())
    code = f"""
import {{ pathToFileURL }} from 'node:url';
const store = new Map();
globalThis.localStorage = {{ getItem: (k) => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, String(v)) }};
const u = (p) => pathToFileURL({root} + '/' + p).href;
const bad = []; const must = (n, c) => {{ if (!c) bad.push(n); }};
const split = await import(u('views/split.js'));
const reader = await import(u('ui/reader.js'));
const St = await import(u('core/state.js'));
const evil = '<img src=x onerror=alert(1)>';
const clean = (html) => !/<img\\b/.test(html);
const soon = new Date(Date.now() + 5 * 3600000).toISOString();
const item = (extra = {{}}) => ({{ key: 'approval:a1', dom: 'approval_a1', kind: 'approval', id: 'a1', section: 'decision', headline: '决定电厂稿件顺序规划如何处理', gist: '2.784T差额疑似重复计费', keywords: ['电厂', '重复计费'], level: 4, priority: 1, risk: 'medium',
  status: 'pending', ts: new Date(Date.now() - 3600000).toISOString(), stamp: 's', expires_at: null, rec_label: '修复成本核算并重跑', topic_title: '电厂改造项目', ...extra }});

// ---- list row
const row = String(split.rowHTML(item(), null, {{ inTopic: true }}));
must('row: the ask leads, the verb is not repeated', row.includes('sr-ask') && row.includes('要你决定') && row.includes('电厂稿件顺序规划如何处理') && !row.includes('决定电厂稿件'));
must('row: the AI recommendation is the second line', row.includes('AI 建议：修复成本核算并重跑'));
must('row: no jargon (risk / level / priority badges, keyword chips, icon tile)', !/中风险|低风险|class="lvl|class="badge|class="pri|kw-chip|class="chip|sr-ic|sr-l3/.test(row));
must('row: no urgency marker while there is time', !row.includes('sr-flag'));
const hot = String(split.rowHTML(item({{ expires_at: soon }}), null, {{ inTopic: true }}));
must('row: one marker when it expires within a day', (hot.match(/sr-flag/g) || []).length === 1 && hot.includes('小时后过期'));
const inType = String(split.rowHTML(item({{ topic_title: evil }}), null, {{ showTopic: true }}));
must('row: where it belongs (type view), escaped', inType.includes('sr-where') && clean(inType));
must('row: a handled approval says what happened', String(split.rowHTML(item({{ section: 'history', status: 'approved', rec_label: '' }}), null, {{}})).includes('已批准'));

// ---- approval reader: the ask first, the technical fields folded away
const a = {{ id: 'a1', kind: 'decision', status: 'pending', title: '电厂稿件中的顺序规划成本比较如何处理？', headline: '决定电厂稿件顺序规划如何处理', gist: '2.784T差额疑似重复计费', keywords: ['电厂', '重复计费'], risk: 'medium', expires_at: soon,
  created_at: new Date(Date.now() - 3600000).toISOString(), recommendation: 'repair：保留研究问题，通过有效实验重新建立结论依据。', context_md: '背景正文',
  options: [{{ key: 'repair', label: '修复成本核算并重跑', description: '核对两阶段成本范围' }}, {{ key: 'drop', label: '撤掉比较结论', description: '暂不比较' }}], payload: {{ question: 'q' }} }};
const html = String(reader.approvalReader({{ approval: a, task: null, events: [] }}));
const head = html.slice(0, html.indexOf('rd-body'));
must('reader: kicker, plain title, gist', head.includes('rd-ask') && head.includes('要你决定') && head.includes('rd-flag') && head.includes('电厂稿件顺序规划如何处理') && head.includes('2.784T差额疑似重复计费'));
must('reader: header carries no badges / keywords / ids', !/class="badge|rd-kws|meta-id|中风险|class="chip/.test(head));
const todo = html.indexOf('rd-todo'); const rec = html.indexOf('rd-rec'); const recap = html.indexOf('data-recap');
must('reader: 你需要做什么 + the AI recommendation come before the recap', todo > 0 && rec > todo && (recap < 0 || recap > rec) && html.includes('选它会：核对两阶段成本范围') && html.includes('保留研究问题'));
must('reader: the recommended option name is said once, without the raw key', html.includes('修复成本核算并重跑') && !html.includes('repair：'));
const folded = (page, label) => {{ const at = page.indexOf('<span>' + label + '</span>'); const tag = page.lastIndexOf('<details', at); return at > 0 && tag >= 0 && !page.slice(tag, page.indexOf('>', tag) + 1).includes(' open'); }};
const det = html.slice(html.indexOf('rd-details'));
must('reader: technical fields live in a folded 详细信息', html.includes('rd-details') && folded(html, '详细信息') && det.includes('电厂') && det.includes('编号'));
must('reader: option meanings and 背景 are folded too', folded(html, '各选项的含义') && folded(html, '背景'));
must('reader: decision bar still there', html.includes('rd-decide') && html.includes('data-act="decide"'));
const evilA = String(reader.approvalReader({{ approval: {{ ...a, headline: evil, gist: evil, recommendation: evil, options: [{{ key: 'x', label: evil, description: evil }}], context_md: evil }}, task: null, events: [] }}));
must('reader escapes', clean(evilA));

// ---- task reader
const t = {{ id: 't1', title: '整理电厂改造仓库', headline: '先确认主线副本，再决定22个文件', gist: 'D盘副本待归档', status: 'waiting_user', level: 1, priority: 2, category: 'dev', kind: 'task', executor: 'codex', cost_usd: 0.4, attempts: 1, max_attempts: 3, project: '电厂改造模型',
  summary: '已完成一页 briefing。', created_at: '2026-11-05T10:00:00Z', updated_at: '2026-11-06T08:00:00Z', source: 'sensor:git' }};
const th = String(reader.taskReader({{ task: t, approvals: [], artifacts: [], runs: [], events: [] }}));
const thead = th.slice(0, th.indexOf('rd-body'));
must('task reader: kicker + plain header, no level / priority / executor / id chips', thead.includes('需要你接手') && thead.includes('rd-meta slim') && !/class="lvl|class="pri|exav|meta-id|rd-kws|\\$0\\.4|尝试/.test(thead));
must('task reader: 你需要做什么 first, then what the AI produced, the technical fields folded', th.indexOf('rd-todo') > 0 && th.indexOf('rd-todo') < th.indexOf('AI 产出') && th.includes('详细信息与设置') && th.includes('rd-details'));
if (bad.length) {{ console.log('FAILED: ' + bad.join(', ')); process.exit(1); }}
console.log('ok');
"""
    r = run_node(code)
    assert r.returncode == 0, (r.stdout + r.stderr)[:2500]
