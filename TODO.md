# qinyuan-bat 工具 Sandbox 改造 TODO

## 0. 已选择的架构

本 TODO 采用“宿主机 Agent/MCP + bat 管理工具 Sandbox”的架构，替代“把整个 Qinyuan main provider 放入容器”的方案。

```text
宿主机 qinyuan-bat
├─ ProjectSupervisor / Orchestrator
├─ Blackboard
├─ provider Executor
│    └─ 启动 Qinyuan_Agent-main（普通宿主机 provider 进程）
├─ SandboxBridgeServer（仅 loopback）
└─ SandboxService
     ├─ Agent run A -> Tool Container-A
     ├─ Agent run B -> Tool Container-B
     └─ Agent run C -> Tool Container-C

Qinyuan_Agent-main
├─ Agent loop
├─ main 私有 MCPManager
└─ Nmap MCP：只返回 sandbox_command

工具镜像
└─ 首版只包含 Nmap 及必要运行组件
```

### 固定职责

- bat 继续拥有 Blackboard、Intent/Execution/Fact、provider 调度、日志、审计、取消和项目生命周期。
- bat 像调用 Codex/Claude/Kimi 一样，以外部进程调用 `Qinyuan_Agent-main`。
- main、MCPManager 和 Nmap MCP 在宿主机运行；工具命令在 bat 管理的独立容器执行。
- bat 不 import main 的 `AgentRunner`、`ThreadRepository`、MCP Client 或工具实现。
- bat 不实现、不启动、不共享 MCPManager/MCP Server；Nmap MCP 只归 main。
- bat 只接收版本化 `sandbox_command`，负责通用命令校验、容器执行、输出限制和生命周期。
- 首版只支持 Nmap。Nuclei、FFUF、HTTPX、Subfinder 及其他 MCP 不在本 TODO 范围内。

### 生命周期决定

- provider 启动时：bat 创建 `agent_run_id`、run context、bridge lease 和短期 token，但不创建 Docker 容器。
- 第一次合法 Sandbox Bridge command：`SandboxService.start_for_agent(agent_run_id)` 按需创建工具容器。
- 同一个 provider run：所有 Nmap 调用串行执行并复用同一个容器。
- provider 正常结束、失败、超时、取消或崩溃：bat 取消活动命令、销毁已经创建的容器、撤销 token。
- 从未调用 Nmap 的 run：不得创建工具容器。

### 当前基线

- 仓库：`qinyuan-bat`
- 分支/提交：`main@350b031`
- `ProjectSupervisor` 已通过可注入 `Executor` 获取 `WorkerResult`。
- `worker/providers.json` 已将 Codex、Claude、Kimi 定义为外部 provider command。
- `LocalExecutionBackend` 已支持 stdin/stdout/stderr、超时和 Windows 进程树取消。
- `ContainerExecutionBackend` 当前用于“把整个 provider 放进一次性容器”；它不是本方案的工具 SandboxService，必须继续保持独立。
- legacy 迁移来源：`qinyuan-legacy@cb55cab` 的 `Sandbox/` 和 `MCPs/Nmap_Mcp/server.py`；bat 只迁移 `Sandbox/` 行为，Nmap MCP 迁移到 main。

## P0：冻结 Bat/Main 跨进程契约

必须先完成协议文档和 fake contract test，再接 Docker。

### P0.1 Provider 契约

- [ ] 将 `qinyuan-main` 注册为普通宿主机 provider command，不走现有整 provider `ContainerExecutionBackend`。
- [ ] bat 通过 stdin 发送一次 UTF-8 任务文本；main 通过 stdout/stderr、退出码和唯一 `BLACKBOARD_RESULT:` 返回。
- [ ] bat 不为 main 增加 Python service API，也不在 Blackboard 层增加 main 专用 import。
- [ ] planning invocation 使用 `no_tools`，允许执行的 invocation 使用 `nmap_optional`；profile 由 bat 决定。

### P0.2 Run context 契约

每个 provider invocation 由 bat 写入受控的 `run_context.json`：

- [ ] `protocol_version`
- [ ] `agent_run_id`：opaque UUID，不使用模型生成内容。
- [ ] `thread_id`：默认与 Execution attempt 一一对应。
- [ ] `tool_profile`：仅 `no_tools | nmap_optional`。
- [ ] `authorized_targets`
- [ ] `deadline`
- [ ] `work_dir` / artifact scope
- [ ] `result_contract`
- [ ] `sandbox_endpoint`

