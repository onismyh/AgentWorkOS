# 01 — Agent runtimes due diligence: Pi, DSH, Super-Agent, dsh-multica-runtime

- Date: 2026-11-05. Method: `git clone --depth 1` into `external/research/<name>` (read-only, nothing built, installed or executed), `npm view` / PyPI metadata queries, and local environment probes (`node -v`, `Get-Command`).
- Snapshots: `pi` @ `469d8651` (2026-11-05, coding-agent 1.5.0) · `dsh` @ `fcc0ad46` (2026-11-04, 0.2.2-rc.1) · `superagent` @ `8c32a9dc` (2026-10-31, 0.1.0) · `dsh-multica-runtime` @ `571d7c03` (2026-10-22, 0.2.0-private.1). Cross-reference only: `multica` @ `f230f9f5` (cloned separately into the same folder; read-only).
- All paths below are relative to `external/research/`. **UNVERIFIED** marks claims taken from docs/code but not exercised.

## 摘要（中文）

四个项目都是 TypeScript/Node 生态。**Pi**（`@earendil-works/pi-coding-agent` 1.5.0，MIT，命令 `pi`）最适合做无头执行器：原生支持 Windows（通过 Git Bash 或可选 `powershell` 工具），提供 `-p` 打印模式、`--mode json` JSONL 事件流、`--mode rpc` 双向 JSONL 协议和进程内 SDK；模型通过 `~/.pi/agent/models.json`（可用 `PI_CODING_AGENT_DIR` 改目录）配置 Anthropic/OpenAI 兼容端点，内置 `deepseek` 提供方（`DEEPSEEK_API_KEY`）。但 Pi **没有任何权限/沙箱系统**，且需要 Node ≥22.19（本机为 22.17，需升级）；本机 Git 在 `E:\Git`，而 PATH 第一个 `bash.exe` 是 WSL 启动器，必须在 settings.json 里显式设置 `shellPath`。**DSH**（`@deepseek-ai/dsh`，MIT，命令 `dsh`，开发者预览、频繁破坏性变更）提供 `dsh --profile headless --json` 一次性 NDJSON 运行、`--profile sdk` JSON-RPC、`--profile acp`，原生 Windows（pwsh + ACL 受限令牌沙箱，`workspace-write` 默认），0.2.2-rc.1 要求 Node ≥24.9；另有 Python SDK（`deepseek-harness-sdk` 0.2.1rc2，自带 win_amd64 运行时，无需系统 Node）。注意 DSH 默认向 DeepSeek 官方 API 上传会话日志、并有 OTel 反馈遥测，需显式关闭。**Super-Agent** 是 DSH bundle 形式的研究原型，仅支持 POSIX（tmux、`sh`、fcntl），在本机只能走 WSL2；其价值在于设计：Source→Trigger→Thread→Eval（确定性 gate + 模型 assessment）→Decision→Ticket（不可变 Mandate）→Assignment→Executor，配合 Subscription 扇出、off/observe/auto 接单模式、Policy 版本与重新校验、结构化 Report 与单问题 Attention（HITL）、持久化 Effects/Outbox，非常值得在我们的任务中心复用。**dsh-multica-runtime** 揭示 Multica daemon 的驱动方式：对每个 CLI 使用其原生机器协议（`claude -p --output-format stream-json`、`codex app-server`、`pi -p --mode json`、ACP），对 DSH 则通过自定义 JSONL v1 协议 `dsh --profile multica --stdio`（`--probe`/`--list-models`、`execute`/`cancel` 命令、`session/text/thinking/tool_call/tool_result/usage/result` 帧）。

## 0. Comparison at a glance

| | Pi | DSH | Super-Agent | dsh-multica-runtime |
|---|---|---|---|---|
| Package / bin | npm `@earendil-works/pi-coding-agent@1.5.0`, bin `pi` → `dist/bundle/cli.js` | npm `@deepseek-ai/dsh` (latest `0.2.1-rc.5`, next `0.2.2-rc.1`), bin `dsh` → `lib/bin.js` | not published (`"private": true`), install as DSH bundle from source | not published (`"private": true`, `UNLICENSED`) |
| Node | `>=22.19.0` | `>=24.9.0` (0.2.2-rc.1; 0.2.1-rc.5 declares none) | `^22.19.0 \|\| >=24.0.0` + `engines.dsh ^0.2.1-alpha.2` | `^22.19.0 \|\| >=24.0.0` + `engines.dsh >=0.2.1-rc.2 <0.3.0` |
| Native Windows | Yes (Git Bash; optional `powershell` tool) | Yes (pwsh tool, ACL sandbox "partial") | No (tmux, `sh -c`, `fcntl`, `#!/bin/sh` shims) → WSL2 | Inherits DSH (Multica has Windows code paths) UNVERIFIED |
| Headless | `-p`, `--mode json`, `--mode rpc`, SDK `createAgentSession` | `--profile headless [--json]`, `--profile sdk` (JSON-RPC), `--profile acp`, Python SDK | DSH web profile + HTTP management API | `dsh --profile multica --stdio` JSONL v1 |
| Permissions | None built-in (extensions can gate) | Sandbox presets `read-only`/`workspace-write`/`danger-full-access` + approval `ask`/`never` | Workers run `danger-full-access` | Auto-approves every approval request |
| License | MIT | MIT | MIT | UNLICENSED (private) |

---

## 1. Pi coding agent (`pi`)

### 1a. Requirements and Windows
- Node `>=22.19.0` (`pi/packages/coding-agent/package.json` `engines`; bundle target `node22.19` in `pi/scripts/build-coding-agent-bundle.mjs:98`; README "Pi requires Node.js 22.19 or newer"). **This box has Node v22.17.0 at `E:\nodejs\node.exe` → must upgrade or run a side-by-side Node.** Old package `@mariozechner/pi-coding-agent@0.73.1` is deprecated ("please use @earendil-works/pi-coding-agent", npm).
- No tmux, gh, pnpm or python needed. tmux only matters for interactive use (`docs/tmux.md`).
- Native Windows is first-class (`pi/packages/coding-agent/docs/windows.md`): Bash resolution order is (1) `shellPath` in `~/.pi/agent/settings.json`, (2) `%ProgramFiles%\Git\bin\bash.exe`, (3) `%ProgramFiles(x86)%\Git\bin\bash.exe`, (4) `bash.exe` on `PATH`. Optional `powershell` tool (`pwsh.exe` → `%ProgramFiles%\PowerShell\7\pwsh.exe` → `powershell.exe`), started with `-NoProfile -NonInteractive -ExecutionPolicy Bypass`; enable with `"defaultTools": ["-bash", "+powershell"]`.
- **Local finding:** Git is at `E:\Git\bin\bash.exe` (not Program Files) and the first `bash.exe` on PATH is `C:\Windows\system32\bash.exe` (legacy WSL launcher). Pi would therefore fall through to the WSL launcher. Set `"shellPath": "E:\\Git\\bin\\bash.exe"` explicitly. No `pwsh` 7 installed (only Windows PowerShell 5.1).
- Windows invocation pitfalls observed by Multica (`multica/server/pkg/agent/pi.go:1190-1250`, `pi_invocation_windows.go`): keep the prompt out of argv and send it on stdin (npm `.cmd`/`.ps1` shims re-tokenise argv; `-File` re-encodes stdin in the ANSI code page); always close stdin ("Pi has been observed to wait indefinitely when stdin never reaches EOF", `pi.go:471`). Calling `node.exe <...>\dist\bundle\cli.js` directly avoids the shim problem entirely.
- WSL is not needed. `docs/windows.md` notes WSL path conversion recognises only `/mnt/<drive>`.

### 1b. Install
- `npm install -g --ignore-scripts @earendil-works/pi-coding-agent` (README: lifecycle scripts not required) → bin `pi`. Alternatives: `powershell -c "irm https://pi.dev/install.ps1 | iex"` (pins deps; can install Node; not inspected), Nix. Self-update `pi update`; disable via `PI_SELF_UPDATE_COMMAND`/`selfUpdateCommand`.

