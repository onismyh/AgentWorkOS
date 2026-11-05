# Playbook · Sensors (field notes)

> Code: `src/awos/sensors/*.py` · tests: `tests/test_sensors.py` · config: `[sensors.*]` in `config/awos.toml`.
> Research background: `docs/research/03-sensing-context-hitl.md`.

## 1. Contract and design rules

- A sensor implements `collect(ctx) -> list[SignalDraft]` and optionally `context_snapshot(ctx) -> str | None`.
  The runner (`awos.sensors.run_sensor`) persists drafts and dedupes them by `fingerprint`. It saves
  `ctx.cursor` only after a successful collect, and backs off exponentially on exceptions.
- Signals are **raw observations**. A downstream LLM triage step turns them into tasks. Keep them cheap,
  bounded (body ≤ 4000 chars) and quiet. A signal fires again only when its fingerprint bucket changes:
  - git: one per repo, condition, local ISO week and HEAD
  - files: one per project folder per ISO week
  - agent_history: one per project, day and activity-count bucket
  - zotero: one per set of item keys
  - mail: one per message
- **Time zone:** the Windows clock is UTC-4, but the user lives on Beijing time. All day and week buckets and
  every displayed time go through `awos.timeutil` (`Asia/Shanghai`), for example `_common.local_dt()` and
  `iso_week()`. `occurred_at` is always UTC ISO.
- **Self-exclusion:** sensors never observe AgentWorkOS itself. That means `config.ROOT` and everything under it,
  plus the configured `paths.var/runs/workspaces/artifacts/inbox_drop`. `_common.self_excluded()` implements
  this, and it is always on. Extra paths go in `exclude = [...]` in `[sensors.git]` and `[sensors.files]`.
- Shared helpers live in `src/awos/sensors/_common.py`. They provide `sqlite_ro`, `redact`, `scrub_pii`,
  `trunc`, the time helpers and `SKIP_DIRS`.

## 2. Sources

