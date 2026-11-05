# 03 · Sensing Sources, Personal Context & HITL Push Channels

> Probed: 2026-11-05 (Beijing ≈ 17:30). Machine: Windows 10 Pro 19045, user `C:\Users\admin`.
> Method: read-only only. SQLite opened with `file:...?mode=ro` (or `&immutable=1` when the owning app holds a lock). The report contains **only metadata, counts, schemas and paths**. It has no message bodies, mail contents, contacts or credentials. Mail account directories are masked.

## 摘要（中文）

1. **现在就能用、不需要任何凭据的数据源**：Zotero 本地 API（`127.0.0.1:23119`，Zotero 9.0.6，3,236 条，支持 `since=<version>` 增量）；claude-mem 数据库（结构很丰富，但 **2026-08-18 以后就停了**，worker 没有运行）；Claude Code `history.jsonl`（1,378 条，2026-08-05 → 今天）和 `projects/*.jsonl`；Codex `state_5.sqlite` 的 threads 表（388 个线程，含 cwd/标题/分支）、`session_index.jsonl`、`ambient-suggestions`；本地 24 个 git 仓库；最近 14 天改过的文件。
2. **对 research/00 的更正**：网易邮箱大师的本地库**不是私有加密格式**，而是普通 SQLite（`D:\MailMasterData\<账号>\mail.db/content.db/search.db`）。标题、发件人、时间、未读状态、摘要都可以只读拿到；正文多数经过压缩，格式没有文档。邮箱大师自带的 `calendar.db` 有 313 个 CalDAV 事件。建议：先用本地元数据做"零凭据"邮件感知；长期再改用 IMAP（学校邮箱需要"客户端专用密码"，163 需要"授权码"）。
3. **不可用、也不建议用的**：微信/企业微信数据库（SQLCipher 加密，取密钥要读进程内存，违反 ToS，有封号风险）→ 改用企业微信"智能机器人"作为采集和审批入口。Outlook 没有 Profile，COM 不可用（但 OUTLOOK.EXE 从 05:11 起一直在无窗口运行，疑似被探针拉起）。Notion 本地缓存 `notion.db` 是明文 SQLite，但已经过时（最后编辑 2026-08-18），也不完整 → 继续用 Codex Notion 连接器。
4. **两个重要的环境坑**：① 系统时区是 **UTC-4（SA Western）**，和北京时间差 12 小时，调度和免打扰必须显式用 `Asia/Shanghai`；② WinINET 系统代理的绕过列表里没有 `127.0.0.1`，PowerShell `Invoke-WebRequest` 访问本地服务会被 Clash 断开；Python 和 curl 遵守 `NO_PROXY`，没有问题。
5. **推送通道**：原生 WinRT Toast 已经实测成功（没有安装任何模块，Action Center 里能看到 "AgentWorkOS test"）。手机侧推荐**企业微信智能机器人（长连接 WebSocket，模板卡片按钮可以回调，不需要公网 IP）**作为主通道，MVP 阶段先用群机器人 webhook 单向推送。飞书自建应用加长连接是同样好用的替代。兜底用 **SMTP 发给自己**，支持回信审批。Server酱免费版每天只有 5 条，PushPlus 要求实名，ntfy 在国内不可靠，都不推荐。
6. **审批设计**：Outbox 条目必须带上 `payload_hash`、`idempotency_key`、`target_version`（防过期）、`allowed_decisions`、`reversible`、`default_on_expiry`。手机审批优先走 IM 卡片回调（只有出站连接）。如果用链接，必须是 HMAC 签名、一次性、短时效的 token，GET 只显示确认页，POST 才生效，因为 IM 会预抓取链接。高风险操作需要 PIN 或者在桌面确认。
7. **个人上下文模型**：分成 7 层（身份/角色、长期目标、活跃项目、承诺与截止、人物、兴趣与技术栈、习惯与偏好）。每条事实都带来源、置信度、敏感级别和衰减期。原始内容永远不出本机，只有脱敏后的摘要才会发给 LLM 或推送通道。

---

## 1. Local source probing

### 1.1 Summary matrix

| Source | Accessible now? | Credential needed | Freshness | Recommended access path |
|---|---|---|---|---|
| Zotero local API | ✅ `connector/ping` 200; `/api/users/0/items` 200 | none | live | HTTP local API (bypass proxy) |
| Zotero sqlite | ⚠️ exclusive-locked; `immutable=1` works | none | live (lock) | API first; `.bak` snapshot for bulk |
| Notion `notion.db` | ✅ plain SQLite (immutable) | none | **stale** (≤ 2026-08-18) | Codex Notion connector / official API |
| claude-mem DB | ✅ `mode=ro` OK | none | **stale** (≤ 2026-08-18) | direct SQLite + FTS5 |
| Claude Code history/projects | ✅ | none | live | JSONL tail with byte-offset cursor |
| Codex state/index/rollouts | ✅ `mode=ro` OK | none | live | `state_5.sqlite` threads + `session_index.jsonl` |
| Mail Master local store | ✅ plain SQLite (immutable) | none | as fresh as last client run | metadata sensor (Phase 1) → IMAP (Phase 2) |
| Mail Master calendar.db | ✅ plain SQLite | none | last client run | read-only snapshot |
| IMAP (Tsinghua / 163) | not configured | client password / 授权码 | live | Phase 2 |
| WeChat / WeCom DBs | ❌ encrypted; do not touch | n/a | n/a | WeCom bot as capture channel |
| Outlook / Windows Calendar | ❌ no MAPI profile | n/a | n/a | none |
| Git repos (24 found) | ✅ | none | live | `git status --porcelain=v2 -b`, `git log` |
| Recent docs | ✅ | none | live | mtime scan with exclude globs |

