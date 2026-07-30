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
