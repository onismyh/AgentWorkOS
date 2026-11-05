# 00 · 本机能力盘点（Environment Inventory）

> 采集时间：2026-11-05 · 机器：Windows 10 Pro 19045 · 用户目录 `C:\Users\admin`
> 只记录能力与元数据，不记录任何凭据/令牌。

## 1. 运行时与基础 CLI

| 工具 | 路径 / 版本 | 备注 |
|---|---|---|
| Node.js | `E:\nodejs` v22.17.0 | Pi / DSH 要求 ≥ 22.19 → 需要独立的便携 Node（见 ADR-003） |
| npm | 11.17.0 | 全局包：claude-code 2.1.311、@openai/codex 0.159.2、@qwen-code/qwen-code 0.14.3、oh-my-claude-sisyphus |
| Python | Anaconda 3.12.3 (`E:\anaconda3`) | 系统 Python；项目使用 uv 管理独立 venv |
| uv | 0.10.7 | 项目依赖管理 |
| git | 2.47.1 | `user.name=onismyh`，全局 http(s).proxy=127.0.0.1:7890 |
| jq / rg / fd | jq 1.8.1；rg、fd 来自 `~/.kimi-code/bin` | |
| sqlite3 | 3.51.1 | |
| Pandoc | 3.1.12 | 文档转换 |
| WSL2 | Ubuntu、suanleme-instance（均 Stopped） | tmux 等 POSIX-only 依赖可放 WSL |
| 缺失 | docker、gh、pnpm、bun、go、ollama、playwright | GitHub API 未认证限流 60 次/小时 |

网络：Clash Verge（mihomo）系统代理 `127.0.0.1:7890`，环境变量 `HTTP(S)_PROXY/ALL_PROXY` 已设置；Google / GitHub / PyPI / npm 均可达。

## 2. Agent 执行环境

| Agent | 无人值守调用 | 认证 | 实测 |
|---|---|---|---|
| **Codex CLI** 0.159.2 | `codex exec --json -o <file> --output-schema <schema> -s read-only\|workspace-write -C <dir> --ephemeral --skip-git-repo-check` | ChatGPT 账号（`~/.codex/auth.json`），模型 `gpt-6-astra` | ✅ `CODEX_OK`，约 7k tokens |
| **Claude Code** 2.1.311 | `claude -p --output-format json\|stream-json --json-schema <schema> --model haiku\|sonnet\|opus --bare --max-budget-usd --permission-mode --allowedTools` | `settings.json` env 中的第三方 Anthropic 兼容网关 | ✅ opus $0.10/次（冷缓存）· sonnet $0.05 · haiku $0.0026 · haiku+`--bare` $0.00016 |
| Qwen Code 0.14.3 | `qwen -p` | OAuth | 未测（备用） |
| Kimi Code | `~/.kimi-code/bin/kimi.exe` | 已登录 | 未测（备用） |
| Pi / DSH | 未安装 | — | 见 research/01 |

要点：
- `claude -p --bare` 会跳过 hooks/插件，**避免自动化会话污染用户的 claude-mem 记忆**，同时成本最低 → 作为 triage 默认通道。
- `--json-schema`（Claude）与 `--output-schema`（Codex）都支持结构化输出 → executor 合同统一用 JSON Schema。
- Codex 走订阅额度，适合重活；Claude 走按量付费网关，需要预算控制。

## 3. Codex 连接器（经 `codex exec` 实测，只读）

| 连接器 | 已认证 | 实测结果 |
|---|---|---|
| Gmail | ✅ | 能检索最近邮件（个人 Gmail，主要是论坛/订阅邮件） |
| Google Calendar | ✅ | 未来一年没有事件（日程不在 Google 日历） |
| Google Contacts | ✅ | 空 |
| Google Drive/Docs/Sheets/Slides | ✅ | 能列出文件夹 |
| GitHub（ChatGPT App） | ✅ | 返回 0 个仓库（App 没有授权仓库访问） |
| **Notion**（含 Notion Calendar） | ✅ | 最近页面：`Week of ‣`、`日程安排`、`求职` → **用户的日程与计划主要在 Notion** |
| Sites | ✅ | `水电解制氢标准图谱` |
| Slack / Outlook / Teams | ❌ | 没有可调用的工具 |

结论：**Codex 是现成的"个人数据网关"**，Gmail / Notion / Drive / Calendar 都通过 `codex exec` + 结构化输出接入，不用另外申请 API Token。

## 4. Codex 内置插件（`~/.codex/config.toml`）

已启用：browser、chrome、computer-use（Windows UI 自动化）、documents、pdf、spreadsheets、presentations、visualize、google-calendar、slack、template-creator。远端缓存中还有：gmail、google-drive、github、notion、superpowers、data-analytics、write-like-me 等。
已有 automation（heartbeat，PAUSED）："每小时监控氢项目后台计算"，说明用户已经习惯"心跳巡检"这种模式。

## 5. Claude Code 生态

- 插件：`claude-mem@thedotmack` 13.15.0（`~/.claude-mem/claude-mem.db` + chroma，记录历史会话观察，是重要的上下文来源）
- 同步的 Skills：docx、pdf、pptx、xlsx、docs、google-workspace、morning（晨报）、skill-creator、import-memory、setup-writing-style
- 没有配置 MCP 服务器；有 `~/.claude/history.jsonl`（约 370 KB）与 `~/.claude/projects/*` 会话记录
- 用过 Claude Code 的项目：VEDA/Energy-Model、多篇论文（电厂改造、climate_energy_supply、Paper-A on Hydrogen、电力转型）、博士生中期、简历、课题组2026论文、dynamic_network_solver

## 6. 桌面应用（信息源 / 交互面）

| 应用 | 状态 | 可接入方式 |
|---|---|---|
| Notion 桌面版 | 运行中 | Codex Notion 连接器 ✅ |
| Zotero | 运行中 | 本地 API `127.0.0.1:23119`、`zotero.sqlite` 只读（见 research/03） |
| 微信 / 企业微信 | 运行中 | 不读取加密数据库（隐私/ToS）；改用企业微信机器人 webhook 推送 |
| 网易邮箱大师 | 已安装 | 本地库是私有格式 → 改走 IMAP（需要用户提供授权码） |
| Outlook | 已安装但**没有配置 Profile** | COM 不可用 |
| Chrome / Edge | 已安装 | Codex chrome/browser 插件可以驱动 |
| VS Code / Trae / Cursor | 已安装 | — |

## 7. 用户工作画像（初始线索，只来自目录名和元数据）

- 某大学博士研究生（能源/气候系统建模：VEDA-TIMES、电力系统转型、氢能、电厂改造）
- 正在推进：多篇论文、博士生中期、求职投递、综合奖学金申请、ConfX 会议
- GitHub：`onismyh`；邮箱：个人 Gmail（工作邮件可能是学校邮箱，经网易邮箱大师收取）

## 8. 约束与风险

- 没有 Docker → Multica 自托管（Postgres+Docker）不能直接跑（见 research/02、ADR-001）
- 系统 Node 低于 Pi/DSH 的要求 → 用项目内的便携 Node，不改系统 Node
- `~/.claude/settings.json` 里有明文网关 Token → 本项目**绝不复制**任何凭据，只通过 CLI 继承认证
- 未认证的 GitHub API 只有 60 次/小时 → 尽量用 git clone 和 Codex GitHub 连接器
