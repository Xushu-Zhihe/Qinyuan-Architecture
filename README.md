# Web 多 CLI Agent 黑板协作平台

一个运行在 localhost 的多 Agent 协作工具。每个项目拥有独立的图黑板、SQLite 数据库、Agent Supervisor、执行目录和实时事件频道。Codex CLI、Claude Code、Kimi CLI 等 provider 可以在同一项目内异步工作，不同项目也可以并发运行。

Web 工作区使用节点表示 `Goal`、`Origin`、`Fact` 和 `Hint`，使用第一类边表示 `Intent`。每条 Intent 可以包含多个独立 Agent execution；stdout、stderr、退出状态和工作目录均保留并可在页面中查看。

## 主要能力

- 创建“仅黑板”或“立即启动 Agent”项目。
- 在项目创建时选择 provider 和项目内最大 Agent 并发数。
- 使用 React Flow 浏览、拖动、创建和连接图元素。
- 将 Intent 建模为边，不再创建 Intent 节点。
- 同一 Intent 合并语义边，同时保留各 provider 的独立 execution 和日志。
- 通过项目级 WebSocket 实时接收图事件和 stdout/stderr 分块。
- 断线后按事件序号和日志字节偏移自动补读，避免实时日志丢失或重复。
- 按实际到达顺序持久化“全部”日志；Claude 可读输出与脱敏原始 JSONL 分开审计。
- 从 Fact 追溯 producing Intent、execution 和 provider。
- 持久化 Planning Attempt、调度等待、健康检查、熔断、恢复和执行指标。
- 分页读取受大小与保留策略约束的日志，并导出二次脱敏的项目审计包。
- 从全局运行面板查看配额、排队任务、活动任务和 provider 健康状态。
- 在项目诊断侧栏筛选规划/执行记录、查看日志、人工重试和受控重新打开项目。
- 同时运行多个相互隔离的项目。
- 项目可安全移入 `web-data/.trash/`，不会触碰外部 `challenge_path` 或 workspace。
- 编辑模式可归档/恢复人工 Fact、Hint，并删除没有执行历史的边；浏览模式为只读。
- 通过 FastAPI 提供 REST、WebSocket 和生产前端静态资源。

## 架构

```mermaid
flowchart TD
    Browser["React + React Flow"] <-->|"REST / WebSocket"| API["FastAPI"]
    API --> Registry[("projects.db")]
    API --> Manager["ProjectService"]
    Manager --> A["ProjectSupervisor A"]
    Manager --> B["ProjectSupervisor B"]
    A --> ADB[("Project A blackboard.db")]
    B --> BDB[("Project B blackboard.db")]
    A --> Executor["async CLI executor"]
    B --> Executor
    Executor --> Codex["Codex CLI"]
    Executor --> Claude["Claude Code"]
    Executor --> Kimi["Kimi CLI"]
```

每个启用的 provider 在项目 Supervisor 下运行独立循环：

1. 读取当前图快照。
2. 输出 `BLACKBOARD_INTENT`，登记或复用 Intent 边。
3. 创建独立 execution 并执行该 Intent。
4. 流式保存和发布 stdout/stderr。
5. 输出 `BLACKBOARD_RESULT`，事务性合并 Fact、普通关系和最终答案声明。
6. 没有可继续工作的内容时进入 idle，收到图更新后再次观察。

项目不使用全局轮次屏障。`generation` 只表示图中的因果层次：初始节点为 G0，从 generation N 节点创建的 Intent 及其 Fact 输出为 G(N+1)。

## 图模型

| 元素 | 表示方式 | 创建者 | 说明 |
| --- | --- | --- | --- |
| `Goal` | 节点 | 人类 | 项目目标 |
| `Origin` | 节点 | 人类 | 初始条件和题面 |
| `Fact` | 节点 | 人类或 Agent | 可追溯到来源和 producing execution |
| `Hint` | 节点 | 人类 | 对 Agent 的正式图内提示 |
| `Intent` | 边 | 人类或 Agent | 从已知节点推进到目标节点的行动 |
| 普通关系 | 边 | 人类或 Agent | `supports`、`contradicts`、`derived_from` 等 |

