import asyncio
import time
from collections import Counter, deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator, Callable

from worker.models import CoordinatorConfig


class PermitCancelled(Exception):
    pass


@dataclass(slots=True)
class _PermitRequest:
    project_id: str
    provider: str
    kind: str
    priority: int
    provider_limit: int
    queued_at: float
    sequence: int
    future: asyncio.Future[None]
    granted: bool = False
    released: bool = False


class ExecutionCoordinator:
    """Grant execution capacity without making planning decisions."""

    def __init__(
        self,
        config: CoordinatorConfig | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config or CoordinatorConfig()
        self._validate_config(self.config)
        self._clock = clock
        self._lock = asyncio.Lock()
        self._pending: list[_PermitRequest] = []
        self._project_order: deque[str] = deque()
        self._sequence = 0
        self._running_global = 0
        self._running_planning = 0
        self._running_by_project: Counter[str] = Counter()
        self._running_planning_by_project: Counter[str] = Counter()
        self._running_by_provider: Counter[str] = Counter()

    @staticmethod
    def _validate_config(config: CoordinatorConfig) -> None:
        integer_limits = (
            config.max_global_running,
            config.max_project_running,
            config.max_global_planning,
            config.max_project_planning,
        )
        if any(
            not isinstance(value, int)
            or isinstance(value, bool)
            or value <= 0
            for value in integer_limits
        ):
            raise ValueError("coordinator limits must be positive integers")
        if config.aging_seconds <= 0:
            raise ValueError("coordinator aging_seconds must be positive")
        if config.max_project_running > config.max_global_running:
            raise ValueError("project running limit cannot exceed global limit")
        if config.max_global_planning > config.max_global_running:
            raise ValueError("global planning limit cannot exceed global limit")
        if config.max_project_planning > config.max_project_running:
            raise ValueError("project planning limit cannot exceed project limit")

    @asynccontextmanager
    async def permit(
        self,
        *,
        project_id: str,
        provider: str,
        kind: str,
        priority: int,
        provider_limit: int,
        cancel_event: asyncio.Event | None = None,
    ) -> AsyncIterator[None]:
        request = await self._acquire(
            project_id=project_id,
            provider=provider,
            kind=kind,
            priority=priority,
            provider_limit=provider_limit,
            cancel_event=cancel_event,
        )
        try:
            yield
        finally:
            await self._release(request)

    async def _acquire(
        self,
        *,
        project_id: str,
        provider: str,
        kind: str,
        priority: int,
        provider_limit: int,
        cancel_event: asyncio.Event | None,
    ) -> _PermitRequest:
        if kind not in {"planning", "execution"}:
            raise ValueError(f"unsupported permit kind: {kind}")
        if provider_limit <= 0:
            raise ValueError("provider_limit must be positive")
        if cancel_event is not None and cancel_event.is_set():
            raise PermitCancelled

        loop = asyncio.get_running_loop()
        async with self._lock:
            self._sequence += 1
            request = _PermitRequest(
                project_id=project_id,
                provider=provider,
                kind=kind,
                priority=priority,
                provider_limit=provider_limit,
                queued_at=self._clock(),
                sequence=self._sequence,
                future=loop.create_future(),
            )
            self._pending.append(request)
            if project_id not in self._project_order:
                self._project_order.append(project_id)
            self._dispatch_locked()

        cancel_wait: asyncio.Task[bool] | None = None
        try:
            if cancel_event is None:
                await request.future
            else:
                cancel_wait = asyncio.create_task(cancel_event.wait())
                done, _ = await asyncio.wait(
                    {request.future, cancel_wait},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if request.future not in done:
                    await self._abandon(request)
                    raise PermitCancelled
            return request
        except asyncio.CancelledError:
            await self._abandon(request)
            raise
        finally:
            if cancel_wait is not None:
                cancel_wait.cancel()
                await asyncio.gather(cancel_wait, return_exceptions=True)

    async def _abandon(self, request: _PermitRequest) -> None:
        async with self._lock:
            if request.released:
                return
            if request.granted:
                self._release_locked(request)
            else:
                self._remove_pending_locked(request)
            request.released = True
            self._dispatch_locked()

    async def _release(self, request: _PermitRequest) -> None:
        async with self._lock:
            if request.released:
                return
            self._release_locked(request)
            request.released = True
            self._dispatch_locked()

    def _release_locked(self, request: _PermitRequest) -> None:
        if not request.granted:
            self._remove_pending_locked(request)
            return
        self._running_global -= 1
        self._decrement(self._running_by_project, request.project_id)
        self._decrement(self._running_by_provider, request.provider)
        if request.kind == "planning":
            self._running_planning -= 1
            self._decrement(
                self._running_planning_by_project,
                request.project_id,
            )

    @staticmethod
    def _decrement(counter: Counter[str], key: str) -> None:
        counter[key] -= 1
        if counter[key] <= 0:
            del counter[key]

    def _remove_pending_locked(self, request: _PermitRequest) -> None:
        try:
            self._pending.remove(request)
        except ValueError:
            pass
        self._discard_empty_projects_locked()

    def _eligible(self, request: _PermitRequest) -> bool:
        config = self.config
        if self._running_global >= config.max_global_running:
            return False
        if (
            self._running_by_project[request.project_id]
            >= config.max_project_running
        ):
            return False
        if self._running_by_provider[request.provider] >= request.provider_limit:
            return False
        if request.kind == "planning":
            if self._running_planning >= config.max_global_planning:
                return False
            if (
                self._running_planning_by_project[request.project_id]
                >= config.max_project_planning
            ):
                return False
        return True

    def _effective_priority(self, request: _PermitRequest) -> float:
        waited = max(0.0, self._clock() - request.queued_at)
        return request.priority + waited / self.config.aging_seconds

    def _next_request_locked(self) -> _PermitRequest | None:
        self._discard_empty_projects_locked()
        for _ in range(len(self._project_order)):
            project_id = self._project_order[0]
            self._project_order.rotate(-1)
            candidates = [
                request
                for request in self._pending
                if request.project_id == project_id and self._eligible(request)
            ]
            if candidates:
                return max(
                    candidates,
                    key=lambda request: (
                        self._effective_priority(request),
                        -request.sequence,
                    ),
                )
        return None

    def _dispatch_locked(self) -> None:
        while self._running_global < self.config.max_global_running:
            request = self._next_request_locked()
            if request is None:
                return
            self._pending.remove(request)
            request.granted = True
            self._running_global += 1
            self._running_by_project[request.project_id] += 1
            self._running_by_provider[request.provider] += 1
            if request.kind == "planning":
                self._running_planning += 1
                self._running_planning_by_project[request.project_id] += 1
            if not request.future.done():
                request.future.set_result(None)
            self._discard_empty_projects_locked()

    def _discard_empty_projects_locked(self) -> None:
        pending_projects = {request.project_id for request in self._pending}
        if not pending_projects:
            self._project_order.clear()
            return
        self._project_order = deque(
            project_id
            for project_id in self._project_order
            if project_id in pending_projects
        )
        for request in self._pending:
            if request.project_id not in self._project_order:
                self._project_order.append(request.project_id)

    async def snapshot(self) -> dict[str, object]:
        async with self._lock:
            return {
                "running": {
                    "global": self._running_global,
                    "planning": self._running_planning,
                    "execution": self._running_global - self._running_planning,
                    "by_project": dict(self._running_by_project),
                    "by_provider": dict(self._running_by_provider),
                },
                "pending": {
                    "total": len(self._pending),
                    "planning": sum(
                        request.kind == "planning" for request in self._pending
                    ),
                    "execution": sum(
                        request.kind == "execution" for request in self._pending
                    ),
                },
            }
