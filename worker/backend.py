from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from worker.models import LogChunk, ProviderConfig, WorkerResult


@dataclass(slots=True)
class BackendHandle:
    process: asyncio.subprocess.Process
    run_id: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class BackendOutcome:
    status: str
    exit_code: int | None
    diagnostic: str | None = None


@dataclass(frozen=True, slots=True)
class CleanupOutcome:
    policy: str | None
    status: str
    detail: str | None = None


class BackendStartError(OSError):
    pass


class ExecutionBackend(ABC):
    """Execution boundary used by the supervisor.

    Backends own process/container startup, streaming, cancellation, timeout
    handling and cleanup.  The supervisor only consumes ``WorkerResult`` and
    never branches on the concrete backend.
    """

    name: str
    isolation: str

    @abstractmethod
    async def start(
        self,
        provider: ProviderConfig,
        command: tuple[str, ...],
        request: str,
        challenge_dir: Path | None,
        work_dir: Path,
    ) -> BackendHandle:
        raise NotImplementedError

    @abstractmethod
    async def stream(
        self,
        handle: BackendHandle,
        request: str,
        stdout_path: Path,
        stderr_path: Path,
        on_chunk: Any,
        max_log_bytes: int,
    ) -> tuple[asyncio.Task[None], asyncio.Task[str], asyncio.Task[str]]:
        raise NotImplementedError

    @abstractmethod
    async def wait(
        self,
        handle: BackendHandle,
        timeout_seconds: float,
        cancel_event: asyncio.Event | None,
    ) -> BackendOutcome:
        raise NotImplementedError

    @abstractmethod
    async def cancel(self, handle: BackendHandle) -> None:
        raise NotImplementedError

    @abstractmethod
    async def cleanup(
        self,
        handle: BackendHandle,
        outcome: BackendOutcome,
    ) -> CleanupOutcome:
        raise NotImplementedError

    @abstractmethod
    async def execute(
        self,
        provider: ProviderConfig,
        request: str,
        challenge_dir: Path | None,
        work_dir: Path,
        on_chunk: Any = None,
        cancel_event: asyncio.Event | None = None,
        max_log_bytes: int = 8 * 1024 * 1024,
        origin_host: str | None = None,
    ) -> WorkerResult:
        raise NotImplementedError