Agent 只能在结果阶段创建 Fact 和普通关系，不能创建或修改 Goal、Origin、Hint，也不能绕过规划阶段创建另一个 Intent。

## 项目隔离

默认数据位于 `web-data/`：

```text
web-data/
  projects.db
  projects/
    <project-id>/
      blackboard.db
      layout.json
      executions/
        plan-<provider>-<id>/
          stdout.txt
          stderr.txt
        run-<provider>-<id>/
          stdout.txt
          stderr.txt
      audit-exports/
        audit-<timestamp>-<id>.zip
```

- 每个项目使用独立 SQLite 和目录。
- 每个项目使用独立 Supervisor、取消令牌和 WebSocket 频道。
- 项目 Blackboard 中的 `projects.status` 是运行状态的权威来源；全局
  `projects.db` 只保存列表索引缓存，并由 `ProjectService` 在生命周期变更和
  读取时同步。
- execution 查询同时校验 `project_id` 与 `execution_id`。
- 停止一个项目不会终止其他项目的 CLI 进程。
- `challenge_path` 是用户显式选择的外部只读上下文路径。

## 环境要求

开发和 CI 使用仓库根目录的固定运行时文件：

- Python `3.12.9`（`.python-version`）；
- Node.js `24.18.1`（`.node-version`）；
- npm `11.16.0`；
- Git；
- 至少一个已安装并完成认证的 CLI provider（Codex、Claude 或 Kimi）；
- 启用默认 qinyuan-agent provider 时需要 `uv`；它会从相邻的
  `Qinyuan_Agent` 项目及其 `uv.lock` 启动，不依赖系统安装的同名 CLI；
- Docker CLI 和可用 daemon 是可选依赖，在使用 `container` 后端或
  qinyuan-agent sandbox 时需要；
- Windows、macOS 或 Linux。

如果需要在受支持的 Windows 机器上安装环境，可运行仓库中的
`QinyuanEnvironmentChecker.exe`。它会把 Docker 标记为可选项，并提供
`--report <path>` 无界面 JSON 检查模式。

## 安装与启动

推荐使用固定 Python 版本创建虚拟环境，并用锁定文件安装依赖：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.lock
Set-Location frontend
npm ci
npm run build
Set-Location ..
.\.venv\Scripts\python web_main.py
```

`requirements.txt` 保留为宽范围的开发入口；可复现构建和 CI 必须使用
`requirements.lock`。前端必须使用 `npm ci`，`frontend/package-lock.json` 和
`frontend/package.json` 中的依赖版本均已固定。

运行后端测试时使用开发依赖锁定文件：

```powershell
.\.venv\Scripts\python -m pip install -r requirements-dev.lock
.\.venv\Scripts\python -m unittest discover -s tests -p "test*.py"
```

打开 [http://127.0.0.1:8000](http://127.0.0.1:8000)。服务只监听 localhost。

生产入口默认读取：

- 数据目录：`web-data/`
- provider 配置：`worker/providers.json`

可以使用环境变量覆盖：

```powershell
$env:BLACKBOARD_DATA_ROOT = 'E:\blackboard-data'
$env:BLACKBOARD_PROVIDER_CONFIG = 'E:\configs\providers.json'
python web_main.py
```
## 开发模式

先启动后端：

```powershell
python web_main.py
```

再启动 Vite：

```powershell
cd frontend
npm run dev
```

Vite 默认地址为 `http://127.0.0.1:5173`，并将 `/api` 和 `/ws` 代理到 `127.0.0.1:8000`。

## 项目模式

### 仅黑板

项目创建后保持 idle。用户可以先添加 Fact、Hint 和 Intent，调整布局并检查图，再点击“启动 Agent”。创建时选择的 provider 会保留，供以后启动使用。

### 立即启动 Agent

创建项目后立即启动所选 provider。每个 provider 异步规划和执行，页面实时显示项目状态、Intent execution 和日志。

### 删除项目

项目卡片的垃圾桶按钮会显示项目名称和状态。确认后，运行中的 Supervisor 会先停止，
SQLite 与文件句柄关闭，项目目录移动到 `web-data/.trash/<project-id>-<timestamp>/`，
随后才删除 `projects.db` 注册记录。移动或注册表操作失败会恢复原目录和可读句柄；
服务返回 409 时，卡片和全部数据保持不变。

