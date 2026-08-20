# Changelog

本文件记录用户可见的功能、兼容性和发布工程变更。项目采用
[Semantic Versioning 2.0.0](https://semver.org/)：

- `MAJOR`：不兼容的 API、配置、命令行或持久化格式变更；
- `MINOR`：向后兼容的新功能或可选能力；
- `PATCH`：向后兼容的修复、文档或发布流程改进；
- `0.x` 阶段仍处于早期开发；任何有意的不兼容变更都必须在本文件中显式说明。

`VERSION` 是仓库发布版本的权威来源。`pyproject.toml` 与
`frontend/package.json` 必须保持相同版本，发布包构建器会拒绝版本不一致。
正式许可证尚待仓库所有者选择；在许可证文件加入前，不应把源码或发布包视为已经获得
开源再分发授权。

## [Unreleased]

- 默认 qinyuan-agent 的执行、释放、能力探测和健康检查统一通过 provider 配置目录
  定位相邻的 `Qinyuan_Agent` uv 项目，避免系统 `PATH` 中多个同名 CLI 导致随机失效。
- qinyuan-agent provider 的 sandbox 生命周期改由 solver project 持有：同一
  `project_id + provider` 的短进程通过稳定 owner ID 和 workspace 接管同一容器，
  并在项目 achieved/stop/delete 收尾后执行可审计的显式释放。
- 新增 `BLACKBOARD_RESULT` v2、`goal_result_ref`、Goal Result Fact、completion claim、SQLite schema v5 和 `ProjectService.get_goal_result()`；多 provider 通过同一 Fact 或显式 `equivalent_to` 达成结果共识，不等价结果进入 `result_conflict`。
- 全自动模式新增独立 OpenAI-compatible Direct LLM Controller，并支持 Prompt/结构化 API 两种平台接入；新 UI 不再选择 provider 作为 controller，旧 provider-controller payload 仅作为兼容入口保留。
- 新增 text/JSON/form `SubmissionPlan`、冻结 plan fingerprint、rejected/replan 与传输重试分流、题目级状态/history、显式平台 health endpoint 测试和运行时 controller/platform secret 清理。
- provider 配置新增 result protocol capability、runtime probe、required Skill 和协议参数；qinyuan-agent/q-subagent 可被确定性识别为 v2 auto-ready。
- 首版明确不支持结果 artifact/文件上传、controller streaming、并行题目和自动 session/lease 恢复。
- 项目支持可恢复删除：运行项目会先停止并清理，项目目录移动到
  `web-data/.trash/`，注册表删除失败会自动回滚。
- 编辑模式支持 Fact/Hint 归档、恢复、无执行历史边删除；浏览模式锁定语义修改，
  活动图隐藏归档节点及悬空边。
- 统一 Planning/Execution 流式日志、WebSocket 重连补读、按到达顺序持久化的
  stdout/stderr 合并视图，并显示 provider 非实时输出状态、日志事件速率、丢弃和补读指标。
- Claude `stream-json` 同时保留可读输出与独立的脱敏原始 JSONL 审计文件。
- 图快照改用独立只读事务，避免运行轮询与 Supervisor 并发访问共享 SQLite 游标。
- 最终答案协议增加 `answer_key` 和 schema v4 迁移，按规范答案指纹达成跨 provider 共识。

### Upgrade notes

- Blackboard schema v4 会在首次打开旧数据库时自动迁移，并在同目录生成迁移前备份。
- Kimi 默认标记为仅任务完成后输出；Codex 与 Claude 使用流式适配器。

- 正式许可证待仓库所有者选择。
- Windows Server 上的真实容器运行仍需要可用的容器守护进程；Linux CI 负责执行
  fake-provider Docker 集成测试。

## [0.2.0] - 2026-07-30

### Added

- 可选的 `ExecutionBackend` 抽象、`LocalExecutionBackend` 与
  `ContainerExecutionBackend`。
- 容器模式的只读 challenge 挂载、独立读写工作目录、网络/能力/环境变量白名单、
  资源限制以及 keep/stop/remove 清理审计。
- Blackboard 和项目注册表的显式 SQLite schema 版本、逐版本迁移与迁移前备份。
- Windows/Linux GitHub Actions 工作流，以及 Linux fake-provider Docker 集成测试。
- 标准 Python 项目元数据、固定的 Python/Node/npm/前端依赖版本和可复现发布包构建器。
- 可从源码重建的 Windows 环境检查器；Docker 明确标记为可选依赖。

### Changed

- Local 后端审计明确标记为 `unsafe_local` 和 `prompt_only`，不再暗示本地工作目录是
  强制隔离边界。
- Planning Attempt 与 Intent Execution 记录执行后端、隔离级别、容器运行 ID、
  清理结果和脱敏后的后端元数据。

## [0.1.0] - 2026-07-30

### Added

- 无全局轮次屏障的多 provider 黑板协作、版本化规划、Intent-Execution 和结构化结果协议。
- Windows 进程树取消、健康管理、公平调度、运行配额、审计导出与 Web 诊断界面。
- 确定性 fake-provider 对照实验与前后端/E2E 测试基线。
