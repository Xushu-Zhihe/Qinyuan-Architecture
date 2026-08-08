from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol

from sandbox.contracts import CommandRequest, CommandResult


@dataclass(frozen=True, slots=True)
class RuntimeContainer:
    container_id: str
    image: str
    image_id: str | None = None
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class SandboxRuntime(Protocol):
    async def create(self, agent_run_id: str) -> RuntimeContainer: ...
    async def execute(self, container: RuntimeContainer, request: CommandRequest, *, artifact_dir=None) -> CommandResult: ...
    async def cancel(self, container: RuntimeContainer, call_id: str) -> bool: ...
    async def destroy(self, container: RuntimeContainer) -> None: ...
    async def close(self) -> None: ...


@dataclass(slots=True)
class SandboxSession:
    agent_run_id: str
    container: RuntimeContainer | None = None
    state: str = "starting"
    create_task: asyncio.Task[RuntimeContainer] | None = None
    command_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    active_call_id: str | None = None
    destroy_error: str | None = None

    @property
    def sandbox_id(self) -> str | None:
        return self.container.container_id if self.container else None