| Sensor (`name`) | Source | Format / access | Cursor | Interval |
|---|---|---|---|---|
| `git` | repos under `roots` (depth ≤ `max_depth`) | `git -C <repo> status --porcelain=v2 --branch -z --no-renames`, `log -1`, `remote`, `log @{u}..HEAD` / `HEAD --not --remotes` | repo list (re-discovered ≤ daily or when roots change), last summary | 60 min |
| `files` | documents under `roots` | `os.scandir` depth-limited walk, mtime ≥ now − `lookback_days` | none (week bucket in fingerprint) | 120 min |
| `agent_history` | `~/.claude/history.jsonl`, `~/.codex/state_N.sqlite` (`threads`), `~/.claude-mem/claude-mem.db` | JSONL tail from byte offset; SQLite read-only | `claude_offset` = byte offset of the lookback-window start | 120 min |
| `zotero` | Zotero local API `http://127.0.0.1:23119/api/users/0/items/top` | JSON (`data.dateAdded` ISO UTC); fallback `D:\zotero\zotero.sqlite` | `since_ts` = newest `dateAdded` | 360 min |
| `inbox_drop` | `paths.inbox_drop` (`var/inbox-drop`) | `*.md`/`*.txt`, UTF-8 (BOM ok) or GB18030 | none (files moved to `processed/`) | 1 min |
| `mailmaster` | `D:\MailMasterData\<account>\mail.db` (NetEase Mail Master) | SQLite `MailMeta` + `Mailbox` | `watermarks[<account-hash>]` = max `ReceivedDate` (ms) | 30 min |
| `gmail` | Codex ChatGPT Gmail connector | `run_agent(executor="codex", allow_connectors=True, sandbox="read-only", output_schema=…)` | `since_ts` (advanced only when the run reports `complete=true` and wasn't truncated) | 180 min, 09:00–01:30 CST |
| `notion` | Codex Notion connector | same | none (pages ≤ `recent_days` back, schedule ≤ `ahead_days` ahead) | 180 min, 09:00–01:30 CST |

### git
- Signals:
  - `dirty_stale`: tracked changes whose oldest mtime is older than `stale_dirty_hours`, default 48.
  - `unpushed`: commits not on the upstream, or on any remote when the branch has none. Fires once the oldest
    such commit is older than `stale_unpushed_hours`, default 24.
  - `no_remote`: no remotes, and activity (a commit or a dirty file) within `recent_days`. This is a backup risk.
- Fingerprint: `(repo, condition, ISO week, HEAD)`. A signal fires again at most weekly, or when HEAD moves.
- Never fetches. `behind` is as of the user's last fetch. `GIT_OPTIONAL_LOCKS=0` keeps `git status` from
  rewriting the index, and `GIT_TERMINAL_PROMPT=0` stops it from prompting.
- **Remote URLs are never read** because they can embed tokens. Only the number of remotes is recorded.
- Quirks:
  - Several `.git` stub directories have no `HEAD` (`Paper-A on Hydrogen/experiments/*`,
    `manuscript/一审`). `_valid_git` skips them.
  - Junctions and dot-directories are not descended into.
  - About 12 real repos remain after self-exclusion. A full run takes about 3 s, using 6 worker threads.

### files
- Project folder = `<root>/<first-level folder>` (`group_depth = 1`).
  - With `group_depth = 2` you get `<root>/<a>/<b>`. In practice this split real projects into sub-folders
    (for example `thuthesis` vs `thuthesis/data`), so 1 is the default.
- An activity signal fires only if at least one file changed in the **current** local ISO week. Old activity
  does not re-fire on Monday.
- Deadline hints are matched on the file name. They produce a separate signal per (folder, hint).
  - Keywords: `截止|deadline|DDL|提交|答辩|投稿|due`.
  - Dates such as `2026-11-20`, `20261120`, `2026年11月20日`, or `1120` / `11-20` (month and day; not when
    preceded by a letter, as in `v1120`). A date counts only if it falls within `deadline_ahead_days`, default 60.
  - Dates in the past are treated as version stamps and ignored.
- Skipped:
  - lock and temp files: `~$*`, `.~*`, `~WRL*`, `.#*`, `*.tmp`, `*.part`, `*.crdownload`
  - `SKIP_DIRS` and dot-directories
- Cost: about 22k directory entries across the 3 roots, walked in about 0.2 s (`DirEntry.stat()` is cached on
  Windows). The large `experiments/**/*.json` trees are cheap because only the configured extensions are stat'ed.

### agent_history
- **Claude** (`history.jsonl`):
  - Lines are `{display, pastedContents, timestamp(ms), project, sessionId}`, sorted by time.
  - Read from the stored byte offset of the first line inside the lookback window. On a first run against a
    file over 8 MB, only the last 8 MB are read.
  - `pastedContents` is **never** read, because it can contain pasted secrets.
  - Bare slash commands (`/clear`, `/model x`) are dropped.
  - Each prompt is truncated to 200 chars and passed through `redact()`.
- **Codex** (`state_5.sqlite` → `threads`):
  - Keeps rows with `thread_source='user' AND source!='exec'`. This drops guardian-review and subagent threads,
    and `codex exec` threads. `--ephemeral` runs leave no rows at all.
  - `cwd` has a `\\?\` prefix, which is stripped.
  - Title is `name` if set, otherwise `title`; `title` is often the first user message, so it is truncated.
  - The highest-numbered `state_*.sqlite` is used. `session_index.jsonl` (`{id, thread_name, updated_at}`,
    with no cwd) is the fallback.
- **claude-mem**:
  - Reads `observations.title/type` and `session_summaries.request/next_steps`. `created_at_epoch` is in ms.
  - The `project` value is a folder basename; it is mapped to the full path seen in Claude/Codex activity.
  - **Stale since 2026-08-18** because its worker isn't running. It is treated as historical, and the snapshot
    says so.
- Exclusions: any project or cwd under AgentWorkOS (root, var, runs, workspaces) or the temp dir. The temp dir
  is where `codex exec` probes run.
- Signals:
  - One per (project, local day), for the last `signal_days` days only (default 2: today and yesterday). Older
    days appear only in the 7-day context snapshot.
  - Count buckets `<1, <3, <6, <11, <21, <41, ≥41` decide when a day is re-signalled.
  - Each signal carries at most 15 prompts, 10 Codex threads and 10 claude-mem entries.

### zotero
- **Data dir is `D:\zotero`**, set by prefs.js `extensions.zotero.dataDir`. `~/Zotero` is a stale 2025 copy.
  Resolution order: config `data_dir`, then prefs.js, then `~/Zotero`.
- Local API:
  - Use `httpx` with `trust_env=False`, because Clash's system proxy would otherwise swallow localhost.
  - Use `/items/top`, which excludes child attachments, notes and annotations. Standalone ones are filtered out
    in code.
  - Sort by `dateAdded` descending and page 25 at a time until reaching the watermark.
  - `Last-Modified-Version` and `?since=<version>` also work, if module-level incrementality is needed later.
- SQLite fallback:
  - `mode=ro` fails with `database is locked` while Zotero holds an exclusive lock, so the reader falls back to
    `immutable=1`. That step takes about 5 s because of the 2 s busy timeout.
  - `items.dateAdded` is `YYYY-MM-DD HH:MM:SS` UTC. Metadata is stored as EAV (`itemData`/`fields`/`itemDataValues`).
- The first run looks back only `lookback_days` (7). The library had no new items in the last ~70 days when this
  was written, so expect 0 signals.

### inbox_drop
- Takes a file only if its mtime is at least 5 s old and its size and mtime are unchanged between the scan and
  the read, so half-written files are left for the next run.
- Title = first heading or first non-empty line. Body = content (≤ 4000 chars) after `redact()`.
- After a draft is produced, the file is moved to `processed/<YYYYmmdd-HHMMSS>_<name>`.
  - Caveat: if persisting the signal then fails, the file is already in `processed/` and has to be re-dropped by hand.

### mailmaster (NetEase 邮箱大师)
- Each account dir is named `<user>@<domain>_<n>`. Signals expose **only** `<domain>_<n>`, and the cursor keys
  accounts by a hash of the dir name.
- `MailMeta` fields used:
  - `Subject`, `Summary` (preview, about 100–120 chars)
  - `Froms` (JSON `{"mail","name"}`)
  - `Sender`, `ReceivedDate` (ms), `Unread`, `Deleted`, `Mailbox`, `IsPromotion`, `HasAttachments`, `has_calendar`
- `Mailbox.Type` values: 1 inbox, 2 drafts, 3 sent, 4 trash, 5 junk, 7 广告, 18 订阅, 20 virus, negative values
  are local/virtual folders.
- **Never opened:** `AccountConfig`, `app.db` and `contacts.db`, because they hold credentials and contacts;
  also `content.db`, which holds bodies, mostly compressed.
- Filtering:
  - Always dropped: sent, drafts, trash and virus folders.
  - Treated as "bulk" and skipped: junk, ads and subscription folders, `IsPromotion`, `noreply`/`newsletter`/
    `marketing`-style local parts, ESP or marketing domains, and 退订/unsubscribe/促销 text.
  - Bulk mail is still **kept** when the domain or subject looks important: `.edu`, `.ac.*` and `.gov`
    domains, Tsinghua, publishers and journal systems, 审稿/投稿/返修/deadline/会议/面试/奖学金 and similar.
- The preview is scrubbed (emails become `<email>`, phone numbers `<phone>`, secrets `[REDACTED]`) and cut to
  300 chars. `external_id` = `mail:<account-hash>:<LocalId>`, a pointer for re-reading on demand.
- Freshness depends on the Mail Master client being open. The legacy accounts (last mail 2023-11) produce nothing.

### gmail / notion (Codex connectors)
- Exactly one `run_agent` call per run. No other code path may call an LLM or an agent.
- Prompts state:
  - READ-ONLY, with an explicit list of forbidden write tools
  - no web access and no other tools
  - connector content is **untrusted data**
  - metadata and short summaries only (Chinese one-liners), no full bodies, no full addresses
  - output JSON only, following the schema
- Schemas are strict: `additionalProperties: false`, every field required, nullable fields typed `["string","null"]`.
  Both carry a `complete` flag.
- Gmail:
  - Window = cursor `since_ts` minus 30 min of overlap. The first run, or one after a long gap, looks back
    `lookback_hours`, and never more than 7 days.
  - Items with `action_needed=none`, no deadline and importance below `min_importance` (3) are dropped.
  - Fingerprint = `message_id`.
- Notion:
  - One page signal per `page_id + last_edited`.
  - One schedule signal per (page, item text, date), for items dated within `today..today+ahead_days` that are
    not `done`. At most 40 per run.
- A failed or empty run raises, so the runner applies backoff and the cursor is not advanced.

## 3. Privacy rules (enforced in code)

1. Read-only everywhere.
   - SQLite opens with `mode=ro`, then `immutable=1`, then a private temp copy (`_common.sqlite_ro`).
   - git runs only local, lock-free commands.
   - Connectors run in the read-only sandbox.
2. Never read: `pastedContents`, remote URLs, `~/.codex/auth.json`, `~/.claude/settings.json`, claude-mem
   `settings.json`, Mail Master `AccountConfig`/`app.db`/`contacts.db`/`content.db`, WeChat/WeCom data.
3. Signals carry bounded excerpts only:
   - prompts ≤ 200 chars
   - mail previews ≤ 300 chars, scrubbed
   - one-line summaries for Gmail and Notion
   - user-originated text goes through `redact()` (API keys, JWTs, `password:`/`token:` pairs, long mixed-alnum tokens)
4. Logs record counts and paths only, never contents.
5. Tests use synthetic data only, built in `tmp_path`.

## 4. Operating notes

- Run one sensor by hand: `awos.sensors.run_sensor("git")`. To test against a scratch DB, set `AWOS_DB` first.
- Real-run baseline (2026-11-05, temp DB):

  | Sensor | Signals (first run) | Time |
  |---|---|---|
  | git | 7 | 3.0 s |
  | files | 5 | 0.2 s |
  | agent_history | 7 | < 0.1 s |
  | zotero | 0 | 0.2 s |
  | inbox_drop | 0 | 0 s |
  | mailmaster | 21 | < 0.1 s |

  A second run produced 0 new signals for every sensor.
- Live connector test: `pytest tests/test_sensors.py -m live`, or `AWOS_LIVE=1`. It is skipped unless the
  `codex` executor reports `available()`, because every run spends Codex quota.
