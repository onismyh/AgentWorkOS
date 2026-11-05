# 执行器实测笔记（Codex / Claude / Pi / DSH）

全部在本机 Windows 10 上实测（2026-11-05）。适配器在 `src/awos/executors/`，统一合同见 `base.py`。

## Codex（codex-cli 0.159.2，ChatGPT 登录，订阅制，cost=0）
- 命令：`codex exec --json --skip-git-repo-check --ephemeral -s read-only|workspace-write -C <ws> -o last_message.txt [--output-schema S.json] [-m M] -`（prompt 走 stdin）。
- 事件：`thread.started` / `turn.started` / `item.started|completed`（`agent_message`、`command_execution`、`mcp_tool_call`、`web_search`、`file_change`、`reasoning`）/ `turn.completed{usage}` / `turn.failed`。
- `--output-schema` 需要 OpenAI 严格模式：每个 object `additionalProperties:false`，所有字段都在 `required`，可选字段改成 nullable → `codex.strictify()`。
- **连接器隔离**：`--disable apps --disable plugins -c mcp_servers.node_repl.enabled=false` 后，一次运行里没有 Gmail/Notion/Drive/Calendar 工具（实测列出工具清单确认）；`--disable browser_use --disable computer_use` 去掉桌面自动化。传感器和动作执行器才用 `allow_connectors=True`。
- **局限**：Codex 的沙箱只限制"写"，读取不限制目录（整盘可读，包括凭据文件）。我们靠 prompt 规则 + 不传 `--add-dir`（它会授予写权限）来约束；不要把它当成读隔离。
- Notion 连接器没有日期筛选，开放式提示会无限搜索（一次 31 万 token）→ 传感器提示里写死了工具调用预算（≤12 次）。

## Claude Code（2.1.311，第三方 Anthropic 网关，按量计费）
- 命令：`claude -p --output-format stream-json --verbose --no-session-persistence --permission-mode dontAsk --allowedTools ... [--json-schema S] [--max-budget-usd B]`（prompt 走 stdin）。
- 结构化输出通过内置 `StructuredOutput` 工具返回，在 `result.structured_output`。
- `--permission-mode dontAsk`：未允许的工具直接拒绝并记入 `permission_denials`，不会卡住等待确认。`Write(./**)`/`Edit(./**)` 把写入限制在工作目录——实测 `../outside.txt` 被拒、`./inside.txt` 成功（tests/test_executors.py 有 live 用例）。
- triage/plan/verify/context 用 `--bare`（跳过 hooks/插件/CLAUDE.md，最便宜：haiku 一次约 $0.0002–$0.003）；execute 不用 bare，但用 `--settings {"disableAllHooks": true}`，避免自动化会话写进 claude-mem。
- 费用：opus 冷缓存一次约 $0.10，sonnet $0.05，haiku $0.003。

## Pi（1.5.0）与 DSH（0.2.2-rc.1）
- 安装在 `runtime/pi`、`runtime/dsh`（便携 Node 24.22.2），用 `node <entry.js>` 调用；`--version`/`--help` 已在带中文空格的路径下实测可用。
- **两者都还没有模型凭据，所以默认 `enabled=false`**（适配器 `available()` 会明确报告原因）。启用步骤：在 `var/secrets.toml` 写 `[pi.env]`（如 `DEEPSEEK_API_KEY`）或 `[dsh.env]`，再把 `config/local.toml` 的 `executors.pi.enabled = true`。
- 事件解析器有单元测试（tests/test_node_runtimes.py），但**还没有用真实模型跑过**。
- DSH 默认会把会话日志上传给 DeepSeek，已在 `config/runtimes/dsh-worker.patch.yml` 关闭，并设置 `DSH_TELEMETRY_DISABLED=1`。

## 进程监督（proc.py）
- prompt 走 stdin（UTF-8，规避 Windows 命令行长度和引号问题），npm `.cmd` shim 解析成真实 exe/js 入口。
- 超时/取消：`taskkill /T /F` + Windows Job Object（KILL_ON_JOB_CLOSE），daemon 死亡时子进程也会被带走。
- 守护进程退出用 `os._exit`，因为 worker 线程不是 daemon 线程，不硬退出会让旧进程拖到 agent 跑完（实测遇到过）。

## Shell 权限（Claude，实测）
- 本机 Claude Code 只有 **PowerShell** 工具，没有 Bash。权限规则两套都写（`executors/claude.py` 的 `SAFE_SHELL` / `DENY_SHELL`）。
- 有效：`PowerShell(git status:*)`、`PowerShell(git -C * status *)`（带路径的 `-C` 形式必须用空格加星号）。
- 无效：`PowerShell(git -C * status:*)`；`cd "<repo>"; git status` 这类复合命令会被拒绝（`Set-Location` 也一样）。
- `--allowedTools` 里单条规则含空格没问题（实测）；拒绝优先于允许。

## Pi / DSH 实测（DeepSeek key 已配置）
- Pi：`deepseek/deepseek-v4-pro`，一个小任务约 4k token / $0.007；事件含 write/read 工具调用；`auth check --provider deepseek` 返回 ready。
- DSH：默认 provider `deepseek-official`、模型 `deepseek-flash`；usage 在每个 `step_end` 事件里（`inputTokens/outputTokens/cacheReadTokens`），需累加；没有费用字段。
- Pi 的 `settings.json` 转义写错会被整个忽略，只给一行警告（`shellPath` 里的反斜杠被 shell 吃掉了一半）——用 Python `json.dump` 生成这个文件，不要手写。