## Provider 配置

`stream_format` 控制原始 provider 输出适配器；`streaming_supported: false` 会在界面明确
显示“完成后输出”，不会被误认为 WebSocket 断线。Claude 使用 `claude-stream-json`，
Codex 使用纯文本流式适配器，Kimi 默认仅在任务完成后输出。

默认配置文件是 `worker/providers.json`：

```json
{
  "coordinator": {
    "max_global_running": 16,
    "max_project_running": 4,
    "max_global_planning": 4,
    "max_project_planning": 2,
    "aging_seconds": 30
  },
  "audit": {
    "max_log_bytes": 8388608,
    "max_retained_log_sets": 500,
    "max_retained_exports": 5,
    "default_log_page_bytes": 262144,
    "max_log_page_bytes": 1048576
  },
  "fact_compaction": {
    "enabled_env": "BLACKBOARD_FACT_COMPACTION_ENABLED",
    "min_facts": 3,
    "max_facts": 16,
    "poll_interval_seconds": 1,
    "max_summary_chars": 4000,
    "llm": {
      "base_url_env": "BLACKBOARD_SUMMARY_BASE_URL",
      "api_key_env": "BLACKBOARD_SUMMARY_API_KEY",
      "model_env": "BLACKBOARD_SUMMARY_MODEL",
      "timeout_seconds": 120
    }
  },
  "providers": [
    {
      "name": "codex",
      "enabled": true,
      "priority": 100,
      "max_running": 4,
      "command": [
        "codex",
        "exec",
        "--dangerously-bypass-approvals-and-sandbox",
        "-C",
        "{agent_work_dir}",
        "-"
      ],
      "health_check": {
        "command": ["codex", "--version"],
        "timeout_seconds": 10,
        "cooldown_seconds": 15,
        "max_cooldown_seconds": 300,
        "failure_threshold": 3
      },
      "planning_timeout_seconds": 120,
      "timeout_seconds": 900
    }
  ]
}
```

### 异步 Fact 合并

`fact_compaction` 可配置一个独立直连 LLM，在后台合并相似的 agent Fact。该
LLM 不进入 solver 列表，不生成 Intent，也不参与答案 quorum。默认关闭；启用时填写
`base_url`、`api_key` 和 `model` 全部放在项目根目录 `.env`，配置文件只保存对应的
环境变量名。进程环境变量会覆盖 `.env`。请求使用兼容 OpenAI
`/chat/completions` 的接口：

```json
{
  "fact_compaction": {
    "enabled_env": "BLACKBOARD_FACT_COMPACTION_ENABLED",
    "min_facts": 3,
    "max_facts": 16,
    "poll_interval_seconds": 1,
    "max_summary_chars": 4000,
    "llm": {
      "base_url_env": "BLACKBOARD_SUMMARY_BASE_URL",
      "api_key_env": "BLACKBOARD_SUMMARY_API_KEY",
      "model_env": "BLACKBOARD_SUMMARY_MODEL",
      "timeout_seconds": 120
    }
  }
}
```

项目根目录 `.env`：

```dotenv
BLACKBOARD_SUMMARY_BASE_URL=https://api.example.com/v1
BLACKBOARD_SUMMARY_API_KEY=your-api-key
BLACKBOARD_SUMMARY_MODEL=your-model-name
BLACKBOARD_FACT_COMPACTION_ENABLED=false
```

候选集由现有 `similar_to` 图和保守的文本相似度预筛选。LLM 只能返回 `merge` 或
`skip`。`merge` 使用 `groups` 列出多个原子重点，每组明确列出实际覆盖的
`source_ids` 和单一 `summary`；同一个来源 Fact 可以出现在多个组中，因为一条 Fact
可能同时包含多个重点。合并成功会为每组创建（或幂等复用）来源为
`fact-summarizer` 的摘要 Fact，以 `derived_from` 指向该组原 Fact，将原有
`addresses`、`supports`、`contradicts` 关系投影到对应摘要节点，再在同一事务中归档
所有被至少一个组覆盖的 agent Fact。人工 Fact 和当前 goal completion claim 使用的
Fact 不会被自动归档。压缩调用共享全局执行配额，失败只记录事件，不阻塞 solver。
黑板工作区顶部的 `Fact 摘要` 状态会显示该独立 LLM 是否已启用、provider/model、当前
是等待候选还是正在摘要、待处理候选组数量及最近错误。

