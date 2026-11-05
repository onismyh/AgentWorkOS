# 工具使用经验（Playbook · 通用）

> 每条：现象 → 原因 → 做法。执行器细节见 `executors.md`，传感器细节见 `sensors.md`。

## Windows / 路径

- **项目路径里有中文和空格（`D:\phd\...`）**
  - npm 的 `npm.cmd` shim 会把路径拆在空格处（`'D:\5.' 不是内部或外部命令`）→ 一律用 `node.exe <pkg>/bin/*.js` 直接调用（npm 本身用 `node runtime/node/node_modules/npm/bin/npm-cli.js`）。
  - Python 3.12 按本地代码页（GBK）读取 `.pth` 文件，editable install 指向的中文路径会乱码，导致 `ModuleNotFoundError: awos` → 改用 **Python 3.13**（改为按 UTF-8 读取 .pth）。项目固定 `.python-version = 3.13`。
  - 所有 Python 入口都加 `-X utf8` 或 `PYTHONUTF8=1`。
- **系统时区是 UTC-4，用户在北京时间** → 所有时钟逻辑都走 `awos.timeutil`（ADR-006）。
- **系统代理 127.0.0.1:7890 不绕过 localhost** → PowerShell 的 `Invoke-WebRequest http://127.0.0.1:8765` 会失败；请用 `curl --noproxy '*'` 或 Python（`NO_PROXY` 已设置）。
- **PATH 中第一个 `bash.exe` 是 WSL 启动器**（`C:\Windows\System32\bash.exe`）→ Pi 的 `shellPath` 必须显式设为 `E:\Git\bin\bash.exe`。
- **Toast 通知**：不用安装任何东西，用 PowerShell 5.1 + WinRT + PowerShell 的 AUMID 就能弹出；XML 以 base64 通过 `-EncodedCommand` 传入，避免中文和引号转义问题。
- **计划任务**：要以"当前用户、仅登录时运行"注册，才能复用 Codex/Claude 的登录态和桌面通知；watchdog 应该 `schtasks /Run` 已注册的 Daemon 任务，不要把 daemon 作为 watchdog 的子进程拉起。

## Agent CLI

- **Codex**：`codex exec` 支持 `--json`（JSONL 事件）、`--output-schema`（严格模式 JSON Schema）、`-o last.txt`、`--ephemeral`、`-s read-only|workspace-write`、`-C <dir>`。ChatGPT 连接器（Gmail、Notion、Drive、Calendar、GitHub）在 `codex exec` 里可以直接用，是现成的个人数据网关。
- **Claude Code**：`claude -p --output-format json|stream-json --json-schema <schema>` 可以得到 `structured_output`；`--bare` 会跳过 hooks/插件/CLAUDE.md，**避免自动化会话写入用户的 claude-mem 记忆**，而且最便宜（haiku + bare 一次约 $0.0002，opus 冷缓存一次约 $0.10）。
- **Pi**（1.5.0）：没有沙箱和权限系统，靠 `--tools` 白名单限制；`--mode json` 的退出码只在崩溃时非 0，成败要从 `message_end.stopReason` 和 `agent_settled.aborted` 判断。没有内置结构化输出，需要在 prompt 里要求最后输出 JSON，再做解析。
- **DSH**（0.2.2-rc.1，开发者预览）：必须用 Node ≥ 24.9；默认会把会话日志上传给 DeepSeek，必须用 patch 关掉（`session-log-deepseek.enabled=false`），并设置 `DSH_TELEMETRY_DISABLED=1`；成功的判断条件是 exit 0 并且 `final.reason.kind=="completed"`；在 Windows 上不要用 `--add-dir`。
- npm 11 默认会拦截依赖包的 install scripts（node-pty、koffi 等原生模块）；DSH 不运行这些脚本也能启动，但部分 shell 工具可能依赖它们（见 executors.md 实测）。

## 数据源

- **网易邮箱大师**：本地库是明文 SQLite（`D:\MailMasterData\<account>\mail.db`），只读打开就能拿到主题、发件人、时间和摘要，不需要 IMAP 授权码。
- **Zotero**：本地 API `http://127.0.0.1:23119/api/users/0/...`，支持 `since=<libraryVersion>` 增量；数据目录在 `D:\zotero`；运行时 `zotero.sqlite` 被锁定。
- **claude-mem**：worker 从 2026-08-18 起没有运行，只能当作历史资料。
- **微信 / 企业微信**：数据库加密，不读取（违反 ToS，且有封号风险）。

## 工程实践

- 让 Codex 做独立的架构评审非常有价值：它通过实际复现找出了 16 类问题（租约 fencing、事务嵌套、审批过期、预算预留等），已全部按 P0/P1 修复（ADR-004，见 `docs/reviews/`）。
- 并行子 Agent 必须**按文件划分所有权**，并在共享合同（`executors/base.py`、`sensors/base.py`、`engine/actions.py`）上先达成一致。

## 写补丁脚本时的坑
- 这个环境里 shell 会把我命令里的双反斜杠吃成单个，`\n` 会变成真换行、`r"\Agent"` 变成 `\A`。含转义的改动用 Edit 工具，不要用 heredoc 里的 Python 补丁脚本。
- Windows 的 Python 看不到 MSYS 的 `/tmp`（脚本内 `open("/tmp/..")` 会失败）；临时脚本放项目的 `var/test-cx/`。
- 邮箱大师的 `OrigBody` 有时是 TEXT 类型的二进制，`SELECT *` 会报 UTF-8 解码错误，要设 `conn.text_factory = bytes`。
- Base64 签名的最后一个字符只有 4 个有效位，测试里翻转它会偶发地不改变解码结果（曾导致链接篡改测试偶发失败）。