### 1.2 Zotero

- Process running. Prefs set `extensions.zotero.httpServer.localAPI.enabled = true`. **Data dir is `D:\zotero`** (`useDataDir=true`). `~/Zotero` is a stale legacy copy (last write 2025-09-25), so ignore it.
- `GET http://127.0.0.1:23119/connector/ping` → 200. `GET /api/users/0/items?limit=1` → 200 with headers `X-Zotero-Version: 9.0.6`, `Zotero-API-Version: 3`, `Zotero-Schema-Version: 42`, `Total-Results: 3236`, `Last-Modified-Version: 78468`. `/collections` → 200. `?since=78400` → `Total-Results: 22`, so **incremental sync by library version works**. Cursor = `Last-Modified-Version`.
- **Proxy gotcha:** the first attempt through PowerShell `Invoke-WebRequest` failed ("connection closed") because WinINET sends `127.0.0.1` to Clash (`ProxyOverride` contains `<local>` but not `127.0.0.1`). `curl --noproxy '*'`, `System.Net.WebClient` with `Proxy=$null`, and Python (env `NO_PROXY=localhost,127.0.0.1,::1` is set) all work.
- `zotero.sqlite` (85 MB) is **exclusively locked** while Zotero runs: `mode=ro` → `database is locked`. `mode=ro&immutable=1` reads, but it can see a torn state when a `-journal` is active. `zotero.sqlite.bak` (refreshed daily, last 2026-11-04) opens cleanly.
- Counts (live, immutable): items 3,905 = 975 journalArticle, 1,089 attachment, 1,068 annotation, 678 note, 34 report, 25 preprint, 11 conferencePaper, …; collections 61; tags 266; fulltextItems 1,089; creators 4,604; feeds 0. `dateAdded` 2022-09-01 → **2026-09-27** (no new items in 14 days; 3 items modified, 1 note edited).
- Relevant tables: `items(itemID,itemTypeID,dateAdded,dateModified,clientDateModified,libraryID,key,version)`, `itemData`/`itemDataValues`/`fields` (EAV metadata: title, DOI, date…), `itemNotes(itemID,parentItemID,note,title)`, `itemAnnotations(itemID,parentItemID,type,text,comment,color,pageLabel,position)`, `itemAttachments(…,path,contentType,lastRead)`, `collections`/`collectionItems`, `tags`/`itemTags`, `fulltextItems`/`fulltextWords`, `deletedItems`, `savedSearches`.
- Context value: reading interests (collections, tags), "currently reading" (`itemAttachments.lastRead`, new annotations), and new notes. Two extras sit in the data dir: plugins `zoterogpt` and `ethereal-pro` (modified today).

### 1.3 Notion desktop

- `%APPDATA%\Notion\notion.db` (5 MB) is **plain SQLite**, readable with `immutable=1` while Notion runs. Tables: `block` 2,428 rows (text 903, **to_do 427**, bulleted_list 233, table_row 133, page 131…), `collection` 9, `collection_view` 58, `space` 2, `notion_user` 41, `comment` 1, `notification` 0, `transactions` 0. A `microsoft_oauth` table has token columns. It is empty, and the sensor must never read it.
- `block.last_edited_time` runs 2024-04-26 → **2026-08-18**, with **0 blocks edited in the last 14 days**. This is an opportunistic offline cache of visited pages, not a mirror. Undocumented schema.
- Verdict: don't use it as a source of truth. Per research/00, the user's schedule and plans live in Notion and the Codex Notion connector is already authenticated, so use that, or an official integration token later. `notion.db` is at most an offline hint for page IDs and titles.

### 1.4 claude-mem (`~/.claude-mem/claude-mem.db`, 99.6 MB + WAL)

| Table | Rows | Key columns |
|---|---|---|
| `observations` | 994 | `project, type, title, subtitle, facts, narrative, concepts, files_read, files_modified, prompt_number, created_at(_epoch), memory_session_id` |
| `session_summaries` | 39 | `project, request, investigated, learned, completed, next_steps, notes, files_read, files_edited, created_at` |
| `user_prompts` | 117 | `content_session_id, prompt_number, prompt_text, created_at` |
| `sdk_sessions` | 3 | `project, platform_source(=claude), user_prompt, started_at, completed_at, status` |
| `pending_messages` | 0 | tool_input/tool_response staging |
| `sync_outbox` | **232,685** | `op_uuid, rev, body, canonical_body, created_at_epoch` (cloud-sync backlog; bloat) |
| `*_fts*` | — | FTS5 indexes over observations / summaries / prompts |

- Date range: prompts 2026-08-12 → 2026-08-18; observations and summaries 2026-08-17 → 2026-08-18. Only 2 projects (729 / 265 observations).
- `type` is free-form: about 10 canonical values (discovery 470, change 99, bugfix 70, decision 15, feature 8…) plus about 300 one-off slugs. Normalize before use.
- **The worker is not running** (port 37777 not listening), so nothing has been captured for about 2.5 months. `settings.json` holds API-key and cloud-sync-token keys. Only key names were inspected; values must never be read.
- Value: `session_summaries.next_steps/learned` and `observations.facts/concepts` are the best ready-made "project state" summaries. Treat them as **historical**. If the user restarts claude-mem, AgentWorkOS's own `claude -p` runs must use `--bare` (research/00) so they don't pollute it.

### 1.5 Claude Code (`~/.claude`)

