"""In-memory lifecycle manager for the full-automatic challenge mode."""

from __future__ import annotations

import asyncio
import re
from uuid import uuid4

from autopilot.controller import AutoController, AutoControllerError
from autopilot.models import (
    AutoHistoryEntry,
    AutoSession,
    AutoStartConfig,
    AutoTask,
)
from orchestrator.projects import ProjectService
from worker.models import AuditConfig, ProviderConfig


class AutoManagerError(RuntimeError):
    """A requested automation operation is invalid or cannot continue."""


_CREDENTIAL_PATTERN = re.compile(
    r"(?i)\b(?:token|api[_-]?key|secret|password)\s*[:=]\s*([^\s,;]+)"
)
_BEARER_PATTERN = re.compile(r"(?i)\bbearer\s+([^\s,;]+)")


def _scrub_text(value: str, platform_prompt: str) -> str:
    scrubbed = value.replace(platform_prompt, "[redacted]")
    values = [match.group(1) for match in _CREDENTIAL_PATTERN.finditer(platform_prompt)]
    values.extend(match.group(1) for match in _BEARER_PATTERN.finditer(platform_prompt))
    for credential in values:
        if len(credential) >= 4:
            scrubbed = scrubbed.replace(credential, "[redacted]")
    scrubbed = _CREDENTIAL_PATTERN.sub(
        lambda match: match.group(0)[: match.group(0).find(match.group(1))]
        + "[redacted]",
        scrubbed,
    )
    return _BEARER_PATTERN.sub("Bearer [redacted]", scrubbed)


