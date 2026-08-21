"""Structured dispatcher decisions passed to the Blackboard workgroup."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class QuestionPayload(BaseModel):
    """The platform-neutral question envelope produced by the dispatcher Skill."""

    model_config = ConfigDict(extra="allow")

    external_id: str = Field(min_length=1, max_length=500)
    name: str = Field(default="Question", max_length=500)
    goal: str = Field(min_length=1, max_length=1_000_000)
    origin: str = Field(default="competition platform", max_length=1_000_000)
    hints: tuple[str, ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)


class DispatcherDecision(BaseModel):
    """One final structured decision after zero or more operation tool calls."""

    model_config = ConfigDict(extra="forbid")

    status: Literal[
        "question",
        "wait",
        "finished",
        "submitted",
        "skipped",
        "abandoned",
        "stopped",
        "error",
    ]
    message: str = Field(default="", max_length=10_000)
    question: QuestionPayload | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

