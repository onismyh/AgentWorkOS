# 02 · Task center: Multica deep-dive and alternatives

> Date: 2026-11-05 · Author: research agent · Status: recommendation for ADR-001
> Cloned read-only (never built or executed) into `external/research/`: `multica` @ `f230f9f` (2026-11-05), `paperclip` @ `beb7338` (2026-11-05), `vibe-kanban` @ `08064e0` (2026-11-03), `symphony` @ `be10a1b` (2026-09-15).
> Paths below such as `multica:server/pkg/db/queries/agent.sql` are relative to `external/research/`. **UNVERIFIED** marks a claim I did not confirm from source or a primary page.

## 中文摘要

Multica 的设计质量很高：issue 和 agent 执行队列（`agent_task_queue`）分开存储；daemon 通过 WebSocket 唤醒，同时每 30 秒轮询兜底，再加 15 秒心跳、15 秒“准备租约”、启动时回收孤儿任务、按失败原因分类重试、按 `(issue, agent)` 串行执行；每次运行的消息、工具调用和 token/成本都会记录。但它是一个面向团队的 SaaS 级系统：约 40 万行 Go、638 个迁移、Postgres 17 + Next.js 16 + pnpm，自托管路径只有 Docker 一种。在 Windows 上不用 Docker 也能跑：原生 Postgres + Go 构建 + Next 构建大约要 1 天，但没有官方支持，而且每次升级都要重新构建、重新迁移。更关键的是，它在执行层**对所有工具调用自动放行**：Claude 用 `bypassPermissions`，Codex 的 requestApproval 一律 accept。它没有 L1–L5 分级，也没有“对外动作 Outbox”。Paperclip（MIT，约 11 万星）的治理模型最接近我们要的：approvals、工具调用审批绑定参数哈希、预算策略与熔断、wakeup 合并和幂等键。但它体量更大（约 63 万行 TS），embedded-postgres 在 Windows 上有多个已知 bug。Vibe Kanban 已宣布停止维护，Symphony 只是规范加 Elixir 参考实现，Backlog.md 和 Task Master 只是文件型任务清单，OpenClaw 和 Hermes 是个人助理网关，不是任务中心。**结论：不整体采用任何一个。自建轻量核心（Python 3.12 + SQLite WAL + FastAPI + 静态 Dashboard，即现有 `src/awos` 骨架），照搬 Multica 的执行队列、租约和恢复语义，Paperclip 的审批、预算和幂等对象，Symphony 的编排状态和退避公式，以及 Vibe Kanban 的 Claude 工具级审批协议。** 第 6 节给出精简 schema 和三套状态机（task / run / approval），第 7 节列出值得复制的模式。

---

## 1. Requirements recap (what we evaluate against)

R1 Inbox → classified tasks with autonomy L1–L5 · R2 planner → subtask DAG · R3 parallel execution by `codex` / `claude` / `pi` / `dsh` CLIs · R4 HITL **Outbox**: approvals for external comms, publishing and sensitive ops · R5 artifacts · R6 durable state, crash recovery, retries · R7 permission isolation · R8 cost/budget control · R9 dashboard with logs, tool-call records, retry/intervene/history · R10 background service on **one Windows 10 PC, no Docker**, simple and portable.

---

## 2. Multica deep-dive

### 2.1 Facts

| Item | Value |
|---|---|
| Repo / stars | `multica-ai/multica`, about 55k stars and 7.2k forks (GitHub org page, Nov 2026, via web search) |
| License | "Multica License" = Apache-2.0 **plus** conditions: no hosted/embedded commercial resale without a licence, and branding requirements (`multica:LICENSE`, `NOTICE`). Fine for personal use |
| Stack | Go 1.26.9 server (chi, pgx/v5, sqlc, gorilla/websocket, robfig/cron) · Next.js 16 web · Electron desktop · PostgreSQL 17 (`pgcrypto`, `pg_trgm`; `pg_bigm`/`pg_cron` optional) · Redis optional (rate limit / relay) |
| Size | about 397k lines of non-test Go under `server/`; **638** up-migrations; `internal/daemon/daemon.go` 11.1k lines; `internal/service/task.go` 8.4k lines |
| Self-host | Official path is `docker-compose.selfhost.yml` / `scripts/install.ps1` (pulls GHCR images, "Docker and Docker Compose must be installed", `SELF_HOSTING.md`). A manual path exists (`SELF_HOSTING_ADVANCED.md` §"Manual Setup (Without Docker Compose)"). |
| Telemetry | On by default: daily anonymous snapshot to `telemetry.multica.ai`. Set `DO_NOT_TRACK=1` to disable (`SELF_HOSTING.md`) |

### 2.2 Data model (final shape from `server/pkg/db/generated/models.go`, sqlc over `server/migrations/`)

| Table | Key columns (abridged) | Notes |
|---|---|---|
| `issue` | `id, workspace_id, number, title, description, status, priority, assignee_type(member/agent/squad), assignee_id, creator_type/id, parent_issue_id, project_id, acceptance_criteria jsonb, context_refs jsonb, origin_type/id, first_executed_at, properties jsonb, revision, last_activity_at, triage_state('pending'), duplicate_of_issue_id` | This is the human-visible work item. It has **no lease fields**; execution state lives elsewhere |
| `issue_dependency` | `issue_id, depends_on_issue_id, type` | DAG edges |
| `issue_status` / `issue_workflow` | per-workspace catalog `key, category(unstarted/started/done/closed), is_system`; workflow `steps jsonb` (status → handler agent/squad/member/project_lead + step instructions) | Entering a status can hand the issue to an agent with step instructions (`internal/issueworkflow/issueworkflow.go`) |
| `agent` | `runtime_id, runtime_mode(local/cloud), instructions, model, thinking_level, custom_args, custom_env, mcp_config, max_concurrent_tasks, status(idle/working/blocked/error/offline), permission_mode(private/public_to), archived_at` | Persona plus launch config |
| `agent_runtime` | `daemon_id, provider(claude/codex/pi/…), status(online/offline), last_seen_at, device_info, max_concurrent_tasks, visibility` | One row per detected CLI per daemon |
| **`agent_task_queue`** | `agent_id, runtime_id, issue_id, status, priority, dispatched_at, started_at, completed_at, prepare_lease_expires_at, attempt, max_attempts(default 2), parent_task_id, retry_of_task_id, rerun_of_task_id, failure_reason, error, result jsonb, session_id, work_dir, durable_work_dir, branch_name, trigger_comment_id, coalesced_comment_ids[], autopilot_run_id, fire_at, wait_reason, originator_user_id, accountable_user_id, trigger_evidence_kind/ref_id, issue_snapshot jsonb, cancelled_by_*` | **The run/attempt row.** Retries are *new rows* linked by lineage |
| `task_message` | `task_id, seq, type, tool, call_id, content, input jsonb, output, output_truncated` | Per-run event log: text, thinking, tool-use, tool-result, status, error, log |
| `task_usage` (+ `task_usage_hourly`) | `task_id, provider, model, input/output/cache_read/cache_write_tokens, cost_usd_ticks (1e-10 USD, provider-reported or NULL), credits_micros` | Rows with NULL cost are priced client-side from a static rate table |
| `task_token` | `token_hash, task_id, agent_id, expires_at` | Task-scoped credential (`mat_…`) handed to the agent process |
| `task_supplement` | `task_id, comment_id, status(pending/delivering/delivered/failed), attempt_count` | Mid-run human "steer" message, injected into the live turn |
| `comment` | `issue_id, author_type/id, content, type(comment/status_change/progress_update/system), parent_id, source_task_id, resolved_at, revision` | Main human↔agent channel |
| `inbox_item` | `recipient_type(member/agent), recipient_id, type, severity(action_required/attention/info), issue_id, title, body, details jsonb, read, archived, actor_*` | Notification and escalation feed. It is **not** an approval object |
| `autopilot` / `autopilot_trigger` / `autopilot_run` | autopilot `status(active/paused/archived), execution_mode(create_issue/run_only), assignee, issue_title_template, pause_reason`; trigger `kind(schedule/webhook/api), cron_expression, timezone, next_run_at, last_fired_at, webhook_token, signing_secret`; run `status(issue_created/running/completed/failed/skipped), planned_at, issue_id, task_id, quota_reservation_id, reason_code` | Cron "autopilots" ≈ our scheduled sensors and digests |
| `issue_wakeup` (+ `_receipt`) | `kind, mode, event_types[], interval_seconds, cron_expression, next_fire_at, condition jsonb, max_fires, fire_count, expires_at, on_timeout, revision`; receipt `event_key, coalesce_key, task_id` | "Wake me when X happens / every N" with dedupe receipts |
| `skill` / `skill_file` / `skill_version` / `agent_skill` | content, files, `content_hash`, versioning | Skills are materialised into each task's workdir |
| `daemon_token`, `daemon_connection` | | Daemon auth and liveness |