- [ ] 通过 `QINYUAN_RUN_CONTEXT_FILE` 将路径传给 main。
- [ ] run context 不包含模型 API key、Docker 凭据或 bridge bearer token。
- [ ] 文件位于本 run work dir，使用最小文件权限，并在 provider 结束后按审计策略保留脱敏副本或清理。

### P0.3 Sandbox Bridge 契约

bridge command 固定为：

```json
{
  "protocol_version": 1,
  "call_id": "tool-call-id",
  "tool": "nmap",
  "arguments": ["-sT", "-Pn", "-n", "-p", "80,443", "127.0.0.1"],
  "timeout_seconds": 60
}
```

bridge result 固定为：

```json
{
  "protocol_version": 1,
  "call_id": "tool-call-id",
  "status": "completed",
  "exit_code": 0,
  "stdout": "...",
  "stderr": "",
  "timed_out": false,
  "stdout_truncated": false,
  "stderr_truncated": false,
  "artifact_refs": []
}
```

- [ ] 请求体不接受 `agent_run_id`、`sandbox_id`、container name、image、network 或 executable path。
- [ ] server 只根据 bearer token 查找 run 和 Sandbox；main/LLM 不能在请求中切换 run。
- [ ] 支持按 `call_id` 取消活动命令；同 token 下重复 `call_id` 必须幂等或明确拒绝。
- [ ] main 和 bat 各自保存同版本合法/非法 fixtures，不能通过跨仓库 import 共享实现。

### P0 验收

- [ ] real `ProjectSupervisor` -> fake qinyuan-main provider 能完成一次 Execution/Fact 写入。
- [ ] real provider wrapper -> fake SandboxService 能覆盖成功、失败、超时、取消、截断和 `outcome_unknown`。
- [ ] 错误 protocol version、未知字段、非法类型、空 `call_id` 和过期 run 在 Docker 调用前失败。
- [ ] qinyuan-main 不存在或 bridge 未启动时，Codex/Claude/Kimi 的既有路径不受影响。

## P1：迁移 Legacy Sandbox 基础模块

迁移来源：

```text
qinyuan-legacy/Sandbox/contracts.py
qinyuan-legacy/Sandbox/models.py
qinyuan-legacy/Sandbox/settings.py
qinyuan-legacy/Sandbox/service.py
qinyuan-legacy/Sandbox/docker_runtime.py
qinyuan-legacy/Sandbox/dockerfile
```

建议目标：

```text
sandbox/__init__.py
sandbox/contracts.py
sandbox/models.py
sandbox/settings.py
sandbox/service.py
sandbox/docker_runtime.py
sandbox/artifacts.py
sandbox/dockerfile
tests/test_sandbox_service.py
tests/test_sandbox_runtime.py
```

- [ ] 迁移行为而不是机械复制；适配 bat 的 async 生命周期、日志、redaction、config 和测试风格。
- [ ] 保留 `SandboxRuntime.create/execute/destroy/close` 边界，使 service tests 使用 fake runtime。
- [ ] 将 `agent_id` 明确改为 `agent_run_id`，一个 provider invocation 对应一个工具 Sandbox。
- [ ] `CommandRequest` 保持 typed argv：`tool`、`arguments`、`timeout_seconds`、`call_id`；禁止 shell command string。
- [ ] `CommandResult` 保留 exit code、stdout/stderr、timeout、truncation，并增加稳定 status/artifact refs。
- [ ] 首版 `allowed_tools` 只有 `nmap`；不要因为镜像规划图提前开放其他工具。
- [ ] 保留 legacy 的通用校验：命令名不能是路径、工具 allowlist、timeout 范围、参数类型和 NUL 校验。
- [ ] bat 不复制 MCP 的 hostname/IP/CIDR、端口数量等 Nmap 业务规则；这些属于 main 的 Nmap MCP。
- [ ] Docker 阻塞调用不得占用 asyncio event loop；使用明确的线程边界或异步 subprocess/runtime adapter。
- [ ] Docker 初始化快速失败；API/read timeout 必须大于最大工具 timeout、kill grace 和响应余量。
- [ ] 使用流式 `exec_create/exec_start` 或等价机制读取输出，不先在内存中收集完整 stdout/stderr。

### P1 镜像

