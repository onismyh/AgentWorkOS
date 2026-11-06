# AgentWorkOS 架构设计（v0.1）

> 目标：在一台 Windows PC 上长期运行、主动感知、理解个人上下文、自主发现并推进任务、在关键节点让人确认，并且可以观测、可以干预、可以迁移的"个人超级智能"。
> 原则：**简单 > 花哨**，**本地优先**，**复用成熟 Agent，而不是重写 Agent**，**任何对外动作都要人确认**。

## 1. 总体结构

```
                ┌────────────────────────── AgentWorkOS daemon (单进程 Python, uv venv) ──────────────────────────┐
 Sources        │                                                                                                  │
 ───────        │  Sensors ──► signals ──► Triage(LLM+规则) ──► tasks(Inbox) ──► Policy(L1–L5 / 权限 / 预算)          │
 git 仓库        │     ▲                                              │                                               │
 本地文档        │     │                                              ▼                                               │
 Claude/Codex   │  Scheduler(tick) ── lease/heartbeat ──► Worker ──► Executor 适配器 ──► codex | claude | pi | dsh      │
 历史 & claude-mem│     │                                 │   ▲                         （子进程、隔离工作区、超时、kill 进程树）│
 Gmail/Notion/   │     │                                 │   └── Planner：复杂任务 → 子任务 DAG（并行 + 依赖）             │
 Drive (经 Codex)│     │                                 ▼                                                               │
 Zotero         │  Recovery/Backup              结构化结果 ─► artifacts / proposed_actions / decisions                   │
 inbox-drop     │                                                     │                                              │
 手动输入        │                                                     ▼                                              │
                │                                  Outbox(approvals) ─► Notify(toast / webhook) ─► 人                   │
                │                                         ▲ 决策 (dashboard / CLI / 签名链接)                           │
                │                                         └──► Action runner（执行已批准动作）→ 任务继续               │
                │  Context builder ──► context_facts / profile.md（个人上下文）──► 注入 triage / plan / execute 提示词  │
                │  FastAPI (127.0.0.1:8765)：REST + SSE + 静态 Dashboard                                             │
                └──────────────────────────────────── SQLite (WAL) var/awos.db ──────────────────────────────────────┘
```

- **单进程 daemon**：调度循环、worker 线程池和 API server 在同一个进程里；Agent 以子进程方式运行。
- **SQLite WAL** 是唯一的状态源（Single Source of Truth）。每次状态迁移都写入 `events` 审计表，可以回溯任意时刻的历史。
- **文件系统**：`var/runs/<run_id>/` 保存每次运行的 prompt、原始输出和最终消息；`var/workspaces/<task_id>/` 是任务的隔离工作区；`var/artifacts/` 存放交付物。

## 2. 为什么不直接用 Multica / DSH Super-Agent 当任务中心（ADR-001 摘要）

- Multica 自托管需要 Docker + PostgreSQL 17 + Go + Next.js，本机没有 Docker；Multica Cloud 又会把任务元数据放到第三方平台。
- dsh-superagent 只管**单个 GitHub 仓库**的编码请求（依赖 tmux 和 gh），不覆盖邮件、日程、文档、论文写作这些个人事务。
- 所以：**自建轻量核心，照搬它们成熟的设计**：Multica 的 issue→agent assignee→runtime daemon→review gate→autopilot→inbox 模型，以及 Super-Agent 的"Source→Work item→独立 session→回到来源回复"和"最少占用人的注意力"原则。**把 Pi / DSH / Codex / Claude Code 作为可插拔的执行器**。
- 以后如果要上 Multica，可以把 AgentWorkOS 当作 Multica 的一个 runtime，或者把 tasks 同步过去（预留 `meta.external_refs`）。

## 3. 数据模型（`src/awos/schema.sql`）

