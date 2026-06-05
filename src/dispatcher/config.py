from __future__ import annotations
from typing import Literal
from pydantic import BaseModel, Field, ConfigDict


# 调度器内部支持的任务类型：
# - bootstrap：项目初始化/启动阶段任务
# - reason：项目级推理任务
# - explore：具体探索任务
TaskType=Literal["reason","explore","bootstrap"]

# 可选的 LLM 服务提供方。
WorkerType=Literal["claudecode","codex","pi","mock"]

# 任务完成后的处理方式：
# - remove：完成后从待处理队列移除
# - stop：完成后停止继续调度
CompletedAction=Literal["remove","stop"]

WORKER_ENV_KEYS:dict[WorkerType,tuple[str,...]]={
    "claudecode":(
        "ANTHROPIC_MODEL",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_AUTH_TOKEN",
    ),
    "codex":(
        "CODEX_MODEL",
        "CODEX_BASE_URL",
        "CODEX_API_KEY",
    ),
    "pi":(
        "PI_MODEL",
        "PI_BASE_URL",
        "PI_API_KEY",
        "PI_PROVIDER_API",
    ),
    "mock":(),
}
class ReasonTaskConfig(BaseModel):
    # 每个推理任务的执行超时时间，单位秒，必须大于 0。
    timeout:int=Field(gt=0)
    max_intents:int=Field(gt=0,default=3)

class ExploreTaskConfig(BaseModel):
    # 每个探索任务的执行超时时间，单位秒，必须大于 0。
    timeout:int=Field(gt=0)
    conclude_timeout:int=Field(gt=0)

class BootstrapTaskConfig(BaseModel):
    # 每个引导任务的执行超时时间，单位秒，必须大于 0。
    timeout:int=Field(gt=0)
    conclude_timeout:int=Field(gt=0)

class TasksConfig(BaseModel):
    # 所有任务阶段的配置
    bootstrap:BootstrapTaskConfig
    reason:ReasonTaskConfig
    explore:ExploreTaskConfig

class ContainerConfig(BaseModel):
    # dispatcher 会为项目启动容器，并在容器内执行worker命令。
    image:str
    network_mode:str
    completed_action:CompletedAction
    cap_add:list[str]=Field(default_factory=list)

class RuntimeConfig(BaseModel):
    # 运行时配置，包含容器配置和任务阶段配置。
    max_workers:int=Field(gt=0)
    max_running_projects:int=Field(gt=0)
    max_project_workers:int=Field(gt=0)
    interval:int=Field(gt=0)
    healthcheck_timeout:int=Field(gt=0)
    prompt_group:str=Field(min_length=1)

class WorkerConfig(BaseModel):
    model_config=ConfigDict(extra="forbid")
    #单个worker的调度画像
    name:str
    type:WorkerType
    task_type:list[TaskType]
    max_running:int=Field(gt=0)
    priority:int=Field(ge=0)
    env:dict[str,str]=Field(default_factory=dict)

    @field_validator("task_types")
    @classmethod
    def validate_task_types(cls,value:list[TaskType])->list[TaskType]:
        #每个worker至少要支持一种任务类型
        if not value:
            raise ValueError("task_types must not be empty")
        if len(set(value))!=len(value):
            raise ValueError("task_types must be unique")
        return value
    
    @field_validator(mode="after")
    def validate_env(self)->"WorkerConfig":
        #