- [ ] 工具镜像首版只安装固定版本 Nmap 和命令 timeout 所需组件，不包含 main、MCP、模型 SDK 或 API key。
- [ ] 固定 image tag/digest；启动时记录实际 image ID。
- [ ] 容器使用非 root 用户、只读 rootfs、受管 tmpfs/workspace、`no-new-privileges`、cap drop、PIDs/CPU/memory 限制。
- [ ] Nmap 首版固定使用无需原始套接字的 `-sT`，不要默认授予 `NET_RAW` 或 privileged。
- [ ] 不挂载 Docker socket、宿主机用户目录、`.env`、SSH key、模型配置或 provider work dir。
- [ ] 网络只连接授权实验网络；禁止 `--network host` 和无约束互联网出口。

### P1 验收

- [ ] fake runtime 覆盖 create/execute/destroy/close 和全部错误分支。
- [ ] real Docker 测试必须显式 opt-in；没有 Docker 时单元测试仍全部通过。
- [ ] 容器内不存在模型 API key、bridge token、宿主机配置和 Docker socket。

## P2：实现一个 Run 一个 Sandbox 的生命周期

- [ ] `SandboxService.start_for_agent(agent_run_id)` 幂等；同 run 并发首次调用只能创建一个容器。
- [ ] 不在全局 registry lock 中执行慢速 Docker create；不同 run 可并行创建。
- [ ] 同 run 的命令首版串行执行，复用同一 container，并避免并发 Nmap 取消语义不清。
- [ ] `stop_for_agent()` 先销毁容器，销毁成功后再删除 session；失败时保留可重试状态。
- [ ] provider 尚未调用工具时，`stop_for_agent()` 是无副作用幂等操作。
- [ ] create 成功但注册失败、service 关闭竞争和 provider 启动失败时清理孤儿容器。
- [ ] bat 启动时扫描带固定 label 的遗留容器，只处理本实例/本项目命名空间，禁止宽泛删除。
- [ ] `close()` 停止新 run，等待或取消进行中的 create/execute，汇总清理错误而不丢失原始 provider 结果。

### P2 验收

- [ ] 同一 run 连续/并发 `start_for_agent()` 只有一个 runtime.create。
- [ ] 两个 run 创建和执行互不阻塞、不共享 session。
- [ ] provider 未调用 Nmap 时 runtime.create 次数为零。
- [ ] destroy 第一次失败后 session 仍可再次清理。

## P3：实现仅 Loopback 的 Sandbox Bridge

建议新增：

```text
sandbox/bridge.py
sandbox/auth.py
sandbox/protocol.py
sandbox/run_registry.py
tests/test_sandbox_bridge.py
```

- [ ] bridge 使用独立内部 listener，只绑定 `127.0.0.1` 的随机端口；不得复用对外监听的 Web/API route。
- [ ] bridge server 在 bat 启动时创建，在关闭时先撤销所有 token，再停止 SandboxService。
- [ ] 每个 provider run 生成高熵短期 bearer token，server 只保存安全摘要和 run 绑定。
- [ ] token 写入受保护临时文件，通过 `QINYUAN_SANDBOX_TOKEN_FILE` 把路径交给 main；不得放入命令行、prompt、stdout、审计 metadata 或 URL。
- [ ] token 只允许一个 `agent_run_id`、一个 tool profile、一个 deadline 和一个并发 command。
- [ ] 首次合法 execute 请求内部调用 `start_for_agent()`；不暴露容器 start/stop/image/network API 给 main。
- [ ] bridge 再次校验 protocol、tool allowlist、argv 类型/大小、NUL、timeout 和 request body 上限。
- [ ] bridge 不解析自然语言，不执行 shell，不接受任意 URL，不代理 LLM 请求。
- [ ] token 过期、provider 结束或项目停止后立即返回 401/410，且不能重新激活 Sandbox。
- [ ] 错误响应脱敏并稳定分类：auth、expired、protocol、policy、sandbox_unavailable、tool_timeout、cancelled、internal。

### P3 验收

- [ ] 非 loopback bind 配置启动即失败。
- [ ] 无 token、错误 token、过期 token、跨 run call、重放 call 和超大 body 均在 Docker 前拒绝。
- [ ] token 和测试 secret 不出现在 access log、traceback、Blackboard、artifact 和 `WorkerResult`。
- [ ] bridge listener 异常退出时活动 run fail closed，不回退宿主机执行。

## P4：把 Qinyuan Main 接入 Provider Executor

建议修改/新增：

