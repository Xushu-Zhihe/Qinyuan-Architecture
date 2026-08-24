"""In-memory lifecycle manager for the full-automatic challenge mode."""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import shutil
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
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
from autopilot.solver_result import SubmissionPlan
from orchestrator.models import GoalResult
from orchestrator.projects import ProjectService
from worker.models import AuditConfig, ProviderConfig
from worker.capabilities import probe_provider_capabilities, static_capability_snapshot


class AutoManagerError(RuntimeError):
    """A requested automation operation is invalid or cannot continue."""


class _SkipTaskRequested(Exception):
    """Internal signal used to unwind the solver for a user-requested skip."""


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
    "submit_pending": {"accepted", "rejected", "awaiting_manual_review", "retry_wait", "failed", "stopped"},
    "rejected": {"replanning", "agent_correcting", "awaiting_manual_review", "skipped", "failed", "stopped"},
    "agent_correcting": {"solver_achieved", "skipped", "failed", "stopped"},
    "replanning": {"submit_pending", "skipped", "failed", "stopped"},
    "retry_wait": {"queued", "submit_pending", "failed", "stopped"},
    "waiting": {"queued", "failed", "stopped"},
    "awaiting_manual_review": {"submit_pending", "skipped", "failed", "stopped"},
}

TASK_REASON_CODES = frozenset(
    {
        "platform_wait",
        "platform_transport",
        "platform_rejected",
        "duplicate_rejected_flag",
        "platform_retry_exhausted",
        "platform_submission_failed",
        "solver_timeout",
        "solver_degraded",
        "result_conflict",
        "task_info_skip_required",
        "skip_failed",
        "skipped",
        "user_stopped",
        "platform_empty_queue_exhausted",
        "platform_error_exhausted",
        "platform_duplicate_id_exhausted",
        "lease_release_failed",
        "service_restarted",
    }
)

MAX_CONSECUTIVE_EMPTY_FETCHES = 20
MAX_CONSECUTIVE_FETCH_ERRORS = 5
MAX_CONSECUTIVE_DUPLICATE_IDS = 3


def _scrub_text(value: str, platform_prompt: str, *secret_values: str) -> str:
    scrubbed = value
    if platform_prompt:
        scrubbed = scrubbed.replace(platform_prompt, "[redacted]")
    values = [match.group(1) for match in _CREDENTIAL_PATTERN.finditer(platform_prompt)]
    values.extend(match.group(1) for match in _BEARER_PATTERN.finditer(platform_prompt))
    values.extend(secret for secret in secret_values if secret)
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