- `history.jsonl`: 1,378 lines, keys `{display, pastedContents, timestamp(ms), project, sessionId}`, range 2026-08-05 → 2026-11-05, 10 distinct projects, **94 prompts in the last 14 days**. `pastedContents` can contain pasted secrets, so scrub it before any off-machine use.
- `projects/`: 13 project dirs, 18 top-level session `*.jsonl` (380 MB) plus 30 nested (subagents). mtime 2026-09-22 → 2026-11-05, 12 sessions touched in 14 days. Record `type`s: `user, assistant, attachment, ai-title, last-prompt, file-history-snapshot, mode, permission-mode, api-request*, tools-used`. Useful keys: `cwd, gitBranch, sessionId, timestamp, aiTitle, message, toolUseResult, isSidechain, entrypoint`.
- Sensor recipe: tail `history.jsonl` by byte offset to get "what the user asked, where, when". For each active session, read only `ai-title` and `last-prompt` records to get a cheap session title. Never ingest `toolUseResult` blobs.

### 1.6 Codex (`~/.codex`)

| File | Content (schema only) | Rows / range |
|---|---|---|
| `session_index.jsonl` | `{id, thread_name, updated_at}` | 259 · 2026-07-31 → 2026-11-05 |
| `state_5.sqlite` → `threads` | `id, cwd, title, source, thread_source, git_branch, git_sha, git_origin_url, first_user_message, model, tokens_used, created_at_ms, updated_at_ms, archived, project_id, agent_role…` | 388 (user 42 · subagent 119 · guardian_review 227), 22 distinct cwd, **53 updated in 14 d** |
| `state_5.sqlite` → `projects`/`project_roots` | named projects ↔ paths | 5 / 5 |
| `thread_history_1.sqlite` (193 MB) | `thread_turns(status, started_at, duration_ms…)`, `thread_items(item_type, item_json, created_at_ms)` | 1,620 turns · 37,396 items |
| `sessions/**/rollout-*.jsonl` | `session_meta, turn_context, response_item, event_msg(task_started/complete, token_count)` | 36 files, 538 MB, 2026-10-31 → 11-05 |
| `history.jsonl` | `{session_id, ts, text}` | 48, last 2026-09-29 |
| `memories_1.sqlite` | `stage1_outputs(raw_memory, rollout_summary…)`, `jobs` | **0 rows** (memory feature unused) |
| `goals_1.sqlite` | `thread_goals(objective, status, token_budget…)` | **0 rows** |
| `automations/automation/automation.toml` | `kind=heartbeat, rrule=FREQ=HOURLY;INTERVAL=1, status=PAUSED` | 1 |
| `ambient-suggestions/<hash>/ambient-suggestions.json` | `{projectRoot, generatedAtMs, suggestions[{id,title,description,prompt,appIds,status}]}` | 6 projects |

- Best cheap signal: `threads WHERE thread_source='user'`, which gives cwd, title, branch and recency per project.
- `ambient-suggestions` is Codex's own proactive output. Ingest it as candidate tasks, deduplicated.
- **Feedback-loop guard:** exclude threads whose `source='exec'`, whose cwd is under `D:\phd\AgentWorkOS`, or which have a guardian/subagent `thread_source`. Otherwise the agent senses its own runs.

### 1.7 NetEase Mail Master (网易邮箱大师 5.7.4)