### Persistent provider lifecycle

A provider that owns resources across short-lived executions can declare an
`agent_lifecycle` block. Its normal command must use both
`{agent_owner_id}` and `{agent_work_dir}`. qinyuan-bat derives the owner from
`project_id + provider`, serializes calls for that owner, and keeps individual
Execution logs in their original directories while the provider uses one
stable workspace. `agent_lifecycle.release_command` must contain
`{agent_owner_id}` and is executed only after the solver project reaches its
terminal cleanup path. Release stdout/stderr and status are recorded under the
project's `agent-lifecycle/` directory and in an
`agent.lifecycle_release_finished` event.

对于不使用 qinyuan-agent sandbox 的 provider（当前为 Codex、Claude、Kimi），将
`project_workspace: true` 写入 provider 配置即可启用项目级附件目录。系统会为每个
`project_id + provider` 创建稳定的 `agent-workspaces/<provider>/` 目录，并在每次短期
execution 中把该目录作为 provider 的工作目录；provider 下载的题目文件和生成的证据、
中间结果都必须放在该目录。单次 execution 的 stdout/stderr 仍保存在自己的日志目录，
不会覆盖附件。纠正 flag 时同一目录会继续复用，只有项目进入终态并执行
`ProjectService.stop_project()` 才清理整个 `agent-workspaces/`，项目被删除时也随项目
目录一起移入回收区。

字段说明：

| 字段 | 说明 |
| --- | --- |
| `name` | provider 名称和执行目录标识 |
| `enabled` | 是否可被项目选择 |
| `priority` | 同一项目许可队列中的基础优先级；不参与事实可信度判断 |
| `max_running` | 该 provider 跨项目同时占用的最大许可数；默认 4 |
| `command` | 参数数组；不经过 shell 解释 |
| `health_check.command` | 可选启动前探测命令；不接收 prompt 或路径占位符 |
| `health_check.timeout_seconds` | 单次健康探测超时 |
| `health_check.cooldown_seconds` | 临时故障的首次冷却时间 |
| `health_check.max_cooldown_seconds` | 指数退避的冷却上限 |
| `health_check.failure_threshold` | 连续失败达到该值后将项目标记为 `degraded` |
| `planning_timeout_seconds` | 规划调用超时；省略时取 `timeout_seconds` 与 120 秒中的较小值 |
| `timeout_seconds` | Intent 执行调用超时 |

`{config_dir}` 会在加载 provider 配置时解析为该 JSON 文件所在目录，可用于声明不受
进程工作目录影响的仓库相对命令路径。`{work_dir}` 表示单次 execution 日志目录，
`{agent_work_dir}` 表示项目级稳定附件目录；`{challenge_dir}`、`{origin_host}` 和
`{agent_owner_id}` 保留到具体调用时再解析。
prompt 通过 stdin 发送，CLI 认证由各 provider 自己管理，平台不读取或保存 API Key。

### 执行配额与公平调度

Web 服务中的项目共享一个 `ExecutionCoordinator`。它只决定外部 CLI 调用何时
获得运行许可，不读取图内容、不生成 Intent，也不改变各 provider 的独立规划：

| 字段 | 说明 |
| --- | --- |
| `coordinator.max_global_running` | 全部项目合计的规划和执行调用上限；默认 16 |
| `coordinator.max_project_running` | 单项目规划和执行调用上限；默认 4 |
| `coordinator.max_global_planning` | 全部项目合计的规划调用上限；默认 4 |
| `coordinator.max_project_planning` | 单项目规划调用上限；默认 2 |
| `coordinator.aging_seconds` | 排队每经过该秒数增加一个有效优先级；默认 30 |

- 规划和 Intent execution 分别计数；规划上限为 execution 保留可用容量。
- 项目之间按轮转顺序获得许可，同一项目达到上限后会让出全局容量。
- 同一项目内先比较 provider `priority`，再叠加等待 aging；低优先级请求不会
  因持续到达的高优先级请求而永久饥饿。
- `missing`、`auth_failed`、`unhealthy` 或仍在 cooldown 的 provider 不进入
  许可队列。项目停止或任务取消时，排队请求会移除，已获得的许可会在退出路径释放。
- 省略 `coordinator` 或 `max_running` 时使用上述兼容默认值；所有配额必须为
  正整数，项目配额不能高于全局配额。

### Provider 健康状态

项目启动前会执行已配置的健康探测，并将结果保存到项目 Blackboard 的
`provider_health` 表。状态包括 `healthy`、`unhealthy`、`auth_failed`、
`missing`、`cooldown` 和 `unknown`：

- `missing`、`auth_failed` 和 `unhealthy` 会停止当前 provider 循环，但不阻塞
  其他 provider。
- 超时、网络错误、5xx 和进程异常进入有上限的指数冷却；冷却到期后先执行
  半开健康探测，再允许一次实际调用。图更新不能绕过 `cooldown_until`。
- 401、403 或明确未登录会进入 `auth_failed`，只有用户再次启动项目才会重新
  探测，不会自动消耗调用额度。
- 协议错误仍只执行一次同 Intent 纠错，不计入 provider 连续健康失败。
- 健康错误写入数据库和事件前会截断并脱敏；确认认证失败后，该次 stdout 和
  stderr 文件会替换为固定摘要，不保留 Token 或完整认证响应。

`GET /api/projects/{project_id}/providers` 返回持久化健康状态；项目详情和图快照
也包含 `provider_health`。状态变化以 `provider.health_changed` 事件写入项目
事件流，可通过 WebSocket 的 `after` 参数回放。未配置 `health_check` 的自定义
provider 保持兼容，启动时显示 `unknown`，首次完整调用成功后变为 `healthy`。

### 审计与日志

每次规划都会创建独立的 Planning Attempt，保存 provider、快照 sequence、排队与
执行时间、stdout/stderr 路径、工作目录、最终判定原因和产生的 Intent。判定状态
包括 `valid`、`idle`、`stale`、`protocol_error`、取消和 provider 进程状态。
Execution 另行保存重试编号、取消原因、调度等待原因、模型、CLI 版本与 token
用量；无法从 CLI 获得的字段明确保存为 `unknown`。

| 字段 | 说明 |
| --- | --- |
| `audit.max_log_bytes` | 每次调用的单个 stdout 或 stderr 文件上限；默认 8 MiB |
| `audit.max_retained_log_sets` | 每个项目保留的已结束规划/执行日志组总数；默认 500 |
| `audit.max_retained_exports` | 每个项目保留的审计 ZIP 数量；默认 5 |
| `audit.default_log_page_bytes` | 日志接口默认单流页大小；默认 256 KiB |
| `audit.max_log_page_bytes` | 日志接口允许的单流页大小上限；默认 1 MiB |

日志在写盘和 WebSocket 推送前脱敏；读取接口保留原有 `stdout`、`stderr` 字段，
同时返回字节偏移、总大小和是否还有下一页。超过保留数量的旧日志文件会删除，
数据库记录保留并标记 `logs_retained=false`。命令参数和环境变量不写入审计表或
导出文件。

审计接口：

- `GET /api/projects/{project_id}/planning-attempts`
- `GET /api/projects/{project_id}/planning-attempts/{attempt_id}`
- `GET /api/projects/{project_id}/planning-attempts/{attempt_id}/logs`
- `GET /api/projects/{project_id}/executions`
- `GET /api/projects/{project_id}/executions/{execution_id}/logs`
- `GET /api/projects/{project_id}/audit/export`

日志接口接受 `stdout_offset`、`stderr_offset` 和 `limit` 查询参数。审计 ZIP 包含图
快照、Planning Attempt、Execution、provider 健康、事件流和仍被保留的日志；
结构化数据与日志在写入 ZIP 前会再次脱敏。

### Web 操作与诊断

项目总览通过 `GET /api/runtime` 展示全局、单项目与规划配额，以及当前活动任务、
等待队列和各项目 provider 健康状态。页面每 3 秒刷新一次；它只读取运行状态，
不会自动启动项目、重试调用或恢复外部 CLI。

工作区“诊断”侧栏提供 Execution 与 Planning Attempt 两个视图，可按 provider、
状态和时间过滤。选择记录后复用底部日志面板读取持久日志；Planning Attempt 可
定位到其产生的 Intent。`interrupted`、`auth_failed`、`cooldown`、协议错误和
超时均使用独立状态文案。服务重启恢复过项目时，工作区会显示被中断的规划与
执行数量。

人工操作：

- `POST /api/projects/{project_id}/executions/{execution_id}/retry` 只由用户点击
  触发。它先创建新的 Execution，使用 `retry_of_execution_id` 关联来源记录，
  不覆盖原状态、日志或产出；活动 Execution 和已完成但尚未重新打开的项目不能重试。
- `POST /api/projects/{project_id}/reopen` 仅接受 `achieved` 项目。它递增
  `goal_version`、清除当前展示的最终答案并恢复为 `idle`；旧答案和完整审计历史
  仍保留，但不会参与新版本共识。重新打开不会自动启动 Agent。
- 审计下载按钮显式调用现有 ZIP 导出接口。停止、重试、重新打开和启动均需要
  用户操作，不会由状态轮询自动触发。

## 执行后端与隔离边界

默认配置使用本地后端：

```json
{
  "execution": {
    "backend": "local"
  }
}
```

### Local 后端

Local 后端保留 Windows 进程树取消、超时和流式日志能力，但审计明确记录
`execution_backend=local`、`isolation=unsafe_local` 和
`challenge_enforcement=prompt_only`。`challenge_dir` 只是提示词和工作流约定，
不是操作系统强制的只读沙箱；不要在含有宿主机凭据、生产目录或未隔离网络的账户下运行默认
provider。

### Container 后端（可选）

在 Docker-compatible runtime 可用时，可以在配置中显式启用：

```json
{
  "execution": {
    "backend": "container",
    "container": {
      "runtime_command": ["docker"],
      "default_image": "python:3.12-alpine",
      "network": "none",
      "capabilities": [],
      "environment": {
        "EXPLICIT_VALUE": "configured"
      },
      "environment_allowlist": ["SAFE_INPUT"],
      "cleanup_policy": "remove",
      "challenge_mount_path": "/challenge",
      "work_mount_path": "/work",
      "read_only_root": true,
      "memory_limit": "256m",
      "cpu_limit": 1.0,
      "pids_limit": 64
    }
  }
}
```

容器后端将 challenge 目录只读挂载、将 execution 工作目录单独读写挂载，默认不继承
宿主机完整环境变量，并把网络、额外 capability、镜像、资源限制和清理策略作为显式配置。
provider 可以用 `container_image` 和 `container_command` 覆盖默认镜像或命令模板。
`cleanup_policy` 支持 `keep`、`stop`、`remove`，实际结果会写入 Planning Attempt、
Execution 和审计事件。

当前 Windows Server 2019 工作区没有可用 Docker daemon，因此本地 fake Docker 测试和
Linux CI 的真实 Docker boundary 测试必须分开标记；不能把 fake 测试结果描述成当前 Windows
主机已完成容器验收。Windows 容器若使用不同挂载目标或参数语义，应在目标 runtime 上单独验证。

## 数据库迁移与备份

Blackboard SQLite 和项目注册表都维护显式 schema version 与 migration history。打开旧版本
数据库时会按版本顺序迁移，并在迁移前使用 SQLite backup API 生成
`*.pre-v<old>-to-v<new>-<timestamp>.bak` 备份；数据库版本高于当前程序时会拒绝打开。
迁移不会把用户数据库、日志或 challenge 数据写入源码发布包。
## Agent 协议

规划阶段最后输出：

```text
BLACKBOARD_INTENT: {"action":"plan","source_node_id":"fact-...","target_node_id":"goal-...","content":"Verify the key"}
```

没有合理工作时输出：

```text
BLACKBOARD_INTENT: {"action":"idle","reason":"No unresolved work"}
```

