"""Explicit dispatcher command and event models."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from automation.manifest import Manifest


CommandType = Literal[
    "operation",
    "skip_current",
    "abandon_current",
    "stop_after_current",
    "stop_run",
    "retry_last",
    "refresh_state",
]


class DispatcherCommand(BaseModel):
    """A normalized action that can be shown and validated before execution."""

    model_config = ConfigDict(extra="forbid")

    command_id: str = Field(default_factory=lambda: uuid4().hex)
    source: Literal["human", "llm"]
    command_type: CommandType
    operation_id: str | None = None
    variables: dict[str, Any] = Field(default_factory=dict)
    original_instruction: str | None = Field(default=None, max_length=4000)
    reason: str = Field(default="", max_length=1000)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def validate_operation(self) -> "DispatcherCommand":
        if self.command_type == "operation" and not self.operation_id:
            raise ValueError("operation command requires operation_id")
        if self.command_type not in {
            "operation",
            "skip_current",
            "abandon_current",
        } and self.operation_id is not None:
            raise ValueError("control command cannot include operation_id")
        return self

    def validate_against(self, manifest: Manifest) -> None:
        if self.operation_id is None:
            return
        if not any(
            operation.id == self.operation_id
            for operation in manifest.operations
        ):
            raise ValueError(
                f"command operation is not declared in manifest: {self.operation_id}"
            )


class DispatcherEvent(BaseModel):
    """A redacted, explicit command or platform-operation event."""

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(default_factory=lambda: uuid4().hex)
    event_type: Literal["command", "operation", "message", "error"]
    command_id: str | None = None
    operation_id: str | None = None
    status: Literal["planned", "running", "succeeded", "failed", "unknown"]
    detail: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