### 1c. Headless usage (`docs/cli.md`, `docs/cli-integration.md`, `docs/json.md`, `docs/rpc.md`, `docs/rpc-commands.md`, `docs/sdk.md`)
- Syntax: `pi [options] [--] [@files...] [messages...]`. Piped stdin is prepended to the first prompt. Non-TTY stdin/stdout auto-selects print mode. There is **no `--cwd`**: the process working directory is the workspace (it also scopes `.pi/`, `AGENTS.md`/`CLAUDE.md` discovery and session grouping). **No wall-clock timeout flag**: the orchestrator must enforce one (bash tool has only a model-supplied optional `timeout` in seconds, `src/core/tools/bash.ts:26`).
- **Print** `-p/--print`: final assistant text on stdout; errors on stderr; nonzero exit if final stopReason is `error`/`aborted`; `length` prints partial text + warning + nonzero.
- **JSON** `--mode json`: strict JSONL (split on LF only, strip CR; do not use Node `readline`). First record `{"type":"session","version":3,"id":...,"cwd":...}`, then `agent_start`, `turn_start`, `message_start`/`message_update` (deltas: `text_delta`, `thinking_delta`, `toolcall_start/delta/end`)/`message_end` (authoritative), `tool_execution_start{toolCallId,toolName,args}`, `tool_execution_update`, `tool_execution_end{result,isError,durationMs}`, `turn_end`, `agent_end{messages,willRetry}`, `auto_retry_*`, `compaction_*`, and finally `agent_settled{aborted}`. **Exit code is nonzero only if the invocation throws**; a failed/aborted assistant response must be detected from events (`message.stopReason` ∈ `pending|stop|length|toolUse|error|aborted|deferred`, `errorMessage`; `docs/message-types.md:134`). Usage/cost are on `message_update.usage` and the final message.
- **RPC** `--mode rpc`: long-lived; stdin commands `{"id","type":"prompt","message"}` (response `data.disposition` = `started|queued|handled`), `steer`, `follow_up`, `abort`, `new_session`, `get_state`, `get_messages`, `set_model`, `compact`, `bash`, `get_last_assistant_text`, `switch_session`, `fork`, etc.; events identical to JSON mode (no session header; use `get_state`). Wait for `agent_settled`. Shutdown = close stdin (SIGTERM/SIGHUP also graceful; second signal immediate). Extension dialogs (`select`/`confirm`/`input`/`editor`) arrive as `extension_ui_request` and are answered with `extension_ui_response` (optional auto-resolve `timeout`) — this is Pi's only HITL channel (`docs/rpc-extension-ui.md`). TypeScript `RpcClient` (`src/modes/rpc/rpc-client.ts`, `promptAndWait()`), example `examples/rpc-client.ts`; Python minimal client in `docs/rpc.md`.
- **SDK**: `import { createAgentSession, SessionManager } from "@earendil-works/pi-coding-agent"`; `session.prompt()`, `subscribe()`, `abort()`, `waitForIdle()`, `getLastAssistantText()`, `dispose()`; options `cwd`, `agentDir`, `sessionManager: SessionManager.inMemory()`, `tools`, `customTools`, `resourceLoader`, `settingsManager` (`docs/sdk.md`). Export `./rpc-entry` also exists.
- Useful flags: `--model provider/id[:thinking]`, `--provider`, `--thinking off|minimal|low|medium|high|xhigh|max`, `--api-key`, `--system-prompt`, `--append-system-prompt` (text or file), `--tools`/`-t` allowlist (`read,bash,edit,write,grep,find,ls,powershell`, `+x/-x` edits), `--exclude-tools`, `--no-tools`, `--no-mcp`, `--no-extensions`, `--skill`, `--no-skills`, `--no-context-files`, `-a/--approve` / `-na/--no-approve` (project trust), `--offline` (= `PI_OFFLINE=1`), `--list-models [search]`.
- Preflight: `pi auth check --provider <p> --json` prints `ready|not_ready|invalid`, exit `0|1|2`.