```text
worker/providers.json
worker/models.py
worker/config.py
worker/backend.py
worker/executor.py
worker/qinyuan_provider.py
tests/test_qinyuan_provider.py
```

- [ ] 新增 `qinyuan-main` provider command 和 health check；行为仍是 stdin/stdout 外部 provider。
- [ ] 增加 provider 配置字段，例如 `tool_sandbox_profile: "nmap"`，不要复用 `container_image` 表示工具 Sandbox。
- [ ] 新增 `ProviderRunContext`/等价对象，携带 run ID、execution kind、tool profile、deadline、work dir 和最小 per-run environment。
- [ ] 扩展 LocalExecutionBackend 以安全传入每 run 环境，不能修改整个 bat 进程的全局环境变量。
- [ ] `worker/qinyuan_provider.py` 组合现有 LocalExecutionBackend、run registry、bridge lease 和 SandboxService；不要把逻辑塞进 `orchestrator/store.py`。
- [ ] planning 使用 `no_tools`；执行阶段只有明确允许工具时才使用 `nmap_optional`。
- [ ] 启动 main 前写入 run context/token 文件并注册 lease；main 启动失败时撤销 lease且不得创建容器。
- [ ] provider 完成后先保存 stdout/stderr/exit code/`BLACKBOARD_RESULT`，再在 finally 中取消命令、清理 Sandbox、撤销 token。
- [ ] 清理失败不能覆盖 provider 原始结果；同时记录 `cleanup_status=failed` 和脱敏 detail。
- [ ] 保持 Codex/Claude/Kimi 及现有整 provider ContainerExecutionBackend 行为不变。

### P4 结果模型

- [ ] 不把宿主机 main 标记为“provider 已容器隔离”；保留 `provider_isolation=host_process/unsafe_local` 的真实含义。
- [ ] 单独记录 `tool_isolation=container`、`agent_run_id`、`tool_sandbox_id`、image ID、tool cleanup status。
- [ ] 不复用 `backend_run_id` 同时表示 provider PID 和工具 container ID。
- [ ] 数据库/API 字段变更使用向前 schema migration，旧项目可正常打开。

### P4 验收

- [ ] fake main 无工具回合通过真实 ProjectSupervisor 完成，Docker create 次数为零。
- [ ] fake main 两次 bridge command 复用一个 Sandbox，并生成一个 Execution/Fact。
- [ ] main 不 import bat，bat 不 import main。
- [ ] main 缺失、模型鉴权失败、bridge 不可用和 Docker 不可用有不同 health/error 分类。

## P5：超时、取消和不确定结果

- [ ] 明确分层 timeout：provider run、MCP/bridge transport、单工具 command、Docker API、kill grace。
- [ ] bridge transport timeout 必须大于 command timeout；Docker API timeout 必须再包含流读取和 kill grace。
- [ ] 项目停止/Execution 取消时：撤销 token -> 取消活动 command -> 必要时 destroy container -> 终止 main 进程树。
- [ ] command 未开始为 `aborted`；确认非零退出为 `failed`；确认 timeout 为 `timeout`；已开始但状态不可确认为 `outcome_unknown`。
- [ ] 不自动重试非幂等或结果不确定的 Nmap command。
- [ ] provider 超时总结流程不得重开已撤销的 Sandbox lease。
- [ ] bat 关闭时等待有限 grace，之后强制销毁工具容器并保留清理审计。

### P5 验收

- [ ] provider 启动前、首次 create 中、command 执行中、结果返回后和 cleanup 中取消均有 deterministic 测试。
- [ ] main 被 taskkill/崩溃后 token 失效，容器最终被清理。
- [ ] Docker API 断连不会被错误记录为扫描成功。

## P6：输出、Artifact 与内存边界

- [ ] Docker runtime 流式读取 stdout/stderr，宿主机内存只保留有界 head/tail。
- [ ] 每个 stream 设置字节上限、截断标记和总 artifact 上限；限制按字节而不是字符。
- [ ] 大输出写入 bat 管理的 run artifact 目录，bridge 只返回 opaque artifact ref，不返回任意宿主机路径。
- [ ] artifact ref 在交给 main 和 Blackboard 前验证 run ownership。
- [ ] provider 日志、tool 日志和 artifact 分开保存，但通过 project/execution/agent_run_id/call_id 关联。
- [ ] redaction 在写文件、发布 WebSocket、写 Blackboard 和导出审计 ZIP 前统一执行。
- [ ] 记录容器 create latency、首次 command latency、复用 command latency、峰值 RSS/容器内存和清理时间。

