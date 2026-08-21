"""Lifecycle manager for one Manifest-driven automation run."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import logging
from pathlib import Path
import re
import shutil
from uuid import uuid4

from automation.commands import DispatcherCommand, DispatcherEvent
from automation.decision import DispatcherDecision
from automation.dispatcher import (
    DispatcherError,
    DispatcherRunResult,
    ManifestDispatcher,
)
from automation.http import ManifestOperationExecutor
from automation.llm import LLMClient, LLMClientError, LLMConfig, RuntimeLLMSecret
from automation.manifest import Manifest, ManifestValidationError
from automation.secrets import RuntimePlatformSecret, SecretRedactor
from automation.store import ManifestStore, ManifestStoreError
from automation.workgroup import BlackboardWorkgroup, WorkgroupQuestion, WorkgroupError
from orchestrator.projects import ProjectService


logger = logging.getLogger(__name__)
_EVENT_SNAPSHOT_KEYS = frozenset(
    {
        "prompt",
        "messages",
        "raw_prompt",
        "raw_response",
        "llm_response",
        "api_key",
        "platform_token",
    }
)


class AutomationManagerError(RuntimeError):
    """Raised when a run cannot be started or controlled."""


@dataclass(slots=True)
class _AutomationRun:
    run_id: str
    manifest: Manifest
    max_tasks: int
    task_timeout_seconds: float
    poll_interval_seconds: float
    started_at: str
    status: str = "starting"
    processed_tasks: int = 0
    current_question: dict[str, object] | None = None
    workgroup_project_id: str | None = None
    workgroup_result: dict[str, object] | None = None
    stop_after_current: bool = False
    error: str | None = None
    events: list[DispatcherEvent] = field(default_factory=list)
    instructions: list[str] = field(default_factory=list)
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None
    llm_secret: RuntimeLLMSecret | None = None
    platform_secret: RuntimePlatformSecret | None = None
    llm_client: LLMClient | None = None
    executor: ManifestOperationExecutor | None = None
    event_log_path: Path | None = None

    def snapshot(self, redactor: SecretRedactor | None = None) -> dict[str, object]:
        redact = redactor or SecretRedactor()
        return {
            "run_id": self.run_id,
            "manifest_id": self.manifest.manifest_id,
            "manifest_hash": self.manifest.content_hash(),
            "status": self.status,
            "processed_tasks": self.processed_tasks,
            "max_tasks": self.max_tasks,
            "current_question": redact.value(self.current_question),
            "workgroup_project_id": self.workgroup_project_id,
            "workgroup_result": redact.value(self.workgroup_result),
            "stop_after_current": self.stop_after_current,
            "error": redact.text(self.error or ""),
            "started_at": self.started_at,
            "events": [event.model_dump(mode="json") for event in self.events],
        }


class AutomationManager:
    """Coordinate parser-produced manifests, dispatcher calls, and workgroups."""

    def __init__(self, service: ProjectService, data_root: Path) -> None:
        self.service = service
        self.data_root = data_root.resolve()
        self.manifests = ManifestStore(self.data_root / "automation" / "manifests")
        self._run: _AutomationRun | None = None
        self._lock = asyncio.Lock()

    async def start(
        self,
        *,
        manifest_id: str,
        dispatcher_config: LLMConfig,
        dispatcher_api_key: str,
        platform_token: str,
        solver_providers: tuple[str, ...],
        agent_limit: int,
        max_tasks: int,
        task_timeout_seconds: float,
        poll_interval_seconds: float,
    ) -> dict[str, object]:
        async with self._lock:
            if (
                self._run is not None
                and self._run.task is not None
                and not self._run.task.done()
            ):
                raise AutomationManagerError("an automation run is already running")
            try:
                manifest = self.manifests.load(manifest_id)
            except KeyError as exc:
                raise AutomationManagerError("manifest not found") from exc
            except ManifestStoreError as exc:
                raise AutomationManagerError("cannot load manifest") from exc
            try:
                manifest.validate_for_run()
            except ManifestValidationError as exc:
                raise AutomationManagerError(str(exc)) from exc
            if not solver_providers:
                raise AutomationManagerError("solver_providers must be non-empty")
            if not 1 <= agent_limit <= len(solver_providers):
                raise AutomationManagerError(
                    "agent_limit must be between 1 and solver provider count"
                )
            run_id = f"automation-{uuid4().hex}"
            run_root = self.data_root / "automation" / "runs" / run_id
            try:
                run_root.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise AutomationManagerError(
                    "cannot create automation run directory"
                ) from exc
            run = _AutomationRun(
                run_id=run_id,
                manifest=manifest,
                max_tasks=max_tasks,
                task_timeout_seconds=task_timeout_seconds,
                poll_interval_seconds=poll_interval_seconds,
                started_at=datetime.now(timezone.utc).isoformat(),
                llm_secret=RuntimeLLMSecret(dispatcher_api_key),
                platform_secret=RuntimePlatformSecret(platform_token),
                event_log_path=run_root / "events.jsonl",
            )
            self._run = run
            run.task = asyncio.create_task(
                self._run_loop(
                    run,
                    dispatcher_config,
                    tuple(solver_providers),
                    agent_limit,
                )
            )
            return run.snapshot(SecretRedactor(dispatcher_api_key, platform_token))

    async def stop(self) -> dict[str, object]:
        run = self._require_run()
        run.cancel_event.set()
        task = run.task
        if task is not None and task is not asyncio.current_task():
            await asyncio.gather(task, return_exceptions=True)
        if run.status not in {"completed", "failed", "stopped"}:
            run.status = "stopped"
        return self.snapshot()

    async def submit_instruction(self, instruction: str) -> dict[str, object]:
        text = instruction.strip()
        if not text:
            raise AutomationManagerError("instruction must be non-empty")
        run = self._require_run()
        if run.status not in {"starting", "running"}:
            raise AutomationManagerError("automation run is not active")
        run.instructions.append(text)
        await self._append_event(
            run,
            DispatcherEvent(
                event_type="message",
                status="planned",
                detail={
                    "source": "human",
                    "original_instruction": self._redactor(run).text(text),
                },
            ),
        )
        return self.snapshot()

    def snapshot(self) -> dict[str, object]:
        run = self._require_run()
        return run.snapshot(self._redactor(run))

    def events(self) -> tuple[dict[str, object], ...]:
        run = self._require_run()
        return tuple(event.model_dump(mode="json") for event in run.events)

    async def aclose(self) -> None:
        if (
            self._run is not None
            and self._run.task is not None
            and not self._run.task.done()
        ):
            await self.stop()
        run = self._run
        if run is not None:
            await self._cleanup_runtime(run)
        self._run = None

    async def _run_loop(
        self,
        run: _AutomationRun,
        dispatcher_config: LLMConfig,
        solver_providers: tuple[str, ...],
        agent_limit: int,
    ) -> None:
        run_root = self.data_root / "automation" / "runs" / run.run_id
        run_root.mkdir(parents=True, exist_ok=True)
        redactor = self._redactor(run)
        assert run.llm_secret is not None
        assert run.platform_secret is not None
        run.llm_client = LLMClient(dispatcher_config, run.llm_secret)
        run.executor = ManifestOperationExecutor(
            run.manifest,
            run.platform_secret,
            timeout_seconds=dispatcher_config.timeout_seconds,
            redactor=redactor,
        )
        workgroup = BlackboardWorkgroup(
            self.service,
            solver_providers=solver_providers,
            agent_limit=agent_limit,
            timeout_seconds=run.task_timeout_seconds,
        )
        run.status = "running"
        platform_state: object | None = None
        try:
            while run.processed_tasks < run.max_tasks and not run.cancel_event.is_set():
                instruction = self._drain_instructions(run)
                pending_attachments = run_root / "attachments" / "_pending"
                first = await self._dispatch(
                    run,
                    platform_state=platform_state,
                    human_instruction=instruction,
                    download_root=pending_attachments,
                )
                decision = self._require_decision(first)
                if decision.status == "wait":
                    await _sleep_or_cancel(
                        run.poll_interval_seconds,
                        run.cancel_event,
                    )
                    continue
                if decision.status in {"finished", "stopped"}:
                    _discard_attachment_directory(pending_attachments)
                    run.status = (
                        "completed"
                        if decision.status == "finished"
                        else "stopped"
                    )
                    break
                if decision.status in {"skipped", "abandoned"}:
                    _discard_attachment_directory(pending_attachments)
                    run.processed_tasks += 1
                    if run.stop_after_current:
                        run.status = "completed"
                        break
                    continue
                if decision.status != "question" or decision.question is None:
                    _discard_attachment_directory(pending_attachments)
                    raise AutomationManagerError(
                        "dispatcher did not return a usable question: "
                        f"{decision.status}"
                    )
                question_attachments = _claim_question_attachments(
                    pending_attachments,
                    run_root / "attachments",
                    run.processed_tasks + 1,
                    decision.question.external_id,
                )
                run.current_question = redactor.value(
                    decision.question.model_dump(mode="json")
                )
                solution = await workgroup.solve(
                    WorkgroupQuestion(
                        external_id=decision.question.external_id,
                        name=decision.question.name,
                        goal=decision.question.goal,
                        origin=decision.question.origin,
                        hints=decision.question.hints,
                        attachments_dir=question_attachments,
                    )
                )
                run.workgroup_project_id = solution.project_id
                run.workgroup_result = redactor.value(solution.dispatcher_context())
                instruction = self._drain_instructions(run)
                second = await self._dispatch(
                    run,
                    workgroup_result=solution.dispatcher_context(),
                    platform_state=decision.metadata,
                    human_instruction=instruction,
                    download_root=question_attachments,
                )
                second_decision = self._require_decision(second)
                if second_decision.status == "wait":
                    platform_state = second_decision.metadata
                elif second_decision.status in {"finished", "stopped"}:
                    run.status = (
                        "completed"
                        if second_decision.status == "finished"
                        else "stopped"
                    )
                    break
                elif second_decision.status in {
                    "skipped",
                    "abandoned",
                    "submitted",
                    "question",
                }:
                    platform_state = second_decision.metadata
                else:
                    raise AutomationManagerError(
                        "dispatcher returned unusable post-solve status: "
                        f"{second_decision.status}"
                    )
                run.processed_tasks += 1
                run.current_question = None
                run.workgroup_result = None
                if run.stop_after_current:
                    run.status = "completed"
                    break
            if run.cancel_event.is_set() and run.status == "running":
                run.status = "stopped"
            elif run.status == "running":
                run.status = "completed"
        except asyncio.CancelledError:
            run.status = "stopped"
            raise
        except (
            AutomationManagerError,
            DispatcherError,
            LLMClientError,
            WorkgroupError,
            OSError,
            ValueError,
        ) as exc:
            run.error = redactor.text(str(exc))
            run.status = "failed"
        except Exception as exc:
            run.error = redactor.text(
                f"automation run failed: {type(exc).__name__}: {exc}"
            )
            run.status = "failed"
        finally:
            await self._cleanup_runtime(run)

    async def _dispatch(
        self,
        run: _AutomationRun,
        *,
        workgroup_result: object | None = None,
        platform_state: object | None = None,
        human_instruction: str | None = None,
        download_root: Path | None = None,
    ) -> DispatcherRunResult:
        assert run.llm_client is not None
        assert run.executor is not None
        dispatcher = ManifestDispatcher(
            run.llm_client,
            run.manifest,
            run.executor,
            event_sink=lambda event: self._append_event(run, event),
            download_root=download_root
            or self.data_root
            / "automation"
            / "runs"
            / run.run_id
            / "attachments",
        )
        result = await dispatcher.run(
            workgroup_result=workgroup_result,
            platform_state=platform_state,
            human_instruction=human_instruction,
            cancel_event=run.cancel_event,
        )
        for command in result.commands:
            if command.command_type == "stop_run":
                run.cancel_event.set()
                run.status = "stopped"
            elif command.command_type == "stop_after_current":
                run.stop_after_current = True
        return result

    async def _append_event(self, run: _AutomationRun, event: DispatcherEvent) -> None:
        safe_event = event.model_copy(
            update={
                "detail": _redact_event_detail(
                    self._redactor(run), event.detail
                )
            }
        )
        run.events.append(safe_event)
        if run.event_log_path is None:
            return
        try:
            with run.event_log_path.open("a", encoding="utf-8") as stream:
                stream.write(safe_event.model_dump_json() + "\n")
        except OSError:
            logger.warning(
                "cannot persist automation event for run %s",
                run.run_id,
                exc_info=True,
            )

    @staticmethod
    def _require_decision(result: DispatcherRunResult) -> DispatcherDecision:
        if result.decision is None:
            raise AutomationManagerError(
                "dispatcher final response must be the documented JSON decision"
            )
        return result.decision

    @staticmethod
    def _drain_instructions(run: _AutomationRun) -> str | None:
        if not run.instructions:
            return None
        instructions = run.instructions[:]
        run.instructions.clear()
        return "\n".join(instructions)

    @staticmethod
    def _redactor(run: _AutomationRun) -> SecretRedactor:
        return SecretRedactor(
            run.llm_secret.api_key if run.llm_secret else "",
            run.platform_secret.token if run.platform_secret else "",
        )

    async def _cleanup_runtime(self, run: _AutomationRun) -> None:
        if run.executor is not None:
            await run.executor.close()
            run.executor = None
        if run.llm_client is not None:
            await run.llm_client.close()
            run.llm_client = None
        if run.llm_secret is not None:
            run.llm_secret.clear()
        if run.platform_secret is not None:
            run.platform_secret.clear()

    def _require_run(self) -> _AutomationRun:
        if self._run is None:
            raise AutomationManagerError("no automation run exists")
        return self._run


async def _sleep_or_cancel(delay: float, cancel_event: asyncio.Event) -> None:
    sleep_task = asyncio.create_task(asyncio.sleep(delay))
    cancel_task = asyncio.create_task(cancel_event.wait())
    done, _ = await asyncio.wait(
        {sleep_task, cancel_task},
        return_when=asyncio.FIRST_COMPLETED,
    )
    if cancel_task in done and cancel_event.is_set():
        sleep_task.cancel()
        await asyncio.gather(sleep_task, return_exceptions=True)
        raise asyncio.CancelledError
    cancel_task.cancel()
    await asyncio.gather(cancel_task, return_exceptions=True)


def _redact_event_detail(redactor: SecretRedactor, value: object) -> object:
    if isinstance(value, Mapping):
        return {
            str(key): _redact_event_detail(redactor, child)
            for key, child in value.items()
            if str(key).casefold() not in _EVENT_SNAPSHOT_KEYS
        }
    if isinstance(value, list):
        return [_redact_event_detail(redactor, child) for child in value]
    if isinstance(value, tuple):
        return [_redact_event_detail(redactor, child) for child in value]
    if isinstance(value, str):
        return redactor.text(value)
    return value


def _claim_question_attachments(
    pending_root: Path,
    attachments_root: Path,
    question_number: int,
    external_id: str,
) -> Path:
    """Move staged downloads into a directory owned by one question."""

    digest = hashlib.sha256(external_id.encode("utf-8")).hexdigest()[:16]
    label = re.sub(r"[^A-Za-z0-9_.-]+", "-", external_id).strip(".-")[:60]
    label = label or "question"
    target = attachments_root / f"question-{question_number:04d}-{label}-{digest}"
    target.parent.mkdir(parents=True, exist_ok=True)
    if pending_root.is_dir():
        if target.exists():
            raise OSError(f"question attachment directory already exists: {target}")
        shutil.move(str(pending_root), str(target))
    target.mkdir(parents=True, exist_ok=True)
    return target


def _discard_attachment_directory(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