| 表 | 作用 |
|---|---|
| `signals` | 传感器原始观测；用 `fingerprint` 去重 |
| `tasks` | 统一任务中心：Inbox 条目、子任务、决策项都是 task。字段包括 `kind`（todo/issue/opportunity/decision/info）、`category`、`priority` P0–P3、`level` L1–L5 + `level_cap`、`status`、租约、重试、成本 |
| `task_deps` | 子任务 DAG 依赖 |
| `runs` / `run_events` | 每一次 Agent 运行，以及其中的消息、工具调用、错误、用量（工具调用记录） |
| `approvals` | **Outbox**：动作 / 决策 / 评审 / 预算审批，带背景、建议、选项、风险、过期策略、执行载荷 |
| `artifacts` | 交付物（路径 + sha256） |
| `notifications` | 推送记录 |
| `events` | 追加写入的审计日志（历史回溯） |
| `sensor_state` | 每个传感器的游标、失败计数、退避 |
| `context_facts` | 个人上下文知识库（role/project/goal/interest/habit/preference/person/deadline），带置信度、来源和 pinned 标记 |
| `cost_ledger` | 成本账本（按天、按执行器） |

### 任务状态机（`models.TRANSITIONS`）

```
inbox ─triage─► triaged ─(自动启动 or 用户接受)─► ready ─claim─► running
                  │                                  ▲            │
                  ├─► dismissed / snoozed            │            ├─► blocked（等子任务）─► ready（子任务全部完成后汇总）
                  └─► waiting_user                   │            ├─► waiting_approval ─(批准/拒绝)─► ready / review / done
                                                     │            ├─► waiting_user（L1/L2 做完自己那部分，移交给人）
                                                     │            ├─► review（L3/L4 交付物待验收）─► done / ready(返工)
                                                     └─ 重试退避 ◄─┤
                                                                  ├─► done（L5 已验证）
                                                                  └─► failed（重试耗尽）─► ready（人工重试）
```

## 4. L1–L5 自主等级与策略引擎（`brain/policy.py` + `config/policy.toml`）

**等级的含义**：等级说明 AI 能把这件事推进到哪一步。AI 总会先把自己那部分做完，再在对应位置交给人：

| 等级 | AI 自主完成的部分 | 交接点 | 执行沙箱 |
|---|---|---|---|
| L1 | 理解整理：背景摘要、待办提取 | `waiting_user`（附 briefing） | read-only |
| L2 | 分析问题，给出方案和推荐 | Outbox `decision`（选项 + 推荐） | read-only |
| L3 | 生成可交付的中间成果 | `review` | workspace-write（只能写任务工作区） |
| L4 | 完成大部分工作，关键环节在 Outbox 等确认 | Outbox `action` → `review` | workspace-write（+ 批准后 apply 回项目） |
| L5 | 端到端完成，自主验证后交付 | `done`（对外动作仍走 Outbox） | workspace-write（+ 白名单目录自动 apply） |

**有效等级** = min(triage 评估的等级, 策略上限)。策略上限由以下因素决定：
1. **类别上限**：comms / admin 等最多 L4（必须由人发出）；system / dev / research / doc 可以到 L5。
2. **工具能力**：需要的执行器、连接器或凭据不可用 → 降级（例如没有 IMAP 就无法读学校邮件）。
3. **权限**：需要写入的目录不在 `writable_roots` → 最多 L3（只能在工作区里产出）。
4. **敏感动作**：`SENSITIVE_ACTIONS`（发邮件、发消息、发布、push、开 MR/PR、删除、花钱、写日历/Notion、回写项目目录）**不论等级都进 Outbox**。唯一的例外是白板上的 git 规则（见下）。
5. **预算**：当日预算或单任务预算用尽 → 生成 `budget` 审批。

### 白板（共同规则）与 git 规则

白板 = 上下文页最顶部的 `rule` 类条目（`brain/whiteboard.py`，pinned，只有用户能改，Agent 写入会被 `agent_write_fact` 拒绝）。它在每个 Agent 提示词（`context_brief`）的最前面，不受长度截断，同时写进 `profile.md`。内置两条，首次启动时写入一次，之后归用户所有（改了保留，删了不会再补）：

- **Git 交付权限**：Agent 默认可以 commit 并 push 到本地/远端**分支**，不需要批准；不能动默认分支（main/master 与仓库的 `origin/HEAD`），不能 force-push，不能改写历史；**发起或合并 MR/PR 必须先告诉用户**。
- **交付时清理中间产物**：交付只留最终文件，不留临时脚本、草稿、日志、截图、缓存、重复/备份副本。

