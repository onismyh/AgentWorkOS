# AgentWorkOS

> 本地常驻、主动感知、理解个人上下文、自主推进任务、关键节点人工确认的"个人超级智能"操作系统。
> Windows 单机 · Python 3.13 + SQLite + FastAPI · 执行器：Codex / Claude Code / Pi / DSH

## 它做什么

```
感知（git · 文档 · AI 会话历史 · 学校/163 邮件 · Gmail · Notion · Zotero · 手动投递）
  → 分诊（LLM：分类、优先级、L1–L5 自主等级、是否自动启动）
  → 规划（复杂任务拆成可以并行的子任务 DAG）
  → 执行（Codex / Claude / Pi / DSH 子进程，隔离工作区，超时，重试，独立质检）
  → HITL（对外动作和关键决策进 Outbox → Windows 通知 / 手机推送 → 你一键确认）
  → 交付（交付物 + 总结，每天早晚各一份摘要）
```

## 快速开始

```powershell
# 1. 安装（uv、Python 3.13 venv、便携 Node 24）
powershell -ExecutionPolicy Bypass -File scripts\install.ps1
# 2. 体检
scripts\awos.cmd doctor
# 3. 前台运行（调试用）或注册为登录自启的后台服务
scripts\awos.cmd daemon
powershell -ExecutionPolicy Bypass -File scripts\service-install.ps1
# 4. 打开控制台
start http://127.0.0.1:8765/
```

常用命令：`awos status` · `awos inbox` · `awos add "标题" -d "描述"` · `awos approve <id> <option>` · `awos sensors` · `awos sensor-run git` · `awos context refresh` · `awos digest`

也可以把 `.md` / `.txt` 文件丢进 `var/inbox-drop/`，它会自动变成 Inbox 条目。

## 自主等级

| 等级 | AI 做到 | 交给你 |
|---|---|---|
| L1 | 理解整理，提取待办 | briefing → 你接手 |
| L2 | 分析问题，给出方案 | 你在 Outbox 做决定 |
| L3 | 产出中间成果 | 你验收和定稿 |
| L4 | 完成大部分工作 | 关键环节你确认 |
| L5 | 端到端完成并通过独立质检 | 直接交付；对外动作仍需确认 |

## 文档

- `docs/architecture.md`：架构
- `docs/decisions/ADR.md`：关键决策
- `docs/research/`：调研报告（本机能力盘点、Pi/DSH/Super-Agent、Multica 及同类方案、感知与 HITL）
- `docs/reviews/`：Codex 架构评审
- `docs/playbook/`：工具使用经验
- `docs/runbook.md`：运维手册

## 目录

```
config/    默认配置与策略（进 git）；local.toml 放本机覆盖（不进 git）
src/awos/  核心代码：db / repo / brain(triage, policy, planner, context) / engine(scheduler, worker, recovery)
           / executors / sensors / hitl(outbox, notify, tokens) / api + dashboard
scripts/   安装、服务注册、awos.cmd
var/       运行数据（不进 git）：awos.db、runs/、workspaces/、artifacts/、logs/、backups/、context/
runtime/   便携 Node、Pi、DSH（不进 git）
```

## 许可证

以 [MIT 许可证](LICENSE) 发布。Pi、DSH、Node、gh 等第三方运行时由安装脚本下载到 `runtime/`（不随本仓库分发），各自遵循其许可证。
