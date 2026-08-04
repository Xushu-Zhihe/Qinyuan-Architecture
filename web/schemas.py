from typing import Literal

from pydantic import BaseModel, Field


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    goal: str = Field(min_length=1)
    origin: str = Field(min_length=1)
    hints: list[str] = Field(default_factory=list)
    mode: Literal["manual", "run"]
    providers: list[str] = Field(default_factory=list)
    agent_limit: int = Field(ge=1, le=16)
    challenge_path: str | None = None


class AutoStart(BaseModel):
    platform_prompt: str = Field(min_length=1)
    controller_provider: str = Field(min_length=1)
    solver_providers: list[str] = Field(min_length=1)
    agent_limit: int = Field(default=2, ge=1, le=16)
    max_tasks: int = Field(default=10, ge=1, le=1000)
    task_timeout_minutes: float = Field(default=60, gt=0, le=1440)
    controller_timeout_seconds: float = Field(default=300, gt=0, le=3600)


class NodeCreate(BaseModel):
    type: Literal["fact", "hint"]
    content: str = Field(min_length=1)


class EdgeCreate(BaseModel):
    type: str = Field(min_length=1)
    from_node_id: str = Field(min_length=1)
    to_node_id: str = Field(min_length=1)
    content: str | None = None


class EdgeUpdate(BaseModel):
    from_node_id: str = Field(min_length=1)
    to_node_id: str = Field(min_length=1)


class LayoutUpdate(BaseModel):
    revision: int = Field(ge=0)
    nodes: dict[str, object] = Field(default_factory=dict)
    edges: dict[str, object] = Field(default_factory=dict)
    viewport: dict[str, object] = Field(default_factory=dict)
