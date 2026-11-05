# ADR 记录（Architecture Decision Records）

每条 ADR：背景 → 决策 → 后果。修改决策时追加新条目，不删除旧条目。

---

## ADR-001 自建轻量任务中心，借鉴 Multica / Paperclip / Super-Agent 的设计（2026-11-05）

**背景**：用户希望用一个类似 Multica 的统一任务中心。调研（research/02）结论如下：
- Multica 约 40 万行 Go 代码、638 个迁移，自托管需要 Docker + PostgreSQL 17，本机没有 Docker；每天约 10 次提交，升级成本高。它没有 L1–L5 等级和 Outbox 对象，工具调用一律自动批准。
- Paperclip 的治理模型最好（审批、预算），但它内嵌的 Postgres 18 beta 在 Windows 上有多个已知 bug。
- Vibe Kanban 已宣布停止维护。Symphony 只有规范和一个 Elixir 参考实现。
- dsh-superagent 依赖 POSIX（tmux、fcntl），只服务单个 GitHub 仓库。

**决策**：用 Python 3.13 + SQLite(WAL) + FastAPI + 无构建步骤的静态 Dashboard 自建核心。借鉴的设计：
- Multica：issue→agent→runtime 模型、review gate、autopilot、运行日志、重试与超时
- Paperclip：哈希绑定的审批、预算硬上限
- Symphony：退避公式
- Super-Agent：Source→Work item→Session→回到来源回复的链路，"最少占用人的注意力"，Attention 作为唯一提问通道

**后果**：没有任何外部服务依赖，迁移只需要 `git clone` + `scripts/install.ps1`。代价是需要自己维护调度与状态机（用测试覆盖）。将来可以把 AgentWorkOS 注册成 Multica 的一个 runtime（Multica 支持 claude/codex/pi/dsh 子进程协议）。

## ADR-002 执行器 = 现成的 Agent CLI 子进程，统一 RunSpec/RunResult 合同（2026-11-05）

**背景**：本机已经登录了 Codex（ChatGPT 订阅，含 Gmail/Notion/Drive 连接器）和 Claude Code（按量计费网关）；Pi、DSH 是用户点名要整合的开源 Agent。

**决策**：不自己写 Agent loop。每个执行器是一个适配器：构造无人值守命令行，经 stdin 传入 prompt，解析 JSONL 事件流，按 JSON Schema 输出结构化结果，超时后杀进程树。路由按任务类别配置（`config/awos.toml [routing]`）。

**后果**：可以直接复用各 CLI 的工具、技能和登录态；新增执行器只需要写一个适配器。各 CLI 的行为差异记录在 `docs/playbook/executors.md`。

## ADR-003 便携 Node 24 运行时放在 runtime/，不改动系统 Node（2026-11-05）

**背景**：系统 Node 是 22.17，Pi 要求 ≥22.19，DSH 要求 ≥24.9；系统 Node 正在支撑 Claude Code、Codex 等全局工具。

**决策**：`scripts/install.ps1` 下载经 SHA256 校验的 Node 24 便携版到 `runtime/node`；Pi 和 DSH 用 `node <entry.js>` 方式安装和调用（npm 的 `.cmd` shim 在带空格的路径下会失败，已实测）。

**后果**：路径里有中文和空格（`D:\phd`）；Pi 1.5.0 和 DSH 0.2.2-rc.1 已实测可以运行。如果以后遇到不支持非 ASCII 路径的工具，可以在 `config/local.toml` 里把 `paths.workspaces` 和 `paths.node_runtime` 指向一个纯 ASCII 目录。

## ADR-004 单一共享 SQLite 连接 + 可重入锁（2026-11-05）

**背景**：Codex 架构评审（docs/reviews/codex-architecture-review.md R02/R03）指出：
- Python 自带的 SQLite 是 3.50.4，受 WAL-reset 公告影响（3.50.7 修复），风险出现在多连接并发写和 checkpoint 时；
- 原先每个线程一个连接，`tx()` 不能嵌套。

**决策**：每个数据库文件只有一个进程内共享连接，所有访问经 `db.LOCK`（RLock）串行；`tx()` 可以嵌套，内层事务并入外层；事件总线在提交之后才推送（`db.after_commit`）；`synchronous=FULL`。

**后果**：负载很小（个人单机），串行化几乎没有性能代价。CLI 进程偶尔会打开第二个连接，可以接受。

## ADR-005 L1–L5 是"AI 自主推进到哪一步"，所有对外动作都审批（2026-11-05）

**决策**：
- 等级决定交接点：L1→waiting_user（附 briefing）、L2→决策、L3/L4→review、L5→done（需要第二个执行器独立质检通过）。
- 有效等级 = min(triage 评估, 类别上限, 能力上限)。
- 发邮件、发消息、发布、push、写日历/Notion、删除、花钱、回写项目目录等对外动作，不论等级都进 Outbox。Agent 只能"提议"，由 Action runner 在批准后按哈希绑定的载荷执行一次；如果执行中途崩溃，标记为 failed（结果未知）并交人工核对，不自动重发。

**后果**：AI 永远不会在用户不知情的情况下对外发声。代价是对外动作需要一次点击确认，这正是用户要求的 HITL。

## ADR-006 系统时区强制为 Asia/Shanghai（2026-11-05）

**背景**：Windows 系统时钟设成了 UTC-4，用户实际作息按北京时间（活跃时段约 12:00–24:00）。

**决策**：所有时钟相关逻辑（静默时段、摘要时间、传感器活跃时段、成本日）都通过 `awos.timeutil` 按 `timezone = "Asia/Shanghai"` 计算；数据库存 UTC。

## ADR-007 Codex 作为个人数据连接器网关（2026-11-05）

**背景**：Codex 的 ChatGPT 连接器（Gmail、Notion、Google Drive、Calendar）已经认证；用户的日程主要在 Notion。网易邮箱大师的本地库是明文 SQLite（学校和 163 邮箱）。

**决策**：Gmail/Notion 传感器通过 `codex exec` + 结构化输出读取；学校和 163 邮件通过只读打开网易邮箱大师的本地库（不需要再申请 IMAP 授权码）。微信和企业微信的数据库是加密的，**不读取**。

**后果**：不需要用户额外提供任何 API Token 就能感知邮件和日程；代价是 Codex 连接器调用比较慢（每次 1–3 分钟），所以把轮询间隔设为 3 小时，并只在活跃时段运行。