落地方式（`hitl/gitops.py`）：Agent 仍然不能直接跑 git 写命令，而是提议 `git_commit` / `git_push` / `open_mr`，由系统执行。`create_from_result` 对 `git_commit` / `git_push` 调用 `gitops.auto_allowed`：目标是非受保护的普通分支（不是 refspec、没有 `+`、远端名合法）就直接放行（审批记录 `decided_via=auto`，仍可在 Outbox 里回看），`git_commit` 带 `branch` 时系统会基于当前 HEAD 新建并切换到该分支再提交；受保护分支或可疑参数照旧进 Outbox（风险标 high）。执行前再核对一次，所以自动放行绝不能解锁默认分支。`open_mr`（`gh pr create`）永远需要用户批准，系统没有“合并”动作。提交还会拒绝凭据、缓存和 `*.bak` / `*.tmp` / `*.orig` 之类的残留文件。清理规则靠提示词（执行 + 质检）和提交过滤把关，不会自动删除工作区文件。`config/policy.toml` 的 `[git]` 可以关掉自动放行（`auto_branch_actions = false`）或增加受保护分支。

## 5. 执行层（`executors/`）

统一合同在 `executors/base.py`（`RunSpec` → `RunResult`），每个适配器负责：
- 构造命令行（无人值守模式 + JSON 事件流 + 结构化输出 schema）
- 流式解析事件 → `run_events`（消息 / 工具调用 / 用量）
- 超时和取消时用 `taskkill /T /F` 杀掉整个进程树；原始日志落盘
- 错误分类：transient（超时、限流、网络、无结构化输出）→ 退避重试；permanent → 失败并通知

| 执行器 | 命令 | 适用 |
|---|---|---|
| codex | `codex exec --json --output-schema S -o last.txt -s <sandbox> -C <ws> --skip-git-repo-check --ephemeral` | 研发、数据、规划、检索（订阅制，成本低）；`allow_connectors` 时读 Gmail/Notion/Drive |
| claude | `claude -p --output-format stream-json --verbose --json-schema S --model M --permission-mode … --allowedTools … --max-budget-usd B`（triage 用 `--bare --model haiku`） | 写作、文档、沟通草稿、triage |
| pi | 便携 Node ≥22.19 下的 `pi`（JSON / RPC 模式） | 轻量编码子任务（可选） |
| dsh | `@deepseek-ai/dsh`（便携 Node） | DeepSeek 生态任务（可选） |
| internal | Python 函数 | 确定性动作（复制文件、apply、发 webhook） |

**执行结果合同**（`brain/schemas.py::EXECUTE_SCHEMA`）：`status`、`summary`、`deliverables[]`、`verification{}`、`proposed_actions[]`、`decisions_needed[]`、`subtasks[]`、`followups[]`、`context_updates[]`。其中 `proposed_actions` 和 `decisions_needed` 会自动转成 Outbox 条目；`subtasks` 会生成子任务 DAG。


### 任务分配（`engine/router.py`）

谁来干一个任务，看三件事（理由写进 `task.meta.routing`，任务页“分配依据”里能看到）：

- **重要性**：P0、3 天内截止、或结果会交到别人手里（comms/career/admin/schedule 的 L3 以上）算“重要”；P3 算“不急”；其余一般。
- **难度**：分诊给的 `difficulty`（easy/medium/hard）优先，否则按 L 等级、是否要改代码或数据、材料多少估；失败过两次或返工过都会升一档（升级链）。
- **内容敏感度**：含邮件原文或个人事务类别（`routing.private_categories`）只交给 Codex / Claude，DeepSeek 通道（pi / dsh）永远拿不到；验证者同理。

重要性 × 难度 → 档位：**经济档**（例行、不急）先 dsh / pi，再 codex / claude 兜底；**标准档**先 codex（订阅制，没有边际成本）再 dsh；**高级档**（难或重要）先 claude / codex，便宜的只做最后选择。每档的顺序在 `config/awos.toml` 的 `[routing.tiers.*]`，只读分析和动手写分开配（pi 没有沙箱，只做只读）。标准档和高级档里，该类别最擅长的执行器排前面（`[routing.affinity]`），分诊的 `executor_hint` 只是软建议（经济档不采纳）；用户自己在任务里指定的执行器永远排第一（`meta.executor_pinned`），但仍受敏感度限制。今日额度用完的执行器排到最后，用到 85% 以上且任务不重要时后移。该档位没有可用执行器时退回类别默认路线 `routing.<category>`（后台的分诊 / 规划 / 简报等仍用这些路线，只交给 codex / claude）。执行失败后的自动切换沿用同一个顺序。重要的 L3 工作和所有 L4 / L5 工作会再让另一个（优先便宜的）执行器独立质检。

