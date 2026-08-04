"""Run the short-lived LLM that talks to an external challenge platform."""

from __future__ import annotations

import asyncio
import inspect
import shutil
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import uuid4

from autopilot.models import AutoControllerResult, AutoTask
from autopilot.protocol import parse_controller_result, render_controller_prompt
from orchestrator.coordinator import ExecutionCoordinator, PermitCancelled
from worker.models import AuditConfig, ProviderConfig, WorkerResult


class AutoControllerError(RuntimeError):
    """A controller call could not produce a valid protocol result."""


@dataclass(frozen=True, slots=True)
class ControllerCall:
    result: AutoControllerResult
    challenge_path: Path | None = None


class AutoController:
    def __init__(
        self,
        *,
        providers: dict[str, ProviderConfig],
        execute,
        coordinator: ExecutionCoordinator,
        runtime_root: Path,
        audit_config: AuditConfig | None = None,
    ) -> None:
        self.providers = providers
        self.execute = execute
        self.coordinator = coordinator
        self.runtime_root = runtime_root.resolve()
        self.audit_config = audit_config or AuditConfig()
        self._accepts_log_limit = self._accepts_keyword(execute, "max_log_bytes")

    @staticmethod
    def _accepts_keyword(function, name: str) -> bool:
        try:
            parameters = inspect.signature(function).parameters
        except (TypeError, ValueError):
            return False
        return name in parameters or any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        )

    async def call(
        self,
        *,
        session_id: str,
        platform_prompt: str,
        provider_name: str,
        action: str,
        completed_ids: tuple[str, ...] = (),
        task: AutoTask | None = None,
        answer: str | None = None,
        timeout_seconds: float,
        cancel_event: asyncio.Event,
    ) -> ControllerCall:
        provider = self.providers.get(provider_name)
        if provider is None or not provider.enabled:
            raise AutoControllerError("controller provider is unavailable")
        if self.execute is None:
            raise AutoControllerError("no execution backend is configured")

        request = render_controller_prompt(
            platform_prompt,
            action,
            completed_ids=completed_ids,
            task=task,
            answer=answer,
        )
        call_id = uuid4().hex
        work_dir = self.runtime_root / "controller" / call_id
        work_dir.mkdir(parents=True, exist_ok=False)
        challenge_path: Path | None = None
        result: WorkerResult | None = None
        try:
            # Controller calls consume the same coordinator capacity as planning
            # calls, so automation cannot bypass global/provider limits.
            try:
                async with self.coordinator.permit(
                    project_id=f"autopilot:{session_id}",
                    provider=provider.name,
                    kind="planning",
                    priority=provider.priority,
                    provider_limit=provider.max_running,
                    cancel_event=cancel_event,
                ):
                    effective_provider = replace(
                        provider,
                        timeout_seconds=min(provider.timeout_seconds, timeout_seconds),
                    )
                    kwargs = {"cancel_event": cancel_event}
                    if self._accepts_log_limit:
                        kwargs["max_log_bytes"] = self.audit_config.max_log_bytes
                    result = await asyncio.wait_for(
                        self.execute(
                            effective_provider,
                            request,
                            None,
                            work_dir,
                            **kwargs,
                        ),
                        timeout=timeout_seconds,
                    )
            except asyncio.TimeoutError as exc:
                raise AutoControllerError("controller call timed out") from exc
            except asyncio.CancelledError:
                raise
            except PermitCancelled as exc:
                if cancel_event.is_set():
                    raise asyncio.CancelledError from exc
                raise AutoControllerError("controller permit wait was cancelled") from exc
            except AutoControllerError:
                raise
            except Exception as exc:
                raise AutoControllerError(
                    f"controller execution failed: {type(exc).__name__}"
                ) from exc

            if cancel_event.is_set() and result.status == "cancelled":
                raise asyncio.CancelledError
            if result.status != "success":
                raise AutoControllerError(
                    f"controller execution returned {result.status}"
                )
            parsed = parse_controller_result(result.output)
            if parsed.status != "valid" or parsed.result is None:
                raise AutoControllerError(parsed.error or "invalid controller result")
            if parsed.result.action == "task":
                challenge_path = self._move_challenge(work_dir, call_id)
            return ControllerCall(parsed.result, challenge_path)
        finally:
            self._remove_transient(work_dir)
            if result is not None:
                for raw_path in (result.stdout_path, result.stderr_path):
                    self._remove_if_inside(Path(raw_path), work_dir)

    def _move_challenge(self, work_dir: Path, call_id: str) -> Path | None:
        staged = work_dir / "challenge"
        if not staged.exists():
            return None
        if staged.is_symlink() or not staged.is_dir():
            raise AutoControllerError("controller challenge attachment is not a directory")
        destination = self.runtime_root / "tasks" / call_id
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            staged.replace(destination)
        except OSError as exc:
            raise AutoControllerError("could not store challenge attachment") from exc
        return destination.resolve()

    @staticmethod
    def _remove_if_inside(path: Path, root: Path) -> None:
        try:
            resolved = path.resolve()
            root_resolved = root.resolve()
            if resolved == root_resolved or root_resolved in resolved.parents:
                if resolved.is_dir():
                    shutil.rmtree(resolved, ignore_errors=True)
                else:
                    resolved.unlink(missing_ok=True)
        except OSError:
            pass

    @staticmethod
    def _remove_transient(path: Path) -> None:
        try:
            shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass
