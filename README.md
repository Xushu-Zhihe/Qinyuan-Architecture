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
- 从 Fact 追溯 producing Intent、execution 和 provider。
- 同时运行多个相互隔离的项目。
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
        <execution-id>/
          stdout.txt
          stderr.txt
          work/
```

- 每个项目使用独立 SQLite 和目录。
- 每个项目使用独立 Supervisor、取消令牌和 WebSocket 频道。
- execution 查询同时校验 `project_id` 与 `execution_id`。
- 停止一个项目不会终止其他项目的 CLI 进程。
- `challenge_path` 是用户显式选择的外部只读上下文路径。

## 环境要求

- Python 3.10 或更高版本。
- Node.js 20.19+ 或 22.12+。
- 至少一个已安装并完成认证的 CLI provider。
- Windows、macOS 或 Linux。

## 安装与启动

```powershell
python -m pip install -r requirements.txt
cd frontend
npm install
npm run build
cd ..
python web_main.py
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

## Provider 配置

默认配置文件是 `worker/providers.json`：

```json
{
  "providers": [
    {
      "name": "codex",
      "enabled": true,
      "priority": 100,
      "command": ["codex", "exec", "-C", "{work_dir}", "-"],
      "timeout_seconds": 900
    }
  ]
}
```

字段说明：

| 字段 | 说明 |
| --- | --- |
| `name` | provider 名称和执行目录标识 |
| `enabled` | 是否可被项目选择 |
| `priority` | 保留的配置元数据，不参与事实可信度判断 |
| `command` | 参数数组；不经过 shell 解释 |
| `timeout_seconds` | 单次规划或执行调用超时 |

支持 `{work_dir}` 和 `{challenge_dir}` 占位符。prompt 通过 stdin 发送，CLI 认证由各 provider 自己管理，平台不读取或保存 API Key。

## Agent 协议

规划阶段最后输出：

```text
BLACKBOARD_INTENT: {"action":"plan","source_node_id":"fact-...","target_node_id":"goal-...","content":"Verify the key"}
```

没有合理工作时输出：

```text
BLACKBOARD_INTENT: {"action":"idle","reason":"No unresolved work"}
```

执行阶段最后输出：

```text
BLACKBOARD_RESULT: {"intent_id":"intent-...","facts":[],"edges":[],"goal_achieved":false,"final_answer":null}
```

结构化结果整份校验并在单个事务中合并。协议错误不会留下部分 Fact 或关系，原始 stdout/stderr 仍会保留。

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

Playwright E2E 首次运行前安装浏览器：

```powershell
npx playwright install chromium
npm run test:e2e
```

E2E 使用本地 fake providers，不调用真实模型，不消耗模型额度。测试覆盖桌面 `1440x900`、窄屏 `390x844`、多项目隔离、人工建图后启动、真实日志文件和 WebSocket 更新。

## 安全与授权

本项目可用于 CTF、安全评估、漏洞研究和渗透测试相关任务，但只能在明确获得授权的环境中使用。不要对未经所有者许可的系统、网络、应用或数据运行自动化安全测试。

工作目录隔离不等同于容器级安全隔离。CLI 的实际文件、网络和进程权限仍取决于操作系统、provider 参数及其自身沙箱配置。

## 当前限制

- 这是本机单进程调度平台，不是分布式任务系统。
- 第一版没有登录、组织、共享或多用户并发编辑。
- 服务重启会把未完成 execution 标记为 interrupted，不自动恢复外部 CLI。
- 最终答案共识使用规范化后的精确文本，不自动向 CTF 平台提交答案。
- 相似 Fact 不自动合并，只保守地保留独立命题和关系。

## 来源与致谢

本项目的黑板协作思想学习自 [oritera/Cairn](https://github.com/oritera/Cairn)。经与 Cairn 作者沟通并获得允许后，本项目结合多 CLI Agent、SQLite、FastAPI 和本地 Web 图工作区进行了独立设计和实现。本项目不是 Cairn 的分支、兼容实现或官方项目，也不复制 Cairn 源码。