def _tool_bootstrap_budget_seconds() -> int:
    raw = os.environ.get("QINYUAN_TOOL_BOOTSTRAP_BUDGET_SECONDS", "300")
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise ValueError(
            "QINYUAN_TOOL_BOOTSTRAP_BUDGET_SECONDS must be an integer"
        ) from exc
    if not 1 <= value <= 300:
        raise ValueError(
            "QINYUAN_TOOL_BOOTSTRAP_BUDGET_SECONDS must be between 1 and 300"
        )
    return value


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
        self._cleanup_failures = 0
        self._finalized_project_ids: set[str] = set()
        self._task_deadline_monotonic: float | None = None
        self._tool_bootstrap_budget_seconds = _tool_bootstrap_budget_seconds()
        self._skip_requested: asyncio.Event | None = None
        self._skip_reason: str | None = None
        self._manual_submission_event: asyncio.Event | None = None
        self._manual_submission_goal_result: GoalResult | None = None
        self._manual_finish_after_accept = False
        self._controller_health: dict[str, object] = {
            "status": "unknown",
            "last_error": None,
            "checked_at": None,
            "model": None,
        }
        self._session_state_root = (self.service.registry.data_root / "autopilot-sessions").resolve()
        self._recovered_session_snapshot: dict[str, object] | None = self._recover_session_snapshot()
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
                    self._controller_health = {
                        "status": "healthy",
                        "last_error": None,
                        "checked_at": _timestamp(),
                        "model": config.controller.model,
                    }
                except asyncio.CancelledError:
                    await self._dispose_modern_runtime()
                    self._clear_config_platform_secret(config)
                    raise
                except Exception as exc:
                    self._controller_health = {
                        "status": "unavailable",
                        "last_error": f"controller preflight failed: {type(exc).__name__}",
                        "checked_at": _timestamp(),
                        "model": config.controller.model,
                    }
                    await self._dispose_modern_runtime()
                    self._clear_config_platform_secret(config)
                    raise AutoManagerError(str(exc)) from exc
            session_id = f"auto-{uuid4().hex}"
            self._finalized_project_ids.clear()
            runtime_root = (
                self.service.registry.data_root / "autopilot" / session_id
            ).resolve()
            try:
                runtime_root.mkdir(parents=True, exist_ok=False)
                session = AutoSession(session_id, config, runtime_root)
                self._session = session
                self._recovered_session_snapshot = None
                self._persist_session(session)
                if self._controller is not None and hasattr(self._controller, "set_runtime_root"):
                    self._controller.set_runtime_root(runtime_root)
                if self._adapter is not None and hasattr(
                    self._adapter, "set_runtime_root"
                ):
                    self._adapter.set_runtime_root(runtime_root)
                self._cancel_event = asyncio.Event()
                self._skip_requested = asyncio.Event()
                self._skip_reason = None
                self._manual_submission_event = asyncio.Event()
                self._manual_submission_goal_result = None
                self._manual_finish_after_accept = False
                self._runner = asyncio.create_task(
                    self._run(session, self._cancel_event)
                )
                return self._snapshot_locked()
            except Exception:
                if not config.is_legacy:
                    await self._dispose_modern_runtime()
                self._remove_runtime_root(runtime_root)
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
                if session.current_task is not None and session.current_external_id is None:
                    session.current_external_id = session.current_task.external_id
                    session.current_task_name = session.current_task.name
                if session.current_external_id is not None:
                    if session.current_task_status in TASK_TRANSITIONS and session.current_task_status != "stopped":
                        try:
                            self._transition(session, "stopped", "user_stopped")
                        except AutoManagerError:
                            session.current_task_status = "stopped"
                            session.current_reason_code = "user_stopped"
                    self._append_stopped_history(session)
            release_task = session.current_task
            release_requested = (
                release_task is not None
                and session.config.stop_active_task_action == "skip"
                and not session.stop_release_attempted
            )
            session.stop_release_attempted = True
        if release_requested:
            await self._release_on_stop(session, release_task)
        if cancel_event is not None:
            cancel_event.set()

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
                self._persist_session(self._session)
            return self._snapshot_locked()

    async def extend_time(self, seconds: float) -> dict[str, object]:
        """Extend the deadline of the currently running solver project."""
        try:
            additional = float(seconds)
        except (TypeError, ValueError) as exc:
            raise AutoManagerError("extension seconds must be a number") from exc
        if not math.isfinite(additional) or additional <= 0 or additional > 24 * 60 * 60:
            raise AutoManagerError("extension seconds must be greater than 0 and at most 86400")
        async with self._lock:
            session = self._session
            if session is None or not self._is_active():
                raise AutoManagerError("no automatic session is running")
            if session.current_task is None or session.current_project_id is None:
                raise AutoManagerError("there is no running task to extend")
            if self._task_deadline_monotonic is None:
                raise AutoManagerError("the solver deadline is not ready")
            self._task_deadline_monotonic += additional
            deadline = datetime.now(timezone.utc).timestamp() + max(
                0.0, self._task_deadline_monotonic - asyncio.get_running_loop().time()
            )
            session.current_task_deadline_at = datetime.fromtimestamp(
                deadline, timezone.utc
            ).isoformat()
            session.last_message = f"task time extended by {int(additional)} seconds"
            self._persist_session(session)
            return self._snapshot_locked()

    async def skip_current_task(self, reason: str = "user skipped") -> dict[str, object]:
        """Request a platform skip for the active task and continue automation."""
        async with self._lock:
            session = self._session
            if session is None or not self._is_active():
                raise AutoManagerError("no automatic session is running")
            if session.current_task is None or session.current_external_id is None:
                raise AutoManagerError("there is no active task to skip")
            if session.current_task_status in {"submit_pending", "accepted", "skipped", "stopped"}:
                raise AutoManagerError("the current task can no longer be skipped")
            adapter_config = getattr(self._adapter, "config", None)
            if (
                isinstance(self._adapter, StructuredPlatformAdapter)
                and adapter_config is not None
                and adapter_config.skip_task is None
            ):
                raise AutoManagerError("structured platform skip endpoint is not configured")
            if self._skip_requested is None:
                raise AutoManagerError("skip control is unavailable")
            self._skip_reason = reason.strip() or "user skipped"
            self._skip_requested.set()
            session.skip_requested = True
            session.last_message = "skip requested; releasing the current task"
            self._persist_session(session)
            return self._snapshot_locked()

    async def finish_after_current_task(self) -> dict[str, object]:
        """Keep the current task running, then finish after its terminal result."""
        async with self._lock:
            session = self._session
            if session is None or not self._is_active():
                raise AutoManagerError("no automatic session is running")
            if session.current_external_id is None:
                raise AutoManagerError("there is no active task to finish")
            if session.current_task_status in {"accepted", "skipped", "stopped"}:
                raise AutoManagerError("the current task can no longer be finished")
            session.finish_after_current_task_requested = True
            session.last_message = (
                "current task will be submitted before automatic session finishes"
            )
            self._persist_session(session)
            return self._snapshot_locked()

    async def submit_current_task(
        self,
        fact_id: str,
        *,
        finish_after_accept: bool = False,
    ) -> dict[str, object]:
        """Queue one explicitly selected active Fact for the paused task."""
        async with self._lock:
            session = self._session
            if session is None or not self._is_active():
                raise AutoManagerError("no automatic session is running")
            if session.current_task_status != "awaiting_manual_review":
                raise AutoManagerError("the current task is not awaiting manual review")
            if session.current_project_id is None or session.current_task is None:
                raise AutoManagerError("there is no paused task to submit")
            if self._manual_submission_event is None:
                raise AutoManagerError("manual submission control is unavailable")
            try:
                goal_result = self.service.get_submission_result(
                    session.current_project_id,
                    fact_id,
                )
            except (KeyError, ValueError) as exc:
                raise AutoManagerError(str(exc)) from exc
            self._manual_submission_goal_result = goal_result
            self._manual_finish_after_accept = bool(finish_after_accept)
            session.current_result_ref = goal_result.result_ref
            session.current_result_fact_id = goal_result.fact.id
            session.current_result_preview = self._scrub_runtime_text(
                session,
                goal_result.fact.content[:500],
            )
            session.current_attempt_phase = "submit"
            self._transition(session, "submit_pending")
            session.last_message = "selected Fact queued for submission"
            self._persist_session(session)
            self._manual_submission_event.set()
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
        result = {
            "session": (
                self._session.snapshot(providers) if self._session is not None else None
            ),
            "providers": list(providers),
            "provider_capabilities": {
                name: self._provider_capabilities[name]
                for name in self.providers
            },
            "controller_health": dict(self._controller_health),
        }
        if result["session"] is None:
            result["session"] = self._recovered_session_snapshot
        return result

    def _persist_session(self, session: AutoSession) -> None:
        try:
            self._session_state_root.mkdir(parents=True, exist_ok=True)
            payload = {"updated_at": _timestamp(), "session": session.snapshot(tuple(self.providers))}
            target = self._session_state_root / f"{session.id}.json"
            temporary = target.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            temporary.replace(target)
        except (OSError, TypeError, ValueError):
            # Session persistence must not change the automation outcome.
            return

    def _tool_bootstrap_requested(self, project_id: str) -> bool:
        """Return whether a trusted download MCP marker exists for this task."""
        try:
            handle = self.service.get_handle(project_id)
            project_path = getattr(getattr(handle, "record", None), "path", None)
            if project_path is None:
                return False
            workspace_root = Path(project_path) / "agent-workspaces"
        except (KeyError, OSError, TypeError, ValueError):
            return False

        try:
            if not workspace_root.is_dir():
                return False
            markers = workspace_root.glob("*/.qinyuan-tool-bootstrap.json")
            for marker in markers:
                if not marker.is_file():
                    continue
                try:
                    payload = json.loads(marker.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, json.JSONDecodeError):
                    continue
                if not isinstance(payload, dict):
                    continue
                if (
                    payload.get("version") == 1
                    and payload.get("qualified_tool") == "download.install_tool"
                    and payload.get("compensate_seconds") == 300
                ):
                    return True
        except OSError:
            return False
        return False

    def _recover_session_snapshot(self) -> dict[str, object] | None:
        try:
            self._session_state_root.mkdir(parents=True, exist_ok=True)
            candidates = sorted(self._session_state_root.glob("*.json"), key=lambda path: path.stat().st_mtime)
        except OSError:
            return None
        recovered: dict[str, object] | None = None
        for path in candidates:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                snapshot = payload.get("session") if isinstance(payload, dict) else None
                if not isinstance(snapshot, dict):
                    continue
                status = str(snapshot.get("status", ""))
                if status not in {"finished", "failed", "stopped"}:
                    snapshot = dict(snapshot)
                    snapshot["status"] = "stopped"
                    snapshot["reason_code"] = "service_restarted"
                    snapshot["last_message"] = "automatic session stopped after service restart"
                    if snapshot.get("current_task_status") not in {None, "finished", "failed", "stopped"}:
                        snapshot["current_task_status"] = "stopped"
                    path.write_text(
                        json.dumps({"updated_at": _timestamp(), "session": snapshot}, ensure_ascii=False),
                        encoding="utf-8",
                    )
                recovered = snapshot
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                continue
        return recovered

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
                finish_after_accept = session.finish_after_current_task_requested
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
                session.finish_after_current_task_requested = False
                session.last_message = "answer accepted"
                if finish_after_accept:
                    session.status = "finished"
                    session.last_message = "answer accepted; automatic session finished by user"
                    break
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
            self._persist_session(session)

    async def _run_modern(self, session: AutoSession, cancel_event: asyncio.Event) -> None:
        seen_ids: set[str] = set()
        try:
            while session.processed_tasks < session.config.max_tasks:
                if cancel_event.is_set():
                    raise asyncio.CancelledError
                self._transition(session, "queued")
                fetched = await self._fetch_modern(session)
                if fetched.status == "retryable_error":
                    session.consecutive_fetch_errors += 1
                    if session.consecutive_fetch_errors >= MAX_CONSECUTIVE_FETCH_ERRORS:
                        raise AutoManagerError(
                            "platform_error_exhausted: consecutive platform fetch errors exhausted"
                        )
                    fetched = await self._retry_fetch(session, fetched, cancel_event)
                if fetched.status == "wait":
                    session.consecutive_empty_fetches += 1
                    session.consecutive_fetch_errors = 0
                    if session.consecutive_empty_fetches >= MAX_CONSECUTIVE_EMPTY_FETCHES:
                        raise AutoManagerError(
                            "platform_empty_queue_exhausted: consecutive empty queue limit exhausted"
                        )
                    self._transition(session, "waiting", "platform_wait")
                    wait_seconds = max(
                        0.1,
                        min(
                            fetched.retry_after_seconds or session.config.poll_interval_seconds,
                            300.0,
                        ),
                    )
                    await self._sleep_or_cancel(wait_seconds, cancel_event)
                    continue
                if fetched.status == "finished":
                    self._transition(session, "finished")
                    session.last_message = self._scrub_runtime_text(
                        session, fetched.message or "platform queue finished"
                    )
                    break
                if fetched.status == "skip_required" and isinstance(fetched.task, AutoTask):
                    self._transition(session, "fetched", "task_info_skip_required")
                    await self._skip_modern(
                        session,
                        fetched.task,
                        self._scrub_runtime_text(
                            session, fetched.message or "task info failed"
                        )
                        or "task info failed",
                    )
                    continue
                if fetched.status != "task" or not isinstance(fetched.task, AutoTask):
                    raise AutoManagerError(
                        self._scrub_runtime_text(
                            session, fetched.message or "platform fetch failed"
                        )
                    )
                task = self._scrub_task(session, fetched.task)
                session.consecutive_empty_fetches = 0
                session.consecutive_fetch_errors = 0
                if task.external_id in seen_ids:
                    session.consecutive_duplicate_ids += 1
                    if session.consecutive_duplicate_ids >= MAX_CONSECUTIVE_DUPLICATE_IDS:
                        raise AutoManagerError(
                            "platform_duplicate_id_exhausted: consecutive duplicate task IDs exhausted"
                        )
                    self._transition(session, "waiting", "platform_wait")
                    await self._sleep_or_cancel(
                        min(2 ** session.consecutive_duplicate_ids, 30), cancel_event
                    )
                    continue
                session.consecutive_duplicate_ids = 0
                session.current_task = task
                session.skip_requested = False
                if self._skip_requested is not None:
                    self._skip_requested.clear()
                seen_ids.add(task.external_id)
                if self._adapter is not None and hasattr(
                    self._adapter, "promote_attachments"
                ):
                    attachment_paths = task.raw_metadata.get("attachment_paths", ())
                    promoted = self._adapter.promote_attachments(
                        task.external_id, attachment_paths
                    )
                    if promoted:
                        challenge_path = (
                            self._adapter.challenge_root(task.external_id)
                            if isinstance(self._adapter, StructuredPlatformAdapter)
                            and hasattr(self._adapter, "challenge_root")
                            else promoted[0]
                        )
                        task = replace(
                            task,
                            challenge_path=challenge_path,
                            raw_metadata={
                                **dict(task.raw_metadata),
                                "attachment_paths": tuple(
                                    str(path.relative_to(session.runtime_root))
                                    for path in promoted
                                ),
                            },
                        )
                        session.current_task = task
                self._transition(session, "fetched")
                self._transition(session, "solving")
                try:
                    await self._solve_task(
                        session,
                        task,
                        cancel_event,
                        retain_project=True,
                    )
                except _SkipTaskRequested:
                    await self._skip_modern(
                        session,
                        task,
                        self._skip_reason or "user skipped",
                    )
                    self._skip_reason = None
                    continue
                except AutoManagerError as exc:
                    if session.config.solver_failure_action == "skip":
                        await self._skip_modern(
                            session,
                            task,
                            self._scrub_runtime_text(session, str(exc)) or "solver failed",
                        )
                        continue
                    raise
                if self._skip_requested is not None and self._skip_requested.is_set():
                    await self._skip_modern(
                        session,
                        task,
                        self._skip_reason or "user skipped",
                    )
                    self._skip_reason = None
                    continue
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
                session.current_result_preview = self._scrub_runtime_text(
                    session, goal_result.fact.content[:500]
                )
                assert self._controller is not None
                plan = await self._controller.plan_submission(
                    task, goal_result, cancel_event=cancel_event
                )
                if self._skip_requested is not None and self._skip_requested.is_set():
                    await self._skip_modern(
                        session,
                        task,
                        self._skip_reason or "user skipped",
                    )
                    self._skip_reason = None
                    continue
                session.current_plan_fingerprint = plan.fingerprint
                session.current_attempt_phase = "submit"
                self._transition(session, "submit_pending")
                if self._skip_requested is not None and self._skip_requested.is_set():
                    await self._skip_modern(
                        session,
                        task,
                        self._skip_reason or "user skipped",
                    )
                    self._skip_reason = None
                    continue
                submission, attempt_records = await self._submit_modern(
                    session, task, goal_result, plan, cancel_event
                )
                correction_skipped = False
                while (
                    submission.status == "rejected"
                    and session.config.rejected_action == "agent_correct"
                    and session.current_correction_count
                    < session.config.max_agent_corrections
                ):
                    self._transition(session, "rejected", "platform_rejected")
                    rejection = (
                        self._scrub_runtime_text(session, submission.message)
                        or "platform rejected the submitted flag"
                    )
                    try:
                        goal_result = await self._return_rejection_to_solver(
                            session,
                            task,
                            plan,
                            rejection,
                            cancel_event,
                        )
                    except _SkipTaskRequested:
                        await self._skip_modern(
                            session,
                            task,
                            self._skip_reason or "user skipped",
                            attempts=attempt_records,
                        )
                        self._skip_reason = None
                        correction_skipped = True
                        break
                    except AutoManagerError as exc:
                        if session.config.solver_failure_action != "skip":
                            raise
                        await self._skip_modern(
                            session,
                            task,
                            self._scrub_runtime_text(session, str(exc))
                            or "solver failed",
                            attempts=attempt_records,
                        )
                        correction_skipped = True
                        break
                    plan = await self._controller.plan_submission(
                        task,
                        goal_result,
                        rejection=rejection,
                        cancel_event=cancel_event,
                    )
                    session.current_plan_fingerprint = plan.fingerprint
                    self._transition(session, "submit_pending")
                    submission, added = await self._submit_modern(
                        session,
                        task,
                        goal_result,
                        plan,
                        cancel_event,
                    )
                    offset = len(attempt_records)
                    for index, record in enumerate(added, start=1):
                        record["attempt"] = offset + index
                    attempt_records.extend(added)
                if correction_skipped:
                    continue
                replans = 0
                while submission.status == "rejected" and session.config.rejected_action == "replan" and replans < session.config.max_replans_after_rejection:
                    self._transition(session, "rejected", "platform_rejected")
                    self._transition(session, "replanning")
                    previous_fingerprint = plan.fingerprint
                    plan = await self._controller.plan_submission(
                        task,
                        goal_result,
                        rejection=(
                            self._scrub_runtime_text(session, submission.message)
                            or "rejected"
                        ),
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
                finish_after_accept = session.finish_after_current_task_requested
                if submission.status == "rejected":
                    self._transition(session, "rejected", "platform_rejected")
                    if session.config.rejected_action in {"pause", "agent_correct"}:
                        self._pause_for_manual_review(session, submission)
                        manual = await self._manual_review_loop(
                            session,
                            task,
                            attempt_records,
                            cancel_event,
                        )
                        if manual is None:
                            continue
                        submission, goal_result, attempt_records, finish_after_accept = manual
                    if session.config.rejected_action == "skip":
                        await self._skip_modern(
                            session,
                            task,
                            self._scrub_runtime_text(session, submission.message)
                            or "rejected",
                            attempts=attempt_records,
                        )
                        continue
                    if session.config.rejected_action in {"fail", "replan"}:
                        self._append_terminal_history(
                            session,
                            task,
                            "rejected",
                            self._scrub_runtime_text(session, submission.message),
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
                    if session.config.rejected_action in {"pause", "agent_correct"}:
                        self._pause_for_manual_review(session, submission, reason_code)
                        manual = await self._manual_review_loop(
                            session,
                            task,
                            attempt_records,
                            cancel_event,
                        )
                        if manual is None:
                            continue
                        submission, goal_result, attempt_records, finish_after_accept = manual
                    else:
                        self._append_terminal_history(
                            session,
                            task,
                            "failed",
                            self._scrub_runtime_text(session, submission.message),
                            reason_code,
                            attempt_records,
                        )
                        raise AutoManagerError(
                            self._scrub_runtime_text(
                                session, submission.message or "platform submission failed"
                            )
                        )
                self._transition(session, "accepted")
                await self._finalize_task_resources(session, task)
                finished_at = _timestamp()
                session.history.append(AutoHistoryEntry(
                    external_id=task.external_id,
                    name=task.name,
                    project_id=session.current_project_id or "",
                    answer="",
                    status="accepted",
                    message=self._scrub_runtime_text(session, submission.message),
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
                session.last_error = None
                if finish_after_accept:
                    self._set_session_terminal(session, "finished")
                    session.last_message = "answer accepted; automatic session finished by user"
                    break
                session.last_message = "answer accepted"
            if session.status not in {"finished", "failed", "stopped"}:
                self._set_session_terminal(session, "finished")
                session.last_message = "maximum task limit reached"
        except asyncio.CancelledError:
            self._set_session_terminal(session, "stopped", "user_stopped")
            session.last_message = "automatic session stopped"
        except (AutoManagerError, DirectControllerError) as exc:
            reason_code = self._reason_code_for_error(str(exc))
            self._set_session_terminal(session, "failed", reason_code)
            session.last_error = self._scrub_runtime_text(session, str(exc))
        except Exception as exc:
            self._set_session_terminal(session, "failed", "platform_submission_failed")
            session.last_error = f"automatic session failed: {type(exc).__name__}"
        finally:
            if session.current_task is not None:
                await self._finalize_task_resources(
                    session,
                    session.current_task,
                )
            await self._dispose_modern_runtime()
            self._clear_runtime_platform_secret(session)
            self._persist_session(session)
            self._remove_runtime_root(session.runtime_root)

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

    def _pause_for_manual_review(
        self,
        session: AutoSession,
        submission: PlatformResult,
        reason_code: str | None = None,
    ) -> None:
        reason_code = reason_code or (
            "platform_rejected"
            if submission.status == "rejected"
            else "platform_submission_failed"
        )
        if reason_code not in TASK_REASON_CODES:
            reason_code = "platform_submission_failed"
        if submission.status == "rejected" and session.current_task_status == "submit_pending":
            self._transition(session, "rejected", "platform_rejected")
        self._transition(session, "awaiting_manual_review", reason_code)
        self._task_deadline_monotonic = None
        session.current_task_deadline_at = None
        session.current_attempt_phase = None
        message = self._scrub_runtime_text(session, submission.message)
        session.last_message = message or "submission not accepted; select a Fact to retry"
        self._persist_session(session)

    async def _manual_review_loop(
        self,
        session: AutoSession,
        task: AutoTask,
        attempt_records: list[dict[str, object]],
        cancel_event: asyncio.Event,
    ) -> tuple[PlatformResult, GoalResult, list[dict[str, object]], bool] | None:
        while True:
            if self._skip_requested is not None and self._skip_requested.is_set():
                await self._skip_modern(
                    session,
                    task,
                    self._skip_reason or "user skipped",
                    attempts=attempt_records,
                )
                return None
            event = self._manual_submission_event
            if event is None:
                raise AutoManagerError("manual submission control is unavailable")
            if not event.is_set():
                await self._sleep_or_cancel(0.1, cancel_event)
                continue
            async with self._lock:
                event.clear()
                goal_result = self._manual_submission_goal_result
                finish_after_accept = self._manual_finish_after_accept
                self._manual_submission_goal_result = None
                self._manual_finish_after_accept = False
            if goal_result is None:
                continue
            assert self._controller is not None
            try:
                plan = await self._controller.plan_submission(
                    task,
                    goal_result,
                    cancel_event=cancel_event,
                )
            except (DirectControllerError, ValueError) as exc:
                self._pause_for_manual_review(
                    session,
                    PlatformResult("fatal_error", message=str(exc)),
                )
                continue
            session.current_plan_fingerprint = plan.fingerprint
            session.current_attempt_phase = "submit"
            submission, added = await self._submit_modern(
                session,
                task,
                goal_result,
                plan,
                cancel_event,
            )
            offset = len(attempt_records)
            for index, record in enumerate(added, start=1):
                record["attempt"] = offset + index
            attempt_records.extend(added)
            if submission.status == "accepted":
                return (
                    submission,
                    goal_result,
                    attempt_records,
                    bool(finish_after_accept or session.finish_after_current_task_requested),
                )
            reason_code = str(
                submission.metadata.get("reason_code", "platform_submission_failed")
            )
            self._pause_for_manual_review(session, submission, reason_code)

    async def _submit_modern(
        self,
        session,
        task,
        goal_result,
        plan,
        cancel_event,
    ) -> tuple[PlatformResult, list[dict[str, object]]]:
        attempts: list[dict[str, object]] = []
        result_value = getattr(goal_result, "result_value", None)
        if (
            isinstance(result_value, str)
            and result_value
            and result_value in getattr(session, "current_rejected_result_values", ())
        ):
            return (
                PlatformResult(
                    "rejected",
                    message="the same deterministic flag value was already rejected",
                    metadata={"reason_code": "duplicate_rejected_flag"},
                ),
                [
                    {
                        "attempt": 1,
                        "phase": "deduplication",
                        "status": "rejected",
                        "external_id": task.external_id,
                        "project_id": session.current_project_id,
                        "goal_version": goal_result.goal_version,
                        "fact_id": goal_result.fact.id,
                        "result_value": result_value,
                        "plan_fingerprint": plan.fingerprint,
                        "result_ref": goal_result.result_ref,
                        "source_fact_ids": list(plan.source_fact_ids),
                        "content_type": plan.content_type,
                        "reason_code": "duplicate_rejected_flag",
                    }
                ],
            )
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
                    "result_value": result_value,
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
                if result.status == "rejected" and isinstance(result_value, str) and result_value:
                    session.current_rejected_result_value = result_value
                    rejected_values = getattr(session, "current_rejected_result_values", None)
                    if rejected_values is not None and result_value not in rejected_values:
                        rejected_values.append(result_value)
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
        reason = self._scrub_runtime_text(session, reason) or "task skipped"
        result = await self._platform_skip_result(task, reason)
        result_message = self._scrub_runtime_text(session, result.message)
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
                result_message or "platform skip failed",
                "skip_failed",
                attempts or [],
            )
            raise AutoManagerError(
                f"skip_failed: {result_message or 'platform skip failed'}"
            )
        if session.current_task_status in {
            "fetched",
            "solving",
            "rejected",
            "replanning",
            "agent_correcting",
            "awaiting_manual_review",
        }:
            self._transition(
                session,
                "skipped",
                self._reason_code_for_error(reason, default="skipped"),
            )
        await self._finalize_task_resources(session, task)
        self._append_terminal_history(
            session,
            task,
            "skipped",
            reason,
            self._reason_code_for_error(reason, default="skipped"),
            attempts or [],
        )
        session.last_message = "task skipped"
        session.processed_tasks += 1
        session.skip_requested = False
        if self._skip_requested is not None:
            self._skip_requested.clear()
        self._skip_reason = None
        self._clear_current_task(session)

    async def _platform_skip_result(self, task: AutoTask, reason: str) -> PlatformResult:
        if isinstance(self._adapter, StructuredPlatformAdapter):
            return await self._adapter.skip_task(task, reason)
        if isinstance(self._adapter, PromptPlatformAdapter) and self._controller is not None:
            async def handler(name, arguments):
                if name != "skip_platform_task":
                    raise ValueError("tool is unavailable in skip phase")
                return await self._adapter.platform_http_request(arguments)
            prompt = self._runtime_platform_prompt()
            return await self._controller.prompt_phase(
                "skip",
                prompt + f"\nRelease task {task.external_id}. Return skipped only after confirmation.",
                handler,
                cancel_event=self._cancel_event,
            )
        raise AutoManagerError("skip adapter is unavailable")

    async def _release_on_stop(self, session: AutoSession, task: AutoTask) -> None:
        try:
            result = await self._platform_skip_result(task, "user stopped")
        except Exception as exc:
            result = PlatformResult("skip_failed", message=str(exc))
        if result.status == "skipped":
            return
        entry = next(
            (item for item in reversed(session.history)
             if item.external_id == task.external_id and item.status == "stopped"),
            None,
        )
        if entry is not None:
            entry.reason_code = "lease_release_failed"
            entry.message = "lease release failed"
            entry.attempts = tuple(entry.attempts) + (
                {"lease_release_failed": True, "reason_code": "lease_release_failed"},
            )
        session.last_error = "lease_release_failed: platform lease release failed"

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
        session.current_task = None
        session.current_external_id = None
        session.current_task_name = None
        session.current_project_id = None
        session.skip_requested = False
        session.finish_after_current_task_requested = False
        session.current_task_started_at = None
        session.current_task_deadline_at = None
        session.current_task_status = None
        session.current_reason_code = None
        session.current_result_ref = None
        session.current_result_fact_id = None
        session.current_result_preview = None
        session.current_attempt_phase = None
        session.current_retry_count = 0
        session.current_replan_count = 0
        session.current_correction_count = 0
        session.current_plan_fingerprint = None
        session.current_rejected_result_value = None
        session.current_rejected_result_values.clear()

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

    def _scrub_runtime_text(self, session: AutoSession, value: str | None) -> str | None:
        if value is None:
            return None
        prompt = (
            self._runtime_platform_prompt()
            if not session.config.is_legacy
            else session.config.platform_prompt
        )
        secrets = []
        if self._controller_secret is not None:
            secrets.append(self._controller_secret.api_key)
        if self._platform_secret is not None:
            secrets.append(self._platform_secret.token)
        return _scrub_text(value, prompt, *secrets)

    def _scrub_task(self, session: AutoSession, task: AutoTask) -> AutoTask:
        return replace(
            task,
            name=self._scrub_runtime_text(session, task.name) or "",
            goal=self._scrub_runtime_text(session, task.goal) or "",
            origin=self._scrub_runtime_text(session, task.origin) or "",
            hints=tuple(
                self._scrub_runtime_text(session, hint) or ""
                for hint in task.hints
            ),
        )

    def _remove_runtime_root(self, runtime_root) -> None:
        try:
            shutil.rmtree(runtime_root)
        except OSError:
            self._cleanup_failures += 1

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
        *,
        retain_project: bool = False,
    ) -> None:
        session.current_external_id = task.external_id
        session.current_task_name = task.name
        project_id: str | None = None
        try:
            mapping = (
                session.config.platform.mapping
                if isinstance(session.config.platform, StructuredPlatformConfig)
                else None
            )
            project = self.service.create_project(
                name=f"Auto {session.completed_tasks + 1}: {task.name}",
                goal=task.goal,
                origin=task.origin,
                hints=task.hints,
                mode="run",
                provider_names=session.config.solver_providers,
                agent_limit=session.config.agent_limit,
                challenge_path=task.challenge_path,
                goal_format=mapping.goal_format if mapping is not None else "plain",
                flag_pattern=mapping.flag_pattern if mapping is not None else None,
                flag_pattern_id=mapping.flag_pattern_id if mapping is not None else None,
            )
            project_id = project.id
            session.current_project_id = project.id
            self._persist_session(session)
            await self.service.start_project(project.id)
            await self._wait_for_solver_project(
                session,
                project.id,
                cancel_event,
            )
        finally:
            try:
                if project_id is not None and not retain_project:
                    await self.service.stop_project(project_id)
            finally:
                self._task_deadline_monotonic = None
                session.current_task_started_at = None
                session.current_task_deadline_at = None
                cleanup = getattr(self._adapter, "cleanup_task_artifacts", None)
                if callable(cleanup) and not retain_project:
                    cleanup(task.external_id)

    async def _wait_for_solver_project(
        self,
        session: AutoSession,
        project_id: str,
        cancel_event: asyncio.Event,
    ) -> None:
        project = self.service.get_handle(project_id)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + session.config.task_timeout_seconds
        self._task_deadline_monotonic = deadline
        session.current_task_started_at = _timestamp()
        session.current_task_deadline_at = datetime.fromtimestamp(
            datetime.now(timezone.utc).timestamp()
            + session.config.task_timeout_seconds,
            timezone.utc,
        ).isoformat()
        self._persist_session(session)
        bootstrap_compensated = False
        try:
            while True:
                if cancel_event.is_set():
                    raise asyncio.CancelledError
                if self._skip_requested is not None and self._skip_requested.is_set():
                    raise _SkipTaskRequested
                if (
                    not bootstrap_compensated
                    and self._tool_bootstrap_requested(project_id)
                ):
                    self._task_deadline_monotonic = (
                        self._task_deadline_monotonic or deadline
                    ) + self._tool_bootstrap_budget_seconds
                    session.current_task_deadline_at = datetime.fromtimestamp(
                        datetime.now(timezone.utc).timestamp()
                        + max(
                            0.0,
                            self._task_deadline_monotonic - loop.time(),
                        ),
                        timezone.utc,
                    ).isoformat()
                    session.last_message = (
                        "tool bootstrap time compensated by "
                        f"{self._tool_bootstrap_budget_seconds} seconds"
                    )
                    self._persist_session(session)
                    bootstrap_compensated = True
                status = str(project.store.snapshot()["project"]["status"])
                if status == "achieved":
                    return
                if status in {"degraded", "stopped"}:
                    raise AutoManagerError(
                        f"solver_degraded: solver project ended with {status}"
                    )
                if loop.time() >= (self._task_deadline_monotonic or deadline):
                    raise AutoManagerError("solver_timeout: solver project timed out")
                await self._sleep_or_cancel(
                    session.config.poll_interval_seconds,
                    cancel_event,
                )
        finally:
            self._task_deadline_monotonic = None
            session.current_task_started_at = None
            session.current_task_deadline_at = None

    @staticmethod
    def _submitted_value_for_feedback(plan: SubmissionPlan) -> str:
        value: object
        if plan.content_type == "text":
            value = plan.text or ""
        elif plan.content_type == "json":
            value = plan.json_value
            if isinstance(value, dict):
                value = next(
                    (
                        value[key]
                        for key in ("flag", "answer", "value")
                        if isinstance(value.get(key), str) and value.get(key)
                    ),
                    value,
                )
        else:
            value = dict(plan.form_fields)
        rendered = (
            value
            if isinstance(value, str)
            else json.dumps(value, ensure_ascii=False, sort_keys=True)
        )
        return rendered[:1000]

    async def _return_rejection_to_solver(
        self,
        session: AutoSession,
        task: AutoTask,
        plan: SubmissionPlan,
        rejection: str,
        cancel_event: asyncio.Event,
    ) -> GoalResult:
        project_id = session.current_project_id
        if not project_id:
            raise AutoManagerError("solver project is missing for flag correction")
        self._transition(session, "agent_correcting", "platform_rejected")
        session.current_correction_count += 1
        submitted = self._submitted_value_for_feedback(plan)
        feedback = (
            "The platform explicitly rejected the previous flag submission.\n"
            f"Rejected submission: {submitted}\n"
            f"Platform feedback: {rejection[:1000]}\n"
            "Treat this as negative evidence. Re-check the exploit or extraction, "
            "produce a different verified flag, and do not resubmit the same value."
        )
        await self.service.reopen_project(project_id)
        project = self.service.get_handle(project_id)
        project.store.add_human_node("hint", feedback)
        session.current_result_ref = None
        session.current_result_fact_id = None
        session.current_result_preview = None
        session.current_attempt_phase = "solve"
        session.last_message = (
            f"rejected flag returned to solver "
            f"({session.current_correction_count}/{session.config.max_agent_corrections})"
        )
        self._persist_session(session)
        await self.service.start_project(project_id)
        await self._wait_for_solver_project(
            session,
            project_id,
            cancel_event,
        )
        goal_result = self.service.get_goal_result(project_id)
        if goal_result is None:
            raise AutoManagerError(
                "solver project corrected without a Goal result Fact"
            )
        self._transition(session, "solver_achieved")
        session.current_result_ref = goal_result.result_ref
        session.current_result_fact_id = goal_result.fact.id
        session.current_result_preview = self._scrub_runtime_text(
            session,
            goal_result.fact.content[:500],
        )
        return goal_result

    async def _finalize_task_resources(
        self,
        session: AutoSession,
        task: AutoTask,
    ) -> None:
        project_id = session.current_project_id
        if project_id and project_id in self._finalized_project_ids:
            return
        if project_id:
            try:
                await self.service.stop_project(project_id)
            except Exception:
                self._cleanup_failures += 1
            else:
                self._finalized_project_ids.add(project_id)
        cleanup = getattr(self._adapter, "cleanup_task_artifacts", None)
        if callable(cleanup):
            try:
                cleanup(task.external_id)
            except (OSError, ValueError):
                self._cleanup_failures += 1

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