## 6. 调度与可靠性（`engine/`）

- **Tick（默认 15 秒）**：到期传感器 → triage 新 signal → 过期审批 → 依赖解锁（blocked→ready）→ 认领 ready 任务并分派到 worker 线程（全局并发 + 每个执行器并发上限 + 预算）→ 心跳续约 → 摘要推送。
- **租约 / 心跳**：`claim_next_task` 用 `BEGIN IMMEDIATE` 原子认领；worker 每 60 秒续约。daemon 重启后，`recovery` 把租约过期或进程已死的 running 任务标记为 interrupted，再退避重排。
- **重试**：`retry_backoff_seconds=[60,300,1800]`，最多 `max_attempts`（默认 3 次），之后转为 failed，进 Inbox 并通知。
- **幂等**：signal 用 fingerprint 去重，task 用 dedupe_key 去重，审批执行用 CAS（`approved→executing→executed`）保证只执行一次。
- **单实例**：`var/daemon.lock` 记录 pid，同时检测端口。**守护**：Windows 计划任务在登录时启动，另有每 10 分钟一次的 `awos ensure-running` 检查。
- **备份**：每天用 SQLite 在线备份到 `var/backups/`，保留 14 份；日志按大小轮转。

## 7. HITL：Inbox / Outbox / 通知

- **Inbox**（需要你看）：`triaged`（未自动启动）、`waiting_user`（AI 已做完自己那部分）、`review`（待验收）、`failed`。
- **Outbox**（需要你批）：`approvals.status=pending`。每一条都包含背景（context_md）、AI 推荐、可执行选项（approve / reject / 自定义选项，可附修改意见）、风险等级、过期时间和过期时的默认动作。
- **通知**：Windows 原生 Toast（不需要安装任何东西）+ 可选 webhook（企业微信机器人 / Server酱 / Bark / ntfy，用户提供 key 后启用）。静默时段只推送 high 风险项；每天早晚各一份摘要。
- **决策入口**：Dashboard 一键处理；CLI `awos approve <id> --option ...`；HMAC 签名的一次性链接（默认只在本机可用）。
- **批准后**：Action runner 执行载荷（例如 apply_to_project、发 webhook、通过 Codex 连接器创建邮件草稿或写 Notion），然后把决策和结果写回任务上下文，任务继续推进（`ready`）。

## 8. 个人上下文（`brain/context.py`）

- `context_facts` 表 + 渲染出来的 `var/context/profile.md`（Dashboard 可以编辑；用户编辑过的条目 `pinned`，Agent 不会覆盖）。
- 每日 context refresh 任务：汇总 claude-mem 观察、Claude/Codex 会话标题、git 活动、最近文档、Notion 页面、Zotero 新条目 → LLM 提出事实增删改 → 低风险直接写入并记录事件，涉及偏好和目标的改动作为 `proposed` 等你确认。
- 用途：triage 优先级判断（和当前项目、截止日期是否相关）、执行提示词里的背景、发现"机会"（opportunity）。
- **隐私**：上下文只存在本地 `var/`（gitignored）；发给模型的只有摘要片段，不含凭据、不含邮件全文。

## 9. 可观测性：Dashboard（`api/` + `dashboard/`）

页面：总览（健康状况、预算、队列）、Inbox、任务（列表 / 树状 DAG / 详情）、Outbox、运行（实时日志 + 工具调用）、工具调用记录、异常、交付物、上下文、传感器、历史（审计事件）。


**项目时间线与里程碑**（看板顶部切换“任务看板 | 项目时间线”，`#/board?mode=timeline&project=<key>`；`api/timeline.py`）：按项目回答三个问题——Agent 给你干了什么、干成什么样、你人为干预了什么。数据全部在读取时从 tasks、runs、approvals、artifacts、审计 events 和上下文事实推出，只有 `milestones` 表是自己的。任务归项目的顺序：话题关联，其次 `tasks.project` 和父任务，最后项目名特征词匹配（其余汇成“未归类”）。页面从上到下：统计（Agent 做了 / 完成 / 你干预了 / 等你处理 / 花费）、里程碑横轴、任务流程图（纯内联 SVG：任务方框 + 你拍板的节点，点方框开任务）、按天分组的动态流（你的操作蓝色加“你”头像，Agent 事件带各自执行器头像；筛选“全部 / Agent 做的 / 我干预的 / 异常”；事件只带短摘要不放原始日志，图片交付物显示缩略图）。你的干预是一等公民：审批、选择、拒绝、返工、取消、忽略、验收、推迟、新建任务、重新来过、改归属、里程碑编辑。里程碑 = 你手动设定的行（增删改走 `/api/milestones`）加自动推出的截止日期和 L4/L5 完成的交付（标“自动”，不可编辑）；已过目标日期仍是“计划中”的算逾期。

