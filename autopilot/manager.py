"""In-memory lifecycle manager for the full-automatic challenge mode."""

from __future__ import annotations

import asyncio
import json
import re
import shutil
from dataclasses import replace
from datetime import datetime, timezone
from uuid import uuid4

from autopilot.controller import AutoController, AutoControllerError
from autopilot.direct_controller import DirectControllerError, DirectLLMController
from autopilot.models import (
    AutoHistoryEntry,
    AutoSession,
    AutoStartConfig,
    AutoTask,
    PromptPlatformConfig,
    RuntimeControllerSecret,
    RuntimePlatformSecret,
)
from autopilot.platform import PromptPlatformAdapter, StructuredPlatformAdapter
from autopilot.platform_models import PlatformResult, StructuredPlatformConfig
from orchestrator.projects import ProjectService
from worker.models import AuditConfig, ProviderConfig
from worker.capabilities import probe_provider_capabilities, static_capability_snapshot


class AutoManagerError(RuntimeError):
    """A requested automation operation is invalid or cannot continue."""


_CREDENTIAL_PATTERN = re.compile(
    r"(?i)\b(?:token|api[_-]?key|secret|password)\s*[:=]\s*([^\s,;]+)"
)
_BEARER_PATTERN = re.compile(r"(?i)\bbearer\s+([^\s,;]+)")

TASK_TRANSITIONS = {
    None: {"queued"},
    "queued": {"fetched", "waiting", "finished", "retry_wait", "failed", "stopped"},
    "fetched": {"solving", "skipped", "failed", "stopped"},
    "solving": {"solver_achieved", "skipped", "failed", "stopped"},
    "solver_achieved": {"submit_pending", "failed", "stopped"},
    "submit_pending": {"accepted", "rejected", "retry_wait", "failed", "stopped"},
    "rejected": {"replanning", "skipped", "failed", "stopped"},
    "replanning": {"submit_pending", "skipped", "failed", "stopped"},
    "retry_wait": {"queued", "submit_pending", "failed", "stopped"},
    "waiting": {"queued", "failed", "stopped"},
}

TASK_REASON_CODES = frozenset(
    {
        "platform_wait",
        "platform_transport",
        "platform_rejected",
        "platform_retry_exhausted",
        "platform_submission_failed",
        "solver_timeout",
        "solver_degraded",
        "result_conflict",
        "task_info_skip_required",
        "skip_failed",
        "skipped",
        "user_stopped",
    }
)


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


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