- `%LOCALAPPDATA%\Netease\MailMaster\data\`: `app.db` (tables `Account, AppConfig, Signature…`, which hold **credentials, so never open it**), `calendar.db`, `contacts.db` (contacts, do not ingest). `%APPDATA%\Netease` does not exist.
- **Mail store: `D:\MailMasterData\`**, one directory per account: 2 × Tsinghua (`mails.tsinghua.edu.cn`) and 2 × 163. One of each is active; the other is a legacy copy whose last mail is 2023-11. Each holds:
  - `mail.db`: `MailMeta(LocalId, RemoteId, OrigDate, ReceivedDate, Size, HasAttachments, Starred, Unread, Deleted, Replied, Forwarded, Mailbox, Subject, Summary, Froms, Tos, Ccs, MessageId, ReferenceIds, InReplyTos, has_calendar, IsPromotion…)`, `Mailbox`, `MailAttachment`, `CalendarReply`, `AccountConfig` (**do not read**).
  - `content.db`: `MailContent(MailId, ContentType, OrigBody, Charset, TransferEncoding, CompressType)`. CompressType=1 for 3,410 of 3,996 rows (compressed with an undocumented codec).
  - `search.db`: FTS table `Search(Subject, Froms, Tos, Ccs, Bccs, Content, Attachments)`.
- All files are **unencrypted SQLite 3** (header checked). Volumes: active Tsinghua account 2,542 messages (2022-07 → 2026-11-04), **53 in the last 14 days**. Active 163 account 1,148 messages, **19 in the last 14 days**.
- `calendar.db`: `Events` 313 (`Summary, Location, DTStart, DTEnd, Status, RecurrenceID, AllDay…`), `Calendars` 5 (4 CalDAV-type), `Reminders` 38. DTStart range 2020-12 → 2027-05. 0 events within ±14 days, so this is a secondary calendar source.
- Mail Master was **not running** during the probe (last DB write 04:24 local today). Freshness depends on the client being open.
- **Feasibility verdict:** a zero-credential, metadata-only email sensor is feasible today (subject, sender, date, flags, the `Summary` preview, threading IDs). Open a copied snapshot or `immutable=1`, never write, and skip `AccountConfig`, `app.db` and `contacts.db`. Risks: undocumented schema, possible change on client update, bodies compressed. **Phase 2 → IMAP** (robust, live, enables reply-to-approve):

| Provider | IMAP | SMTP | Credential |
|---|---|---|---|
| Tsinghua (student) | `mails.tsinghua.edu.cn:993` SSL | `mails.tsinghua.edu.cn:465` SSL | **客户端专用密码** (webmail → 设置 → 安全设置; 2FA is mandatory, so the login password won't work) |
| 163 / 126 | `imap.163.com:993` / `imap.126.com:993` | `smtp.163.com:465` / `smtp.126.com:465` | 授权码 (enable IMAP/SMTP in web settings). NetEase may require the IMAP `ID` command from non-whitelisted clients. |
| QQ | `imap.qq.com:993` | `smtp.qq.com:465` | 授权码 |
| Gmail | `imap.gmail.com:993` | `smtp.gmail.com:465/587` | App password (2-Step Verification required). Needs the proxy. Already readable via the Codex Gmail connector, so low priority. |

### 1.8 WeChat / WeCom

- Weixin 4.x data at `D:\xwechat_files` (plus legacy `Documents\WeChat Files`), WeCom at `Documents\WXWork`. Existence check only; nothing was opened.
- The DBs are SQLCipher-encrypted. Decrypting them needs the key from process memory, which is a ToS violation, carries account-restriction risk, gets flagged by security tools, and breaks on every client update. It also exposes third parties' private messages. **Recommendation: do not read WeChat/WeCom databases.**
- Alternatives:
  1. Make the WeCom 智能机器人 (or Feishu bot) the **capture inbox**: the user forwards or pastes an important message to the bot, and it becomes a task.
  2. The user forwards key items to email or Notion.
  3. A daily "anything from WeChat?" prompt in the digest.

### 1.9 Windows calendar / Outlook

- `HKCU\Software\Microsoft\Office\16.0\Outlook\Profiles` has 0 profiles and there are no `.ost/.pst` files, so Outlook COM is unusable. **Note:** `OUTLOOK.EXE` (PID 24384) has been running windowless since 05:11 local, probably spawned by an earlier COM probe. It is harmless but can be closed.
- Windows Mail & Calendar package present (5.5 MB LocalState; app retired). New Outlook package is empty. No `.ics` files in Downloads, Documents or Desktop.
- Real calendar sources: **Notion** (primary per research/00), Mail Master `calendar.db` (secondary), Google Calendar (empty).

### 1.10 Local git repositories (depth ≤ 4, `fd -H -I -t d '^\.git$'`)

24 `.git` entries found, all under the four roots. `D:\transfer` has none. No repo showed `[ahead/behind]`.

| Repo | Branch (status -sb) | Dirty files | Last commit |
|---|---|---|---|
| `D:\papers\0. thuthesis` | `revision/thesis-2026-10-18…origin` | 6 | 2026-10-20 |
| `D:\papers\Paper-A on Hydrogen\DataML\opensource` | `main…origin/main` | 8 | 2025-12-19 |
| `D:\papers\Paper-A on Hydrogen\experiments\{climate_regret_v7, weather_record_v8, weather_record_v9}` | — (empty/invalid `.git` dirs) | — | — |
| `D:\papers\climate_energy_supply` | `main…origin/main` | 0 | 2026-10-01 |
| `D:\papers\示例论文B` | `main…origin/main` | 170 | 2026-04-21 |
| `…\示例论文B\claude-scholar` | `main…origin/main` | 0 | 2026-06-05 |
| `…\示例论文B\manuscript\一审` | (empty `.git`; resolves to parent) | — | — |
| `D:\papers\电厂改造` | `self-evolve/review-mar24` (no upstream) | 32 | 2026-03-24 |
| `D:\papers\电厂改造\temp\figures4papers` | `main…origin/main` | 0 | 2026-03-17 |
| `D:\papers\电厂改造claude` | `feature/industrial-sectors…origin` | 20 | **2026-11-03** |
| `D:\phd\AgentWorkOS` | `No commits yet on main` | 4+ | — |
| `D:\phd\AgentWorkOS\external\research\{dsh, dsh-multica-runtime, multica, paperclip, pi, superagent, symphony, vibe-kanban}` | clean clones | 0 | 2026-09-15 … 11-05 |
| `D:\personal\intern\ProjectX\crypto` | `main…origin/main` | 12 | 2024-12-06 |
| `D:\personal\intern\cc\Energy-Model 2.0` | `main…origin/main` | 22 | 2026-07-20 |
| `D:\personal\intern\mobile\MobileWorld` | `main…origin/main` | 0 | 2026-07-02 |

Signals worth surfacing: long-dirty repos (170 files uncommitted since April), a branch with no upstream, and invalid `.git` stubs. The repo sensor must exclude `external/research/*`.

### 1.11 Recently modified files (last 14 days, excluding `.git`, `node_modules`, `__pycache__`, `.venv`, `external`)

- Total **41,540** files, of which 40,143 are `.json` experiment outputs (`Paper-A on Hydrogen\experiments\weather_record_v8` alone has 33,976). Non-JSON: 1,397.
- By extension (non-JSON): `.csv` 406 · `.py` 282 · `.pdf` 263 · `.png` 208 · `.md` 126 · `.svg` 20 · `.sol` 16 · `.log` 13 · `.xlsx` 10 · `.pptx` 8 · `.txt` 7 · `.docx` 3 · `.doc` 1 · `.xls` 1 · `.tex` 1 · `.bib` 1.
- By top-level folder (all / non-JSON): `papers\Paper-A on Hydrogen` 40,875 / 757 · `papers\电厂改造claude` 503 / 481 · `transfer\<senior-labmate-A handover>` 55 · `transfer\<senior-labmate-B work>` 55 · `phd\ConfX` 25 · `transfer\课题组2026论文` 10 · `phd\AgentWorkOS` 8 · `papers\0. thuthesis` 6 / 4 · `phd\综合奖学金申请` 1 · root files 3.
- Implication: the document sensor needs **exclude globs for generated outputs** (`experiments/**`, `*.json`, `*.sol`, `*.log`) and should emit one coalesced "project touched" signal per folder per window, not one per file.

### 1.12 Environment findings that affect the design

1. **Timezone:** the system TZ is `SA Western Standard Time` (UTC-04:00, no DST), but the user lives in China (UTC+8), a 12-hour offset. Store everything in UTC and render and schedule in an explicit `Asia/Shanghai`. Never use naive local time for quiet hours, digests or cron.
2. **Proxy:** domestic push endpoints (`qyapi.weixin.qq.com`, `openws.work.weixin.qq.com`, `open.feishu.cn`, `smtp.163.com`) should go direct. Localhost calls must bypass the proxy. Avoid `Invoke-WebRequest` for `127.0.0.1` because WinINET doesn't bypass it.
3. **Activity rhythm** (1,420 prompt events from Claude history and Codex user threads, converted to Beijing time): 12–18h 39% · 18–24h 36% · 10–12h 10% · 00–02h 9% · 02–06h 4% · 06–10h 1.5%. Weekdays and weekends are about equal. Suggested default **quiet hours 01:30–09:30 CST** for non-urgent pushes, and digests at about 11:00 and 21:30 CST.

---

## 2. Notification / HITL push channels (reachable from China)

**Live test:** a native WinRT toast was shown from Windows PowerShell 5.1 with no module installed. It used AUMID `{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe` with title **"AgentWorkOS test"** and a `Dismiss` system action. Delivery was verified by `ToastNotificationManager.History.GetHistory(appId)`: count = 1, tag `awos-probe` found. BurntToast, `winotify`, `win11toast` and `winsdk` are **not** installed, and none of them is needed.

| Channel | Setup effort | User must provide | Reliability in CN | Two-way? | Notes |
|---|---|---|---|---|---|
| **Windows toast (native WinRT via PowerShell)** | none (tested ✅) | nothing | high, desktop only | partial: `activationType="protocol"` button → opens `http://127.0.0.1/...` confirm page | Only when the PC session is active. Focus Assist can hide banners. A branded AUMID needs a Start-menu shortcut. |
| BurntToast / winotify | `Install-Module` / `pip install` | nothing | same | same | Unnecessary given native XML works |
| **WeCom 群机器人 webhook** | ~5 min: create internal group → add robot | webhook key (secret URL) | high (WeCom push) | ❌ push only | 20 msg/min per bot. Text, markdown, image, file and template cards, but no click callbacks. |
| **WeCom 智能机器人 (API mode, 长连接)** | ~30–60 min; needs WeCom admin (a self-registered org works; verify) | BotID + long-connection Secret | high | ✅ **template-card button → `template_card_event` over outbound WebSocket** (`wss://openws.work.weixin.qq.com`); reply within 5 s; update card via `aibot_respond_update_msg` | No public IP. One active connection per bot. Official Node SDK `@wecom/aibot-node-sdk`; Python via community SDK or raw WS. |
| Feishu 自定义机器人 webhook | ~5 min | webhook URL + sign secret (HMAC-SHA256, ±1 h) | high | ❌ (card buttons can only open URLs) | 100 req/min, 5/s. Avoid sending on the hour. |
| **Feishu 自建应用 bot (长连接)** | ~30–60 min: create app, enable bot, subscribe `card.action.trigger` via long connection, publish version | App ID + App Secret | high | ✅ interactive cards with callbacks over WS (`lark-oapi` `ws.Client`); respond < 3 s | Equivalent to the WeCom AI bot with arguably better card UX. The user must install Feishu on the phone. |
| Server酱 Turbo (sct.ftqq.com) | 2 min (WeChat scan) | SendKey (`SCT…`) | high (WeChat service account) | ❌ | **Free = 5 msgs/day**; paid ≈ ¥3+/mo for 1,000/day |
| PushPlus | 5 min + **real-name verification** (required since 2024-08) | token | medium-high | ❌ | 200/day on the WeChat channel, 5 req/min, same content ≤ 3/h |
| Bark | 2 min | device key | high (APNs) | ❌ (tap opens `url`) | **iOS only**; phone OS unknown |
| ntfy (ntfy.sh or self-hosted) | 10 min | topic (+ token) | **low/uncertain**: FCM unavailable, Android needs a foreground service, OEM battery killers, ntfy.sh reachability | partial (HTTP actions need the phone to reach the server) | Not recommended for CN |
| Email-to-self (SMTP) | 10 min | 163 授权码 or Tsinghua client password | high | ✅ slow: reply `APPROVE <code>` → IMAP poll | Good for digests, audit trail, works anywhere |
| Telegram (claude-mem supports it) | — | bot token | blocked without VPN on phone | ✅ | Not recommended |

**Recommendation**

- **Tier 0 (local, zero credentials, do now):** native WinRT toast plus a localhost dashboard. Toast buttons open the local confirm page.
- **Primary phone channel: WeCom.** WeCom is already installed and is a work messenger, so it fits the use.
  - MVP: a 群机器人 webhook for one-way push. The only credential is the webhook key.
  - Upgrade: a **智能机器人 over long connection** for one-tap approvals via template-card buttons. It needs no inbound port and works off-campus.
  - If WeCom admin creation is blocked, use **Feishu 自建应用 + 长连接** instead. It has the same architecture.
- **Fallback: SMTP email-to-self** (163 授权码 or Tsinghua 客户端专用密码). It carries the daily digest, catches pushes when IM fails, and supports reply-code approvals through IMAP.
- **Avoid:** Server酱 free tier (5/day), PushPlus (real-name requirement plus quotas), ntfy and Telegram (CN reachability). Bark only if the phone is an iPhone.
- **Privacy rule for every cloud channel:** push a **title plus a redacted one-line summary** only. Full context stays on the local dashboard.

---

## 3. Approval UX patterns → AgentWorkOS Outbox design

### 3.1 Survey

| System | Mechanism | Ideas worth stealing |
|---|---|---|
| LangGraph / LangChain v1 | `interrupt(value)` in a node plus a checkpointer and `thread_id`. Resume with `Command(resume=…)`. `HumanInTheLoopMiddleware(interrupt_on={tool: {allowed_decisions:[approve, edit, reject, respond], when: predicate}})`. Agent Inbox renders `HumanInterrupt{action_request{action,args}, config{allow_accept,allow_edit,allow_respond,allow_ignore}, description}`. | Per-action **allowed_decisions**. "Edit" replaces the args, and the model is told. Disallow edit on destructive tools. Durable pause and resume. |
| Paperclip (`external/research/paperclip`) | `approvals(type, status: pending/revision_requested/approved/rejected/cancelled, payload jsonb, decision_note, decided_by, decided_at)`. Issue-thread interactions: `request_confirmation`, `request_checkbox_confirmation`, `request_item_verdicts`, `ask_user_questions`, `suggest_tasks`, `decision`, with `resolverPolicy` (anyone/human_only/not_creator), `continuationPolicy` (wake_assignee…), **target binding → `stale_target` expiry**, `supersedeOnUserComment`, **deterministic `idempotencyKey`**, and withdraw. | "A card is a coordination record, **not a grant of authority**": execution re-checks policy. Staleness on target revision. Idempotency. `revision_requested` as a first-class outcome. |
| Multica (`external/research/multica`) | `inbox_item(recipient, type, severity: action_required/attention/info, issue_id, title, body, details jsonb, actor, read, archived, section: action/activity)`. | Separate an **action section** from an **activity feed**. A severity triage that controls routing. |
| HumanLayer | `@hl.require_approval()` decorator on tools. The call is held until a human approves or denies with a comment via Slack/email. `human_as_tool()` for free-form questions. | Approval as a **decorator at the tool boundary**. Channel-agnostic contact routing. A denial comment is fed back to the agent. |
| OpenAI Agents SDK (≥ 0.8) | `needs_approval=True | async fn` on tools → `result.interruptions` (`ToolApprovalItem`) → `state = result.to_state(); state.approve(i)` or `reject(i, rejection_message=…)` → `Runner.run(agent, state)`. `RunState.to_json()/from_json()` persists pauses. `always_approve` gives sticky decisions. | Serializable paused state. **Sticky vs per-call** approvals. Warning: snapshots are not authenticated, so the server must bind the decision to an identity and a payload. |
| GitHub Copilot coding agent | Works on `copilot/*` branches and opens a **draft PR**. A human reviews and requests changes via comments. Actions workflows need "Approve and run". | **Propose as a diff/draft, never act directly.** Iterating through review comments counts as a decision type. |

### 3.2 Outbox item (maps onto the existing `approvals` table in `src/awos/schema.sql`)

```jsonc
{
  "id": "apr_01J…", "idempotency_key": "reply:mail:<msgid>:v1",    // dedupe retries / re-sensing
  "kind": "action",                    // action | decision | review | permission | budget
  "action_type": "send_email",         // existing enum
  "title": "Reply to reviewer re: revision deadline",            // ≤ 60 chars, push-safe
  "summary_push": "Draft reply ready; deadline in 2 days",       // redacted 1-liner for cloud channels
  "context_md": "…why now, evidence with source refs (mail:<id>, notion:<page>, repo:<path>)…", // local only
  "recommendation": {"option": "approve", "rationale": "…", "confidence": 0.8},
  "options": [{"key":"approve","label":"Send"},{"key":"edit","label":"Edit first"},
              {"key":"snooze","label":"Remind 18:00"},{"key":"reject","label":"Don't send"}],
  "allowed_decisions": ["approve","edit","reject","respond","snooze"],
  "risk": {"level":"medium","reversible":false,"external_visibility":"recipient","cost_usd":0},
  "payload": {"…exact thing to execute…"}, "payload_hash": "sha256:…",
  "target_ref": "mail:<thread>", "target_version": "<last msg id/etag>",   // staleness check
  "priority": "P1", "expires_at": "…Z", "default_on_expiry": null,   // null = just expire
  "remind_at": "…Z", "reminder_count": 0,
  "sensitivity": "private",            // public | internal | private | secret
  "channel_refs": [{"channel":"wecom_aibot","msg_id":"…","sent_at":"…"}],
  "status": "pending", "decision": null, "decision_note": null, "edited_payload": null,
  "decided_via": null, "decided_by": null, "token_hash": "…", "token_used_at": null
}
```

Suggested deltas to `approvals`: add `idempotency_key UNIQUE`, `summary_push`, `payload_hash`, `target_ref`, `target_version`, `allowed_decisions`, `reversible`, `sensitivity`, `priority`, `remind_at`, `reminder_count`, `channel_refs`, `edited_payload`, `token_used_at`, `superseded_by`. Extend `status` with `snoozed`, `revision_requested`, `stale`, `superseded`.

### 3.3 Lifecycle

```
draft ──(policy says needs human)──► pending ──notify──► (notified_at set)
  │                                    │  ├─ snooze ─► snoozed ──remind_at──► pending
  │ (policy auto-approves low-risk)    │  ├─ approve / edit ─► approved ─► executing ─► executed | failed
  └──────────────► approved            │  ├─ reject ─► rejected            (failed → new Outbox item "retry?")
                                       │  ├─ respond / request changes ─► revision_requested ─► agent re-plans ─► new item (supersedes)
                                       │  ├─ target_version changed ─► stale   (re-derive, re-ask)
                                       │  ├─ newer item same idempotency scope ─► superseded
                                       │  └─ expires_at ─► expired (apply default_on_expiry ONLY if allowed below)
```

**Default-on-expiry policy:** auto-apply only when `risk.level = low` **and** `reversible = true` **and** `external_visibility = none` (for example, filing a local todo or archiving a noise signal). Never auto-apply sends, posts, pushes, deletes, spend or calendar invites. Those simply expire, and the item is listed in the next digest.

**Execution guard (from Paperclip):** an approval is not authority. At execution time the executor re-checks that `payload_hash` matches, that `target_version` is unchanged, that policy still permits the action, and that the approval has not been used. It then logs to `events`.

**Routing:**
- P0 (decision needed < 24 h, or blocking): immediate phone push plus toast, quiet hours ignored.
- P1: batched hourly outside quiet hours.
- P2: digest only.
- Cap phone pushes at about 8 per day. Coalesce with the same `idempotency_key`.

### 3.4 One-click approve from the phone, safely, for a localhost-only service

**Preferred path: IM card callback (no inbound exposure).**
- The service keeps an **outbound** WebSocket to WeCom 智能机器人 or Feishu.
- Card buttons carry `event_key = awos:<approval_id>:<option>:<nonce>`. The nonce is 128-bit random and single-use, stored hashed.
- On callback:
  1. Verify the sender ID equals the configured owner (WeCom `userid` / Feishu `open_id`) and reject anyone else.
  2. Verify the nonce is unused and unexpired, and that the item is still `pending`.
  3. Answer within 3–5 s with a card update ("Approved ✓ — executing").
  4. Execute asynchronously, then update the card again with the result.
- A second message confirms every decision, so tampering is visible.

**Link path (toast button, email, or phone on the same LAN):**
- Token = `v1.<approval_id>.<option>.<exp>.<nonce>.<HMAC-SHA256(server_secret, all fields ‖ payload_hash)>`.
  - The secret lives in `config/secrets*` (git-ignored) and never leaves the machine.
  - Only `sha256(token)` is stored, the token is single-use, and `exp ≤ min(expires_at, 12 h)`.
- **GET never mutates.** WeCom, Feishu and WeChat pre-fetch URLs for previews, and a GET-approve would auto-approve. GET renders a confirm page with the title, risk, recommendation and a payload diff. A **POST** (with the token and a CSRF nonce from the page) commits.
- High-risk or irreversible actions additionally require a 6-digit local PIN (hashed, with rate limiting and lockout), or are dashboard-only. "Edit" always opens the editor and never commits in one click.
- Binding: by default the server listens on `127.0.0.1` only. Optional LAN mode binds to the LAN IP only when the Windows network profile is *Private*, with a firewall rule scoped to `LocalSubnet`. Campus Wi-Fi often isolates clients, so don't depend on it.
- No public tunnels by default. If ever needed, use Tailscale (DERP relay is slow in CN) rather than an open tunnel, and keep the PIN requirement.
- **Email reply path:** the subject contains `[AWOS #<short-id>]` and the body asks for `APPROVE <6-char code>`. The IMAP poller accepts only replies from the owner's own address whose `In-Reply-To` matches, then checks the code (HMAC-derived, single-use). Treat it as medium-assurance and disallow it for high-risk items.

---

## 4. Personal context model

### 4.1 Structure (stored as `context_facts` rows plus a rendered `context.md` snapshot)

| Layer (`kind`) | Fields | Example shape (not real data) | Primary sources | Cadence |
|---|---|---|---|---|
| **Identity & roles** (`role`) | title, org, stage, responsibilities[], active_since | "PhD Y5, energy-systems modelling; thesis revision owner; handover recipient for 2 labmates' work" | user-confirmed seed, folder names, mail signatures (metadata), Codex/Claude cwd | quarterly, user-pinned |
| **Long-term goals** (`goal`) | goal, horizon, milestones[{name, due, status}], linked_projects | graduation/thesis, publications, job search, scholarship | Notion (plans), thesis repo branch names, folder names, user | monthly review |
| **Active projects** (`project`) | name, aliases (paths, repo, Zotero collection, Notion page, Codex project_id, cwd), status, next_actions[], blockers[], last_activity, health (dirty files, days since commit), collaborators(role refs) | `电厂改造claude`: last commit 2026-11-03, 20 dirty, branch `feature/industrial-sectors` | git sensor, recent-files sensor, Codex threads, Claude history, claude-mem `next_steps`, Zotero collections | event-driven (per sensor run) plus daily consolidation |
| **Commitments & deadlines** (`deadline`) | what, due (CST), source_ref, owner, confidence, status | "submit revision by …" | Mail Master metadata/IMAP, Notion to-dos and calendar, Mail Master `calendar.db` | each sensor run; re-confirm 72 h / 24 h before due |
| **People** (`person`) | pseudonymous id, role (advisor, co-author, labmate, reviewer, recruiter), org, channel, projects[], last_interaction, response_expectation | `person:7f3a` = "co-author, project X, replies via email" | mail From/To headers (hashed), Notion users, git authors | weekly; **names and addresses stored locally only** |
| **Interests & tech stack** (`interest`) | topic/tool, strength, evidence_count, last_seen | VEDA/TIMES, power-system flexibility, hydrogen-climate, LaTeX/thuthesis, Python/uv, agents | Zotero collections, tags and annotations; file extensions; repos; prompts (topic-classified) | weekly decay-weighted recompute |
| **Habits & preferences** (`habit`, `preference`) | active hours (CST), quiet hours, digest times, language (zh/en), notification caps, auto-approve rules, tone for drafts, preferred tools per task | "active 12:00–01:30 CST; quiet 01:30–09:30; zh for push, en for papers" | timestamp histograms (Claude, Codex, git), explicit user settings, approval history (approve/reject rates per action_type) | weekly; preferences user-pinned |

Every fact carries `source` (list of `source_ref`s, not content), `confidence`, `status` (`proposed → active → stale/rejected`), `pinned`, and the proposed additions **`sensitivity`**, `first_seen`, `last_seen`, `ttl_days`/`decay`, and `evidence_count`. User-pinned facts are never overwritten. Agents can only *propose* changes, and these appear as a weekly `review` Outbox item: "5 context updates: accept all / review".

### 4.2 Update pipeline

1. **Sensor pass** (every 5–30 min per source, cursor in `sensor_state`): rule-based extraction only, with no LLM. Updates `last_activity`, dirty counts, new mail metadata counts and new Zotero items, and emits `signals`.
2. **Daily consolidation** (~22:00 CST, cheap model, `claude -p --bare` or `codex exec --ephemeral` with redacted input): signals → proposed fact diffs plus candidate tasks and deadlines.
3. **Weekly review** (Sunday digest): the user accepts or rejects proposed facts, stale projects are flagged, and goal and milestone status is refreshed.
4. **On-demand:** before any executor run, render a **task-scoped context slice** (only the facts linked to that project, at most about 1–2 k tokens) instead of the whole profile.

### 4.3 Privacy guards

- **Local-only storage:** SQLite under `var/` (git-ignored, along with `*.db`). Raw source content is **not persisted**. Store pointers (`mail:<local_id>`, `zotero:<key>`, `file:<path>#sha256`) plus derived facts. Re-read the source on demand.
- **Never leaves the machine:** credentials and tokens (`auth.json`, `settings.json` keys, Mail Master `AccountConfig`/`app.db`, Notion `microsoft_oauth`), raw mail bodies, contacts DBs, any WeChat/WeCom data, other people's email addresses and phone numbers, `pastedContents` from Claude history, and anything tagged `secret`.
- **Redaction before any LLM or cloud call:**
  - a regex and entropy scanner for keys, tokens and passwords (block the call if one is found);
  - emails, phones and ID numbers → placeholders;
  - person names → `person:<hash>` aliases, with the mapping kept local.
  - Note that the Claude gateway in research/00 is a third-party endpoint, so treat it as off-machine.
- **Channel minimization:** cloud push channels get `title` plus `summary_push` only (enforced by `sensitivity`). Full `context_md` is viewable only on the localhost dashboard.
- **Egress audit:** every off-machine call is logged to `events` (destination, byte count, sensitivity, redaction hits). The weekly digest reports "what left the machine".
- **User controls:** `forget <source|project|person>` deletes facts and pointers. Per-source enable flags. `ttl_days` defaults (for example, people 180 d, deadlines 30 d after due). A source allow-list, with WeChat/WeCom DBs hard-denied in code.
- **Self-loop exclusion:** sensors drop AgentWorkOS's own Codex and Claude sessions (by cwd and source), and AgentWorkOS runs `claude` with `--bare` so it doesn't write into claude-mem.

---

## 5. Recommended sensor order (by value per effort)

1. Codex threads, Claude history, git repos and recent files: zero credentials, live, best "what am I working on" signal.
2. Zotero local API (`since=` cursor).
3. Mail Master metadata (snapshot, read-only), then IMAP once the user provides the client password or 授权码.
4. Notion via the Codex connector: to-dos, schedule, plans.
5. claude-mem: one-off historical backfill of `session_summaries` into `context_facts(proposed)`.
6. Push: toast now, WeCom webhook next, WeCom 智能机器人 or Feishu app for two-way, SMTP fallback.

## Sources

- Feishu long-connection callbacks: <https://open.feishu.cn/document/event-subscription-guide/callback-subscription/receive-and-handle-callbacks?lang=zh-CN>, <https://open.feishu.cn/document/feishu-cards/card-callback-communication?lang=zh-CN>, <https://feishu.apifox.cn/doc-7518469>
- Feishu custom bot limits and signing: <https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot?lang=zh-CN>, <https://open.feishu.cn/document/faq/breaking-change/webhook-v2-robot-exceeds-frequency-limit-management?lang=zh-CN>
- WeCom 智能机器人 long connection and card events: <https://developer.work.weixin.qq.com/document/path/101833>, <https://developer.work.weixin.qq.com/document/path/101463>, <https://developer.work.weixin.qq.com/document/path/101027>, <https://developer.work.weixin.qq.com/document/path/101839>
- WeCom group-bot rate limit: <https://developer.work.weixin.qq.com/community/question/detail?content_id=16547899322417682326>, <https://cloud.tencent.com/document/product/248/50413>
- Server酱 quotas: <https://sct.ftqq.com/docs/getting-started/faq/>, <https://sct.ftqq.com/compare/>
- PushPlus quotas and real-name rule: <https://pushplus.plus/use.html>, <https://www.pushplus.plus/doc/help/limit.html>
- ntfy without FCM: <https://docs.ntfy.sh/privacy/>
- Tsinghua mail client settings: <https://mail.tsinghua.edu.cn/coremail/help/notice.html>, <https://mail.tsinghua.edu.cn/coremail/help/clientoption_zh_CN.jsp>
- OpenAI Agents SDK HITL: <https://openai.github.io/openai-agents-python/human_in_the_loop/>
- LangChain HITL middleware: <https://docs.langchain.com/oss/python/langchain/human-in-the-loop>
- Paperclip / Multica: local clones in `external/research/paperclip` (`packages/db/src/schema/approvals.ts`, `packages/shared/src/constants.ts`, `skills/paperclip/SKILL.md`) and `external/research/multica` (`server/migrations/001_init.up.sql`, `607_inbox_item_section.up.sql`)
