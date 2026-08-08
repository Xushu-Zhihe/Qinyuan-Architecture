from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path

from sandbox.artifacts import ArtifactStore
from sandbox.contracts import BridgeProtocolError, CommandRequest, CommandResult
from sandbox.models import RuntimeContainer, SandboxRuntime, SandboxSession
from sandbox.settings import SandboxSettings


class SandboxUnavailable(RuntimeError):
    category = "sandbox_unavailable"


class SandboxClosed(RuntimeError):
    category = "cancelled"


class SandboxService:
    """Owns one lazy tool container per provider invocation."""

    def __init__(
        self,
        runtime: SandboxRuntime,
        settings: SandboxSettings | None = None,
        *,
        artifact_store: ArtifactStore | None = None,
    ) -> None:
        self.runtime = runtime
        self.settings = settings or SandboxSettings()
        self.artifact_store = artifact_store
        self._sessions: dict[str, SandboxSession] = {}
        self._registry_lock = asyncio.Lock()
        self._closing = False
        self._active: set[asyncio.Task[object]] = set()

    @property
    def sessions(self) -> dict[str, SandboxSession]:
        return dict(self._sessions)

    async def _create(self, session: SandboxSession) -> RuntimeContainer:
        if self._closing:
            raise SandboxClosed("sandbox service is closed")
        try:
            container = await self.runtime.create(session.agent_run_id)
        except Exception as exc:
            session.state = "failed"
            raise SandboxUnavailable("tool sandbox could not be created") from exc
        session.container = container
        session.state = "ready"
        return container

    async def start_for_agent(self, agent_run_id: str) -> SandboxSession:
        if not isinstance(agent_run_id, str) or not agent_run_id.strip():
            raise ValueError("agent_run_id must be a non-empty opaque value")
        async with self._registry_lock:
            if self._closing:
                raise SandboxClosed("sandbox service is closed")
            session = self._sessions.get(agent_run_id)
            if session is None:
                session = SandboxSession(agent_run_id=agent_run_id, state="starting")
                session.create_task = asyncio.create_task(self._create(session))
                self._sessions[agent_run_id] = session
            create_task = session.create_task
        if create_task is not None:
            try:
                await create_task
            except Exception:
                async with self._registry_lock:
                    if self._sessions.get(agent_run_id) is session:
                        self._sessions.pop(agent_run_id, None)
                raise
        if session.container is None or session.state != "ready":
            raise SandboxUnavailable("tool sandbox is not ready")
        return session

    async def execute(self, agent_run_id: str, request: CommandRequest) -> CommandResult:
        session = await self.start_for_agent(agent_run_id)
        if session.container is None:
            raise SandboxUnavailable("tool sandbox has no container")
        async with session.command_lock:
            if self._closing:
                raise SandboxClosed("sandbox service is closing")
            session.active_call_id = request.call_id
            try:
                kwargs: dict[str, object] = {}
                if self.artifact_store is not None:
                    kwargs["artifact_dir"] = self.artifact_store.root / agent_run_id / request.call_id
                execute = self.runtime.execute
                parameters = inspect.signature(execute).parameters
                if "artifact_dir" not in parameters and not any(
                    item.kind is inspect.Parameter.VAR_KEYWORD
                    for item in parameters.values()
                ):
                    kwargs.pop("artifact_dir", None)
                result = await execute(session.container, request, **kwargs)
                if not isinstance(result, CommandResult):
                    raise TypeError("runtime returned an invalid command result")
                if result.call_id != request.call_id:
                    raise TypeError("runtime result call_id does not match request")
                refs = result.artifact_refs
                if self.artifact_store is not None:
                    refs = tuple(
                        ref if ref.startswith(f"artifact:{agent_run_id}:")
                        else f"artifact:{agent_run_id}:{request.call_id}:{ref.rsplit(':', 1)[-1]}"
                        for ref in refs
                    )
                return replace(result, artifact_refs=refs)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise SandboxUnavailable("tool sandbox execution failed") from exc
            finally:
                session.active_call_id = None

    async def cancel(self, agent_run_id: str, call_id: str) -> bool:
        session = self._sessions.get(agent_run_id)
        if session is None or session.container is None:
            return False
        try:
            return bool(await self.runtime.cancel(session.container, call_id))
        except Exception:
            return False

    async def stop_for_agent(self, agent_run_id: str) -> bool:
        async with self._registry_lock:
            session = self._sessions.get(agent_run_id)
        if session is None:
            return True
        if session.create_task is not None and not session.create_task.done():
            try:
                await session.create_task
            except Exception:
                pass
        if session.container is None:
            async with self._registry_lock:
                self._sessions.pop(agent_run_id, None)
            return True
        if session.active_call_id is not None:
            await self.cancel(agent_run_id, session.active_call_id)
        async with session.command_lock:
            try:
                session.state = "stopping"
                await self.runtime.destroy(session.container)
            except Exception as exc:
                session.state = "destroy_failed"
                session.destroy_error = type(exc).__name__
                return False
        session.state = "stopped"
        async with self._registry_lock:
            self._sessions.pop(agent_run_id, None)
        if self.artifact_store is not None:
            # Artifact retention is an explicit policy; the store may be
            # configured to retain files for audit and is therefore not
            # removed here.
            pass
        return True

    async def close(self) -> tuple[str, ...]:
        self._closing = True
        sessions = tuple(self._sessions)
        errors: list[str] = []
        await asyncio.gather(*(self.stop_for_agent(item) for item in sessions), return_exceptions=True)
        for item in sessions:
            session = self._sessions.get(item)
            if session is not None and session.destroy_error:
                errors.append(f"{item}:{session.destroy_error}")
        try:
            await self.runtime.close()
        except Exception as exc:
            errors.append(type(exc).__name__)
        return tuple(errors)