class AutoManager:
    def __init__(
        self,
        *,
        service: ProjectService,
        provider_configs: tuple[ProviderConfig, ...],
        execute=None,
        audit_config: AuditConfig | None = None,
        poll_interval_seconds: float = 1.0,
        controller_factory=None,
        platform_adapter_factory=None,
    ) -> None:
        self.service = service
        self.providers = {provider.name: provider for provider in provider_configs}
        self.execute = execute
        self.audit_config = audit_config
        self.default_poll_interval_seconds = poll_interval_seconds
        self.controller_factory = controller_factory
        self.platform_adapter_factory = platform_adapter_factory
        self._session: AutoSession | None = None
        self._runner: asyncio.Task[None] | None = None
        self._cancel_event: asyncio.Event | None = None
        self._lock = asyncio.Lock()
        self._controller: DirectLLMController | None = None
        self._adapter: PromptPlatformAdapter | StructuredPlatformAdapter | None = None
        self._controller_secret: RuntimeControllerSecret | None = None
        self._platform_secret: RuntimePlatformSecret | None = None
        self._provider_capabilities = {
            name: static_capability_snapshot(provider)
            for name, provider in self.providers.items()
        }

    async def start(
        self,
        config: AutoStartConfig,
        controller_secret: RuntimeControllerSecret | None = None,
        platform_secret: RuntimePlatformSecret | None = None,
    ) -> dict[str, object]:
        async with self._lock:
            if self._is_active():
                raise AutoManagerError("an automatic session is already running")
            self._validate_config(config)
            if not config.is_legacy:
                if controller_secret is None or not controller_secret.api_key.strip():
                    raise AutoManagerError("controller api key is required")
                assert config.controller is not None
                try:
                    self._controller_secret = controller_secret
                    if platform_secret is None:
                        platform_secret = RuntimePlatformSecret(
                            prompt=(
                                config.platform.prompt
                                if isinstance(config.platform, PromptPlatformConfig)
                                else ""
                            )
                        )
                    self._platform_secret = platform_secret
                    if (
                        isinstance(config.platform, PromptPlatformConfig)
                        and not platform_secret.prompt.strip()
                    ):
                        raise AutoManagerError("platform prompt is required")
                    if isinstance(config.platform, PromptPlatformConfig):
                        object.__setattr__(config.platform, "prompt", "")
                    controller_factory = self.controller_factory or DirectLLMController
                    self._controller = controller_factory(config.controller, controller_secret)
                    if self.platform_adapter_factory is not None:
                        self._adapter = self.platform_adapter_factory(config.platform, platform_secret)
                    elif isinstance(config.platform, PromptPlatformConfig):
                        self._adapter = PromptPlatformAdapter(config.platform)
                    elif isinstance(config.platform, StructuredPlatformConfig):
                        self._adapter = StructuredPlatformAdapter(config.platform, secret=platform_secret)
                    else:
                        raise AutoManagerError("platform configuration does not match mode")
                    probed = await asyncio.gather(
                        *(
                            probe_provider_capabilities(self.providers[name])
                            for name in config.solver_providers
                        )
                    )
                    self._provider_capabilities.update(
                        {name: result for name, result in zip(config.solver_providers, probed)}
                    )
                    incompatible = [
                        name
                        for name in config.solver_providers
                        if not self._provider_capabilities[name].get("auto_ready", False)
                    ]
                    if incompatible:
                        raise AutoManagerError(
                            f"solver provider capability probe failed: {incompatible}"
                        )
                    await self._controller.preflight()
                except asyncio.CancelledError:
                    await self._dispose_modern_runtime()
                    self._clear_config_platform_secret(config)
                    raise
                except Exception as exc:
                    await self._dispose_modern_runtime()
                    self._clear_config_platform_secret(config)
                    raise AutoManagerError(str(exc)) from exc
            session_id = f"auto-{uuid4().hex}"
            runtime_root = (
                self.service.registry.data_root / "autopilot" / session_id
            ).resolve()
            try:
                runtime_root.mkdir(parents=True, exist_ok=False)
                session = AutoSession(session_id, config, runtime_root)
                self._session = session
                if self._controller is not None and hasattr(self._controller, "set_runtime_root"):
                    self._controller.set_runtime_root(runtime_root)
                if isinstance(self._adapter, PromptPlatformAdapter):
                    self._adapter.set_runtime_root(runtime_root)
                self._cancel_event = asyncio.Event()
                self._runner = asyncio.create_task(
                    self._run(session, self._cancel_event)
                )
                return self._snapshot_locked()
            except Exception:
                if not config.is_legacy:
                    await self._dispose_modern_runtime()
                shutil.rmtree(runtime_root, ignore_errors=True)
                raise

    async def stop(self) -> dict[str, object]:
        async with self._lock:
            session = self._session
            runner = self._runner
            cancel_event = self._cancel_event
            if session is None:
                return self._snapshot_locked()
            if session.status not in {"finished", "failed", "stopped"}:
                session.status = "stopping"
                if session.current_external_id is not None:
                    if session.current_task_status in TASK_TRANSITIONS and session.current_task_status != "stopped":
                        try:
                            self._transition(session, "stopped", "user_stopped")
                        except AutoManagerError:
                            session.current_task_status = "stopped"
                            session.current_reason_code = "user_stopped"
                    self._append_stopped_history(session)
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
        if runner is not None and not runner.done():
            runner.cancel()
        if runner is not None and runner is not asyncio.current_task():
            await asyncio.gather(runner, return_exceptions=True)
        async with self._lock:
            if self._session is not None and self._session.status not in {
                "finished",
                "failed",
            }:
                self._session.status = "stopped"
                if self._session.current_task_status is not None:
                    self._session.current_task_status = "stopped"
                    self._session.current_reason_code = "user_stopped"
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
            "provider_capabilities": {
                name: self._provider_capabilities[name]
                for name in self.providers
            },
        }

    def _is_active(self) -> bool:
        return self._runner is not None and not self._runner.done()

    def _validate_config(self, config: AutoStartConfig) -> None:
        available = {
            name for name, provider in self.providers.items() if provider.enabled
        }
        selected = set(config.solver_providers)
        if config.is_legacy:
            selected.add(config.controller_provider)
        unknown = sorted(selected - available)
        if unknown:
            raise AutoManagerError(f"unknown or disabled providers: {unknown}")
        if len(set(config.solver_providers)) != len(config.solver_providers):
            raise AutoManagerError("solver providers must be unique")
        if not config.is_legacy:
            incompatible = [
                name for name in config.solver_providers
                if not static_capability_snapshot(self.providers[name])["auto_ready"]
            ]
            if incompatible:
                raise AutoManagerError(f"solver providers are not auto-ready: {incompatible}")

    async def _run(self, session: AutoSession, cancel_event: asyncio.Event) -> None:
        if session.config.is_legacy:
            await self._run_legacy(session, cancel_event)
        else:
            await self._run_modern(session, cancel_event)

    async def _run_legacy(self, session: AutoSession, cancel_event: asyncio.Event) -> None:
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

    async def _run_modern(self, session: AutoSession, cancel_event: asyncio.Event) -> None:
        seen_ids: set[str] = set()
        try:
            while session.processed_tasks < session.config.max_tasks:
                if cancel_event.is_set():
                    raise asyncio.CancelledError
                self._transition(session, "queued")
                fetched = await self._fetch_modern(session)
                if fetched.status == "retryable_error":
                    fetched = await self._retry_fetch(session, fetched, cancel_event)
                if fetched.status == "wait":
                    self._transition(session, "waiting", "platform_wait")
                    wait_seconds = max(
                        0.1,
                        min(
                            fetched.retry_after_seconds or session.config.poll_interval_seconds,
                            300.0,
                        ),
                    )
                    await self._sleep_or_cancel(wait_seconds, cancel_event)
                    self._transition(session, "queued")
                    continue
                if fetched.status == "finished":
                    self._transition(session, "finished")
                    session.last_message = fetched.message or "platform queue finished"
                    break
                if fetched.status == "skip_required" and isinstance(fetched.task, AutoTask):
                    self._transition(session, "fetched", "task_info_skip_required")
                    await self._skip_modern(
                        session,
                        fetched.task,
                        fetched.message or "task info failed",
                    )
                    continue
                if fetched.status != "task" or not isinstance(fetched.task, AutoTask):
                    raise AutoManagerError(fetched.message or "platform fetch failed")
                task = fetched.task
                if task.external_id in seen_ids:
                    raise AutoManagerError("platform returned a duplicate external_id")
                seen_ids.add(task.external_id)
                if isinstance(self._adapter, PromptPlatformAdapter):
                    attachment_paths = task.raw_metadata.get("attachment_paths", ())
                    promoted = self._adapter.promote_attachments(task.external_id, attachment_paths)
                    if promoted:
                        task = replace(
                            task,
                            challenge_path=promoted[0],
                            raw_metadata={**dict(task.raw_metadata), "attachment_paths": tuple(str(path.relative_to(session.runtime_root)) for path in promoted)},
                        )
                self._transition(session, "fetched")
                self._transition(session, "solving")
                try:
                    await self._solve_task(session, task, cancel_event)
                except AutoManagerError as exc:
                    if session.config.solver_failure_action == "skip":
                        await self._skip_modern(session, task, str(exc))
                        continue
                    raise
                goal_result = self.service.get_goal_result(session.current_project_id or "")
                if goal_result is None:
                    project_id = session.current_project_id or ""
                    conflict_checker = getattr(self.service, "goal_result_conflict", None)
                    if project_id and callable(conflict_checker) and conflict_checker(project_id):
                        raise AutoManagerError(
                            "result_conflict: solver providers produced multiple qualifying result groups"
                        )
                    raise AutoManagerError(
                        "solver project achieved without a Goal result Fact"
                    )
                self._transition(session, "solver_achieved")
                session.current_result_ref = goal_result.result_ref
                session.current_result_fact_id = goal_result.fact.id
                session.current_result_preview = goal_result.fact.content[:500]
                assert self._controller is not None
                plan = await self._controller.plan_submission(
                    task, goal_result, cancel_event=cancel_event
                )
                session.current_plan_fingerprint = plan.fingerprint
                session.current_attempt_phase = "submit"
                self._transition(session, "submit_pending")
                submission, attempt_records = await self._submit_modern(
                    session, task, goal_result, plan, cancel_event
                )
                replans = 0
                while submission.status == "rejected" and session.config.rejected_action == "replan" and replans < session.config.max_replans_after_rejection:
                    self._transition(session, "rejected", "platform_rejected")
                    self._transition(session, "replanning")
                    previous_fingerprint = plan.fingerprint
                    plan = await self._controller.plan_submission(
                        task,
                        goal_result,
                        rejection=submission.message or "rejected",
                        cancel_event=cancel_event,
                    )
                    if plan.fingerprint == previous_fingerprint:
                        raise AutoManagerError(
                            "submission replan did not change the frozen plan"
                        )
                    session.current_replan_count += 1
                    session.current_plan_fingerprint = plan.fingerprint
                    self._transition(session, "submit_pending")
                    submission, added = await self._submit_modern(
                        session, task, goal_result, plan, cancel_event
                    )
                    attempt_records.extend(added)
                    replans += 1
                if submission.status == "rejected":
                    self._transition(session, "rejected", "platform_rejected")
                    if session.config.rejected_action == "skip":
                        await self._skip_modern(
                            session,
                            task,
                            submission.message or "rejected",
                            attempts=attempt_records,
                        )
                        continue
                    self._append_terminal_history(
                        session,
                        task,
                        "rejected",
                        submission.message,
                        "platform_rejected",
                        attempt_records,
                    )
                    raise AutoManagerError("platform rejected the Goal result")
                if submission.status != "accepted":
                    reason_code = str(
                        submission.metadata.get("reason_code", "platform_submission_failed")
                    )
                    if reason_code not in TASK_REASON_CODES:
                        reason_code = "platform_submission_failed"
                    self._append_terminal_history(
                        session,
                        task,
                        "failed",
                        submission.message,
                        reason_code,
                        attempt_records,
                    )
                    raise AutoManagerError(submission.message or "platform submission failed")
                self._transition(session, "accepted")
                finished_at = _timestamp()
                session.history.append(AutoHistoryEntry(
                    external_id=task.external_id,
                    name=task.name,
                    project_id=session.current_project_id or "",
                    answer="",
                    status="accepted",
                    message=submission.message,
                    result_ref=goal_result.result_ref,
                    result_fact_id=goal_result.fact.id,
                    attempt_count=len(attempt_records),
                    plan_fingerprints=tuple(
                        dict.fromkeys(
                            str(item["plan_fingerprint"])
                            for item in attempt_records
                        )
                    ),
                    attempts=tuple(attempt_records),
                    accepted_at=finished_at,
                    finished_at=finished_at,
                ))
                session.completed_tasks += 1
                session.processed_tasks += 1
                self._clear_current_task(session)
            if session.status not in {"finished", "failed", "stopped"}:
                self._set_session_terminal(session, "finished")
                session.last_message = "maximum task limit reached"
        except asyncio.CancelledError:
            self._set_session_terminal(session, "stopped", "user_stopped")
            session.last_message = "automatic session stopped"
        except (AutoManagerError, DirectControllerError) as exc:
            reason_code = self._reason_code_for_error(str(exc))
            self._set_session_terminal(session, "failed", reason_code)
            session.last_error = str(exc)
        except Exception as exc:
            self._set_session_terminal(session, "failed", "platform_submission_failed")
            session.last_error = f"automatic session failed: {type(exc).__name__}"
        finally:
            await self._dispose_modern_runtime()
            self._clear_runtime_platform_secret(session)
            shutil.rmtree(session.runtime_root, ignore_errors=True)

    async def _fetch_modern(self, session: AutoSession) -> PlatformResult:
        if isinstance(self._adapter, StructuredPlatformAdapter):
            return await self._adapter.fetch_task()
        if not isinstance(self._adapter, PromptPlatformAdapter) or self._controller is None:
            raise AutoManagerError("prompt platform adapter is unavailable")

        async def handler(name, arguments):
            if name == "platform_http_request":
                return await self._adapter.platform_http_request(arguments)
            if name == "download_attachment":
                return await self._adapter.download_attachment(arguments)
            if name == "extract_task_fields":
                return await self._adapter.extract_task_fields(arguments)
            raise ValueError("tool is unavailable in fetch phase")

        prompt = self._runtime_platform_prompt()
        return await self._controller.prompt_phase(
            "fetch",
            prompt + "\nFetch exactly one task. Return protocol_version 2 action JSON.",
            handler,
            cancel_event=self._cancel_event,
        )

    async def _retry_fetch(
        self,
        session: AutoSession,
        result: PlatformResult,
        cancel_event: asyncio.Event,
    ) -> PlatformResult:
        for attempt in range(session.config.max_transport_retries):
            self._transition(session, "retry_wait", "platform_transport")
            await self._sleep_or_cancel(result.retry_after_seconds or min(2 ** attempt, 30), cancel_event)
            self._transition(session, "queued")
            result = await self._fetch_modern(session)
            if result.status != "retryable_error":
                return result
        raise AutoManagerError("platform_retry_exhausted: platform fetch transport retries exhausted")

    async def _submit_modern(
        self,
        session,
        task,
        goal_result,
        plan,
        cancel_event,
    ) -> tuple[PlatformResult, list[dict[str, object]]]:
        attempts: list[dict[str, object]] = []
        result: PlatformResult | None = None
        while len(attempts) <= session.config.max_transport_retries:
            attempt_number = len(attempts) + 1
            session.current_retry_count = attempt_number - 1
            if isinstance(self._adapter, StructuredPlatformAdapter):
                result = await self._adapter.submit_result(task, plan, goal_result)
            else:
                result = await self._submit_prompt(session, task, goal_result, plan)
            attempts.append(
                {
                    "attempt": attempt_number,
                    "phase": "transport",
                    "status": result.status,
                    "external_id": task.external_id,
                    "project_id": session.current_project_id,
                    "goal_version": goal_result.goal_version,
                    "fact_id": goal_result.fact.id,
                    "plan_fingerprint": plan.fingerprint,
                    "result_ref": goal_result.result_ref,
                    "source_fact_ids": list(plan.source_fact_ids),
                    "content_type": plan.content_type,
                    "reason_code": (
                        "platform_transport"
                        if result.status == "retryable_error"
                        else "platform_rejected"
                        if result.status == "rejected"
                        else None
                    ),
                }
            )
            if result.status != "retryable_error":
                return result, attempts
            if len(attempts) > session.config.max_transport_retries:
                break
            self._transition(session, "retry_wait", "platform_transport")
            await self._sleep_or_cancel(
                result.retry_after_seconds or min(2 ** (attempt_number - 1), 30),
                cancel_event,
            )
            self._transition(session, "submit_pending")
        assert result is not None
        return (
            PlatformResult(
                "fatal_error",
                message="submission transport retries exhausted",
                metadata={"reason_code": "platform_retry_exhausted"},
            ),
            attempts,
        )

    async def _submit_prompt(self, session, task, goal_result, plan) -> PlatformResult:
        if not isinstance(self._adapter, PromptPlatformAdapter) or self._controller is None:
            raise AutoManagerError("prompt platform adapter is unavailable")

        async def handler(name, arguments):
            if name == "get_solver_result":
                if str(arguments.get("project_id", "")) != str(session.current_project_id or ""):
                    raise ValueError("project_id is not the current solver project")
                return {
                    "status": "ready",
                    "result_ref": goal_result.result_ref,
                    "goal_result_fact": {"id": goal_result.fact.id, "content": goal_result.fact.content},
                    "supporting_facts": [
                        {"id": fact.id, "content": fact.content}
                        for fact in goal_result.supporting_facts
                    ],
                    "supporting_providers": list(goal_result.supporting_providers),
                }
            if name == "list_solver_facts":
                if str(arguments.get("project_id", "")) != str(session.current_project_id or ""):
                    raise ValueError("project_id is not the current solver project")
                limit = max(1, min(int(arguments.get("limit", 20)), 50))
                relation = arguments.get("relation")
                if relation not in {None, "", "supports_goal", "goal_result"}:
                    raise ValueError("unsupported fact relation")
                cursor_value = arguments.get("cursor")
                try:
                    start = max(0, int(cursor_value or 0))
                except (TypeError, ValueError) as exc:
                    raise ValueError("invalid fact cursor") from exc
                facts = [
                    {"id": goal_result.fact.id, "content": goal_result.fact.content, "relation": "goal_result"},
                    *[
                        {"id": fact.id, "content": fact.content, "relation": "supports_goal"}
                        for fact in goal_result.supporting_facts
                    ],
                ]
                if relation:
                    facts = [fact for fact in facts if fact["relation"] == relation]
                page = facts[start : start + limit]
                next_cursor = str(start + limit) if start + limit < len(facts) else None
                return {"status": "ready", "facts": page, "next_cursor": next_cursor}
            if name == "preview_submission":
                request = arguments.get("request")
                if not isinstance(request, dict):
                    raise ValueError("preview requires request")
                fingerprint = self._adapter.freeze_submission(request)
                return {"valid": True, "plan_fingerprint": fingerprint}
            if name == "submit_platform_result":
                return await self._adapter.replay_submission(str(arguments.get("frozen_plan_fingerprint", "")))
            raise ValueError("tool is unavailable in submit phase")

        prompt = self._runtime_platform_prompt()
        instruction = {
            "platform_contract": prompt,
            "task": {"external_id": task.external_id, "name": task.name},
            "goal_result": {"result_ref": goal_result.result_ref, "content": goal_result.fact.content},
            "submission_plan": plan.to_dict(),
            "instruction": "Preview and submit this frozen plan once, then return accepted, rejected, retryable_error, or fatal_error action JSON.",
        }
        return await self._controller.prompt_phase(
            "submit",
            json.dumps(instruction, ensure_ascii=False),
            handler,
            cancel_event=self._cancel_event,
        )

    async def _skip_modern(
        self,
        session: AutoSession,
        task: AutoTask,
        reason: str,
        *,
        attempts: list[dict[str, object]] | None = None,
    ) -> None:
        if isinstance(self._adapter, StructuredPlatformAdapter):
            result = await self._adapter.skip_task(task, reason)
        elif isinstance(self._adapter, PromptPlatformAdapter) and self._controller is not None:
            async def handler(name, arguments):
                if name != "skip_platform_task":
                    raise ValueError("tool is unavailable in skip phase")
                return await self._adapter.platform_http_request(arguments)
            prompt = self._runtime_platform_prompt()
            result = await self._controller.prompt_phase(
                "skip",
                prompt + f"\nRelease task {task.external_id}. Return skipped only after confirmation.",
                handler,
                cancel_event=self._cancel_event,
            )
        else:
            raise AutoManagerError("skip adapter is unavailable")
        if result.status != "skipped":
            if session.current_task_status in TASK_TRANSITIONS and session.current_task_status not in {
                None,
                "failed",
                "stopped",
            }:
                self._transition(session, "failed", "skip_failed")
            self._append_terminal_history(
                session,
                task,
                "failed",
                result.message or "platform skip failed",
                "skip_failed",
                attempts or [],
            )
            raise AutoManagerError(
                f"skip_failed: {result.message or 'platform skip failed'}"
            )
        if session.current_task_status in {"fetched", "solving", "rejected", "replanning"}:
            self._transition(
                session,
                "skipped",
                self._reason_code_for_error(reason, default="skipped"),
            )
        self._append_terminal_history(
            session,
            task,
            "skipped",
            reason,
            self._reason_code_for_error(reason, default="skipped"),
            attempts or [],
        )
        session.processed_tasks += 1
        self._clear_current_task(session)

    @staticmethod
    def _append_terminal_history(
        session: AutoSession,
        task: AutoTask,
        status: str,
        message: str | None,
        reason_code: str,
        attempts: list[dict[str, object]],
    ) -> None:
        session.history.append(
            AutoHistoryEntry(
                external_id=task.external_id,
                name=task.name,
                project_id=session.current_project_id or "",
                answer="",
                status=status,
                message=message,
                result_ref=session.current_result_ref,
                result_fact_id=session.current_result_fact_id,
                reason_code=reason_code,
                attempt_count=len(attempts),
                plan_fingerprints=tuple(
                    dict.fromkeys(
                        str(item["plan_fingerprint"])
                        for item in attempts
                    )
                ),
                attempts=tuple(attempts),
                finished_at=_timestamp(),
            )
        )

    @staticmethod
    def _append_stopped_history(session: AutoSession) -> None:
        if session.current_external_id is None:
            return
        if session.history and session.history[-1].external_id == session.current_external_id and session.history[-1].status == "stopped":
            return
        session.history.append(
            AutoHistoryEntry(
                external_id=session.current_external_id,
                name=session.current_task_name or session.current_external_id,
                project_id=session.current_project_id or "",
                answer="",
                status="stopped",
                message="automatic session stopped",
                reason_code="user_stopped",
                finished_at=_timestamp(),
            )
        )

    @staticmethod
    def _set_session_terminal(
        session: AutoSession,
        status: str,
        reason_code: str | None = None,
    ) -> None:
        if status not in {"finished", "failed", "stopped"}:
            raise AutoManagerError(f"unsupported terminal session status: {status}")
        current = session.current_task_status
        if current is not None and status in TASK_TRANSITIONS.get(current, set()):
            AutoManager._transition(session, status, reason_code)
            AutoManager._ensure_terminal_history(session, status, reason_code)
            return
        session.status = status
        if current is not None and status in {"failed", "stopped"}:
            session.current_task_status = status
            session.current_reason_code = reason_code
            AutoManager._ensure_terminal_history(session, status, reason_code)

    @staticmethod
    def _ensure_terminal_history(
        session: AutoSession,
        status: str,
        reason_code: str | None,
    ) -> None:
        if status not in {"failed", "stopped"} or session.current_external_id is None:
            return
        if any(
            entry.external_id == session.current_external_id and entry.status == status
            for entry in session.history
        ):
            return
        session.history.append(
            AutoHistoryEntry(
                external_id=session.current_external_id,
                name=session.current_task_name or session.current_external_id,
                project_id=session.current_project_id or "",
                answer="",
                status=status,
                message=session.last_error,
                reason_code=reason_code or "platform_submission_failed",
                finished_at=_timestamp(),
            )
        )

    @staticmethod
    def _reason_code_for_error(
        message: str,
        *,
        default: str = "platform_submission_failed",
    ) -> str:
        for reason_code in TASK_REASON_CODES:
            if reason_code in message:
                return reason_code
        return default

    @staticmethod
    def _transition(session: AutoSession, status: str, reason_code: str | None = None) -> None:
        allowed = TASK_TRANSITIONS.get(session.current_task_status, set())
        if status not in allowed:
            raise AutoManagerError(f"invalid automatic task transition: {session.current_task_status} -> {status}")
        if reason_code is not None and reason_code not in TASK_REASON_CODES:
            raise AutoManagerError(f"unknown automatic task reason code: {reason_code}")
        session.current_task_status = status
        session.current_reason_code = reason_code
        session.status = status

    @staticmethod
    def _clear_current_task(session: AutoSession) -> None:
        session.current_external_id = None
        session.current_task_name = None
        session.current_project_id = None
        session.current_task_status = None
        session.current_reason_code = None
        session.current_result_ref = None
        session.current_result_fact_id = None
        session.current_result_preview = None
        session.current_attempt_phase = None
        session.current_retry_count = 0
        session.current_replan_count = 0
        session.current_plan_fingerprint = None

    @staticmethod
    def _clear_runtime_platform_secret(session: AutoSession) -> None:
        AutoManager._clear_config_platform_secret(session.config)

    @staticmethod
    def _clear_config_platform_secret(config: AutoStartConfig) -> None:
        platform = config.platform
        if isinstance(platform, PromptPlatformConfig):
            object.__setattr__(platform, "prompt", "")

    def _runtime_platform_prompt(self) -> str:
        return self._platform_secret.prompt if self._platform_secret is not None else ""

    async def _dispose_modern_runtime(self) -> None:
        controller = self._controller
        adapter = self._adapter
        secret = self._controller_secret
        platform_secret = self._platform_secret
        self._controller = None
        self._adapter = None
        self._controller_secret = None
        self._platform_secret = None
        close_calls = []
        if controller is not None:
            close_calls.append(controller.close())
        if adapter is not None:
            close_calls.append(adapter.close())
        if close_calls:
            await asyncio.gather(*close_calls, return_exceptions=True)
        if secret is not None:
            secret.clear()
        if platform_secret is not None:
            platform_secret.clear()

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
                    raise AutoManagerError(f"solver_degraded: solver project ended with {status}")
                if asyncio.get_running_loop().time() >= deadline:
                    await self.service.stop_project(project.id)
                    raise AutoManagerError("solver_timeout: solver project timed out")
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