### 2.3 Issue status and state machine

Seven built-ins in four lifecycle categories (`multica:server/internal/issuestatus/issuestatus.go`): `backlog, todo` (unstarted) · `in_progress, in_review, blocked` (started) · `done` (done) · `cancelled` (closed). Since migration 337 the column accepts any catalog key matching `^[a-z0-9][a-z0-9_]{0,31}$`; membership is checked in the app layer. Workflows fix the order `backlog, todo, … custom …, blocked, done, cancelled`. Entering `backlog`, `done` or `cancelled` never hands the issue off (`AcceptsHandoff`). Agents move issues themselves via the CLI (`multica issue status <id> in_review`). Review gates are therefore just a status (`in_review`) plus a workflow handler (a member or a reviewer agent). There is no first-class approval object.

### 2.4 Execution (task-queue) state machine

```
           CreateAgentTask / CreateRetryTask(fire_at)
                 │                         │
                 ▼                         ▼
              queued ◄── promote due ── deferred
                 │ ClaimAgentTask (FOR UPDATE SKIP LOCKED, prepare lease)
                 ▼
            dispatched ──(path lock busy)──► waiting_local_directory
                 │ StartTask (CAS on dispatched_at)        │
                 ▼                                         │
              running ◄────────────────────────────────────┘
        ┌────────┼───────────┬────────────┐
        ▼        ▼           ▼            ▼
    completed  failed    cancelled   (failed by sweeper: timeout / runtime_offline /
                 │                    runtime_recovery / queued_expired)
                 └─► MaybeRetryFailedTask → NEW row (attempt+1, retry_of_task_id)
```

Statuses CHECK (`migrations/128_comment_routing_escalation.up.sql`): `queued, dispatched, running, waiting_local_directory, deferred, completed, failed, cancelled`.

- **Claim** (`queries/agent.sql: ClaimAgentTask`): a single `UPDATE … WHERE id = (SELECT … ORDER BY priority DESC, created_at ASC LIMIT 1 FOR UPDATE SKIP LOCKED)` sets `dispatched`, `dispatched_at=now()` and `prepare_lease_expires_at=now()+lease`. It is only eligible if the runtime is `online` with a fresh `last_seen_at`, and if **no other task of the same agent on the same issue** (or chat session) is `dispatched`, `running` or `waiting_local_directory`. Result: different agents can work the same issue in parallel, but one agent never runs duplicates.
- **One pending row per (issue, agent, thread)**: a partial unique index (`ON CONFLICT … WHERE status IN ('queued','dispatched')`). New comments arriving while a task is pending are **merged** into it (`coalesced_comment_ids`, `MergeCommentIntoPendingTask`) instead of spawning new runs.
- **Prepare lease**: between claim and `StartTask`, the daemon extends `prepare_lease_expires_at` every ~15 s (`ExtendAgentTaskPrepareLease`). If the claim response was lost, `ReclaimStaleDispatchedTask*` re-delivers it once the lease has expired. `RequeueAgentTaskAfterClaimFailure` and `LockAgentTaskStartClaim` CAS on `dispatched_at`, so a stale delivery can never start a newer claim.
- **Running liveness** comes from the daemon heartbeat (`agent_runtime.last_seen_at`, every 15 s), not a per-task lease.
- **Sweeper** (`cmd/server/runtime_sweeper.go`, every 30 s): dispatch timeout 300 s; running timeout 9000 s, which is **AND-gated** on the runtime being stale beyond the reconnect grace (default 3 h), so healthy multi-hour runs survive partitions. Queued tasks expire only when both the task age and the runtime heartbeat age exceed the grace.
- **Daemon restart**: `RecoverOrphanedTasksForRuntime` fails every in-flight row with `failure_reason='runtime_recovery'`, then hands the rows to auto-retry.

### 2.5 Retry, timeout and failure taxonomy

