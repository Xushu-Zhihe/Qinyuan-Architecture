from __future__ import annotations

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
