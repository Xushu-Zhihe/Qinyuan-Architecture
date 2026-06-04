from __future__ import annotations
from typing import Literal
from pydantic import BaseModel, Field


# 调度器内部支持的任务类型：
# - bootstrap：项目初始化/启动阶段任务
# - reason：项目级推理任务
# - explore：具体探索任务
TaskType=Literal["reason","explore","bootstrap"]

# 可选的 LLM 服务提供方。
ProviderType=Literal["deepseek","glm","kimi","PI","mock"]

# 任务完成后的处理方式：
# - remove：完成后从待处理队列移除
# - stop：完成后停止继续调度
CompletedAction=Literal["remove","stop"]

WORKER_ENV_KEYS:dict[ProviderType,tuple[str,...]]={
    "deepseek":(
        "DEEPSEEK_MODEL",
        "DEEPSEEK_BASE_URL",
        "DEEPSEEK_API_KEY",
    ),
    "glm":(
        "GLM_MODEL",
        "GLM_BASE_URL",
        "GLM_API_KEY",
    ),
    "kimi":(
        "KIMI_MODEL",
        "KIMI_BASE_URL",
        "KIMI_API_KEY",
    ),
    "PI":(
        "PI_MODEL",
        "PI_BASE_URL",
        "PI_API_KEY",
        "PI_PROVIDER_API",
    ),
    "mock":(),
}

class LLMconfig(BaseModel):
    """LLM 调用相关配置。"""

    # 使用哪个模型服务提供方。
    provider: ProviderType="mock"

    # 具体模型名称；mock 模式下保留默认值即可。
    model: str="mock"

    # 保存 API Key 的环境变量名，例如 DEEPSEEK_API_KEY。
    api_key_env:str|None=None

    # 自定义服务地址；使用默认官方地址时可以为空。
    base_url:str|None=None

    # LLM 请求超时时间，范围限制在 1-600 秒。
    timeout:int=Field(default=120,ge=1,le=600)

class TaskConfig(BaseModel):
    """单类任务的调度配置。"""

    # 当前配置对应哪一种任务。
    task_type: TaskType

    # 是否启用这类任务。
    enabled:bool=True

    # 单个任务执行超时时间，范围限制在 1-600 秒。
    timeout:int=Field(default=120,ge=1,le=600)

class DispatcherConfig(BaseModel):
    """dispatcher 的总配置。"""

    # worker 名称，用于认领任务、续约 heartbeat 和记录执行者。
    worker:str="dispatcher-worker"

    # 任务完成后的默认处理方式。
    completed_action: CompletedAction="remove"

    # 最大并发任务数，至少为 1。
    max_concurrent_tasks:int=Field(default=1,ge=1)

    # LLM 配置；默认使用 mock，方便本地无密钥启动。
    llm:LLMconfig=Field(default_factory=LLMconfig)

    # 默认启用三类任务配置。
    tasks:list[TaskConfig]=Field(
        default_factory=lambda:[
            TaskConfig(task_type="bootstrap"),
            TaskConfig(task_type="reason"),
            TaskConfig(task_type="explore"),
        ]
    )