class AutoManager:
    def __init__(
        self,
        *,
        service: ProjectService,
        provider_configs: tuple[ProviderConfig, ...],
        execute=None,
        audit_config: AuditConfig | None = None,
        poll_interval_seconds: float = 1.0,
    ) -> None:
        self.service = service
        self.providers = {provider.name: provider for provider in provider_configs}
        self.execute = execute
        self.audit_config = audit_config
        self.default_poll_interval_seconds = poll_interval_seconds
        self._session: AutoSession | None = None
        self._runner: asyncio.Task[None] | None = None
        self._cancel_event: asyncio.Event | None = None
        self._lock = asyncio.Lock()

    async def start(self, config: AutoStartConfig) -> dict[str, object]:
        async with self._lock:
            if self._is_active():
                raise AutoManagerError("an automatic session is already running")
            self._validate_config(config)
            session_id = f"auto-{uuid4().hex}"
            runtime_root = (
                self.service.registry.data_root / "autopilot" / session_id
            ).resolve()
            runtime_root.mkdir(parents=True, exist_ok=False)
            session = AutoSession(session_id, config, runtime_root)
            self._session = session
            self._cancel_event = asyncio.Event()
            self._runner = asyncio.create_task(self._run(session, self._cancel_event))
            return self._snapshot_locked()

    async def stop(self) -> dict[str, object]:
        async with self._lock:
            session = self._session
            runner = self._runner
            cancel_event = self._cancel_event
            if session is None:
                return self._snapshot_locked()
            if session.status not in {"finished", "failed", "stopped"}:
                session.status = "stopping"
            if cancel_event is not None:
                cancel_event.set()
            project_id = session.current_project_id

        if project_id is not None:
            try:
                await self.service.stop_project(project_id)
            except Exception:
                # The runner will report a stable stopped state; do not leak
                # provider or platform details from a cleanup failure.
                pass
        if runner is not None and runner is not asyncio.current_task():
            await asyncio.gather(runner, return_exceptions=True)
        async with self._lock:
            if self._session is not None and self._session.status not in {
                "finished",
                "failed",
            }:
                self._session.status = "stopped"
                self._clear_prompt(self._session)
            return self._snapshot_locked()

    async def aclose(self) -> None:
        await self.stop()
        runner = self._runner
        if runner is not None and not runner.done():
            await asyncio.gather(runner, return_exceptions=True)
        async with self._lock:
            self._runner = None
            self._cancel_event = None

    async def snapshot(self) -> dict[str, object]:
        async with self._lock:
            return self._snapshot_locked()

    def _snapshot_locked(self) -> dict[str, object]:
        providers = tuple(self.providers)
        return {
            "session": (
                self._session.snapshot(providers) if self._session is not None else None
            ),
            "providers": list(providers),
        }

    def _is_active(self) -> bool:
        return self._runner is not None and not self._runner.done()

    def _validate_config(self, config: AutoStartConfig) -> None:
        available = {
            name for name, provider in self.providers.items() if provider.enabled
        }
        selected = {config.controller_provider, *config.solver_providers}
        unknown = sorted(selected - available)
        if unknown:
            raise AutoManagerError(f"unknown or disabled providers: {unknown}")
        if len(set(config.solver_providers)) != len(config.solver_providers):
            raise AutoManagerError("solver providers must be unique")

    async def _run(self, session: AutoSession, cancel_event: asyncio.Event) -> None:
        session.status = "running"
        completed: list[str] = []
        try:
            while session.completed_tasks < session.config.max_tasks:
                if cancel_event.is_set():
                    raise asyncio.CancelledError
                session.status = "running"
                controller_call = await self._controller_call(
                    session,
                    cancel_event,
                    action="next",
                    completed_ids=tuple(completed),
                )
                decision = controller_call.result
                if decision.action == "wait":
                    session.status = "waiting"
                    session.last_message = "platform queue is temporarily empty"
                    await self._sleep_or_cancel(
                        session.config.poll_interval_seconds, cancel_event
                    )
                    continue
                if decision.action == "finished":
                    session.status = "finished"
                    session.last_message = "platform queue finished"
                    break
                if decision.action != "task":
                    raise AutoManagerError("controller returned an invalid next action")
                task = AutoTask(
                    external_id=decision.external_id or "",
                    name=_scrub_text(decision.name or "", session.config.platform_prompt),
                    goal=_scrub_text(decision.goal or "", session.config.platform_prompt),
                    origin=_scrub_text(decision.origin or "", session.config.platform_prompt),
                    hints=tuple(
                        _scrub_text(hint, session.config.platform_prompt)
                        for hint in decision.hints
                    ),
                    challenge_path=controller_call.challenge_path,
                )
                if task.external_id in completed:
                    raise AutoManagerError("controller returned a duplicate external_id")
                await self._solve_task(session, task, cancel_event)
                answer = self._project_answer(session)
                session.status = "submitting"
                submission = await self._controller_call(
                    session,
                    cancel_event,
                    action="submit",
                    completed_ids=tuple(completed),
                    task=task,
                    answer=answer,
                )
                if submission.result.action != "accepted":
                    raise AutoManagerError("platform rejected the answer")
                session.history.append(
                    AutoHistoryEntry(
                        external_id=task.external_id,
                        name=task.name,
                        project_id=session.current_project_id or "",
                        answer=answer,
                        status="accepted",
                        message=None,
                    )
                )
                completed.append(task.external_id)
                session.completed_tasks += 1
                session.current_external_id = None
                session.current_task_name = None
                session.current_project_id = None
                session.last_message = "answer accepted"
            if session.status not in {"finished", "failed", "stopped"}:
                session.status = "finished"
                session.last_message = "maximum task limit reached"
        except asyncio.CancelledError:
            session.status = "stopped"
            session.last_message = "automatic session stopped"
        except AutoManagerError as exc:
            session.status = "failed"
            session.last_error = str(exc)
        except Exception as exc:
            session.status = "failed"
            session.last_error = f"automatic session failed: {type(exc).__name__}"
        finally:
            if session.status in {"finished", "failed", "stopped"}:
                self._clear_prompt(session)

    async def _controller_call(self, session, cancel_event, **kwargs):
        controller = AutoController(
            providers=self.providers,
            execute=self.execute,
            coordinator=self.service.coordinator,
            runtime_root=session.runtime_root,
            audit_config=self.audit_config,
        )
        try:
            return await controller.call(
                session_id=session.id,
                platform_prompt=session.config.platform_prompt,
                provider_name=session.config.controller_provider,
                timeout_seconds=session.config.controller_timeout_seconds,
                cancel_event=cancel_event,
                **kwargs,
            )
        except AutoControllerError as exc:
            raise AutoManagerError(str(exc)) from exc

    async def _solve_task(
        self,
        session: AutoSession,
        task: AutoTask,
        cancel_event: asyncio.Event,
    ) -> None:
        session.current_external_id = task.external_id
        session.current_task_name = task.name
        try:
            project = self.service.create_project(
                name=f"Auto {session.completed_tasks + 1}: {task.name}",
                goal=task.goal,
                origin=task.origin,
                hints=task.hints,
                mode="run",
                provider_names=session.config.solver_providers,
                agent_limit=session.config.agent_limit,
                challenge_path=task.challenge_path,
            )
            session.current_project_id = project.id
            await self.service.start_project(project.id)
            deadline = asyncio.get_running_loop().time() + session.config.task_timeout_seconds
            while True:
                if cancel_event.is_set():
                    raise asyncio.CancelledError
                snapshot = project.store.snapshot()["project"]
                status = str(snapshot["status"])
                if status == "achieved":
                    return
                if status in {"degraded", "stopped"}:
                    raise AutoManagerError(f"solver project ended with {status}")
                if asyncio.get_running_loop().time() >= deadline:
                    await self.service.stop_project(project.id)
                    raise AutoManagerError("solver project timed out")
                await self._sleep_or_cancel(
                    session.config.poll_interval_seconds, cancel_event
                )
        except asyncio.CancelledError:
            if session.current_project_id is not None:
                await self.service.stop_project(session.current_project_id)
            raise

    def _project_answer(self, session: AutoSession) -> str:
        if session.current_project_id is None:
            raise AutoManagerError("solver project is missing")
        project = self.service.get_handle(session.current_project_id)
        value = project.store.snapshot()["project"].get("final_answer")
        if not isinstance(value, str) or not value.strip():
            raise AutoManagerError("solver project achieved without a final answer")
        return value.strip()

    @staticmethod
    async def _sleep_or_cancel(delay: float, cancel_event: asyncio.Event) -> None:
        try:
            await asyncio.wait_for(cancel_event.wait(), timeout=delay)
        except asyncio.TimeoutError:
            return
        raise asyncio.CancelledError

    @staticmethod
    def _clear_prompt(session: AutoSession) -> None:
        object.__setattr__(session.config, "platform_prompt", "")