执行阶段必须输出单行严格 JSON。`facts` 和 `edges` 的成员都是对象，不是字符串或二维数组：

```text
BLACKBOARD_RESULT: {"intent_id":"intent-...","facts":[{"ref":"flag-fact","content":"flag = flag{example}"}],"edges":[{"type":"supports","from":"flag-fact","to":"goal-..."}],"goal_achieved":true,"final_answer":"flag{example}"}
```

`facts` 项只能包含 `ref`、`content`；`edges` 项只能包含 `type`、`from`、`to`。结构化结果整份校验并在单个事务中合并。协议错误不会留下部分 Fact 或关系，原始 stdout/stderr 仍会保留。

执行进程成功但协议解析失败时，Supervisor 会保留失败 execution，并针对同一 Intent 创建一次带解析错误反馈的纠错 execution。第二次仍失败才把本轮计为失败。

## 实验与效果评估

`experiments/` 提供与生产 `BlackboardStore`、`ProjectSupervisor`、Intent-Execution
协议和共享调度器相连的可重复实验 Runner，不使用另一套纯模拟状态机。默认配置只
运行 deterministic fake providers：

```powershell
python -m experiments `
  --config experiments/configs/deterministic_smoke.json `
  --output experiments/results/deterministic-smoke-v1-20260730
```

默认套件对每个条件使用相同的四项任务、Prompt、24 次模型调用上限、5 秒超时和
随机种子，并重复三次；最多三个项目共享同一个 `ExecutionCoordinator` 并发运行。
七个条件为：

| 条件 | 调度与策略 |
| --- | --- |
| `single_agent_serial` | 单 Agent 串行规划与执行 |
| `fixed_roles` | 固定 Planner 与 Executor，串行交接 |
| `round_robin` | 多 Agent 按固定轮询顺序串行运行 |
| `role_free_async` | 当前无角色异步协作方案 |
| `no_stale_check` | 关闭两次 stale-plan 检查 |
| `no_protocol_correction` | 协议错误后不创建纠错 Execution |
| `single_execution_per_intent` | 每个 Intent 最多保留一次 Execution |

每次运行保存 `blackboard.db`、完整事件、Planning/Execution 原始日志、图快照和
`result.json`。套件根目录保存规范化配置、环境与源码 SHA-256、随机种子、
`raw_results.jsonl`、`summary.json` 和当次分析脚本副本。可以从原始结果重新分析：

```powershell
python -m experiments.analyze experiments/results/deterministic-smoke-v1-20260730
```

采集指标包括任务成功率、规则判定的答案正确率、总耗时、首个 Fact/达到目标时间、
实际模型调用和统一预算、可获得的 token 与估算成本、重复 Intent、无效 Execution、
协议错误、stale plan、provider 恢复时间、故障后剩余任务完成率、吞吐量和并发项目
Jain 公平指数。答案一致性仅保留为提议数量、唯一答案数和最大一致组等信号，正确性
由任务的 `expected_answer` 规则独立判定。

真实 CLI 模式不会被默认命令触发。只有配置显式设置 `"mode": "real"`，同时提供
provider 配置并传入费用授权开关时才会运行：

```powershell
python -m experiments `
  --config E:\experiment\real-suite.json `
  --provider-config worker/providers.json `
  --allow-real-providers `
  --output E:\experiment\results\real-run-01
```

2026-07-30 的默认 fake 套件完成 84 个运行，`runner_errors=0`，并保留 84 份
SQLite。四个对照/当前条件与 stale 消融均为 12/12；关闭协议纠错为 9/12，单
Intent 一次 Execution 为 6/12，失败集中在专门要求纠错或失败后重试的夹具。
这只能证明实验框架可重复执行、消融约束生效且工程指标可采集，不能证明无角色
异步方案在真实模型上的质量或效率优势。

## CLI 入口

现有 CLI 入口继续使用同一套 Intent-edge 运行时：

```powershell
python main.py --goal "Recover the flag" --origin "Challenge statement" --challenge E:\ctf\challenge
```

## 测试

后端测试：

```powershell
python -m unittest discover -s tests -v
python -m compileall main.py web_main.py orchestrator worker web tests
```