- `failure_reason` is a canonical enum (`server/pkg/taskfailure/failure.go`). **Platform** reasons: `queued_expired, runtime_offline, runtime_reconnect_timeout, runtime_recovery, timeout, iteration_limit, agent_blocked, api_invalid_request, skill_bundle_unavailable, environment_prepare_failed, cleanup_unconfirmed …`. **Agent** reasons: 14 `agent_error.*` values produced by `Classify(rawError)` (provider auth, quota, context overflow, network, …).
- `retryableReasons` (`internal/service/task.go:~5497`) = `runtime_offline, runtime_recovery, timeout, codex_semantic_inactivity, agent_error.provider_network, skill_bundle_unavailable`. Default `max_attempts=2` (first run + one retry). `max_attempts<=1` disables retry. `provider_network` widens the ceiling to 3 and defers the final attempt by 5 s. `runtime_offline` retries start `deferred` and are promoted only after a fresh heartbeat. Autopilot and triage runs are never auto-retried, because their scheduler owns re-runs.
- **Resume safety**: `resumeUnsafeFailureReason` (`iteration_limit, api_invalid_request, context_overflow, codex_semantic_inactivity, …`) forces a fresh session on retry, because these failures poison the transcript. Other failures resume via `session_id`.
- **Lineage**: `retry_of_task_id` (system retry) is kept distinct from `rerun_of_task_id` (human rerun) so reports can separate them.
- **Daemon-side watchdogs** (`CLI_AND_DAEMON.md` §Configuration): agent timeout `0` (no cap) by default; idle watchdog 2 h; tool watchdog defaults to the idle value; Codex first-turn no-progress timeout 60 s; max 20 concurrent tasks per daemon.
- **Windows process control**: children are spawned `CREATE_SUSPENDED`, assigned to a Job Object with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`, then resumed (`server/pkg/agent/proc_windows.go`, `internal/daemon/processtree/controller_windows.go`). Cancel kills the whole tree.

### 2.6 Daemon ↔ server protocol

- **Wake + poll**: the server pushes a WebSocket wake signal (`internal/daemonws/hub.go`, `notifier.go`), and the daemon **batch-claims** across all its runtimes (`POST /api/daemon/tasks/claim` with `runtime_ids`, `max_tasks`). A catch-up poll runs every 30 s. With a healthy WS the claim poll is relaxed to about 3 m. Heartbeat: `POST /api/daemon/heartbeat` every 15 s.
- **Task lifecycle endpoints** (`internal/daemon/client.go`): `…/prepare-lease`, `…/start`, `…/wait-local-directory`, `…/progress`, `…/messages` (batched `task_message`), `…/usage`, `…/session` (pin the resume id early), `…/complete`, `…/fail`, `…/cancel-ack`, `…/supplements/claim|ack`, `runtimes/{id}/recover-orphans`. Terminal reports retry on a backoff schedule.
- **Per-task environment** (`internal/daemon/daemon.go:~180`): `MULTICA_TOKEN` (task-scoped `mat_` token), `MULTICA_SERVER_URL, MULTICA_WORKSPACE_ID, MULTICA_AGENT_ID, MULTICA_TASK_ID, MULTICA_TASK_SLOT`, plus a per-task `TMP/TEMP/TMPDIR`. The `multica` binary is prepended to `PATH`, so the agent talks back through the **CLI** (comments, status, sub-issues). Each task gets an isolated workdir under `~/multica_workspaces`, a per-task `CODEX_HOME` and per-task skills. The runtime brief is written as `CLAUDE.md` or `AGENTS.md` into the workdir instead of being passed as a system prompt.

### 2.7 Exact agent CLI invocations (`server/pkg/agent/*.go`)

| CLI | argv built by Multica | Protocol / parsing |
|---|---|---|
| Claude Code (`claude.go: buildClaudeArgs`) | `claude -p --output-format stream-json --input-format stream-json --verbose --permission-mode bypassPermissions --disallowedTools AskUserQuestion [--strict-mcp-config] [--mcp-config <f>] [--model M] [--effort E] [--max-turns N] [--resume <session>] [--settings <managed.json>] + custom_args` | NDJSON frames `assistant / user / system / result / log / control_request / control_response`. Usage comes from per-step assistant usage and the `result` totals, minus a baseline snapshot. The prompt goes to stdin as stream-json, which allows mid-turn supplements. Blocked user overrides: `-p, --output-format, --input-format, --permission-mode, --mcp-config, --effort, --settings` |
| Codex (`codex.go: buildCodexArgs`) | `codex app-server --listen stdio://` (+ filtered `-c key=val`; MCP goes in the per-task `$CODEX_HOME/config.toml`) | JSON-RPC: `initialize` → `thread/start` (or `thread/resume`) with `{model, cwd, approvalPolicy:null, sandbox:null, persistExtendedHistory:true}` → `turn/start` → notifications. **All** `item/commandExecution/requestApproval`, `item/fileChange/requestApproval` and `mcpServer/elicitation/request` are answered `accept`. Permission requests get a turn-scoped grant |
| Pi (`pi.go: buildPiArgsFor`) | `pi -p --mode json [--session <path>] [--model M] [--thinking L] [--skill <SKILL.md>]… + custom_args [--extension <mcp-ext>]`. When supplements are enabled: `pi --mode rpc …` | JSON events `agent_start, turn_start, message_update{text_delta,thinking_delta}, tool_execution_start/end, turn_end, agent_end, auto_retry_start/end, error`. `--tools` is deliberately not passed |
| DSH (`dsh.go: dshLaunchArgs`) | `dsh --profile multica --stdio` (needs the "multica" runtime profile installed). Models are listed via `dsh --profile multica --list-models` | Versioned JSONL protocol v1. The daemon sends `{"v":1,"type":"execute","request_id","cwd","prompt","resume_session_id","model":{provider,id},"mcp_servers":[…]}` or `{"type":"cancel"}`. It receives `ready, session, text, thinking, tool_call, tool_result, usage, protocol_error, result` |

All backends normalise into one `Message{type: text|thinking|tool-use|tool-result|status|error|log, tool, call_id, input, output}` stream and one `Result{status: completed|failed|aborted|timeout|cancelled, output, error, duration_ms, session_id, usage map[model]TokenUsage, resume_rejected}` (`pkg/agent/agent.go`). **We should copy this contract almost verbatim** for `src/awos/executors/base.py`.

### 2.8 CLI surface (`CLI_AND_DAEMON.md`)

`multica setup [self-host]`, `login`/`auth status`/`logout`, `daemon start|stop|status|logs [--foreground] [--profile]`, `daemon autostart enable|status|disable` (Windows: HKCU `…\Run` value `Multica`), `workspace list|switch|get|members`, `issue list|get|create|update|assign|status|reorder`, `issue comment list [--thread|--recent|--tail|--since] / add [--reply] [--steer|--steer-active] / delete`, `issue metadata get|set|rm`, `issue subscriber …`, `issue runs [--active] [--family]`, `issue run-messages <task_id> [--after seq]`, `issue usage`, plus `project …`, `agent …`, `autopilot …`, `skill …`. The agent-facing design points are worth noting: thread-aware comment reads with cursors (to bound context), `--steer` caps at 3 injected messages per run, and `runs --active --family` answers "is someone already working next to me?".

### 2.9 Can the server run natively on Windows without Docker?

**Technically yes (UNVERIFIED by build), but not worth it.** Evidence: the documented manual path needs only Go 1.26.9, Node 22, pnpm 10.28.2 and PostgreSQL 17 with the stock contrib `pgcrypto` + `pg_trgm`. `pg_bigm` and `pg_cron` are guarded and optional. Redis is optional (fail-open). I found no unix-only imports outside build-tagged files in `server/{cmd,internal,pkg}`, and the daemon side is Windows-aware (26 non-test `*_windows.go` files). Steps:

1. Portable Go and EDB PostgreSQL 17 zip; `initdb -E UTF8`; `pg_ctl register` as a service.
2. `go build ./cmd/server ./cmd/migrate ./cmd/multica`.
3. `migrate up` (638 steps).
4. `pnpm install && pnpm build` for the Next.js 16 web app (930 KB lockfile).
5. Run `server` + `next start` + Postgres + daemon as four background processes (NSSM or Task Scheduler).
6. Set `JWT_SECRET` and `DO_NOT_TRACK=1`; read the login code from the logs.

**Effort**: about 1 day to stand up, plus an ongoing upgrade burden. The project lands many commits per day (HEAD is MUL-8647, #9985), and each upgrade means rebuilding Go and Next and running new migrations. The server is unsupported on Windows. None of `go`, `pnpm` or Postgres is installed today (`00-environment-inventory.md`). Running it this way also violates our "never run untrusted build scripts" rule, unless we fork-review a pinned commit.

### 2.10 Fit against our requirements

Strong on R3, R6, R9 (runs, messages, usage, retries, cancel, rerun, steer) and on autopilots. **Weak or misaligned on R1/R4/R7**:
- No autonomy levels.
- No approval/Outbox object (`inbox_item` is notification-only).
- Every tool call is auto-approved at the executor layer.
- No per-action policy.
- Multi-tenant concepts (workspace, member, squad, visibility, invocation permission, Composio, channels for Lark, DingTalk, WeCom) are dead weight on a single PC.

Verdict: a **design reference, not a runtime**.

---

## 3. Alternatives survey

Star counts and dates come from web search snippets of GitHub pages, Nov 2026 unless noted. Last commit is from our clone when cloned, otherwise from search.

| Project | Stars | Last activity | License | Stack / persistence | Native Windows, no Docker? | HITL / approvals | Fit as our task center |
|---|---|---|---|---|---|---|---|
| **Multica** | ~55k | 2026-11-05 (clone) | Apache-2.0 + extra conditions | Go + Next.js 16 + **Postgres 17** | Server: unofficial manual build (§2.9). Daemon: yes | Status `in_review` + workflow handlers. **No tool/action approvals** (auto-accept) | Reference only |
| **Paperclip** (`paperclipai/paperclip`) | ~111k | 2026-11-05 (clone) | MIT | Node/TS (Express-style server, about 626k LOC) + React · **embedded Postgres 18 beta** (`embedded-postgres@18.1.0-beta.16`, patched) · Drizzle, 430 migrations | Officially `npx paperclipai onboard`. Windows bugs: missing `@embedded-postgres/windows-x64` beta.16 binary (#3094, #8457, PR #9211), stale `postmaster.pid` after kill (#2333), WIN1252 encoding (#1026), `C:\C:\` path (#410), refuses to run as Administrator | **Best-in-class**: `approvals`, `issue_thread_interactions`, `tool_action_requests` bound to an args hash, budgets with hard stop | Too heavy and fragile on Windows. **Copy its governance model** |
| **Vibe Kanban** (`BloopAI/vibe-kanban`) | ~28.6k | 2026-11-03 (clone). **Sunsetting** since 2026-04-10, community-maintained; DB migrations frozen since 2026-03-17 | Apache-2.0 | Rust (axum + sqlx) + React · **SQLite** (journal_mode=DELETE) | Yes (`npx vibe-kanban`, Tauri app) | Per-tool approval for Claude via `--permission-prompt-tool=stdio` control protocol, and for Codex via app-server requests | Coding-only (worktrees, PRs). Sunset. **Copy the executor approval plumbing** |
| **OpenAI Symphony** | ~25k (June 2026; current UNVERIFIED) | 2026-09-15 (clone) | Apache-2.0 | `SPEC.md` (2.3k lines) + Elixir/OTP reference. **No durable DB**: recovery is tracker- and filesystem-driven | Elixir on Windows possible (UNVERIFIED) | Implementation-defined. Spec: "approval requests MUST NOT stall a run indefinitely"; tools run host-side, without credentials in the child | Not a task center; needs Linear, GitHub or Jira. **Copy orchestrator states and backoff** |
| **Backlog.md** (`MrLesk/Backlog.md`) | ~7.3k | active 2026 | MIT | TS/Bun CLI · one Markdown file per task in `backlog/` · web board on 127.0.0.1 · MCP | Yes | Human checkpoints are a convention only (spec → plan → review). No claim, lock or assignee | Good human-readable export format only |
| **Claude Task Master** (`eyaltoledano/claude-task-master`) | ~28.3k | active 2026 | MIT + Commons Clause (UNVERIFIED) | Node · single `.taskmaster/tasks/tasks.json` · MCP | Yes | None. Statuses `pending, in-progress, done, review, deferred, cancelled`; `dependencies[]`; `next` picks the highest-priority ready task | PRD→DAG planner idea only |
| **OpenClaw** (`openclaw/openclaw`) | ~394k | active 2026 | MIT | Node 24+ gateway, Pi agent runtime, channels (WhatsApp, Telegram, …), `HEARTBEAT.md` every 30 m, `~/.openclaw/cron/jobs.json` | Native CLI/gateway supported (Scheduled Task); Windows Hub uses an app-owned WSL distro | Per-channel pairing/allowlists. No task DB or approval queue (UNVERIFIED in detail) | A **sensor/notification channel** and a possible executor, not a task center |
| **Hermes Agent** (`NousResearch/hermes-agent`) | ~260k | v0.18+ (2026) | MIT | Python, gateway, built-in cron (`~/.hermes/cron/`, `%LOCALAPPDATA%\hermes` on native Windows), skills learning loop | Native Windows documented; cron `.sh` bug #46332 | Not a task board | Possible extra executor (Multica already supports `hermes`) |
| **DBOS Transact (Py)** | n/a | active 2026 (June 2026 release notes) | MIT | Library; durable workflows/steps in **SQLite or Postgres**; `DBOS.recv/send` for HITL ("agent inbox" example) | Yes (pure Python) | Durable waits for days | Viable *library* under our core (§5.3). Not a task center |
| **LangGraph** | very high | v1.2 (May 2026) | MIT | `interrupt()` + `Command(resume=…)`, `SqliteSaver` checkpointer | Yes | Graph-level interrupts; the node re-executes from the top on resume | Only for in-process LLM graphs, not for supervising external CLIs |
| **Temporal** | very high | active | MIT | `temporal server start-dev --db-filename x.db` (SQLite, single binary, "not for production") | Yes (CLI binary) | Signals/updates; infinite waits | Overkill; a second service plus SDK determinism rules |
| **Inngest AgentKit** | n/a | AgentKit 0.13.2 (Nov 2025) | server SSPL→Apache, AgentKit Apache-2.0 | `inngest start` single process with SQLite | Likely (UNVERIFIED) | `step.waitForEvent` with timeout | Adds a server plus an SSPL licence; TS-centric |
| HumanLayer SDK | ~11k | SDK removed (#646) | Apache-2.0 | n/a | n/a | `require_approval` decorator, deprecated | Pattern only |
| NicholasMTElliott/task-board, LoopTroop, Friday, Orca, Composio AO | small (UNVERIFIED) | 2026 | various | GitHub Projects/Trello or worktree IDEs | mixed | manual gates | Coding-centric; not evaluated further |

---

## 4. Deep reads of the top candidates

### 4.1 Paperclip: governance done right, runtime too heavy

Paths are under `paperclip:packages/db/src/schema/` and `packages/shared/src/constants.ts`.
- **Issues** (`issues.ts`): `status` uses the same 7 values as Multica (`backlog, todo, in_progress, in_review, done, blocked, cancelled`). Further columns: `status_version` (optimistic concurrency), `checkout_run_id`, `execution_run_id`, `execution_locked_at` (**atomic checkout**: the second claimer gets `409`, "never retry a 409", `docs/api/issues.md`), `review_policy(anyone/not_creator/human_only)`, `execution_policy` / `execution_state` jsonb, `origin_kind/origin_id/origin_fingerprint`, `request_depth` (bounds agent-spawned recursion), `billing_code`, `monitor_next_check_at` (follow-up checks), `unblock_descriptor`.
- **Runs** (`heartbeat_runs.ts`): status `queued, scheduled_retry, running, succeeded, interrupted, failed, cancelled, timed_out`. Further columns: `controller_boot_id`, `controller_lease_expires_at`, `process_pid`, `process_group_id`, `last_output_at`, `last_output_seq`, `retry_of_run_id`, `process_loss_retry_count`, `scheduled_retry_at/attempt/reason`, `liveness_state(completed/advanced/plan_only/empty_response/blocked/failed/needs_followup)`, `session_id_before/after`, `log_ref`, `log_bytes`, `stdout_excerpt`, `stderr_excerpt`, `usage_json`, `cost_accounting_pending`, `context_snapshot`. The **liveness classification** ("did this run actually advance the task?") is a good idea for L5 loops.
- **Run events** (`heartbeat_run_events.ts`): `run_id, seq, event_type, stream, level, message, source_event_id, protocol_schema_version`.
- **Wakeups** (`agent_wakeup_requests.ts`): `source(timer/assignment/on_demand/automation)`, `status(queued/deferred_issue_execution/claimed/coalesced/skipped/completed/failed/cancelled)`, `coalesced_count`, `idempotency_key`, `run_id`. If an agent is already running, new wakeups are **coalesced**, not duplicated.
- **Approvals** (`approvals.ts`, `issue_approvals.ts`, `approval_comments.ts`): `type` (`hire_agent, approve_ceo_strategy, budget_override_required, request_board_approval`), `status(pending/revision_requested/approved/rejected/cancelled)`, `payload jsonb`, `decision_note`, `decided_by`, `decided_at`. A many-to-many link to issues and a comment thread per approval. Resolution wakes the requester (`docs/start/core-concepts.md`).
- **Interactions** (`issue_thread_interactions.ts`): kinds `suggest_tasks, ask_user_questions, request_confirmation, request_checkbox_confirmation, request_item_verdicts`; status `pending, accepted, rejected, answered, cancelled, expired, failed`; `continuation_policy(none/wake_assignee/wake_assignee_on_accept)`; `resolver_policy(anyone/not_creator/human_only)`; `idempotency_key`; `payload` / `result`. This is exactly our **L2 "decision" Outbox item**.
- **Tool-call approvals** (`tool_access.ts`, `server/src/services/tool-approval-execution.ts`):
  - `tool_invocations` has `status(pending/authorized/denied/awaiting_approval/executing/succeeded/failed/cancelled/timed_out/rate_limited)`, `approval_state`, `risk_level`, `arguments_hash`, `arguments_summary`, `policy_decision(allow/deny/require_approval/rate_limited)`, `idempotency_key`.
  - `tool_action_requests` has `status(pending/approved/executing/rejected/expired/cancelled/executed/failed)`, `canonical_arguments_hash`, `signed_arguments`, `preview_markdown`, `expires_at`.
  - Execution is a locked transaction: it re-reads both rows `FOR UPDATE`, checks `approved`, and **verifies the args hash still matches** before `executing`. It records the outcome exactly once.
  - Policies are `allow, block, require_approval, trust_rule, rate_limit`.
  - **This is the model for our Outbox.**
- **Cost and budget** (`cost_events.ts`, `budget_policies.ts`, `budget_incidents.ts`):
  - Cost events record `provider, model, tokens, cost_cents numeric(24,7), cost_status(reported/estimated/unpriced), pricing_provenance, idempotency_key, receipt_hash, heartbeat_run_id, issue_id, billing_code`.
  - Budget policies are keyed by `scope_type(company/agent/project)` and `window_kind(calendar_month_utc/lifetime)`, with `amount`, `warn_percent=80`, `hard_stop_enabled`, `unpriced_usage_policy='block'`, `reservation_cents`.
  - Incidents are `open/resolved/dismissed`, with resolution `keep_paused | raise_budget_and_resume` via a `budget_override_required` approval.
- **Plan decomposition** (`issue_plan_decompositions.ts`): `source_issue_id, status(in_flight/…), request_fingerprint, requested_children jsonb, child_issue_ids jsonb, accepted_interaction_id`. This gives **idempotent subtask fan-out**: a planner retry cannot create duplicate children.
- **Adapters**: `claude --print - --output-format stream-json --verbose [--dangerously-skip-permissions]` (`packages/adapters/claude-local/src/server/execute.ts:853`, `acp.ts:681`); `codex exec --json [--dangerously-bypass-approvals-and-sandbox]` (`codex-local/src/server/codex-args.ts:47-61`); also `pi_local`, `hermes_local`, `openclaw_gateway`, `process`, `http` (`docs/agents-runtime.md`).
- **Why not adopt**: about 890 non-test top-level service modules in `server/src/services`, 183 schema files and an embedded Postgres 18 *beta* with a recurring Windows binary gap and crash-recovery bugs. It is also a "zero-human company" metaphor (CEO, org chart, hiring) that we would have to fight.

### 4.2 Vibe Kanban: the executor-side approval protocol

- **Model** (`vibe-kanban:crates/db/src/models/`): `task(status: todo/inprogress/inreview/done/cancelled, parent_workspace_id)` → `workspace` (worktree) → `session` → `execution_process(run_reason: setupscript/cleanupscript/archivescript/codingagent/devserver, status: running/completed/failed/killed, exit_code, executor_action json, dropped)` → `coding_agent_turn(agent_session_id, agent_message_id, prompt, summary, seen)` plus `execution_process_logs`. It uses SQLite via sqlx. The useful split is that one task can have many processes of different *reasons* (setup, agent, cleanup).
- **Claude per-tool HITL** (`crates/executors/src/executors/claude.rs:157-194`, `claude/client.rs:279-330`, `claude/protocol.rs`):
  - Command: `claude -p --permission-prompt-tool=stdio --permission-mode=bypassPermissions --verbose --output-format=stream-json --input-format=stream-json --include-partial-messages --replay-user-messages`.
  - Claude emits `control_request{subtype:"can_use_tool", tool_name, input, tool_use_id}` on stdout. The host answers `control_response` `Allow{updated_input}` or `Deny{message, interrupt}`.
  - A PreToolUse hook that returns `ask` forces the request even in bypass mode.
  - `AskUserQuestion` is routed to a question approval.
  - This is how we get **tool-level Outbox for Claude Code without an MCP proxy**. (Using the value `stdio` for `--permission-prompt-tool` is what the Agent SDK does internally; that it is a public contract is UNVERIFIED, so pin the CLI version and add a contract test.)
- **Codex**: `codex app-server` (`codex.rs:451`). Server-initiated `requestApproval` requests can be held until a human decides (Multica auto-accepts instead).
- **Status**: sunsetting. Do not depend on it.

### 4.3 Symphony: the cleanest orchestrator spec

- Internal claim states: `Unclaimed → Claimed → Running | RetryQueued → Released`. Run-attempt phases: `PreparingWorkspace, BuildingPrompt, LaunchingAgentProcess, InitializingSession, StreamingTurn, Finishing, Succeeded, Failed, TimedOut, Stalled, CanceledByReconciliation` (`symphony:SPEC.md` §7).
- Tick order: **reconcile running → validate config → fetch candidates → sort (priority, created_at, id) → dispatch while slots remain**. Global and per-state concurrency caps (§8.1-8.3).
- Backoff: continuation after a clean exit is 1 s. Failure retries use `min(10 s · 2^(attempt-1), max_retry_backoff=5 m)` (§8.4).
- Stall detection: elapsed time since the last agent event is compared to `stall_timeout_ms`, then kill + retry (§8.5). Startup cleanup removes workspaces of terminal issues (§8.6).
- Approval policy must be documented. A run must never stall on an approval or a user-input request (§10.5).
- Tools that mutate external systems execute **host-side**. Tracker credentials are stripped from the child environment (`secret_environment_names()`) (§10.5). This maps directly onto our Outbox action runner.

---

## 5. Recommendation

### 5.1 Decision: **(b) build the lightweight core.** Do not adopt any candidate wholesale.

1. **Windows / no-Docker.**
   - Multica's server is Docker-first and would be an unsupported four-process native build.
   - Paperclip's embedded Postgres is a moving beta with documented Windows startup and crash-recovery bugs.
   - VK is sunset.
   - Our skeleton (`src/awos`, Python 3.12 + uv + stdlib `sqlite3` in WAL mode + FastAPI on 127.0.0.1) needs **zero** new system services, so the whole state is one file `var/awos.db` plus `var/runs/`.
   - Backup is `sqlite3 .backup`, and portability is copying a folder.
2. **Semantics we need are missing upstream.**
   - Neither Multica nor VK has L1–L5 autonomy or an action Outbox. Multica explicitly auto-approves every tool call.
   - Paperclip has the governance primitives but wrapped in a company simulator.
   - Adding L1–L5 plus Outbox to Multica means forking a project with about 10 commits a day.
3. **Stability.** We would own about 3–5k lines of Python that we understand. Upstream projects ship hundreds of migrations a year (Multica 638, Paperclip 430). Tracking them is a permanent tax.
4. **Agents are not reimplemented.** The heavy lifting (coding, tools, MCP, connectors) stays in `codex`, `claude`, `pi` and `dsh`. The task center only supervises child processes, which is exactly the part Multica's daemon shows is tractable.
5. **Escape hatch.** Keep `tasks.meta.external_refs` and the Multica-compatible `Message`/`Result` contract. If we later want Multica's UI, AWOS can mirror tasks to it via its REST API or the `multica` CLI (UNVERIFIED: a write API exists for issues and comments, per the CLI reference) instead of migrating state.

### 5.2 How L1–L5 and the Outbox map onto the core

| Level | Sandbox / permissions given to the executor | Outbox interactions | Terminal handoff |
|---|---|---|---|
| L1 understand | read-only (`codex -s read-only`, `claude --permission-mode plan`/`--allowedTools Read,Grep,Glob`) | none | `waiting_user` + briefing artifact |
| L2 advise | read-only | `approval(kind=decision)` with options and a recommendation; `continuation=wake_on_accept` spawns child tasks | `waiting_approval` → children / `waiting_user` |
| L3 partial | workspace-write in `var/workspaces/<task>` only | `review` approval on artifacts | `review` |
| L4 mostly | workspace-write plus allow-listed internal actions | **every** sensitive `action_type` → `approval(kind=action)`, executed host-side after approval | `review` |
| L5 end-to-end | as L4, plus a verify run | sensitive actions still go to the Outbox unless a standing `trust_rule` policy exists (Paperclip `TOOL_POLICY_TYPES`) | `done` if the verify run passes, else `review` |

Effective level = `min(assessed_level, level_cap, policy_cap(category, executor))`. Budget breach → `approval(kind=budget)` plus pausing the root task (Paperclip incidents).

### 5.3 What not to add (yet)

- DBOS, Temporal, Inngest and LangGraph all solve *in-process* durable workflow replay. Our unit of work is an **external CLI process** that we cannot replay deterministically. What we need instead is leases, heartbeats, orphan recovery and new-attempt retries, which are about 300 lines of SQL and Python.
- Revisit DBOS Transact (SQLite-backed, MIT, pure Python) only if the planner grows multi-step LLM workflows that need exactly-once step memoisation.

---

## 6. Distilled schema (v2 proposal, deltas against `src/awos/schema.sql`)

The existing v1 is already close. The main structural change is to **move execution state (lease, attempt, session) from `tasks` to `runs`** (Multica `agent_task_queue`, Paperclip `heartbeat_runs`). Retries become new `runs` rows, and the task only points at `active_run_id`. All timestamps are ISO-8601 UTC. Money is an integer `cost_micro_usd`, never a float (Multica uses 1e-10 USD ticks, Paperclip numeric(24,7)).

```sql
-- WORK ITEMS ---------------------------------------------------------------
tasks(            -- keep v1 columns, plus:
  revision INTEGER NOT NULL DEFAULT 0,        -- optimistic concurrency (Multica issue.revision / Paperclip status_version)
  active_run_id TEXT,                          -- replaces lease_owner/lease_expires_at (move to runs)
  origin_kind TEXT, origin_ref TEXT,           -- manual | sensor:<n> | schedule:<id> | planner:<decomp_id> | approval:<id>
  origin_fingerprint TEXT,                     -- with dedupe_key: idempotent capture
  request_depth INTEGER NOT NULL DEFAULT 0,    -- bounds agent-spawned recursion (Paperclip)
  review_policy TEXT DEFAULT 'human_only',     -- who may close review (Paperclip ISSUE_REVIEW_POLICIES)
  agent_profile TEXT,                          -- FK agent_profiles.name; executor chosen through it
  monitor_next_check_at TEXT                   -- follow-up / "check again later" (Paperclip monitor_*)
)
task_deps(task_id, depends_on, type TEXT NOT NULL DEFAULT 'blocks')    -- Multica issue_dependency
plan_decompositions(id, source_task_id, status in_flight|completed|failed,
  request_fingerprint TEXT UNIQUE, requested_children JSON, child_task_ids JSON,
  accepted_approval_id, created_at, completed_at)            -- idempotent DAG fan-out (Paperclip)
task_comments(id, task_id, author TEXT,       -- 'user' | 'agent:<profile>' | 'system'
  kind comment|progress|status_change|steer, body, run_id, delivered_at, created_at)
                                               -- human steer → delivered into live run (Multica task_supplement)

-- EXECUTORS ----------------------------------------------------------------
agent_profiles(name PK, executor codex|claude|pi|dsh|internal, model, effort,
  instructions_path, sandbox_profile, allowed_tools JSON, env_allowlist JSON,
  max_concurrency INTEGER, max_turns, idle_timeout_s, hard_timeout_s, enabled)
executor_status(executor PK, path, version, status online|degraded|offline,
  last_probe_at, last_error)                  -- Multica agent_runtime, collapsed to one machine
worker_boots(boot_id PK, pid, started_at, last_heartbeat_at, stopped_at)
                                               -- controller identity (Paperclip controller_boot_id)

-- EXECUTION ----------------------------------------------------------------
runs(             -- keep v1 columns, plus:
  status TEXT,    -- queued|deferred|claimed|running|succeeded|failed|timed_out|cancelled|interrupted
  agent_profile TEXT, priority INTEGER,
  not_before TEXT,                            -- deferred retry / backoff (Multica fire_at)
  claimed_by_boot TEXT, lease_expires_at TEXT,-- prepare lease (claim→start) and running lease
  heartbeat_at TEXT, last_output_at TEXT, last_event_seq INTEGER NOT NULL DEFAULT 0,
  retry_of_run_id TEXT, rerun_of_run_id TEXT, -- system retry vs human rerun lineage (Multica)
  failure_reason TEXT,                        -- canonical enum, see §6.2
  session_id TEXT, resume_safe INTEGER,       -- resume pointer; 0 forces a fresh session on retry
  liveness TEXT,                              -- advanced|plan_only|empty_response|blocked|needs_followup (Paperclip)
  workspace_dir TEXT, idempotency_key TEXT UNIQUE
)
CREATE UNIQUE INDEX ux_runs_one_active ON runs(task_id, purpose)
  WHERE status IN ('queued','deferred','claimed','running');   -- Multica one-pending-per-(issue,agent)
run_events(id, run_id, seq INTEGER NOT NULL, ts, kind, -- text|thinking|tool_use|tool_result|status|error|log|usage|approval
  tool TEXT, call_id TEXT, input JSON, output TEXT, truncated INTEGER,
  UNIQUE(run_id, seq))                        -- Multica task_message; seq makes daemon batch re-sends idempotent
usage(id, run_id, executor, model, input_tokens, output_tokens, cache_read_tokens,
  cache_write_tokens, cost_micro_usd INTEGER, cost_status reported|estimated|unpriced,
  pricing_version TEXT, idempotency_key TEXT UNIQUE, occurred_at)   -- replaces cost_ledger

-- HITL OUTBOX --------------------------------------------------------------
approvals(        -- keep v1 columns, plus:
  kind TEXT,      -- decision|question|review|action|budget|permission
  status TEXT,    -- pending|revision_requested|approved|rejected|expired|cancelled|executing|executed|failed
  payload_hash TEXT NOT NULL,                 -- sha256(canonical JSON of payload); re-checked at execute (Paperclip)
  preview_md TEXT,                            -- exactly what the human sees and signs off
  resolver_policy TEXT DEFAULT 'human_only',
  continuation TEXT DEFAULT 'wake_task',      -- none|wake_task|wake_on_accept|spawn_children
  idempotency_key TEXT UNIQUE,                -- run_id + call_id or a planner fingerprint
  requested_by_run_id TEXT, tool_call_id TEXT,-- link back to the run_events row (tool-level approvals)
  execute_attempts INTEGER NOT NULL DEFAULT 0, external_ref TEXT  -- e.g. sent message-id for de-dup
)
approval_comments(id, approval_id, author, body, created_at)     -- revision_requested dialogue
action_policies(id, action_type, category, min_level, decision allow|require_approval|deny|trust_rule,
  rate_limit_per_day, expires_at, note)       -- Paperclip tool_policies, collapsed

-- BUDGETS ------------------------------------------------------------------
budget_policies(id, scope_type global|executor|category|root_task, scope_id,
  window day|month|lifetime, limit_micro_usd, warn_pct DEFAULT 80, hard_stop INTEGER DEFAULT 1,
  unpriced_policy block|allow, active)
budget_incidents(id, policy_id, window_start, observed_micro_usd, threshold soft|hard,
  status open|resolved|dismissed, approval_id, resolved_at)

-- SCHEDULING ---------------------------------------------------------------
schedules(id, name, kind cron|interval|event, cron_expr, timezone, interval_s,
  execution_mode create_task|run_only, template JSON, agent_profile, level_cap,
  enabled, pause_reason, next_run_at, last_fired_at)          -- Multica autopilot + trigger
schedule_fires(id, schedule_id, planned_at, status created|running|completed|failed|skipped,
  task_id, run_id, reason_code, UNIQUE(schedule_id, planned_at))  -- exactly-once firing per slot
-- keep: signals, artifacts(+version, supersedes_id, mime), notifications, events (append-only audit),
--       sensor_state, context_facts, kv
```

### 6.1 Task state machine (keep `models.TRANSITIONS`; add these guards)

```
inbox ─triage─► triaged ─accept/auto(level≤cap)─► ready ─claim tx─► running
  │               │                                 ▲  │               │
  └─dismiss/snooze┘                  deps unmet ►blocked◄┘               │
running ─run ok─► per level: L1→waiting_user · L2→waiting_approval(decision) · L3/L4→review · L5→verify run→done|review
running ─needs Outbox─► waiting_approval ─approved+executed─► ready (continuation) │ rejected → waiting_user
running ─run failed─► retryable & attempts left ? ready(not_before=backoff) : failed
review ─human accept─► done │ request changes → ready (task_comments.kind=steer carried into the next run)
any non-terminal ─cancel─► cancelled (kills the active run's Job Object)
```

- Rule 1: `tasks.status='running'` is set **only** inside the claim transaction that inserts or claims a `runs` row.
- Rule 2: every transition bumps `revision`, writes `events`, and uses `UPDATE … WHERE id=? AND revision=?`.
- Rule 3: a parent becomes `ready` for synthesis only when all `blocks` deps are terminal-success. A failed child moves the parent to `waiting_user`, not `failed` (our policy).
- Rule 4: child transitions are recorded as durable child-event rows in the same transaction (`claimed_at`, `processed_at`), and a sweeper wakes the parent. This copies Multica's `issue_child_event` table plus the `child_done` wakeup rule (`internal/scheduler/jobs_child_done.go`, 30 s sweep), so a crash between "child done" and "wake parent" is never lost.

### 6.2 Run state machine and failure taxonomy

```
queued ─(not_before>now)─► deferred ─due─► queued
queued ─claim (BEGIN IMMEDIATE; pick ORDER BY priority DESC, created_at; set claimed_by_boot, lease=now+30s)─► claimed
claimed ─spawn ok (pid, Job Object)─► running        claimed ─lease expired, never started─► queued (re-deliver)
running ─heartbeat every 10–15s (lease=now+60s)─► running
running ─exit 0 + valid structured result─► succeeded
running ─error / exit≠0─► failed(reason)     running ─no output > idle_timeout─► timed_out(stalled)
running ─user cancel─► cancelled             worker boot changes with run claimed|running ─► interrupted
```

- Platform reasons: `worker_restart, lease_expired, stalled, hard_timeout, budget_exceeded, policy_denied, workspace_prepare_failed, cancelled`.
- Agent reasons: `agent.provider_auth, agent.rate_limit, agent.provider_network, agent.context_overflow, agent.invalid_request, agent.schema_invalid, agent.unknown` (classify from stderr/result text as Multica `taskfailure.Classify` does).
- Retryable: `worker_restart, lease_expired, stalled, agent.provider_network, agent.rate_limit (min 60 s backoff), agent.schema_invalid (once, with a repair prompt)`. Default `max_attempts=2`, or 3 for network errors.
- Backoff: `min(10s·2^(n-1), 5m)` + 20% jitter (Symphony).
- Resume-unsafe (fresh session on retry): `agent.context_overflow, agent.invalid_request, stalled`.

### 6.3 Approval (Outbox) state machine

```
pending ─approve─► approved ─action-runner claim tx (status='approved' AND payload_hash matches)─► executing
   │ ─reject─► rejected (task → waiting_user or ready with note)        executing ─ok─► executed (external_ref saved)
   │ ─request changes─► revision_requested ─agent resubmits─► pending (new payload_hash)  executing ─error─► failed ─retry─► executing
   │ ─expires_at passed─► expired (apply default_on_expiry or nothing)  executing at crash ─► re-check external_ref/idempotency before retry
   └─task cancelled─► cancelled
```

The agent **never** holds credentials for sensitive actions. It emits a structured `proposed_action` (or Claude/Codex raise a tool approval). AWOS creates the approval, and the host-side action runner executes it after approval (Symphony §10.5, Paperclip `tool-approval-execution.ts`).

---

## 7. Design patterns worth copying (with sources)

1. **Work item ≠ execution attempt.** Keep human-facing `tasks` separate from `runs`. A retry is a new row with `retry_of_run_id`; a human rerun gets `rerun_of_run_id` (`multica:…/models.go AgentTaskQueue`, Paperclip `heartbeat_runs.retry_of_run_id`).
2. **Atomic claim with skip-locked semantics.** In SQLite, use `BEGIN IMMEDIATE` followed by `UPDATE runs SET status='claimed' … WHERE id=(SELECT … LIMIT 1) AND status='queued' RETURNING *`. This is the equivalent of Multica `ClaimAgentTask`, and SQLite's single writer gives the serialisation for free. Add the "same task + same profile not already active" predicate and the partial unique index `ux_runs_one_active`.
3. **Two-phase lease.** A short *prepare* lease covers claim→spawn (Multica 15 s extender). A *running* lease is refreshed by heartbeats. Reclaim only rows that are `claimed` with `started_at IS NULL` and an expired lease, and CAS on `claimed_at` so a stale worker cannot start a newer claim (`ReclaimStaleDispatchedTaskForRuntime`, `LockAgentTaskStartClaim`).
4. **Boot-scoped orphan recovery.** On daemon start, mark every `claimed`/`running` run with `claimed_by_boot != current` as `interrupted(worker_restart)` and feed it to the retry policy (`RecoverOrphanedTasksForRuntime`). Kill leftover PIDs recorded in `runs.pid` if they are still alive.
5. **Timeouts gated by liveness.** Never kill a run on wall clock alone while its process is producing output. Use an idle/stall timer on `last_output_at`, with the hard timeout as a backstop (Multica `FailStaleTasks`, Symphony §8.5).
6. **Canonical failure taxonomy driving retry/resume** (`multica:server/pkg/taskfailure`, `internal/service/task.go retryableReasons/resumeUnsafeFailureReason`). Persist the reason, not just the error text.
7. **Coalescing.** If a task already has a queued run, merge new triggers (comments, sensor updates) into it (`coalesced_comment_ids`, Paperclip `agent_wakeup_requests.coalesced_count`) instead of spawning parallel runs.
8. **Idempotency keys everywhere.** Use them on task capture (`dedupe_key` / `origin_fingerprint`), schedule fires (`UNIQUE(schedule_id, planned_at)`), plan fan-out (`request_fingerprint`), usage rows, approvals (`run_id:call_id`) and external actions (`external_ref`). Sources: Paperclip `issue_create_idempotency_keys`, `issue_plan_decompositions`, `cost_events.idempotency_key`.
9. **Sequenced run event log.** Store `run_events(run_id, seq)` with a unique pair, `call_id` pairing tool_use↔tool_result, and truncation flags. The dashboard tails it by `?after=seq` (Multica `task_message`, `issue run-messages --after`).
10. **One normalised executor contract.** Every adapter yields `Message{type, tool, call_id, input, output}` plus one `Result{status, output, error, session_id, usage{model→tokens,cost}}` (`multica:server/pkg/agent/agent.go`). Adapters own the protocol quirks; the core never parses CLI-specific JSON.
11. **Context via files, not argv.** Write the per-run brief as `AGENTS.md`/`CLAUDE.md` into the run workspace, and keep prompts in `var/runs/<id>/prompt.md` (Multica MUL-5392 notes). This avoids Windows argv limits (`pkg/agent/argv_limit.go`) and keeps the CLIs' own instruction loading.
12. **Blocked-args filter.** User or profile `custom_args` must not override protocol or permission flags (`claudeBlockedArgs`, `codexBlockedArgs`, `pi.go:1114-1174`).
13. **Process isolation on Windows.** Spawn `CREATE_SUSPENDED`, assign a Job Object with `KILL_ON_JOB_CLOSE`, then resume. Give each run its own `TEMP/TMP`, workspace dir and `CODEX_HOME`, and an env allowlist (no inherited tokens; Symphony `secret_environment_names`). Python can do this via `ctypes` or `pywin32`.
14. **Scoped callback credential.** If agents call back into AWOS (status, comments, child tasks), give each run a short-lived token bound to `run_id` (Multica `task_token`, `MULTICA_TOKEN=mat_…`) and a tiny `awos` CLI on `PATH`.
15. **Hash-bound approvals.** The human approves `preview_md` together with `payload_hash`, and execution re-verifies the hash under lock (Paperclip `bindVerifiedToolApprovalArguments`). Edits create a new hash and a new decision.
16. **Tool-level HITL for Claude** via `--permission-prompt-tool stdio` and `control_request can_use_tool` → allow/deny (VK `claude/client.rs`). **For Codex**, answer app-server `*/requestApproval` from the Outbox instead of auto-accepting. Fallback for both: read-only sandbox plus a `proposed_actions` JSON output.
17. **Budgets as policy + incident + approval.** Use soft warn at 80% and a hard stop that pauses the scope. The override is a `kind=budget` approval. Unpriced usage is blocked by default (Paperclip `budget_policies.unpriced_usage_policy`). Store the provider-reported cost when available; otherwise estimate from a versioned rate table and mark `cost_status='estimated'` (Multica `cost_usd_ticks` NULL → client-side pricing).
18. **Run liveness classification** (`advanced / plan_only / empty_response / needs_followup`) to stop L5 loops that "succeed" without progress (Paperclip `RUN_LIVENESS_STATES`).
19. **Orchestrator tick order**: reconcile → validate → fetch candidates → sort → dispatch to free slots, with global and per-executor caps (Symphony §8). It is one authority, single-threaded, so state mutations are serialised.
20. **Thread-bounded context reads for agents** (`comment list --thread --tail N`, cursors). This keeps the prompt size bounded on long tasks (`CLI_AND_DAEMON.md` §Comments).

---

## 8. Executor launch recipes distilled for AWOS (Windows)

| Executor | Non-interactive argv (our default) | Tool-level HITL variant | Parse |
|---|---|---|---|
| claude | `claude -p --output-format stream-json --input-format stream-json --verbose --permission-mode <plan\|acceptEdits\|bypassPermissions per level> --disallowedTools AskUserQuestion --allowedTools … [--model] [--max-turns] [--resume sid] [--bare for triage]` | add `--permission-prompt-tool stdio` (VK) | NDJSON `assistant/user/system/result/control_request` |
| codex | `codex exec --json -s read-only\|workspace-write -C <ws> --output-schema <f> --skip-git-repo-check` (inventory 00) | `codex app-server --listen stdio://` + JSON-RPC `thread/start` → `turn/start`; route `requestApproval` to the Outbox | JSONL events / JSON-RPC notifications |
| pi | `pi -p --mode json [--session <file>] [--model] [--thinking] [--skill <SKILL.md>]` | `pi --mode rpc` (supports mid-run input) | `message_update / tool_execution_* / agent_end` |
| dsh | `dsh --profile <profile> --stdio`, then JSONL `execute` / `cancel` (protocol v1) | the profile defines the tools. **UNVERIFIED** how to author a non-Multica DSH profile (see research/01) | `ready/session/text/tool_call/tool_result/usage/result` |

