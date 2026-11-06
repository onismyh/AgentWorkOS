# AgentWorkOS 运维手册（Runbook）

> 适用：Windows 10/11（主要平台），以及 macOS（见 §2.1）；当前用户登录时在后台运行。仓库路径含空格和中文（`D:\phd\AgentWorkOS`），下文所有命令都已按这种路径测试过。
> 约定：`<root>` = 仓库根目录；PowerShell 命令在 `<root>` 下执行。

## 1. 新机器安装

前置：git、[uv](https://docs.astral.sh/uv/)（没有 uv 时 `install.ps1` 会打印安装命令后退出）、能访问 PyPI / nodejs.org（本机走系统代理 `127.0.0.1:7890`）。

```powershell
git clone <repo-url> AgentWorkOS
cd AgentWorkOS
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install.ps1            # 可重复执行
# 可选开关：-RegisterService（顺带注册计划任务）  -AddToPath（把 scripts\ 加入用户 PATH，之后可直接敲 awos）
#           -SkipNode  -ForceNode（删掉 runtime\node 重新下载）  -NodeMajor 24  -Proxy http://127.0.0.1:7890
```

`install.ps1` 依次完成以下步骤，每一步都是幂等的：

1. 检查 `uv`。
2. 如果 `.venv` 不存在，执行 `uv venv --python 3.13` 创建；已经存在则保留（如果版本不是 3.13，只给出警告）。
3. 执行 `uv pip install -e .[dev]`。
4. 创建 `var\` 下的各个子目录：workspaces、artifacts、logs、runs、backups、context、inbox-drop。
5. 如果 `runtime\node\node.exe` 不存在，下载**便携 Node.js v24 LTS**（win-x64 zip）。版本从 `https://nodejs.org/dist/index.json` 解析，按 `SHASUMS256.txt` 校验 SHA256 后再解压。**不改动系统 Node**（本机 `E:\nodejs` v22.17）。下载走 Windows 系统（IE）代理，也可以用 `-Proxy` 指定。

迁移旧数据：把旧机器的 `var\` 整个拷过来（见 §6），然后注册服务（§2）。

CLI 用法：`scripts\awos.cmd <子命令>`，例如 `status`、`inbox`、`approve <id> [option]`、`doctor`、`backup`。也可以加 `-AddToPath`，之后直接敲 `awos ...`。启动器只从 `%~dp0` 推导路径，并临时切换到 `chcp 65001`，退出时恢复原来的代码页。

## 2. 后台服务（计划任务）注册 / 注销

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\service-install.ps1 -DryRun   # 只打印，不改动（-WhatIf 同义）
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\service-install.ps1           # 注册并立即启动 Daemon
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\service-uninstall.ps1         # 先注销 Watchdog → awos stop → 再注销 Daemon
```

| 任务 | 触发 | 动作 | 关键设置 |
|---|---|---|---|
| `\AgentWorkOS\Daemon` | 当前用户登录（延迟 30 秒）；注册后立即启动 | `.venv\Scripts\pythonw.exe -X utf8 -m awos.cli daemon`，工作目录 `<root>` | 无执行时限；失败后每 1 分钟重启（最多 999 次）；`IgnoreNew`；允许用电池运行，切换到电池不停止 |
| `\AgentWorkOS\Watchdog` | 每 10 分钟一次，无限重复 | `pythonw.exe -X utf8 -m awos.cli ensure-running` | 时限 5 分钟；`IgnoreNew` |

- 两个任务都以**当前用户、仅登录时（Interactive）、非提权**运行。`pythonw` 不弹窗口。任务设置了 Hidden，在任务计划程序 GUI 中要勾选"查看 → 显示隐藏的任务"才能看到。
- 注册时如果报 `Access is denied (0x80070005)`，在**管理员** PowerShell 里执行一次即可（任务本身仍然以你的身份、非提权运行）。
- 重新执行 `service-install.ps1` 会覆盖原有定义（`-Force`），可用来更新。
- 临时暂停：`awos stop` 只能停掉当前进程，Watchdog 会在 10 分钟内把它重新拉起来。要真正暂停：

  ```powershell
  Disable-ScheduledTask -TaskPath '\AgentWorkOS\' -TaskName Watchdog; scripts\awos.cmd stop
  # 恢复：
  Enable-ScheduledTask -TaskPath '\AgentWorkOS\' -TaskName Watchdog; Start-ScheduledTask -TaskPath '\AgentWorkOS\' -TaskName Daemon
  ```

- 查看状态：`scripts\awos.cmd status`；`Get-ScheduledTask -TaskPath '\AgentWorkOS\' | Get-ScheduledTaskInfo`（`LastTaskResult=0` 表示正常）。
- **健康检查不要用 `Invoke-WebRequest`**：本机系统代理不会绕过 127.0.0.1，PS 5.1 的请求会被发往代理而失败。改用 `scripts\awos.cmd status`，或者 `curl.exe --noproxy "*" http://127.0.0.1:8765/`。

### 2.1 macOS

前置只有 git 和 curl（Claude Code 按官方方式自行安装并登录）。`install.sh` 一键装好其余全部，可重复执行：

1. 没有 uv 就装到 `~/.local/bin`；建 `.venv`（Python 3.13）并装依赖；建 `var/` 子目录。
2. Node ≥ 22.19：优先 `runtime/node`，其次 PATH 上的 `node`；都没有就从 nodejs.org 下载便携 Node LTS 到 `runtime/node`（按 SHASUMS256 校验），不动系统 Node。
3. `awos setup`：把 codex、pi、dsh 按 `config/awos.toml [runtimes]` 里固定的版本装进 `runtime/<名字>`（npm，不跑安装脚本，不装到全局；PATH 上已有的 CLI 优先）；然后找本机已有的凭据接上（见 §2.2）。

```bash
scripts/install.sh                  # 一键安装；--service 顺带注册 launchd，--skip-runtimes 不装 agent CLI
scripts/awos setup                  # 只重装 agent CLI / 重新找凭据（例如刚设置了 DEEPSEEK_API_KEY 之后）
scripts/awos doctor                 # 体检：执行器、传感器、daemon 状态
scripts/awos daemon                 # 前台运行（调试）；或 scripts/awos ensure-running 在后台启动（独立会话，关掉终端不受影响）
scripts/service-install.sh          # 注册 LaunchAgent（登录自启 + 崩溃自动重启）；--dry-run 只打印 plist
scripts/service-uninstall.sh        # 注销 LaunchAgent 并停止 daemon，var/ 保留
```

- LaunchAgent：`~/Library/LaunchAgents/com.agentworkos.daemon.plist`，`RunAtLoad` + `KeepAlive{SuccessfulExit=false}`：崩溃后由 launchd 拉起（间隔 60 秒），`awos stop` 这种正常退出不会被拉起，因此不需要 Windows 上的 Watchdog。launchd 只给最小 PATH，注册时会把当前终端的 PATH 写进 plist，安装了新的 CLI（codex、gh 等）后重跑一次 `service-install.sh`。
- `awos ensure-running` 在 macOS 上优先 `launchctl kickstart` 已加载的 LaunchAgent，否则直接后台启动。`launchctl bootout`/`kill` 发送的 SIGTERM 会让 daemon 像 stop 文件那样正常退出。
- 本机覆盖写在 `config/local.toml`（不进 git）：`sensors.git.roots` / `sensors.files.roots` 改成 Mac 上的目录；Mail Master 的本地库只在 Windows 上，`sensors.mailmaster.enabled = false`；Zotero 默认数据目录是 `~/Zotero`。
- 通知：`toast` 通道在 macOS 上用 `osascript` 发到通知中心（来源显示为"脚本编辑器"，在 系统设置 → 通知 里允许它）。`action`/`high` 级别带提示音；点击通知不会打开链接，链接在控制台和手机推送里。
- `gh`、`node` 在 macOS 上找不到 `runtime/` 下的副本时，会用 PATH 上的版本；Windows 上仍然只用 `runtime/` 下的。
- 执行器子进程放在独立的进程组里，超时、取消或恢复时整组杀掉（对应 Windows 的 Job Object / `taskkill /T`）。
- 系统代理：macOS 上 GUI 应用自动走"系统设置"里的代理，命令行工具不会。环境变量里没有 `HTTPS_PROXY` 时，执行器会读 `scutil --proxy` 把系统代理传给 agent（`network.use_system_proxy = false` 可关）。

### 2.2 Agent 凭据（`awos setup`）

只引用本机已有的登录，不复制、不打印密钥；`var/secrets.toml` 已有的段落不改，新写的段落文件权限 0600。

| 执行器 | 找什么 | 写到哪里 |
|---|---|---|
| codex | `~/.codex/auth.json`（ChatGPT 登录，与 Codex 应用/CLI 共用）；没有登录但环境里有 `OPENAI_API_KEY` 时执行 `codex login --with-api-key` | Codex 自己的 `~/.codex` |
| dsh | 环境变量 `DEEPSEEK_API_KEY`；否则 DeepSeek Harness 应用的账号登录 `~/.dsh/.credentials.yaml` | `[dsh] env` 或 `[dsh] provider/model/credentials`（登录文件以链接方式接入隔离的 `runtime/dsh-home`） |
| pi | 环境变量 `DEEPSEEK_API_KEY`；否则已有的 Pi 登录 `~/.pi/agent/auth.json` | `[pi] env` 或 `[pi] agent_dir` |
| claude | 用 Claude Code 自己的登录 | — |

DeepSeek 账号登录只能给 dsh 用；pi 需要 DeepSeek 开放平台的 API key。设置 `DEEPSEEK_API_KEY` 后重跑 `scripts/awos setup` 即可。

### 2.3 Agent 沙箱（最小权限）

原则：默认只给完成任务通常需要的权限，越过边界时不是失败，而是**请你批准**（human in the loop）。配置在 `[sandbox]`，实现在 `src/awos/executors/sandbox.py`、`egress.py`。

**默认能做的**

- 在本次任务的工作区里读写（只读任务不能写），装依赖：`npm/pip/uv/cargo/go` 的缓存放在本次运行的私有临时目录里，不写 `~/.npm`、`~/.cache`。
- 命令行联网到常用的包仓库和代码托管站点：pypi、npm、GitHub、GitLab、Gitee、crates、Go proxy、conda、CTAN、Hugging Face 和常见国内镜像。列表见 `DEFAULT_DOMAINS`，可以用 `sandbox.allowed_domains` 改。本机设了系统代理时自动经它出去。
- 读用户的项目目录和可读目录。

**一律拦下的**

- 写工作区以外的地方。
- 读凭据（`~/.ssh`、`~/.aws`、各 agent 的登录文件、`var/secrets.toml` 等）。
- 读 AgentWorkOS 的数据：`var/` 里有数据库里的全部邮件、其他任务的工作区、运行日志、API token。
- 读其他 agent 的历史（`~/.codex`、`~/.claude`、`~/.dsh`、`~/.pi`）和邮件/信息。
- 访问本机和局域网（控制台 API、ssh-agent、docker 等 unix socket）。
- 访问名单以外的域名。
- 用 `osascript`、`open` 启动沙箱外的程序。

**需要更多权限时（human in the loop）**

- 网络：agent 在 `proposed_actions` 里提议 `grant_network`（要哪些域名、为什么）。pi、dsh 的出站全部经过 AgentWorkOS 的过滤代理，它被拦下的域名系统会记下来，agent 没主动申请时也会自动生成一条"放行网络"的审批。你批准后只对这个任务放行，任务带着这些域名自动重跑。
- 写你的项目：照旧走 `apply_to_project` 审批。
- 提交代码：见 §2.4。

**各执行器怎么落实**（macOS 不允许 Seatbelt 嵌套，能配严的用它自己的沙箱，其余的套 AgentWorkOS 的 `sandbox-exec`）

| | 文件 | 网络 |
|---|---|---|
| claude | Claude Code 自带沙箱，没有"不走沙箱"的后门（`allowUnsandboxedCommands=false`）；Read/Edit 对凭据和 `var/` 敏感文件另有拒绝规则 | 沙箱的 `allowedDomains`，Claude 自己的代理执行，会告诉 agent 哪个域名被拦了 |
| codex | Codex 权限配置（`permissions.awos`），实测优先于用户配置里的 `sandbox_mode`；低于 0.167 退回 `-s` | Codex 自带的 network proxy + 域名名单 |
| dsh | 整个进程套 Seatbelt；`runtime/dsh-home` 只有数据目录可写，配置层、`AGENTS.md`、skills、`.env` 只读 | 只能连本次运行的过滤代理（DeepSeek API + 名单，拒绝内网地址），被拦的域名会记下来 |
| pi | 整个进程套 Seatbelt；`runtime/pi-home` 只有 `tmp/` 和两个缓存文件可写，工具（rg、fd 由 setup 预装）、设置、MCP 配置、扩展只读 | 同 dsh |

四个执行器在 macOS 上都实测过：在工作区里 `npm install` 成功；访问 example.com、读 `~/.ssh` 里的探针文件、写工作区外、读 `var/api_token`、连本机服务器都被拦下。

所有执行器的环境变量都会去掉看起来像凭据的变量，只保留各自登录要用的前缀。Windows/Linux 上 codex、dsh 用各自的原生沙箱，claude 在 Linux 上有 bubblewrap 时才开沙箱。

### 2.4 Git：agent 分支

- 只有 `agent/<主题>` 分支上的 `git_commit` / `git_push` 不需要批准（`config/policy.toml [git]`）。提交用 git 底层命令直接写到分支上（私有 index → `commit-tree` → 带旧值校验的 `update-ref`），不检出任何东西，所以：
  - 你当前的分支、暂存区和工作区都不会被切换或改动；
  - 分支里的符号链接不会把文件写到仓库外；
  - 两个提交同时写同一个分支时，后一个会失败而不是把前一个覆盖掉。
- 仓库配置了 git hooks（pre-commit、commit-msg 等，含 `core.hooksPath`/husky）时，hooks 照常运行：
  - 系统在 `var/tmp` 里建一个临时副本（`clone --shared`），在沙箱里正常 `git commit`。hooks 能格式化文件，改动会进提交；不通过就不提交，并把输出写进审批结果。
  - 沙箱里只有这个副本可写；你装好的 `node_modules`、`.venv` 以只读方式接进去；联网走同样的域名名单。
  - 提交取回到你的仓库，用带旧值校验的方式写入分支。
  - 没有沙箱的平台上，这类提交要你批准后才运行 hooks。
- 其他分支、main/master 和远端默认分支、MR/PR 都要批准。
- 敏感任务的 git 动作一律要批准，判断时会沿着上级任务一路往上查，所以拆出来的子任务也算：
  - 实际层级（按类别上限算）是 L1/L2 或只读沙箱的任务；
  - 私人类别（`routing.private_categories`）的任务；
  - 带邮件或私人内容、只能交给可信执行器的任务；
  - 来自 `no_auto_sources`（mailmaster、gmail、notion、github）的任务。

## 3. 日志和数据在哪里

| 内容 | 位置 |
|---|---|
| 状态库（唯一状态源，WAL） | `var\awos.db`（另有 `awos.db-wal` 和 `awos.db-shm`） |
| daemon 日志（5 MB × 5 轮转） | `var\logs\daemon.log` |
| 每次 Agent 运行的 prompt / 原始输出 / 结果 | `var\runs\<run_id>\` |
| 任务隔离工作区 / 交付物 | `var\workspaces\<task_id>\`、`var\artifacts\` |
| 自动备份（每天一份，保留 14 份） | `var\backups\awos-YYYY-MM-DD.db`；手动备份用 `awos backup` |
| 个人上下文 | `var\context\profile.md` |
| 单实例锁 / 停止信号 | `var\daemon.lock`、`var\daemon.stop` |
| 密钥（都在 gitignore 中） | `var\secrets.toml`（推送通道）、`var\link_key`（审批链接 HMAC 密钥，首次使用时自动生成）、`var\api_token` |
| 便携 Node | `runtime\node\` |
| 通知记录 | `notifications` 表（Dashboard，或 `sqlite3 var\awos.db "select created_at,channel,status,error,title from notifications order by created_at desc limit 20"`） |
| 计划任务历史 | 事件查看器 → Microsoft-Windows-TaskScheduler/Operational（需要先启用"所有任务历史记录"） |

建议给 `var\` 收紧权限。D 盘根目录默认允许 Authenticated Users 修改，同机的其他账户可能读到密钥：

```powershell
icacls var /inheritance:r /grant:r "${env:USERNAME}:(OI)(CI)F" "SYSTEM:(OI)(CI)F" "Administrators:(OI)(CI)F"
```

## 4. 通知通道

### 4.1 行为

- **默认通道是 Windows 原生 Toast**：PowerShell 5.1 + WinRT，不用安装任何东西。所有在 `var\secrets.toml` 的 `[notify]` 段里填了密钥的手机通道会**自动启用**。
- **静默时段**：`scheduler.quiet_hours`，按北京时间计算，默认 01:30–09:30，可以跨午夜。这段时间只推送 `level="high"` 的通知，其余记为 `suppressed`。
- **去重**：同一个 `title + ref_id` 在 30 分钟内只发一次。失败的发送不计入去重，下次会重试。
- **隐私**：云端通道（企业微信、飞书、Server酱等）只收到标题和不超过 200 字的摘要（`notify.phone_body_chars`），完整内容只留在本机 Dashboard。错误信息入库前会抹掉密钥。
- **相关配置**：写在 `config\local.toml` 的 `[notify]` 段：
  - `disabled_channels = ["pushplus"]`：禁用某个已配置的通道。
  - `webhook_min_level = "action"`：info 级通知只走 Toast，不推到手机。
  - `toast_app_id = "..."`：已注册品牌 AUMID 时使用。
- **改完 `secrets.toml` 不需要重启**：每次发送都会重新读取。
- **链接**：通知里的链接指向 `http://127.0.0.1:8765/...`，**只能在这台电脑上打开**。手机上看到的是提醒，审批请回到电脑上处理（或等以后接入 IM 卡片回调）。

### 4.2 配置手机推送（`var\secrets.toml`，示例全部是占位符）

```toml
# var/secrets.toml —— 已在 gitignore 中，绝不要提交；只填你需要的通道
[notify]
# 企业微信群机器人（推荐）：企业微信 → 建一个只有自己的内部群 → 群设置 → 添加群机器人 → 复制 Webhook 地址。每个机器人每分钟最多 20 条
wecom_webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=YOUR-WECOM-ROBOT-KEY"

# 飞书自定义机器人：群设置 → 群机器人 → 添加机器人 → 自定义机器人；建议开启"签名校验"，并把密钥填到 feishu_secret
feishu_webhook = "https://open.feishu.cn/open-apis/bot/v2/hook/YOUR-HOOK-ID"
feishu_secret  = "YOUR-FEISHU-SIGN-SECRET"     # 可选

# Server酱 Turbo：sct.ftqq.com 微信扫码 → SendKey（SCT 开头）。免费版每天 5 条；Server酱³ 的 sctp 开头密钥也支持
serverchan_key = "SCTxxxxxxxxxxxxxxxxxxxxxxxx"

# Bark（仅 iPhone）：在 App 首页复制形如 https://api.day.app/<device_key> 的地址
bark_url = "https://api.day.app/YOUR-DEVICE-KEY"

# ntfy：手机 App 订阅一个难猜的 topic（国内可达性不稳定，不推荐作为主通道）
ntfy_url   = "https://ntfy.sh/awos-CHANGE-ME-random-topic"
ntfy_token = ""                                # 可选：自建服务或保留 topic 时使用

# PushPlus（需要实名认证）
pushplus_token = "YOUR-PUSHPLUS-TOKEN"

# 发邮件给自己（可选，适合收摘要）：163 授权码，或学校邮箱的客户端专用密码
smtp_host     = "smtp.163.com"
smtp_port     = 465                            # 465 = SSL；587 = STARTTLS
smtp_user     = "you@163.com"
smtp_password = "YOUR-SMTP-AUTH-CODE"
smtp_to       = "you@163.com"                  # 默认等于 smtp_user
```

测试某个通道（会真的发出一条消息）：

```powershell
.venv\Scripts\python.exe -X utf8 -c "from awos.hitl.notify import notify; print(notify('AgentWorkOS 测试', '手机推送测试', level='action', channels=['wecom'], dedupe=False))"
```

然后查看 `notifications` 表里这一行的 `status` 和 `error`（§3）。

### 4.3 审批链接（`hitl/tokens.py`）

- 链接格式为 `http://127.0.0.1:<port>/approve/<token>`，用 HMAC-SHA256 签名，密钥是 `var\link_key`。数据库里只保存 `sha256(token)`。
- 每个审批**同一时间只有一个有效链接**，重新签发会让旧链接作废。审批一旦被处理、过期、取消，或者 payload 发生变化，链接立即失效。
- 有效期默认 24 小时，并且不会超过审批本身的 `expires_at`。
- 删除 `var\link_key` 等于轮换密钥，所有未使用的链接都会失效。

## 5. Toast 不显示怎么排查

当前方案使用的 AppUserModelID 是 Windows PowerShell 自带的 `{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe`。这个 ID 在系统里默认就存在，所以零配置。代价是通知的来源会显示为 **"Windows PowerShell"**，正文底部带 "AgentWorkOS" 署名。

1. **看记录**：在 `notifications` 表里找 `channel='toast'` 的行。
   - `suppressed` + `quiet hours`：当前处于静默时段，属于正常行为。
   - `suppressed` + `AWOS_NOTIFY_DISABLE`：环境变量关闭了通知。
   - `failed` + `toasts disabled: DisabledForApplication/DisabledForUser`：在系统设置中被关闭了，见下一步。
   - `failed` + `timed out`：PowerShell 20 秒没有返回，通常是机器太忙或杀毒软件拦截。
   - 没有记录：可能被 30 分钟去重跳过了，或者调用方根本没有调用 notify。
2. **系统设置**：设置 → 系统 → 通知和操作 → 打开"获取来自应用和其他发送者的通知"，并确认列表里的 **Windows PowerShell** 是开启的。
3. **专注助手 / 勿扰**：只会隐藏弹窗，通知仍然在操作中心（Win+A）里。
4. **会话**：任务以 Interactive 方式运行，所以必须有用户登录。如果被改成"不管用户是否登录都运行"，或者以 SYSTEM 身份运行，Toast 不会显示。
5. **直接诊断**（PowerShell 5.1；PowerShell 7 不支持这种 WinRT 写法）：

   ```powershell
   [void][Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
   $id = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
   [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($id).Setting      # 期望 Enabled
   @([Windows.UI.Notifications.ToastNotificationManager]::History.GetHistory($id)).Count      # 操作中心里的条数
   ```

6. **端到端测试**（会弹出一条通知）：

   ```powershell
   .venv\Scripts\python.exe -X utf8 -c "from awos.hitl.notify import notify, dashboard_url; print(notify('AgentWorkOS 测试通知', '点按钮打开控制台', level='action', url=dashboard_url(), channels=['toast'], dedupe=False))"
   ```

7. 点击通知或按钮会打开浏览器访问 `http://127.0.0.1:8765/`，如果打不开，说明 daemon 没在运行（`awos status`）。
8. 想要显示"AgentWorkOS"品牌名：需要一个带 `System.AppUserModel.ID` 属性的开始菜单快捷方式（目前未实现）。实现后，在 `config\local.toml` 中设置 `[notify] toast_app_id = "AgentWorkOS"`。在没有完成注册之前**不要**设置这一项，否则 Toast 会静默失败。

## 6. 备份与恢复 `var\`

**自动备份**：daemon 每天用 SQLite 在线备份 API 生成 `var\backups\awos-YYYY-MM-DD.db`，保留 14 份。随时可以执行 `scripts\awos.cmd backup` 手动备份一次。

**完整备份**（例如迁移前）：

```powershell
Disable-ScheduledTask -TaskPath '\AgentWorkOS\' -TaskName Watchdog; scripts\awos.cmd stop    # 停掉 daemon，保证一致性
robocopy var D:\Backup\AgentWorkOS-var /MIR /XD workspaces /R:1 /W:1   # workspaces 可再生成，按需排除
Enable-ScheduledTask -TaskPath '\AgentWorkOS\' -TaskName Watchdog; Start-ScheduledTask -TaskPath '\AgentWorkOS\' -TaskName Daemon
```

- daemon 运行时**不要**只复制 `awos.db`：WAL 里可能还有没合并的数据。在线备份请用 `awos backup`。
- `secrets.toml`、`link_key`、`api_token` 都是敏感文件，备份的存放位置也要保密。

**恢复**：

1. 停止 daemon（Disable Watchdog + `awos stop`，或者执行 `service-uninstall.ps1`）。
2. 整体还原：把备份目录拷回 `<root>\var`。
   只还原数据库：把 `var\backups\awos-YYYY-MM-DD.db` 复制为 `var\awos.db`，并且**删除旧的 `var\awos.db-wal` 和 `var\awos.db-shm`**（它们属于被替换的那个库，留着会损坏数据）。
3. 启动：`Enable-ScheduledTask ... Watchdog` + `Start-ScheduledTask ... Daemon`（或执行 `service-install.ps1`），然后用 `awos status` / `awos doctor` 检查。
4. 恢复到旧库之后，旧库中仍为 pending 的审批会重新出现。之前发出的审批链接如果对不上 `token_hash`，会自动失效，在 Dashboard 里重新处理即可。