前端单元测试和构建：

```powershell
cd frontend
npm test -- --run
npm run build
```

2026-07-31 当前工作区基线：pytest `162 passed, 3 skipped`，跳过项为未显式启用的容器集成用例；前端单元测试 `28/28`、Playwright E2E `14/14`，生产构建成功。fake Docker backend 测试与真实 Docker boundary 测试必须分别报告。

Playwright E2E 首次运行前安装浏览器：

```powershell
npx playwright install chromium
npm run test:e2e
```

E2E 使用本地 fake providers，不调用真实模型，不消耗模型额度。测试覆盖桌面
`1440x900`、窄屏 `390x844`、多项目隔离、人工建图后启动、运行总览、规划/执行
诊断、Intent 跳转、审计入口、真实日志文件、WebSocket 更新与断线重连。测试端口可通过
`BLACKBOARD_E2E_API_PORT` 与 `BLACKBOARD_E2E_WEB_PORT` 调整。

## 发布与版本

`VERSION` 是当前版本号（目前为 `0.3.0`），并且必须与 `pyproject.toml` 和
`frontend/package.json` 一致。发布前运行：

```powershell
.\.venv\Scripts\python tools\build_release.py --check-only
.\.venv\Scripts\python tools\build_release.py
```

发布 ZIP 使用固定的源文件清单和 `RELEASE-MANIFEST.json` SHA-256 清单。构建器会拒绝
SQLite/数据库、日志、凭据、challenge、`experiments/results`、`web-data`、
`worker/runs`、前端构建产物和用户数据；它也会扫描高置信度的 API key、云访问 key 和
私钥标记。发布前先阅读 `CHANGELOG.md`，正式开源许可证尚待仓库所有者选择，未加入许可证
文件前不要自行再分发。

GitHub Actions 位于 `.github/workflows/windows.yml` 和
`.github/workflows/linux.yml`：Windows 验证后端、前端、生产构建、fake E2E、环境检查器
和发布包；Linux 另外运行 fake-provider Docker boundary 集成测试。CI 不调用真实模型，
provider 凭据只应存在于用户自己的环境变量或专用配置文件中。
## 安全与授权

本项目可用于 CTF、安全评估、漏洞研究和渗透测试相关任务，但只能在明确获得授权的环境中使用。不要对未经所有者许可的系统、网络、应用或数据运行自动化安全测试。

工作目录隔离不等同于容器级安全隔离。默认 Codex 和 Claude 配置会关闭 CLI 自身的审批与沙箱，只适合在外部隔离的虚拟机或容器中运行。CLI 的实际文件、网络和进程权限取决于操作系统账户以及虚拟机或容器的边界；不要在含有宿主机共享目录、生产凭据或未隔离局域网访问的环境中使用默认配置。

## 当前限制

- 这是本机单进程调度平台，不是分布式任务系统。
- 第一版没有登录、组织、共享或多用户并发编辑。
- 服务重启后首次打开项目时，会把遗留的 `queued`/`running` Planning Attempt
  和 `planned`/`running` execution 幂等标记为 `interrupted`，记录
  `server restarted` 事件，并把遗留的
  `running` 项目恢复为 `idle`。恢复事件可通过 WebSocket `after` 参数重放；
  外部 CLI 不会自动重启，必须由用户再次启动项目。
- v2 Goal Result Fact 共识只认可同一 Fact 或显式 `equivalent_to` 连通分量；多个不等价结果进入 `degraded/result_conflict`。新自动模式可按平台契约提交，手动项目不会自动提交。
- 默认不自动合并相似 Fact；启用 `fact_compaction` 后，独立摘要 provider 会以多组
  原子重点的方式保守压缩，并保留来源和关系追溯。

## 来源与致谢

本项目的黑板协作思想学习自 [oritera/Cairn](https://github.com/oritera/Cairn)。经与 Cairn 作者沟通并获得允许后，本项目结合多 CLI Agent、SQLite、FastAPI 和本地 Web 图工作区进行了独立设计和实现。本项目不是 Cairn 的分支、兼容实现或官方项目，也不复制 Cairn 源码。