---

## 9. Open questions / UNVERIFIED

- Whether the Multica server compiles and runs on Windows (inferred from the absence of unix-only imports and from the docs, not built).
- Whether Claude Code's `--permission-prompt-tool stdio` is a stable public contract. Pin the version (2.1.311 per the inventory) and add a contract test.
- DSH profile authoring outside Multica, and the minimum Node version for Pi/DSH (inventory: ≥ 22.19, so a portable Node is needed).
- Current star counts are search-snippet values. Task Master's licence terms and Symphony's current stars were not checked on GitHub directly (unauthenticated API budget).

## 10. Sources

- Multica: `multica:server/pkg/db/generated/models.go`; `server/pkg/db/queries/agent.sql` (`ClaimAgentTask`, `CreateRetryTask`, `ReclaimStaleDispatchedTask*`, `ExtendAgentTaskPrepareLease`, `FailStaleTasks`, `RecoverOrphanedTasksForRuntime`, `ExpireStaleQueuedTasks`); `server/migrations/001_init.up.sql`, `055_task_lease_and_retry.up.sql`, `109_…`, `128_…`, `337_issue_status_open_check.up.sql`; `server/internal/issuestatus/issuestatus.go`; `server/internal/issueworkflow/issueworkflow.go`; `server/internal/service/task.go`; `server/pkg/taskfailure/failure.go`; `server/cmd/server/runtime_sweeper.go`; `server/internal/daemon/{client.go,daemon.go,task_cli.go,processtree/controller_windows.go}`; `server/pkg/agent/{agent.go,claude.go,codex.go,pi.go,dsh.go,proc_windows.go}`; `CLI_AND_DAEMON.md`; `SELF_HOSTING.md`; `SELF_HOSTING_ADVANCED.md`; `LICENSE`; https://github.com/multica-ai
- Paperclip: `paperclip:packages/db/src/schema/{issues,heartbeat_runs,heartbeat_run_events,agent_wakeup_requests,approvals,issue_approvals,issue_thread_interactions,issue_plan_decompositions,tool_access,cost_events,budget_policies,budget_incidents}.ts`; `packages/shared/src/constants.ts`; `server/src/services/tool-approval-execution.ts`; `docs/start/core-concepts.md`; `docs/agents-runtime.md`; `docs/api/approvals.md`; `packages/adapters/{claude-local,codex-local}`; https://github.com/paperclipai/paperclip and issues [#3094](https://github.com/paperclipai/paperclip/issues/3094), [#8457](https://github.com/paperclipai/paperclip/issues/8457), [#2333](https://github.com/paperclipai/paperclip/issues/2333), [#1026](https://github.com/paperclipai/paperclip/issues/1026), [#410](https://github.com/paperclipai/paperclip/issues/410), [PR #9211](https://github.com/paperclipai/paperclip/pull/9211)
- Vibe Kanban: `vibe-kanban:crates/db/src/models/{task,execution_process,coding_agent_turn}.rs`; `crates/executors/src/{approvals.rs,executors/claude.rs,executors/claude/client.rs,executors/codex.rs}`; README sunset banner; https://www.vibekanban.com/blog/shutdown (via third-party summaries such as https://vibecoding.app/blog/vibe-kanban-review); https://www.star-history.com/bloopai/vibe-kanban/
- Symphony: `symphony:SPEC.md` §7, §8, §10.5; https://github.com/openai/symphony; https://www.helpnetsecurity.com/2026/04/28/openai-symphony-codex-orchestration-linear/
- Backlog.md: https://github.com/MrLesk/Backlog.md (README via raw.githubusercontent.com). Task Master: https://github.com/eyaltoledano/claude-task-master and `docs/task-structure.md`
- OpenClaw: https://github.com/openclaw/openclaw, https://docs2.openclaw.ai/platforms/windows. Hermes Agent: https://github.com/NousResearch/hermes-agent, https://hermes-agent.nousresearch.com/docs/user-guide/windows-native, issue [#46332](https://github.com/NousResearch/hermes-agent/issues/46332)
- DBOS: https://docs.dbos.dev/python/examples/agent-inbox, https://www.dbos.dev/blog/new-in-dbos-june-2026. LangGraph: https://docs.langchain.com/oss/python/langgraph/interrupts. Temporal: https://docs.temporal.io/cli/server. Inngest: https://agentkit.inngest.com/advanced-patterns/human-in-the-loop. HumanLayer: https://github.com/humanlayer/humanlayer/blob/main/humanlayer.md