### 1d. Model providers (`docs/providers.md`, `docs/models.md`, `docs/models-configuration.md`)
- Agent dir `~/.pi/agent` (override `PI_CODING_AGENT_DIR`): `settings.json`, `models.json`, `auth.json`, `mcp.json`, `trust.json`, `AGENTS.md`, `SYSTEM.md`, `APPEND_SYSTEM.md`, `extensions/`, `skills/`, `prompts/` (`docs/configuration.md`). Credential precedence: `--api-key` > `auth.json` > `models.json apiKey` > env/ambient.
- Built-in providers (env var): Anthropic `ANTHROPIC_API_KEY` (+ `ANTHROPIC_AUTH_TOKEN` bearer, `ANTHROPIC_OAUTH_TOKEN`), OpenAI `OPENAI_API_KEY`, **DeepSeek `DEEPSEEK_API_KEY`** (provider id `deepseek`, `https://api.deepseek.com`, `openai-completions`; `pi/packages/ai/src/providers/deepseek.ts`), Azure, Bedrock, Vertex, Gemini, OpenRouter, Groq, xAI, Mistral, Moonshot/Kimi, ZAI/GLM, Qwen token plans, MiniMax, Xiaomi, Copilot, Cloudflare, Fireworks, Together, etc. DeepSeek model ids come from the pi.dev catalog (`deepseek-v4-pro`, `deepseek-v4-flash`, `deepseek-v4.1-flash` appear in repo fixtures) — run `pi --list-models deepseek` (exact current ids UNVERIFIED).
- **`ANTHROPIC_BASE_URL`/`OPENAI_BASE_URL` are NOT read** (grep found none; SDK clients receive an explicit `baseURL: model.baseUrl`, `api/anthropic-messages.ts:1270+`). Use `models.json`.
- (i) Anthropic-compatible endpoint: `models.json` → `{"providers":{"my-gw":{"baseUrl":"https://gw.example","api":"anthropic-messages","apiKey":"$MY_GW_TOKEN","models":[{"id":"claude-sonnet-4-5","reasoning":true,"contextWindow":200000,"maxTokens":64000}]}}}`. Re-route the built-in provider only: `{"providers":{"anthropic":{"baseUrl":"https://proxy.example"}}}`. `apiKey`/header values accept literal, `$NAME`/`${NAME}`, or `!command`. Bearer auth: `"authHeader": true` or `"headers":{"Authorization":"Bearer ${MY_GW_TOKEN}"}` (behaviour of `authHeader` with `anthropic-messages` UNVERIFIED). Note the Anthropic SDK appends `/v1/messages` to `baseUrl` (so normally no trailing `/v1`; the doc's proxy example uses `/v1` — verify per gateway). Compat switches under `compat` (e.g. `supportsCacheControlOnTools:false`, `supportsEagerToolInputStreaming:false`, `forceAdaptiveThinking`).
- (ii) OpenAI-compatible: `"api":"openai-completions"` (or `openai-responses`), `"baseUrl":"https://host/v1"`, `compat` such as `supportsDeveloperRole:false`, `maxTokensField:"max_tokens"`, `thinkingFormat:"deepseek"|"qwen"|...`.
- (iii) DeepSeek: just `DEEPSEEK_API_KEY` + `--model deepseek/<id>`.
- Subscription reuse: Pi has its own OAuth flows (`pi/packages/ai/src/auth/oauth/`): `anthropic` (Claude Pro/Max, `claude.ai/oauth/authorize`), `openai-codex` (ChatGPT, browser or device code), `openai-chatgpt`, `github-copilot`, `kimi-coding`, `xai`, `openrouter`, `radius`, via `/login` in interactive mode (stored in `auth.json`). It does **not** import `~/.codex/auth.json` or Claude Code credentials (no such code found; both exist on this box). A one-time interactive `/login` (device code works headless) is required, after which `-p`/json/rpc reuse `auth.json`. A Claude OAuth token string containing `sk-ant-oat` passed as `ANTHROPIC_OAUTH_TOKEN`/key is sent as bearer with Claude Code identity headers (`api/anthropic-messages.ts:1251-1308`) — code path verified, use UNVERIFIED and subject to provider terms. `pi auth print-bearer-token --provider openai-codex` can export tokens to other clients.

### 1e. Tools, permissions, extensions, MCP
- Default tools `read,bash,edit,write`; also `grep,find,ls,powershell`; built-in extensions `codemode` (QuickJS sandbox scripts calling tools) and `tool_search` (off by default) (`docs/cli.md`).
- **No permission system or sandbox** (README "Permissions & Containerization"; `docs/security.md`: "does not ask for approval before every tool call"). Gates are DIY via extensions hooking `tool_call` (`examples/extensions/permission-gate.ts`, `protected-paths.ts`, `confirm-destructive.ts`); in non-UI modes the example blocks dangerous commands (`ctx.hasUI` false). Isolation options: Gondolin micro-VM extension, Docker, OpenShell (`docs/containerization.md`).
- Project trust (`docs/security.md`): `.pi/settings.json`, `.pi/mcp.json`, `.pi/extensions|skills|prompts|themes`, `.pi/SYSTEM.md`, project `.agents/skills` load only when trusted; json/rpc/print cannot prompt, so `defaultProjectTrust: "ask"|"never"` skips them; use `--no-approve` explicitly. `AGENTS.md`/`CLAUDE.md` always load unless `-nc`.
- Extensions: TypeScript modules (`pi.registerTool`, `pi.on("tool_call")`, commands, providers, MCP servers) from `~/.pi/agent/extensions/`, `.pi/extensions/`, `-e`; packages via `pi install npm:...|git:...` (`docs/extensions.md`, `docs/packages.md`). Skills: Agent Skills spec, `SKILL.md` with `name`/`description`, from `~/.pi/agent/skills`, `.pi/skills`, `~/.agents/skills`, `.agents/skills`, `--skill` (`docs/skills.md`).
- MCP: built-in (`builtin:mcp`), stdio and streamable HTTP, `~/.pi/agent/mcp.json` / `.pi/mcp.json` (`{"mcpServers":{...}}` standard format), CLI `pi mcp add|remove|list|login|logout`, tools named `mcp__<server>__<tool>`, exposure modes incl. `deferred` (`docs/mcp.md`).
- Shell tool env: `PI_SESSION_ID`, `PI_SESSION_FILE`, `PI_PROVIDER`, `PI_MODEL`, `PI_REASONING_LEVEL`; process markers `AI_AGENT=pi`, `PI_CODING_AGENT=true` (`docs/environment-variables.md`).

### 1f. Sessions
- JSONL tree files at `~/.pi/agent/sessions/--<cwd with / \ : → ->--/<timestamp>_<session-id>.jsonl` (`docs/session-format.md`). Override with `--session-dir` > `PI_CODING_AGENT_SESSION_DIR` > `sessionDir`. `--no-session` = in-memory. Resume: `-c/--continue` (latest for cwd), `--session <path|id>`, `--session-id <id>` (open-or-create in current cwd; `[A-Za-z0-9._-]`, alnum at both ends), `--fork`. A session's cwd is fixed; resuming elsewhere errors in non-interactive modes (`docs/sessions.md`). Automatic compaction (`compaction.*` settings).

### 1i. Risks
- Very fast cadence: 1.0.0 on 2026-10-01 → 1.5.0 on 2026-11-02; 4 "Breaking" headings since 0.99 in `CHANGELOG.md` (e.g. tool-argument validation moved to `pi-schema` in 1.5.0). Package rename in 2026-05. Pin exact versions.
- New-contributor issues/PRs auto-closed (README). Telemetry: install/update telemetry on by default (`enableInstallTelemetry`, `PI_TELEMETRY=0`), version check (`PI_SKIP_VERSION_CHECK`), model-catalog refresh from pi.dev (`--offline`).
- No sandbox: any worker can touch everything the OS user can. License MIT.

---

## 2. DSH / DeepSeek Harness (`dsh`)

### 2a. Requirements and Windows
- Node `>=24.9.0` (`dsh/package.json`, `dsh/apps/cli/package.json:179`; npm `@deepseek-ai/dsh@0.2.2-rc.1` engines `>=24.9.0`; `0.2.1-rc.5` declares none). Uses `node:sqlite` (`packages/storage/storage-sqlite`, `session-query-sqlite`). Desktop app has Windows x64 packaging scripts (`package:desktop:win:x64`).
- pnpm is needed only for `dsh plugin` (forwards to pnpm; must be on PATH; `apps/cli/reference/README.md` "Plugin management"). No tmux/gh/python required for the CLI (`dsh-tmux-context` is optional context).
- Native Windows: base bundle swaps POSIX rows for PowerShell rows by `process.platform` (`packages/bundle/base/cordis.patch.yml:249-287`: `bash-sandbox`/`tool-bash` disabled on win32, `pwsh-sandbox`/`tool-pwsh` enabled). PowerShell executor resolves `pwshPath` → well-known locations → PATH → Windows PowerShell 5.1 fallback (`packages/shell/pwsh-local/README.md`; non-ASCII stdin under 5.1 may mis-decode). Sandbox on win32 = ACL restricted-token runner via `koffi` FFI (`packages/sandbox/sandbox-windows-acl`), enforcement reported as **`partial`** (hard links, unconfined reads); Linux uses bwrap/Landlock, macOS Seatbelt (`packages/sandbox/sandbox-local/README.md`). Native addon `@deepseek-ai/node-addon-system` has no win32 build but is POSIX-only functionality (Landlock/flock).
- Windows limitation: headless `--add-dir` grants are rejected by the ACL backend when a subprocess tool executes (`packages/bundle/headless/README.md`, "Windows extra roots"). This breaks the documented "grant the shared `.git` dir of a worktree" pattern on Windows.
- Python SDK alternative: PyPI `deepseek-harness-sdk` **0.2.1rc2** (pre-release only; `pip install --pre`), pulls `deepseek-harness-runtime-bin` with a `win_amd64` wheel; "Normal SDK execution needs no system Node.js" (`docs/user/guide/python-sdk.md`, `python/sdk/README.md`; Python ≥3.10).

### 2b. Install
- `npm install -g @deepseek-ai/dsh@<version>` or `npx @deepseek-ai/dsh web` → bin `dsh` (`lib/bin.js`). dist-tags: `latest 0.2.1-rc.5`, `next 0.2.2-rc.1`, `alpha 0.2.2-alpha.3`. `--ignore-scripts` compatibility UNVERIFIED.

### 2c. Headless usage (`apps/cli/README.md`, `apps/cli/reference/README.md`, `docs/cli-help.md`, `packages/bundle/headless/README.md`, `packages/sdk/protocol/README.md`)
- Launcher: `dsh [--profile] <name> [--from-default-profile <tpl>] [--patch <file>]... [--dump-config|--dump-default-config|--dump-config-schema] [app-args...]`. Launcher flags must precede app args. Profiles live at `$DSH_HOME/profiles/<name>` (`package.json` with `dsh.profile.bundles` + `cordis.patch.yml`); shipped templates `web`, `headless`, `sdk`, `sdk-minimal`, `acp` auto-initialise. Layer order: bundle patches → profile `cordis.patch.yml` → `$DSH_HOME/cordis.patch.yml` → `--patch` overlays (later wins per row; a patch **replaces** a row's whole `config`).
- **One-shot**: `dsh --profile headless [--json] [--session-id <id>] [--permission-mode <preset>] [--approval-policy ask|never] [--add-dir <abs>]... [task...|-]`. Task from argv, or stdin when omitted/`-`. cwd = workspace root (recorded in the session). Default mode: final text on stdout, reasoning deltas on stderr under `dsh: reasoning:`. **Exit 0 iff final `turn/end` reason is `completed`, else 1**; failures print `dsh: <code>: <message>` on stderr.
- `--json` NDJSON on stdout (`packages/bundle/headless/src/json-stream.ts:255-330`): `{"type":"session","sessionId","cwd"}`, `{"type":"status","phase":"turn_start|step_start|step_end|turn_end", turn, step, usage?, reason?}`, `{"type":"thinking","text"}`, `{"type":"text","text"}` (per committed step, not per token), `{"type":"tool_call","callId","tool","input"}`, `{"type":"tool_result","callId","status":"completed|error","result","error?"}`, terminal `{"type":"final","sessionId","reason","text"}` (lossless); out-of-turn failure → `{"type":"error",...}` without `final`. Strings capped at 8 KiB, lines at 32 KiB (except `final`). Supervisor rule from the README: require exit 0 **and** `final.reason.kind === "completed"`; a `tool_result` "completed" may still wrap a nonzero shell exit.
- No timeout flag. Signals: first SIGINT/SIGTERM = graceful drain up to 5 s, **SIGTERM exits 0**, SIGINT 130, second signal forces exit — so exit 0 alone is not success. On Windows `child.kill()` is a hard terminate (UNVERIFIED how DSH flushes then). Interrupted runs: await exit before resuming the session.
- Headless approvals: no live answerer → requests resolve `unavailable` and fail closed; `--approval-policy never` rejects deterministically (`packages/interaction/user-approval/README.md`).
- **SDK** `dsh --profile sdk` (or `sdk-minimal`): JSON-RPC 2.0 over stdio, newline-delimited. Client→server `initialize` (`provider`, `model`, `reasoningEffort`, `maxTokens`, `approvalRequests`, `capabilities.userQuestions`), `session/prompt`, `session/query`, `session/resume`, `session/wait`, `session/working-directory/get|set`, `shutdown`; server→client `approval/request` (→ `allowed-once|rejected|cancelled`), `user-question/ask`, notifications `session.event`, `session.status`, `session.query.completed`, `subagent.started/finished`. TS client `@deepseek-ai/dsh-sdk-client`; Python `DeepSeekHarness(dsh_home=..., cwd=..., provider=..., model=..., max_tokens=..., profile="sdk"|"sdk-minimal").run(prompt, session_id=...)` → `RunResult.final_response/finish_reason`, `resume_session()`, `request_timeout_seconds`, `approval_requests=True`. This is the path for real HITL (approvals answered by our orchestrator).
- **ACP** `dsh --profile acp` (Agent Client Protocol over stdio). Web UI `dsh web` (`--host`, `--port` default 3080, `--no-open`, TLS flags).
- Inspection without boot: `dsh --profile headless --dump-config`, `--dump-config-schema`. Startup failures write `$DSH_HOME/logs/startup-<ts>-<uuid>.log`.

### 2d. Model providers (`docs/user/guide/providers.md`, `packages/llm/*/README.md`, `packages/bundle/base/cordis.patch.yml`)
- Default route: `agent-default-model` → `provider: deepseek-official`, `model: deepseek-flash`. Built-in DeepSeek catalog: `deepseek-flash` (DeepSeek-V4.1-Flash, text+image, 1,048,576 ctx), `deepseek-v4-pro` (`packages/llm/llm-deepseek/src/models.ts`); with a key and the official endpoint the list follows `GET https://api.deepseek.com/models`.
- **(iii) DeepSeek**: `DEEPSEEK_API_KEY`; the native adapter speaks **Anthropic Messages** at `https://api.deepseek.com/anthropic`; override with config `baseURL` or `DEEPSEEK_BASE_URL` ("a Messages-compatible root") (`packages/llm/llm-deepseek/README.md:57,83`). Signed-in DeepSeek accounts (`llm-deepseek-account`) are disabled in headless.
- **(i)/(ii) Other endpoints** go through `@deepseek-ai/dsh-llm-pi-ai` (row id `llm-pi-ai`; it wraps Pi's `pi-ai` library). Patch:
  `- id: llm-pi-ai` / `config: providers: <id>: {api: anthropic-messages|openai-completions|openai-responses, baseURL, apiKeyEnv: <ENV>, headers?, compat?, models: [{id, contextWindow?, maxTokens?, input?, reasoningEfforts?}], retryPolicy?}`; then point `agent-default-model` at `{provider: <id>, model: <id>}`. Built-in catalog providers (`anthropic`, `openai`, `moonshotai`, `zai`, ...) need only `apiKeyEnv` (+ optional `baseURL` override). OpenAI-compatible gateway fixes: `compat.supportsDeveloperRole: false`, `compat.maxTokensField: max_tokens`, `compat.thinkingFormat: deepseek`. Profile path written by the Web UI: `$DSH_HOME/profiles/<profile>/cordis.patch.yml`.
- Credential resolution (`apps/cli/reference/README.md` "Shared deployment behavior"): inherited env, `$DSH_HOME/.credentials.yaml`, `<cwd>/.env`, `$DSH_HOME/.env`. **Headless disables the managed credential store**: env → `<cwd>/.env` → `$DSH_HOME/.env` only; GUI-saved keys/sign-ins ignored (re-enable rows `credentials`, `authorization`, `deepseek-account`).
- Codex/Claude login reuse: pi-ai OAuth sign-in (e.g. `openai-codex`) exists via the harness authorization seam but needs the credential store (Web/SDK), not headless. Separately, optional bundles `@deepseek-ai/dsh-subagent-codex` and `@deepseek-ai/dsh-subagent-claude-code` (`dsh plugin --profile <p> add ...`) run genuine unattended Codex / Claude Code sessions as delegation tools using their **native** auth (Claude bundle ships a pinned runtime, never the host `claude`) (`packages/subagent/subagent-*/README.md`).
- Proxy: `HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY`/`NO_PROXY` (no SOCKS, no CIDR in NO_PROXY); a project `.env` setting proxy vars makes DSH **refuse to start** (`docs/user/guide/network-proxy.md`).

### 2e. Tools, permissions, extensions, MCP
- Base tools: fs (`read`/`edit`/`write`, optional `apply_patch`), fs-search, bash (POSIX) or pwsh (Windows), jobs, todo, goal, plan-mode, `web_search`/`web_fetch` (DeepSeek Search API by default; fetch runs without per-call confirmation and can reach private hosts), subagent (`spawn`/`fork`), agent-team, workflow (PTC), skills, compaction, `ask_user_question` (`packages/bundle/base/cordis.patch.yml`).
- Permission presets (row `permission`): `read-only` (sandbox read-only, approval ask), `workspace-write` (default; writes limited to workspace + temp, reads/network not confined), `danger-full-access` (approval never). Process fallback `DSH_PERMISSION_MODE`. Experimental `--permission-mode auto --approval-policy never` = model-reviewed full access (`packages/experimental/auto-review`).
- Everything is a Cordis plugin; profiles compose YAML rows; third-party bundles via `dsh plugin --profile <p> add <npm|git|path>`; compatibility enforced via `engines.dsh` (prerelease-inclusive, `packages/boot/app-boot/src/plugin-compatibility.ts:52`).
- Skills (`packages/skill/skill-filesystem` README): `<project>/.dsh/skills`, `<project>/.agents/skills`, custom, `$DSH_HOME/skills`, `$DSH_AGENTS_HOME|~/.agents/skills`; `SKILL.md` or flat `<name>.md`. Instructions: `AGENTS.md`/`CLAUDE.md` (65,536-byte budget).
- MCP: `@deepseek-ai/dsh-mcp-client` rows (`serverName`, `transport: stdio|streamable-http`, `command/args/env/cwd` or `url/headers`, `toolCallTimeoutMs` 60 s, reconnect); none enabled by default; tools `mcp__<server>__<tool>` (`packages/mcp/mcp-client/README.md`).
- Subprocesses get a credential-scrubbed env (`*KEY*`, `*TOKEN*`, `*SECRET*`, `*PASSWORD*` removed; see `dsh-multica-runtime/src/environment.ts` comments).

### 2f. Sessions
- JSONL logs at `$DSH_HOME/sessions` (`session-persistence-jsonl` row, `root: dshHomePath('sessions')`); `DSH_HOME` default `~/.dsh` (`packages/util/home-paths/README.md`). Storage KV at `$DSH_HOME/storages`; SQLite search index in-memory/off by default. Headless resume: `--session-id <id>` from the same cwd, same preset; refuses unknown ids, other cwd, subagent sessions, live sessions. SDK: `session/resume`.

### 2i. Risks
- "Developer preview ... THERE WILL BE COMPATIBILITY-BREAKING CHANGES" (README); 12 upgrade-guide folders between v0.1.7-rc.2 and v0.2.2-alpha.3 (`docs/upgrade-guide/`); only RC/alpha releases on npm, PyPI pre-release only.
- **Data egress defaults**: `session-log-deepseek` is on by default and sends "complete unaccepted log suffixes" with every request to the official DeepSeek API (opt out `- id: session-log-deepseek` / `config: {enabled: false}`); OTel session upload `FEEDBACK_ONLY` to `https://dsh-otel-collector.deepseeksvc.com/v1/logs` (opt out `DSH_TELEMETRY_DISABLED=1` or `DSH_TELEMETRY_MODE=DISABLED`); anonymous id in `$DSH_HOME/.anonymous-user-id` (`packages/bundle/base/cordis.patch.yml`, `packages/session/session-log-deepseek/README.md`).
- `SAFETY.md`: not audited, sandbox not a guarantee. Workspace `.env` files are read as an env layer (credential injection vector). Windows sandbox enforcement `partial`. License MIT; 323-line `THIRD_PARTY_NOTICES.md`.

---

## 3. Super-Agent (`dsh-superagent-prototype`)

### 3a/b. Requirements, Windows, install
- "Research prototype for reference only, not an official DeepSeek product" (README). Prereqs: Node 22.19+/24+, pnpm, git 2.32+, Python 3.8+, **tmux**, GitHub CLI 2.48+, DSH within `engines.dsh ^0.2.1-alpha.2`, `lark-cli` for Feishu (`SETUP.md`, `docs/006-deployment.md`). POSIX-only in practice: `scripts/dsh-superagent` (Python, `import fcntl`, runs `dsh --profile superagent` inside tmux session `dsh-superagent`), prepare command executed as `sh -c` (`src/executor/dsh.ts:254`), `#!/bin/sh` command shims (`src/infra/environment.ts:89`), executor `directory` must start with `/` (`src/policy.ts`), wrapper scripts in `~/.local/bin`. **Windows path: WSL2 Ubuntu only.** Uses `node:sqlite` (`src/app/store.ts:2`).
- Install (from source; not on npm): `npm install -g @deepseek-ai/dsh@0.2.1-rc.3`; `pnpm install && pnpm build`; `dsh superagent --from-default-profile web --dump-config`; `dsh plugin --profile superagent add "link:$PWD"`; copy `examples/profile.cordis.patch.yml` (`webserver` 127.0.0.1:3082, `dsh-superagent.stateDir`); `scripts/dsh-superagent start|status|stop|restart` (exit 0 ok, 1 down, 2 part error, 3 waiting for setup).

### 3g. Architecture and design (docs 000–008, `src/`)
**Philosophy (`docs/000-philosophy.md`)** — core principle: "Take as little human attention as possible, and give as much value as possible." Every message, notification, question, setting, panel element and doc paragraph must give more value than the attention it costs, or it is removed.
- Agent behaviour: (1) acts only when asked by an allowed person (or in a conversation opened to requests), or when a team-written rule allows it — and unrequested work starts in a mode that only records what it *would* do; (2) speaks only when a person must act or can use a result: confirm a request once naming what it will do, stay quiet while working, write again for a result, a needed decision or an inability to finish; short messages, in the conversation where the request was made; (3) asks one concrete, structured question at a time; (4) stops by impact, not effort — a local change goes as far as a reviewable result, a change needing a design decision stops with a clear question; (5) reports honestly — drafts marked unfinished, failures reported as failures, limits never bypassed to avoid asking.
- Design rules: few concepts (a business need becomes an attribute of an existing concept or prompt text, never a new concept); few settings (a default replaces a setting when one value suits almost every team; behaviour a team describes in words is prompt text); a panel that shows only what needs a person (healthy state takes no space); documents read in order.
- Operator responsibility: start narrow (short allowed list, proactive work recorded only), tune for less noise (shorter/stricter prompts or pause, never more messages), never use it to produce volume, own what it says.

**Domain (`docs/001-domain.md`, `src/core/model.ts`)** — flow Source → Trigger → Thread → Eval → Decision → Ticket (Mandate) → Assignment → Executor, replies via Channel.
- **Source**: kind (implementation) + instance (config); owns its read cursor and bot identity; drops its own messages and out-of-scope events before the domain.
- **Trigger**: immutable event `{id, source, threads[] (first = primary, hosts the Eval), actor{source,id}, human, addressed?: mention|direct, at, text, channel?, url?, meta}`; `text`/`meta` are untrusted; accepting a Trigger twice yields one Ticket.
- **Thread**: ordered triggers; at most one running Eval; holds Subscriptions, a `pending` debounce buffer and Decision history; **Correlation** (threads sharing a trigger) adds context, never routes.
- **Eval / Decision**: gate + assessment, stateless; Decision `accept|reopen|stop|ignore|unavailable` with coded `reason[]`, `strength explicit|inferred`, `policyVersion`, `audit`.
- **Ticket**: only Eval creates it; immutable **Mandate** `{strength, policyVersion, basis{via: addressed|followup|admission|listed|bot, sender, rules, scope, row}, actor, scope}`; holds `channels`, `inputs`, `undelivered`, `attention`, `follows` (e.g. its PRs), `ended{by: executor|human, outcome: completed|merged|closed|dropped|stopped}`; defines its own state independent of the agent session.
- **Assignment**: Ticket↔Executor binding, `pending→running→ended`, `report{state: progress|waiting|ready|blocked|failed|dropped, summary}`; a ticket has several over time.
- **Subscription**: many-to-many Ticket↔Thread; later triggers fan out to live tickets as context without Eval; a person's message resumes an ended (not human-stopped) ticket under the same Mandate; bots/CI never wake ended tickets.
- Rejected concepts (documented with reasons): cross-Source identity mapping (prompt text instead), Eval scheduling its own delayed triggers (waiting conditions are Sources), at-most-one-ticket-per-thread, cancelling running tickets on policy change, inferring "waiting on a person" from free text.

**Authorization (`docs/002-authorization.md`, `src/policy.ts`)** — the host (code) enforces allowed lists, rules and Mandates; message content can never change permissions.
- **Path 1 — Eval (new work).** A deterministic **gate** yields strength:
  - an allowed person addressing the agent (`mention`/`direct`) = **explicit**;
  - a follow-up on a thread that has tickets = explicit (listed sender) or inferred (unlisted);
  - otherwise the per-Source **admission table**: rows = receiving scopes (GitHub `issues|pulls|comments|ci`; Feishu chat ids, `other`, `direct`); cells `listedUnaddressed` (inferred|ignore, default inferred), `unlistedAddressed` (explicit|inferred|ignore, default ignore), `unlistedUnaddressed` (inferred|ignore, default ignore); unaddressed cells exist only where the Source observes all messages;
  - listed bots → inferred at most, never explicit, never stop; other bots not admitted.
- **Path 2 — Subscriptions (existing work).** Deliver context, bypass the gate, grant nothing. A person's follow-up on a ticket's own thread is *owned* by that ticket's Executor, which handles it in scope or `handback`s it once with a reason; only unowned messages go to Eval; while owned, the main Session may only stop. Unread follow-ups are released to Eval when a ticket ends.
- **Accept mode** (part of Policy): `off` (no inferred candidates) · `observe` (default; assessed, positive outcome recorded as ignore "would accept") · `auto` (positive assessment accepts). Explicit requests work in every mode. Each assessment judges every enabled rule exactly once; a proactive accept needs ≥1 matching rule; a matched rule's process/communication instructions override defaults for that ticket only.
- **Policy version** = hash of rules + accept mode + Source policy parts (`src/policy.ts:117`). A change discards running Evals and requeues their triggers, and sends a `policy-changed` Input (rule changed/disabled, requester no longer allowed, proactive no longer accepted) only to affected live tickets.
- **Revalidation before resume** (Mandates are never revoked mid-run): explicit follow-up passes; inferred needs `auto` + matched rules still enabled; table-admitted needs ≥ original strength under today's table; bot must stay listed; others need the sender still allowed; the Source must still listen to the thread.
- Only three operations change a Mandate: accept creates it, a person's stop withdraws it, failed revalidation revokes it. "Stop" written in a thread is an ordinary Trigger decided by Eval; panel Stop is a command.
- Eval timeout counts from delivery to the main Session; expiry requeues twice, the third records `unavailable` (health `evals`); the panel asks a person only for recent, latest, undismissed `unavailable` Decisions.

**Execution (`docs/003-execution.md`)**
- One Executor Session `worker-<ticket id>` and one directory per ticket under `<stateDir>/work`; optional prepare command (once, `sh`, 10 min, e.g. `git worktree add -b agent/$DSH_SUPERAGENT_NAME "$DSH_SUPERAGENT_DIR" ...`).
- Ticket **stages** (first match wins): person/delivery (Input undelivered) → person/answer (has Attention) → person/review (latest Report `ready`) → person/blocked|failed (no human message since) → working/queued → idle (waiting/ended) → working.
- Workers run `danger-full-access`; foreground shell commands move to background jobs after 5 s so new messages are taken at the next step; background work gets 30 min after a turn, then jobs are killed and the ticket reported waiting.
- Concurrency `maxWorkers` (default 3) + one extra slot reserved for a person's message/status request, so new work never blocks a human continuing existing work.
- Execution variables (no credentials): `DSH_SUPERAGENT_DIR|NAME|TICKET|BLOCKED_COMMANDS`, `DSH_SUPERAGENT_GITHUB_REPOSITORY|ACCOUNT|COMMIT_EMAIL|NUMBER|PR_HEAD`, `DSH_SUPERAGENT_FEISHU_BOT|CHAT`.
- Command shims `<stateDir>/worker-bin` (agent `gh`/`lark-cli`, blocked commands) and `<stateDir>/worker-gitconfig` (agent-only credentials) — explicitly "defaults, not a security boundary".
- Transient provider/network errors (408/429/5xx/reset/timeout) retried 3× with growing backoff; failed prepare is not retried.
- Restart: running Assignments end "host restarted"; replacements start with a recovery note ("check directory, git history and GitHub before repeating any outside action"), oldest ticket first.
- Ticket end: `ready` without PR = completed; `dropped` = nothing to do (silent); with PRs, ends when its own PRs are closed/merged; ended tickets keep Subscriptions so a later human comment resumes them.
- "Stored work first, done afterwards": every side effect is a durable Effect row.

**HITL**
- The Executor calls `orchestrator_report{state, summary, human_action?: one concrete question (≤1000 chars), question_for_trigger_id?, pr_number?, reply_to_trigger_id?}` (`src/app/tools/executor.ts:75-100`). The question becomes the ticket's `Attention` (stage person/answer); the main Session forwards it unchanged to the conversation where the asking person wrote (else the ticket's origin); already-authorized work may continue; the person's reply arrives as a follow-up Trigger owned by that ticket.
- Other worker tools: `orchestrator_ack`, `orchestrator_thread_unrelated`, `orchestrator_thread_follow`, `orchestrator_trigger_handback`. Main Session tools: `orchestrator_route` (batch accept/attach/stop/ignore, ≤20 items, each independent), `orchestrator_eval`/`_read`/`_decide`, `orchestrator_reply` (purposes acknowledgement|answer|result|attention), `orchestrator_ticket_retry|request_status|note`, `orchestrator_trigger_requeue`, `orchestrator_settings_save`, `orchestrator_prompts_save`, `orchestrator_read`.
- People's levers: panel Stop (withdraws the Mandate), Retry (new Assignment in the same session/dir/branch), Evaluate again, Pause processing (stops dispatch/Evals/delivery/replies while Sources keep recording; a person's pause holds across restarts).

**How it decides to act** — main Session (`dsh-superagent-router` preset) only routes (notes ≤30 words, no technical decisions, never waits: guard refuses `sleep`, shell timeouts >30 s, job waits >5 s). Assessment prompt (`prompts/admission.md`): establish three facts (targets this project; intent to have a specific change implemented; direction settled for a small local edit) with source quotes in `intent_evidence`; return `rule_assessments` with exactly one `{rule_id, matched, reason}` per enabled rule; every decision carries `direction{implementation_requested, target_basis, requires_design_decision, evidence}`; uncertainty → ignore quietly; ticket scope = small internal change. Default rules: `copy` (typo/wording fixes), `visual` (local spacing/alignment with an established target) (`src/policy.ts:62-65`).

**Prompts (`docs/004-prompts.md`)** — team sections: Communication, Ticket scope, Delivery requirements, Team skills; overridable built-in parts `runtime/shared|orchestrator|worker|subscriptions|replies|dispatch|work|admission|handoff/*|environment|policy`, `skill/github|feishu`; versioned (revision, base revision, conflict refusal); Policy section regenerated each request and placed last for cache stability. Strength order: host-enforced facts > built-in/generated sections > matched-rule process > team sections/skills > outside text (data only).

**Panel (`docs/005-panel.md`)** — answers in order: "Does anything need me now?", "Is the system healthy?", "History of one piece of work". Tabs Overview/Source/Trigger/Eval/Execution/Settings; principles: counts equal list lengths, healthy states take no space, one place per concept, destructive actions confirm.

**Config schema (`src/policy.ts`, `docs/006-deployment.md`)** — blocks: `source:github {enabled, config{repository, cli, observeIssues, observePulls, observeComments, observeCi}}`, `source:feishu {config{cli, chats[{chatId,name,listen: mentions|all}], directMessages, otherChats: none|mentions}}`, `policy {rules[{id,name,enabled,instruction}], sources{<kind>{allowed[{id,name?}], bots[], admission{<row>{cells}}, syncChat?}}}`, `eval {mode: off|observe|auto (default observe), timeoutSeconds (120), model{provider,model,reasoningEffort?}|null}`, `executor {model, maxWorkers (3), directory, prepare, blockedCommands}`, `paused`. Management API `http://127.0.0.1:<port>/orchestrator/api/` with header `x-orchestrator-request: 1`: `GET state|settings|sources`, `PUT settings/<block> {value, revision}` (409 on stale revision), `POST executor/pause`, `POST setup/check`, `POST policy-sync/<id>`, `GET|PUT prompts`, `POST tickets/<id>/retry|stop`. State: one SQLite DB `<stateDir>/orchestrator.sqlite` + directory lock.

**Source interface (`docs/008-cookbook.md`, `src/sources/kind.ts`)** — a `SourceKind<C>` object registered by a plugin:
- identity/config: `kind` (stable id, prefixes Trigger/Thread keys), `title`, `description` (zh/en), `schema` (zod; defaults must receive nothing), `fields: FieldSpec[]` (auto-generated settings form: `switch|select|number|text|fact|rows`), `facts?(deployment)`, `health: string[]` (claimed health entries);
- intake filter: `scope?({meta}, config) → {listened, observed?, row?, reason?}` — a pure function of stored `meta` + config, reused for Mandate revalidation; out-of-scope triggers are stored as `filtered` with the reason;
- write side: `channel?: { allowed?(address, config), send(host, message) → 'sent' | 'gone' (throw = retryable; must be idempotent by message.id), link(address) }`;
- context for assessment: `threadContext?: { messageId(trigger), read(commands, trigger, 'thread'|'root'|'parent', cursor, config) → {messages: ContextMessage[], next?, body}, images?, image? }`, plus `find?(query) → {threads, terms}`, `repliesTo?(trigger)`;
- policy part: `policy?: { schema (incl. allowed[]), fields, explicit, unnamed?, rows?(config) → AdmissionRowView[], bots?, sync?{label, ready, run} }`, `refreshLabels?`;
- executor env: `environment?: { variables: VariableSpec[], values({deployment, config, trigger}) }` with `DSH_SUPERAGENT_<KIND>_*` names.
- Loop contract: wait `ctx.ready`; read nothing until `ctx.configured()`; read `ctx.config(kind)` each round (undefined = Source off); `ctx.receive(trigger)` → `stored|duplicate|filtered` (stable ids make re-reads safe); advance the cursor (`ctx.state`, typed `KV`) only after every trigger is stored; count malformed items instead of failing the batch; report health `error` only after 3 consecutive failures; honour the AbortSignal. Optional `ctx.tickets.bindThread`/`threadClosed` for PR-like lifecycles; `ctx.notify` for Source-originated notices.

**Architecture (`docs/007-architecture.md`)**
- `Runtime` = composition root (locks `stateDir`, opens Store, builds parts, startup/shutdown).
- `Engine` = synchronous, only writer of Triggers/Threads/Decisions/Tickets/Assignments; internal parts Intake → Evals → Tickets → Assignments → Replies; callers get role-scoped `Pick` interfaces (`EngineForSources|MainSession|People|Settings|Executor`); imperative commands (`stopTicket`) vs past-tense facts (`assignmentStarted`).
- Ports: `Repositories`, `Policy`, `SourceScope`, `Assessor`, `Executor`, `Outbox` (`src/core/ports.ts`); domain events `trigger.received`, `eval.started`, `decision.recorded`, `ticket.created|resumed|ended`, `assignment.started|ended`, `thread.subscribed|unsubscribed`, `trigger.requeued`, `policy.changed`.
- Durable `Effects` queue (kinds `assessment`, `launch`, `delivery`, `relay`, `notice`, `reply`): enqueue inside the transaction, run after commit, per-kind concurrency and exponential backoff, `Later` to defer without counting a failure.
- `Outbox` with receipts, idempotent sends and per-ticket-per-conversation ordering; `HealthBoard` with exclusive entry ownership; one SQLite Store.
- `src/core` does no I/O and imports nothing outside itself except Node hashing — a portable, testable kernel (`src/core/memory.ts` in-memory repositories).
- Reuse: MIT. Port the domain model + Engine rules (TypeScript + zod) or re-implement them in our stack; the DSH-specific glue (`src/executor/dsh.ts`, `src/app/sessions.ts`, agent presets, panel client) is not portable.

**What to carry into our task center (design reuse checklist)**
1. Separate *authorization* (deterministic gate, allowed lists, admission table, immutable Mandate with recorded basis) from *judgement* (LLM assessment with per-rule verdicts and quoted evidence).
2. `off → observe → auto` rollout for any proactive rule; log "would accept" decisions for review before enabling.
3. Thread-level serialisation (one Eval per thread, debounce batch) and idempotent trigger ids.
4. Subscriptions for follow-up routing without re-authorisation; executor-owned follow-ups with explicit hand-back.
5. Structured Reports (`progress|waiting|ready|blocked|failed|dropped`) + single-question Attention as the only HITL channel; ticket stage derived by a first-match table.
6. Policy versioning, targeted `policy-changed` inputs, revalidation on resume instead of mid-run cancellation.
7. Durable Effects/Outbox (enqueue in the transaction, execute after commit, idempotency keys, per-conversation ordering, health error after 3 consecutive failures).
8. Per-ticket directory + prepare command + documented env variables; a reserved concurrency slot for humans; a recovery note after restart.

---

## 4. dsh-multica-runtime and Multica's agent protocol (question h)

- Bridge plugin (`dsh-multica-runtime/src/index.ts`, `src/protocol.ts`, `cordis.patch.yml`) mounted as the reserved one-shot row id `headless-runner` over `dsh-base`; disables `hmr` and `session-telemetry-otel` (does **not** disable `session-log-deepseek`); `session-persistence-jsonl.root = $MULTICA_DSH_SESSION_ROOT ?? $DSH_HOME/sessions`; persona says headless, no `ask_user_question`.
- Modes (app args): `dsh --profile multica --probe` → `{"v":1,"type":"probe","runtime":"dsh","plugin_version","protocol_version":1}` (daemon discovers the profile only after this); `--list-models` → `{"type":"models","models":[{id:"<provider>/<model>",label,provider,default?,thinking{supported_levels,default_level}}]}`; `--stdio` → emits `ready{capabilities{resume,cancel,models,thinking,usage,tools,mcp:["stdio","streamable-http"]}}`, then accepts **exactly one** `execute` per process.
- Inbound (stdin JSONL, ≤8 MiB): `{"v":1,"type":"execute","request_id","cwd","prompt","resume_session_id?","model?{provider,id,reasoning_effort?}","reasoning_effort?","mcp_servers":[{name, transport:"stdio", command, args, env, cwd?, tool_call_timeout_ms?}|{name, transport:"streamable-http", url, headers}]}` and `{"v":1,"type":"cancel","request_id"}`.
- Outbound frames: `session{session_id, resumed}`, `text`, `thinking`, `tool_call{call_id,name,arguments}`, `tool_result{call_id,name,output (≤256 KiB),is_error,truncated?}`, `usage{provider,model,input_tokens,output_tokens,cache_read_tokens?,cache_write_tokens?,reasoning_tokens?}`, terminal `result{status: completed|failed|aborted|cancelled, session_id?, output, stop_reason?, resume_rejected, error?{code: DSH_RESUME_REJECTED|DSH_RUNTIME_ERROR}}`, `protocol_error{code,message}`. Exit 0 for completed/cancelled.
- Approvals: every `approval/request` answered `allowed-once` (cancelled during cancel) — i.e. Multica runs DSH effectively unattended inside the `workspace-write` sandbox. Credential scrub patched to forward only `MULTICA_TOKEN` starting with `mat_` (`src/environment.ts`, `src/task-env.ts`).
- Daemon side (`multica/server/internal/daemon/dsh_profile.go:22-28`, `multica/server/pkg/agent/dsh.go:120,224-260`): Multica drives each CLI through **its own native machine protocol** — `claude -p --output-format stream-json`, `codex app-server`, `kimi acp`, OpenCode ACP, Pi `-p --mode json` (or `--mode rpc` with task supplement; prompt on stdin, `--session <path>` resume, `pi.go:1197-1250`) — and DSH through this bridge (`dsh --profile multica --stdio`) because "DSH ships no machine-drivable mode of its own" suitable for it. It spawns the process with cwd = task dir, a context timeout (`opts.Timeout`, `WaitDelay` 10 s), owned process tree, stderr tail capture; env knobs `MULTICA_DSH_PATH`, `MULTICA_DSH_PROFILE_BUNDLE` (auto-provision via `dsh plugin --profile multica add`), `MULTICA_DSH_PLUGIN_PATH` (pnpm location). Bridge not yet on npm (multica#6936 per code comment).
- Risks: private/UNLICENSED, validated only against DSH 0.2.1-rc.2; we should treat it as a protocol reference, not a dependency.

---

## 5. Cross-cutting risks for our use
1. **Node version**: system Node 22.17 satisfies none of Pi (≥22.19), DSH 0.2.2 (≥24.9), Super-Agent/bridge (≥22.19). Install a side-by-side Node 24 (≥24.9) rather than changing `E:\nodejs`.
2. **No guardrails in Pi**; DSH's Windows sandbox is `partial`; Super-Agent workers are full-access. Use dedicated worktrees, ideally a dedicated low-privilege Windows account, and keep personal credentials (`~/.codex`, `~/.claude`, gh) out of worker environments.
3. **Telemetry/egress**: DSH session-log upload to DeepSeek and OTel; Pi install telemetry + pi.dev catalog/version checks.
4. **Churn**: both pre-1.0-style cadence (Pi weekly minors with breaking notes; DSH RC/alpha only). Pin exact versions; re-run smoke tests on every bump.
5. **Paths**: this repo lives under `D:\phd\...` (space + CJK). Windows shim and ACL behaviour with such paths is UNVERIFIED; prefer ASCII paths for runtimes, homes and worktrees (e.g. `D:\agentrt\...`).
6. **Untrusted repo content**: Pi loads `AGENTS.md`/`CLAUDE.md` regardless of trust; DSH reads `<cwd>/.env` as an env layer and refuses to start if it sets proxy vars.

---

## 6. Recommended integration recipe (Windows 10, this box)

### 6.0 One-time prerequisites (shared)
1. Node 24 LTS (≥24.9; exact patch UNVERIFIED) unpacked side-by-side, e.g. `D:\agentrt\node24\node.exe` (portable zip; do not replace `E:\nodejs`). Keep `HTTP_PROXY`/`HTTPS_PROXY=http://127.0.0.1:7890` in the worker env (both runtimes honour them); add `NO_PROXY=localhost,127.0.0.1`.
2. Per-task git worktree created by the orchestrator (outside the runtimes), ASCII path, e.g. `D:\agentrt\work\T-0001`.
3. Secrets only via the child process env (never argv, never repo files).

### 6.1 Pi (recommended primary executor)
Install:
```powershell
D:\agentrt\node24\npm.cmd install -g --prefix D:\agentrt\pi --ignore-scripts @earendil-works/pi-coding-agent@1.5.0
# entry: D:\agentrt\pi\node_modules\@earendil-works\pi-coding-agent\dist\bundle\cli.js
```
Agent dir `D:\agentrt\pi-home` (set `PI_CODING_AGENT_DIR`), file `settings.json`:
```json
{
  "shellPath": "E:\\Git\\bin\\bash.exe",
  "defaultProjectTrust": "never",
  "enableInstallTelemetry": false,
  "defaultProvider": "deepseek",
  "defaultModel": "deepseek-v4-pro",
  "retry": { "enabled": true, "maxRetries": 3 },
  "compaction": { "enabled": true }
}
```
Optional `models.json` (Anthropic-/OpenAI-compatible gateways):
```json
{
  "$schema": "https://pi.dev/schemas/models.schema.json",
  "providers": {
    "claude-gw": { "baseUrl": "https://claude-gw.example", "api": "anthropic-messages", "apiKey": "$CLAUDE_GW_TOKEN",
                   "models": [ { "id": "claude-sonnet-4-5", "reasoning": true, "input": ["text","image"], "contextWindow": 200000, "maxTokens": 64000 } ] },
    "oai-gw":    { "baseUrl": "https://oai-gw.example/v1", "api": "openai-completions", "apiKey": "$OAI_GW_TOKEN",
                   "compat": { "supportsDeveloperRole": false, "maxTokensField": "max_tokens" },
                   "models": [ { "id": "some-model" } ] }
  }
}
```
Preflight (once per config change): `node cli.js auth check --provider deepseek --json` (exit 0 = ready) and `node cli.js --list-models deepseek`. For ChatGPT/Claude subscriptions instead of keys: run `node cli.js` interactively once with the same `PI_CODING_AGENT_DIR` and `/login` (device code), which fills `auth.json`.

Headless run (orchestrator spawns; cwd = worktree; prompt written to stdin as UTF-8 then stdin closed):
```text
D:\agentrt\node24\node.exe D:\agentrt\pi\node_modules\@earendil-works\pi-coding-agent\dist\bundle\cli.js
  -p --mode json --no-approve
  --session-dir D:\agentrt\pi-sessions --session-id T-0001
  --model deepseek/deepseek-v4-pro --thinking high
  [--tools read,bash,edit,write,grep,find,ls] [--append-system-prompt D:\agentrt\prompts\worker.md] [--offline]
env: PI_CODING_AGENT_DIR=D:\agentrt\pi-home  PI_SKIP_VERSION_CHECK=1  PI_TELEMETRY=0
     DEEPSEEK_API_KEY=...  (or CLAUDE_GW_TOKEN / OAI_GW_TOKEN)  HTTP(S)_PROXY=...
```
Parsing contract: split stdout on `\n` (strip `\r`); line 1 `type=session` gives the session id/cwd; log `tool_execution_start/end` as tool events; the final answer is the text blocks of the last `message_end` with `message.role=="assistant"`; success requires `agent_settled.aborted==false` and that message's `stopReason` not in `error|aborted|length`; process exit code is meaningful only for crashes. Resume/follow-up: same command, same cwd, same `--session-id`. Timeout: orchestrator wall clock → `taskkill /T /F /PID <pid>` (Windows has no graceful SIGTERM for Node children; UNVERIFIED nuance). For graceful abort, steering, multi-turn or HITL dialogs, use `--mode rpc` instead (`{"id":"1","type":"prompt","message":...}` → wait `agent_settled` → `{"id":"2","type":"get_last_assistant_text"}`; cancel `{"type":"abort"}`; answer `extension_ui_request` dialogs; close stdin to exit). Add a permission-gate extension (`-e D:\agentrt\pi-ext\gate.ts`) if any confirmation policy is required.

### 6.2 DSH (secondary executor; DeepSeek-native, has a sandbox)
Install:
```powershell
D:\agentrt\node24\npm.cmd install -g --prefix D:\agentrt\dsh @deepseek-ai/dsh@0.2.2-rc.1
# entry: D:\agentrt\dsh\node_modules\@deepseek-ai\dsh\lib\bin.js
```
Overlay `D:\agentrt\dsh-home\worker.patch.yml` (passed with `--patch`; replaces whole row configs):
```yaml
- id: session-log-deepseek
  config:
    enabled: false
- id: agent-default-model
  config:
    provider: deepseek-official
    model: deepseek-v4-pro
# Anthropic-/OpenAI-compatible alternative (then point agent-default-model at claude-gw/claude-sonnet-4-5):
# - id: llm-pi-ai
#   config:
#     providers:
#       claude-gw: { api: anthropic-messages, baseURL: https://claude-gw.example, apiKeyEnv: CLAUDE_GW_TOKEN, models: [{ id: claude-sonnet-4-5 }] }
#       oai-gw: { api: openai-completions, baseURL: https://oai-gw.example/v1, apiKeyEnv: OAI_GW_TOKEN, compat: { supportsDeveloperRole: false, maxTokensField: max_tokens }, models: [{ id: some-model }] }
```
Preflight: `node bin.js --profile headless --patch D:\agentrt\dsh-home\worker.patch.yml --dump-config` (no boot; shows the composed tree; initialises the `headless` profile under `DSH_HOME`).

Headless run (cwd = worktree; task on stdin via `-`, stdin closed):
```text
D:\agentrt\node24\node.exe D:\agentrt\dsh\node_modules\@deepseek-ai\dsh\lib\bin.js
  --profile headless --patch D:\agentrt\dsh-home\worker.patch.yml
  --json --permission-mode workspace-write --approval-policy never -
env: DSH_HOME=D:\agentrt\dsh-home  DSH_TELEMETRY_DISABLED=1  DSH_TELEMETRY_MODE=DISABLED
     DEEPSEEK_API_KEY=... (or CLAUDE_GW_TOKEN / OAI_GW_TOKEN)  HTTP(S)_PROXY=...  NO_PROXY=localhost,127.0.0.1
```
Parsing contract: first line `type=session` (save `sessionId`); `tool_call`/`tool_result` are tool events; `text` arrives per committed step; success = exit 0 **and** `final.reason.kind=="completed"`; answer = `final.text`; an `error` line without `final` = runner failure. Do not pass `--add-dir` on Windows (ACL backend rejects tool subprocesses with extra roots) — so either use full clones per task, or let the orchestrator perform `git commit/push` outside DSH. Resume: `--session-id <sessionId>` from the same cwd with the same flags. Timeout: orchestrator wall clock + process-tree kill; remember SIGTERM-style shutdown exits 0. For approvals answered by our orchestrator (real HITL) or a Node-free install, use `--profile sdk` JSON-RPC (`initialize{approvalRequests:true}`) or the Python SDK: `uv pip install --pre "deepseek-harness-sdk==0.2.1rc2"` → `DeepSeekHarness(dsh_home=r"D:\agentrt\dsh-home", cwd=worktree, provider="deepseek-official", model="deepseek-v4-pro", approval_requests=True)` (UNVERIFIED end-to-end on this box).

### 6.3 Super-Agent and the bridge
Do not run Super-Agent on native Windows; if a live demo is wanted, use WSL2 Ubuntu with its own Node 24, pnpm, tmux, gh and DSH. Reuse its domain model (§3g) in our task center; reuse the multica bridge only as a wire-protocol reference (§4).

---

## 7. UNVERIFIED items (not exercised)
- Any runtime behaviour: nothing was installed or executed. In particular: Pi on Node 24 with `shellPath=E:\Git\bin\bash.exe`; DSH ACL sandbox and pwsh 5.1 fallback on this Windows 10 build; behaviour with CJK/space paths; `--ignore-scripts` for DSH.
- Exact current DeepSeek model ids in Pi's pi.dev catalog; Pi `authHeader` semantics for `anthropic-messages`; whether gateway `baseUrl` needs `/v1`.
- Graceful-stop semantics of both CLIs when killed on Windows.
- Python SDK `deepseek-harness-sdk 0.2.1rc2` behaviour (only PyPI metadata checked: win_amd64 runtime wheel exists).
- Super-Agent compatibility with DSH 0.2.2-rc.1 (range check is prerelease-inclusive, but docs pin 0.2.1-rc.3).
- Use of Claude subscription OAuth tokens via `ANTHROPIC_OAUTH_TOKEN` (code path exists; terms of service not assessed).