### P6 验收

- [ ] 超大输出不会导致 bat/main 内存无界增长。
- [ ] 多字节 UTF-8 截断不会产生坏编码或错算上限。
- [ ] 一个 run 无法读取另一个 run 的 artifact。

## P7：API Key 与容器安全边界

- [ ] bat 不把自身环境、provider 环境或模型 API key复制到工具容器。
- [ ] 工具容器环境使用显式 allowlist，默认空；Nmap 参数只通过 typed argv 传入。
- [ ] Docker socket/API 凭据只由 bat 的 Sandbox runtime 持有，main/MCP/工具容器均不可见。
- [ ] 不给工具容器宿主机 PID/IPC namespace、privileged、host network 或宽泛 bind mount。
- [ ] bridge token 只能请求已配置的工具 profile，不能用于读取文件、环境、容器 inspect 或 Docker 通用 API。
- [ ] provider command、health check、run context、error detail 和 audit export 全部通过 secret redaction。
- [ ] 文档明确剩余风险：main 本身运行在宿主机；bat 必须配合 main 的 `nmap_optional/no_tools` profile 禁用宿主机 PowerShell。
- [ ] 可选的宿主机 LLM Gateway 属于后续强化，不阻塞首版 Nmap Sandbox 迁移。

### P7 验收

- [ ] 恶意 bridge 请求、恶意 Nmap 输出和异常堆栈中找不到测试 API key/token。
- [ ] 容器 inspect 证明没有 secret env、host mount、Docker socket 和额外 capabilities。
- [ ] qinyuan-main profile 配置错误并暴露宿主机 PowerShell 时，health/preflight 必须 fail closed 或明确阻止启用工具 Sandbox provider。

## P8：审计、Web/API、文档和测试

- [ ] Blackboard/Execution 审计展示 provider PID/run、tool Sandbox ID、image、command 状态、duration、exit code、truncation 和 cleanup status。
- [ ] Web/API 明确区分“provider 在宿主机”和“工具在容器”，禁止用一个 `isolation=container` 误导用户。
- [ ] UI/API 不展示 bearer token、完整敏感 argv、API key 或宿主机绝对 secret 路径。
- [ ] 更新 README：选定架构、main provider 配置、工具镜像构建、bridge 监听、安全边界、故障排查和清理命令。
- [ ] 更新 provider config 示例和环境检查器，检查 main executable、Docker、工具 image、loopback bind 和 schema version。
- [ ] 单元测试使用 fake runtime/fake provider/fake bridge，不依赖 Docker 或真实模型。
- [ ] 增加 opt-in real Docker smoke：创建工具容器、执行 `nmap --version`、执行授权目标扫描、清理容器。
- [ ] 增加跨仓库 acceptance：real bat -> real main CLI -> fake model -> real Nmap MCP -> fake/real Sandbox Bridge。
- [ ] 记录 fake-provider 证据与真实模型效果证据的边界，不用 fake 测试宣称 Agent 效果提升。

实施顺序：

1. [ ] P0：协议、fixtures 和 fake contract tests。
2. [ ] P1-P2：Sandbox runtime/service 与懒创建生命周期。
3. [ ] P3：loopback bridge、token 和 run registry。
4. [ ] P4：qinyuan-main provider wrapper 接入真实 ProjectSupervisor。
5. [ ] P5-P7：取消、输出、artifact、secret 和容器加固。
6. [ ] P8：跨仓库 acceptance、Web/API、README 和真实 Docker smoke。

## 最终验收

- [ ] main 像 Codex/Claude 一样作为宿主机 provider 被 bat 调用，不成为 bat 内部 Python service。
- [ ] MCP 只归 main；bat 不启动、不共享也不调用 Nmap MCP。
- [ ] main 不调用 Nmap 时不创建工具容器；同一 run 多次调用复用同一容器。
- [ ] 多个并行 Agent run 各自拥有独立 token、Sandbox、命令状态和 artifact scope。
- [ ] main/MCP 不接触 Docker socket，工具容器不接触模型 API key 或宿主机 secret。
- [ ] provider 失败、项目取消、bat 关闭和 Docker 异常后无孤儿容器或仍有效 token。
- [ ] Blackboard 保留真实 provider 结果和独立 Sandbox/cleanup 审计，不夸大隔离范围。
