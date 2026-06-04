from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, StringConstraints


# 统一的“非空字符串”类型：
# - strip_whitespace=True 会自动去掉首尾空白
# - min_length=1 保证去掉空白后不能为空串
NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


# 全局设置：约束 intent 和 reason 的超时时间。
class Settings(BaseModel):
    # ge=5 表示该值必须大于等于 5。
    intent_timeout: int = Field(ge=5)
    reason_timeout: int = Field(ge=5)


# ArtifactRef 表示 fact 关联的外部产物，比如扫描结果、截图、日志文件。
class ArtifactRef(BaseModel):
    path: NonEmptyStr
    description: NonEmptyStr | None = None


# WorkerClaim 表示某个 worker 曾经处理过 intent 的一段历史。
class WorkerClaim(BaseModel):
    worker: NonEmptyStr
    started_at: datetime
    ended_at: datetime | None = None
    end_reason: NonEmptyStr | None = None


# Fact 表示一条已经确认的客观事实。
class Fact(BaseModel):
    id: NonEmptyStr
    description: NonEmptyStr
    # kind 是可选的结构化类型，比如 shell / service_scan / credential。
    kind: NonEmptyStr | None = None
    # artifacts 用来挂外部证据，避免把大块原始数据塞进 description。
    artifacts: list[ArtifactRef] = Field(default_factory=list)


# Intent 表示一个待探索或已完成的探索方向。
class Intent(BaseModel):
    id: NonEmptyStr
    # Python 里 from 是关键字，所以代码里用 from_，对外 JSON 仍然叫 from。
    from_: list[NonEmptyStr] = Field(alias="from")
    # to 为 None 表示 intent 还没有结论；有值时表示它产出了哪个 fact。
    to: NonEmptyStr | None = None
    description: NonEmptyStr
    creator: NonEmptyStr
    # 当前执行这个 intent 的 worker；未认领时为空。
    worker: NonEmptyStr | None = None
    last_heartbeat_at: datetime | None = None
    created_at: datetime
    concluded_at: datetime | None = None
    # kind 是可选的结构化类型，比如 recon / exploit / verify。
    kind: NonEmptyStr | None = None
    # worker_history 保存谁在什么时候推进过这个 intent。
    worker_history: list[WorkerClaim] = Field(default_factory=list)
    # params 用来承载额外结构化参数，便于以后做更强的自动调度。
    params: dict[str, Any] | None = None

    # 允许既用字段名 from_ 初始化，也允许读取/输出别名 from。
    model_config = {"populate_by_name": True}


# Hint 表示图外的人类提示或策略建议。
class Hint(BaseModel):
    id: NonEmptyStr
    content: NonEmptyStr
    creator: NonEmptyStr
    created_at: datetime


# ProjectReason 表示项目级 reason lease 的状态。
class ProjectReason(BaseModel):
    worker: NonEmptyStr
    trigger: NonEmptyStr
    started_at: datetime
    last_heartbeat_at: datetime


# ProjectMeta 表示项目的基础元信息。
class ProjectMeta(BaseModel):
    id: NonEmptyStr
    title: NonEmptyStr
    # 项目状态只能是这三个固定值之一。
    status: Literal["active", "stopped", "completed"]
    created_at: datetime
    # 当前如果有人占着 reason lease，就会带上这块信息。
    reason: ProjectReason | None = None


# ProjectSummary 在 ProjectMeta 基础上增加统计信息。
class ProjectSummary(ProjectMeta):
    fact_count: int
    intent_count: int
    working_intent_count: int
    unclaimed_intent_count: int
    hint_count: int


# ProjectDetail 表示完整项目详情：项目元信息 + 图数据。
class ProjectDetail(BaseModel):
    project: ProjectMeta
    facts: list[Fact]
    intents: list[Intent]
    hints: list[Hint]


# 创建项目时可以内联带入一些初始 hints。
class CreateHintInline(BaseModel):
    content: NonEmptyStr
    creator: NonEmptyStr


# 创建项目的请求体。
class CreateProjectRequest(BaseModel):
    title: NonEmptyStr
    origin: NonEmptyStr
    goal: NonEmptyStr
    hints: list[CreateHintInline] | None = None


# 单独创建 hint 的请求体。
class CreateHintRequest(BaseModel):
    content: NonEmptyStr
    creator: NonEmptyStr


# 创建 intent 的请求体。
class CreateIntentRequest(BaseModel):
    # min_length=1 表示 from 列表至少要有一个来源 fact。
    from_: list[NonEmptyStr] = Field(alias="from", min_length=1)
    description: NonEmptyStr
    creator: NonEmptyStr
    # worker 可选；如果带上它，表示创建时就顺便认领。
    worker: NonEmptyStr | None = None
    # 这三个字段都是新增的可选结构化字段。
    kind: NonEmptyStr | None = None
    params: dict[str, Any] | None = None
    worker_history: list[WorkerClaim] = Field(default_factory=list)

    model_config = {"populate_by_name": True}


# 普通 heartbeat 请求体，只需要说明是谁在续约。
class HeartbeatRequest(BaseModel):
    worker: NonEmptyStr


# 认领项目级 reason lease 的请求体。
class ReasonClaimRequest(BaseModel):
    worker: NonEmptyStr
    trigger: NonEmptyStr


# 结束一个 intent 并提交结论 fact 的请求体。
class ConcludeRequest(BaseModel):
    worker: NonEmptyStr
    description: NonEmptyStr


# 声明整个项目完成的请求体。
class CompleteRequest(BaseModel):
    from_: list[NonEmptyStr] = Field(alias="from", min_length=1)
    description: NonEmptyStr
    worker: NonEmptyStr

    model_config = {"populate_by_name": True}


# conclude 接口的响应：会同时返回新 fact 和更新后的 intent。
class ConcludeResponse(BaseModel):
    fact: Fact
    intent: Intent


# 更新项目状态时，只允许在 active 和 stopped 之间切换。
class UpdateProjectStatusRequest(BaseModel):
    status: Literal["active", "stopped"]


# 修改项目标题的请求体。
class UpdateProjectTitleRequest(BaseModel):
    title: NonEmptyStr


# 当项目已完成但后来发现判断有误时，用 reopen 恢复项目。
class ReopenRequest(BaseModel):
    description: NonEmptyStr
    creator: NonEmptyStr


# reopen 接口会返回恢复后的项目，以及新增的纠错 fact 和 intent。
class ReopenResponse(BaseModel):
    project: ProjectMeta
    fact: Fact
    intent: Intent