**收件箱的读法**（`js/domain/readable.js`）：每行一句白话结论或要求（“要你决定：…”“要你批准：…”“待你验收：…”“需要你接手：…”）加“AI 建议：…”，右侧只留一个相对时间和至多一个紧迫标记；项目标题写“N 项要你处理”和一句当前状态；风险 / 等级 / 优先级徽章、关键词标签、编号和成本折叠进阅读页的“详细信息”。阅读页先给“你需要做什么”（高亮 AI 推荐），再是前情提要，背景、来源、选项含义、时间线都折叠。

**外观（`dashboard/css/glass.css`，最后加载）**：对齐 macOS 的玻璃质感——页面背后是柔和壁纸，侧栏悬浮、工具栏毛玻璃（内容从它下面滑过）、卡片/抽屉/弹窗半透明加模糊，系统蓝作唯一强调色。动效是“流水”式：切换页面时可见的卡片按 `--i` 依次升起（只在导航时播放，SSE 实时刷新不重播），KPI 数字滚动，进度条生长，按压有轻微弹簧回弹，侧栏高亮块在条目之间滑动。`prefers-reduced-motion` 关闭动画，`prefers-reduced-transparency` 或不支持 `backdrop-filter` 时退回实色。总览页的“白板”卡片（`/api/whiteboard`）显示所有 Agent 共同遵守的规则，可在卡片上直接添加，管理在上下文页。
操作：重试 / 取消 / 修改等级与优先级 / 驳回 / 推迟 / 新建任务 / 审批 / 立即运行传感器 / 编辑上下文。
实时：SSE `/api/stream` 推送事件总线消息。

## 10. 安全

- API 默认只监听 `127.0.0.1`；写操作需要带上 `var/api_token` 里的 token（Dashboard 由服务端注入），防止 CSRF 和其他本机进程误调用。
- 执行器子进程会清理环境变量（移除本系统自己的密钥）；Agent 拿不到 `var/secrets.toml`。
- 沙箱：Codex `-s read-only|workspace-write`；Claude `--permission-mode` + 工具白名单；写入真实项目目录必须经过 `apply_to_project` 审批（L5 白名单目录除外）。
- 所有对外动作都是"提议"，由 Action runner 在获批后执行，Agent 运行时不直接执行。执行器提示词明确禁止，事后再审计 `run_events` 中的工具调用，发现发信类调用就告警。

## 11. 可迁移性与版本管理

- 仓库内容 = 代码 + 文档 + 默认配置；`var/`（数据）、`runtime/`（便携 Node、npm 前缀）、`config/local.toml`、`var/secrets.toml` 都不进 git。
- 迁移到新机器：`git clone` → `scripts/install.ps1`（uv sync、便携 Node、pi/dsh 安装、计划任务注册）→ 拷贝 `var/`（可选）。
- 文档：`docs/research/*`（调研）、`docs/decisions/*`（ADR）、`docs/runbook.md`（运维）、`docs/playbook.md`（工具使用经验）。

## 12. 模块归属（并行开发切分）

| 模块 | 文件 | 负责人 |
|---|---|---|
| 存储 / 状态机 / 配置 | `db.py` `repo.py` `models.py` `config.py` `schema.sql` | 主控（已完成） |
| 执行器 | `executors/*` | 子 Agent E |
| 传感器 | `sensors/*` | 子 Agent S |
| API + Dashboard | `api/*` `dashboard/*` | 子 Agent A |
| 通知 + 服务脚本 | `hitl/notify.py` `hitl/tokens.py` `scripts/*.ps1` | 子 Agent H |
| Triage / 策略 / 规划 / 上下文 / 调度 / Worker / Outbox / CLI | `brain/*` `engine/*` `hitl/outbox.py` `cli.py` `llm.py` | 主控 |
| 架构评审与任务拆分复核 | — | Codex（`codex exec`） |
