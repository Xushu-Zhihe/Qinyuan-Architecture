import asyncio
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

from orchestrator.audit import prune_audit_logs
from orchestrator.coordinator import (
    ExecutionCoordinator,
    PermitCancelled,
    PermitLease,
)
from orchestrator.events import ProjectEventHub
from orchestrator.fact_compactor import FactCompactor
from orchestrator.health import (
    TERMINAL_HEALTH_STATUSES,
    HealthProbe,
    ProbeResult,
    classify_probe_failure,
    classify_worker_failure,
    probe_provider,
    summarize_failure,
    worker_failure_detail,
)
from orchestrator.protocol import (
    last_result_line,
    parse_intent,
    parse_result,
    render_execution_request,
    render_intent_request,
    render_timeout_summary_request,
)
from orchestrator.store import BlackboardStore, ExecutionRecord, IntentRecord
from worker.agent_lifecycle import release_agent_lifecycle
from worker.executor import execute_provider
from worker.models import (
    AuditConfig,
    FactCompactionConfig,
    LogChunk,
    ProviderConfig,
    WorkerResult,
)
from worker.provider_adapters import get_provider_adapter


Executor = Callable[..., Awaitable[WorkerResult]]
ProjectStatusWriter = Callable[[str, str | None], None]
PermitCallback = Callable[[PermitLease], Awaitable[None] | None]

TIMEOUT_SUMMARY_SECONDS = 30.0
TIMEOUT_SUMMARY_MAX_CHARS = 12 * 1024


@dataclass(frozen=True, slots=True)
class IntentAttemptResult:
    status: str
    protocol_error: str | None = None
    rejected_result_line: str | None = None


@dataclass(frozen=True, slots=True)
class SupervisorPolicy:
    check_stale_plans: bool = True
    protocol_correction_attempts: int = 1
    max_executions_per_intent: int | None = None

    def __post_init__(self) -> None:
        if self.protocol_correction_attempts < 0:
            raise ValueError("protocol_correction_attempts must be non-negative")
        if (
            self.max_executions_per_intent is not None
            and self.max_executions_per_intent <= 0
        ):
            raise ValueError("max_executions_per_intent must be positive")


class _BufferedLogPublisher:
    def __init__(
        self,
        store: BlackboardStore,
        hub: ProjectEventHub,
        write_lock: asyncio.Lock,
        event_type: str,
        source: str,
        metadata: dict[str, object],
        merged_path: Path,
        *,
        flush_interval: float = 0.1,
        max_batch_bytes: int = 16 * 1024,
    ) -> None:
        self.store = store
        self.hub = hub
        self.write_lock = write_lock
        self.event_type = event_type
        self.source = source
        self.metadata = metadata
        self.flush_interval = flush_interval
        self.max_batch_bytes = max_batch_bytes
        self.offsets = {"stdout": 0, "stderr": 0}
        self.pending = {"stdout": "", "stderr": ""}
        self._lock = asyncio.Lock()
        self._publish_lock = asyncio.Lock()
        self._timer: asyncio.Task[None] | None = None
        self._merged_offset = 0
        self._merged_path = merged_path
        self._merged_path.parent.mkdir(parents=True, exist_ok=True)
        self._merged_path.write_text("", encoding="utf-8")

    def _take_locked(self, stream: str) -> tuple[str, int] | None:
        text = self.pending[stream]
        if not text:
            return None
        self.pending[stream] = ""
        offset = self.offsets[stream]
        self.offsets[stream] += len(text.encode("utf-8"))
        return text, offset

    async def _publish(self, stream: str, text: str, offset: int) -> None:
        byte_length = len(text.encode("utf-8"))
        merged_text = f"[{stream}]\n{text}"
        merged_bytes = merged_text.encode("utf-8")
        metadata = {
            **self.metadata,
            "stream": stream,
            "byte_offset": offset,
            "byte_length": byte_length,
            "combined_byte_offset": self._merged_offset,
            "combined_byte_length": len(merged_bytes),
        }
        try:
            with self._merged_path.open("ab") as handle:
                handle.write(merged_bytes)
                handle.flush()
        except OSError:
            pass
        self._merged_offset += len(merged_bytes)
        async with self.write_lock:
            event = self.store.append_event(
                self.event_type,
                self.source,
                metadata,
            )
        await self.hub.publish(
            {**event, "payload": {**metadata, "text": text}}
        )

    async def _flush_after_delay(self) -> None:
        try:
            await asyncio.sleep(self.flush_interval)
            await self.flush()
        except asyncio.CancelledError:
            return

    async def _drain(self, streams: tuple[str, ...]) -> None:
        async with self._publish_lock:
            batches: list[tuple[str, str, int]] = []
            async with self._lock:
                for stream in streams:
                    batch = self._take_locked(stream)
                    if batch is not None:
                        batches.append((stream, *batch))
            for stream, text, offset in batches:
                await self._publish(stream, text, offset)

    async def add(self, chunk: LogChunk) -> None:
        should_flush = False
        async with self._lock:
            self.pending[chunk.stream] += chunk.text
            if (
                len(self.pending[chunk.stream].encode("utf-8"))
                >= self.max_batch_bytes
            ):
                should_flush = True
            if self._timer is None or self._timer.done():
                self._timer = asyncio.create_task(self._flush_after_delay())
        if should_flush:
            await self._drain((chunk.stream,))

    async def flush(self) -> None:
        async with self._lock:
            timer = self._timer
            self._timer = None
        if timer is not None and timer is not asyncio.current_task():
            timer.cancel()
        await self._drain(("stdout", "stderr"))

    async def close(self) -> None:
        await self.flush()


class ProjectSupervisor:
    def __init__(
        self,
        store: BlackboardStore,
        providers: tuple[ProviderConfig, ...],
        challenge_dir: Path | None,
        project_dir: Path,
        hub: ProjectEventHub | None = None,
        execute: Executor = execute_provider,
        coordinator: ExecutionCoordinator | None = None,
        global_semaphore: asyncio.Semaphore | None = None,
        max_iterations: int | None = None,
        failure_threshold: int = 3,
        idle_poll_interval: float = 1.0,
        project_status_writer: ProjectStatusWriter | None = None,
        health_probe: HealthProbe = probe_provider,
        audit_config: AuditConfig | None = None,
        policy: SupervisorPolicy | None = None,
        answer_quorum_provider_count: int | None = None,
        fact_compaction_config: FactCompactionConfig | None = None,
    ) -> None:
        self.store = store
        self.providers = {
            provider.name: provider
            for provider in providers
            if provider.enabled
        }
        self.challenge_dir = challenge_dir
        self.project_dir = project_dir
        self.hub = hub or ProjectEventHub()
        self.execute = execute
        self.coordinator = coordinator
        self.global_semaphore = global_semaphore
        if self.coordinator is None and self.global_semaphore is None:
            self.coordinator = ExecutionCoordinator()
        self.max_iterations = max_iterations
        self.failure_threshold = failure_threshold
        self.idle_poll_interval = idle_poll_interval
        self.project_status_writer = project_status_writer
        self.health_probe = health_probe
        self.audit_config = audit_config or AuditConfig()
        self.policy = policy or SupervisorPolicy()
        self.answer_quorum_provider_count = (
            answer_quorum_provider_count
            if answer_quorum_provider_count is not None
            else len(self.providers)
        )
        if self.answer_quorum_provider_count <= 0:
            raise ValueError("answer_quorum_provider_count must be positive")
        self._execute_accepts_log_limit = self._accepts_keyword(
            execute,
            "max_log_bytes",
        )
        self._execute_accepts_origin_host = self._accepts_keyword(
            execute,
            "origin_host",
        )
        self._execute_accepts_agent_owner_id = self._accepts_keyword(
            execute,
            "agent_owner_id",
        )
        self._execute_accepts_agent_work_dir = self._accepts_keyword(
            execute,
            "agent_work_dir",
        )
        self.cancel_event = asyncio.Event()
        self.wake_event = asyncio.Event()
        self.write_lock = asyncio.Lock()
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.manual_tasks: dict[str, asyncio.Task[None]] = {}
        self._agent_status = {name: "idle" for name in self.providers}
        self._failures = {name: 0 for name in self.providers}
        self._agent_lifecycle_locks = {
            name: asyncio.Lock()
            for name, provider in self.providers.items()
            if provider.agent_lifecycle is not None
        }
        self._released_agent_lifecycles: set[str] = set()
        self._lifecycle_cleanup_task: asyncio.Task[None] | None = None
        self.fact_compactor = (
            FactCompactor(
                store=self.store,
                project_dir=self.project_dir,
                config=fact_compaction_config,
                hub=self.hub,
                coordinator=self.coordinator,
                execute=self.execute,
                write_lock=self.write_lock,
                on_graph_changed=self.wake_event.set,
            )
            if fact_compaction_config is not None
            and fact_compaction_config.enabled
            else None
        )
        self.store.ensure_provider_health(tuple(self.providers))

    @staticmethod
    def _accepts_keyword(function: Callable[..., object], name: str) -> bool:
        try:
            parameters = inspect.signature(function).parameters
        except (TypeError, ValueError):
            return False
        return name in parameters or any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        )

    async def _append_runtime_event(
        self,
        event_type: str,
        source: str,
        payload: dict[str, object],
    ) -> None:
        event = self.store.append_event(event_type, source, payload)
        await self.hub.publish(event)

    def agent_statuses(self) -> dict[str, str]:
        return dict(self._agent_status)

    def provider_health(self) -> dict[str, dict[str, object]]:
        return {
            str(record["provider"]): record
            for record in self.store.provider_health()
            if record["provider"] in self.providers
        }

    async def _set_agent_status(self, provider: str, status: str) -> None:
        self._agent_status[provider] = status
        event = self.store.append_event(
            "agent.status_changed",
            provider,
            {"provider": provider, "status": status},
        )
        await self.hub.publish(event)

    def _write_project_status(
        self,
        status: str,
        final_answer: str | None = None,
    ) -> None:
        if self.project_status_writer is None:
            self.store.set_project_status(status, final_answer)
            return
        self.project_status_writer(status, final_answer)

    async def _set_project_status(
        self,
        status: str,
        final_answer: str | None = None,
    ) -> None:
        sequence = int(self.store.snapshot()["sequence"])
        self._write_project_status(status, final_answer)
        for event in self.store.events_after(sequence):
            await self.hub.publish(event)

    def _health_policy(self, provider: ProviderConfig) -> tuple[float, float, int]:
        health = provider.health_check
        if health is None:
            return 1.0, 60.0, 3
        return (
            health.cooldown_seconds,
            health.max_cooldown_seconds,
            health.failure_threshold,
        )

    async def _set_provider_health(
        self,
        provider: str,
        status: str,
        *,
        consecutive_failures: int,
        last_error: str | None = None,
        cooldown_until: str | None = None,
        cli_version: str | None = None,
    ) -> dict[str, object]:
        if cli_version is None:
            try:
                cli_version = str(
                    self.store.provider_health_for(provider)["cli_version"]
                )
            except KeyError:
                cli_version = "unknown"
        sequence = int(self.store.snapshot()["sequence"])
        record = self.store.set_provider_health(
            provider,
            status,
            consecutive_failures=consecutive_failures,
            last_error=last_error,
            cooldown_until=cooldown_until,
            cli_version=cli_version,
        )
        for event in self.store.events_after(sequence):
            await self.hub.publish(event)
        return record

    async def _record_provider_success(
        self,
        provider: ProviderConfig,
        *,
        reset_failures: bool = True,
        cli_version: str | None = None,
    ) -> dict[str, object]:
        previous = self.store.provider_health_for(provider.name)
        failures = 0
        if not reset_failures:
            failures = int(
                self.store.provider_health_for(provider.name)[
                    "consecutive_failures"
                ]
            )
        record = await self._set_provider_health(
            provider.name,
            "healthy",
            consecutive_failures=failures,
            cli_version=cli_version,
        )
        if str(previous["status"]) in {"cooldown", "unhealthy"}:
            await self._append_runtime_event(
                "provider.recovered",
                provider.name,
                {
                    "provider": provider.name,
                    "previous_status": previous["status"],
                    "cli_version": record["cli_version"],
                },
            )
        return record

    async def _record_provider_failure(
        self,
        provider: ProviderConfig,
        category: str,
        source_status: str,
        detail: str,
    ) -> dict[str, object]:
        current = self.store.provider_health_for(provider.name)
        failures = int(current["consecutive_failures"]) + 1
        cooldown_until = None
        if category == "cooldown":
            base, maximum, threshold = self._health_policy(provider)
            delay = min(maximum, base * (2 ** min(failures - 1, 20)))
            cooldown_until = (
                datetime.now(timezone.utc) + timedelta(seconds=delay)
            ).isoformat()
        else:
            _, _, threshold = self._health_policy(provider)
        record = await self._set_provider_health(
            provider.name,
            category,
            consecutive_failures=failures,
            last_error=summarize_failure(category, source_status, detail),
            cooldown_until=cooldown_until,
        )
        await self._set_agent_status(provider.name, "failed")
        project_status = str(self.store.snapshot()["project"]["status"])
        if (
            (category in TERMINAL_HEALTH_STATUSES or failures >= threshold)
            and project_status not in {"degraded", "achieved", "stopped"}
        ):
            await self._set_project_status("degraded")
        if (
            category in TERMINAL_HEALTH_STATUSES
            or failures >= threshold
        ) and (
            str(current["status"]) not in TERMINAL_HEALTH_STATUSES
            and int(current["consecutive_failures"]) < threshold
        ):
            await self._append_runtime_event(
                "provider.circuit_opened",
                provider.name,
                {
                    "provider": provider.name,
                    "status": category,
                    "consecutive_failures": failures,
                    "threshold": threshold,
                },
            )
        return record

    async def _check_provider(
        self,
        provider: ProviderConfig,
        *,
        reset_failures: bool = True,
    ) -> str:
        if provider.health_check is None:
            current = self.store.provider_health_for(provider.name)
            record = await self._set_provider_health(
                provider.name,
                "unknown",
                consecutive_failures=int(current["consecutive_failures"]),
                last_error=(
                    str(current["last_error"])
                    if current["last_error"] is not None
                    else None
                ),
            )
            return str(record["status"])
        try:
            result = await self.health_probe(provider)
        except Exception as exc:
            result = ProbeResult("failed", f"health probe raised {type(exc).__name__}")
        if result.status == "success":
            record = await self._record_provider_success(
                provider,
                reset_failures=reset_failures,
                cli_version=result.cli_version,
            )
        else:
            category = classify_probe_failure(result)
            record = await self._record_provider_failure(
                provider,
                category,
                result.status,
                result.detail,
            )
        await self._append_runtime_event(
            "provider.health_check_finished",
            provider.name,
            {
                "provider": provider.name,
                "probe_status": result.status,
                "health_status": record["status"],
                "cli_version": record["cli_version"],
            },
        )
        return str(record["status"])

    async def _record_worker_failure(
        self,
        provider: ProviderConfig,
        result: WorkerResult,
    ) -> str:
        detail = worker_failure_detail(result)
        category = classify_worker_failure(
            result,
            detail,
            get_provider_adapter(provider.name),
        )
        record = await self._record_provider_failure(
            provider,
            category,
            result.status,
            detail,
        )
        return str(record["status"])

    def _origin_host(self) -> str | None:
        for node in self.store.snapshot().get("nodes", []):
            if not isinstance(node, dict) or node.get("type") != "origin":
                continue
            raw = str(node.get("content", "")).strip()
            if not raw:
                continue
            parsed = urlparse(raw if "://" in raw else f"//{raw}")
            if parsed.hostname and not any(char.isspace() for char in parsed.hostname):
                return parsed.hostname.rstrip(".").lower()
        return None

    @staticmethod
    def _cooldown_delay(record: dict[str, object]) -> float:
        cooldown_until = record["cooldown_until"]
        if not cooldown_until:
            return 0.0
        try:
            deadline = datetime.fromisoformat(str(cooldown_until))
        except ValueError:
            return 0.0
        return max(
            0.0,
            (deadline - datetime.now(timezone.utc)).total_seconds(),
        )

    async def _ensure_provider_ready(self, provider: ProviderConfig) -> bool:
        while not self.cancel_event.is_set():
            record = self.store.provider_health_for(provider.name)
            status = str(record["status"])
            if status in TERMINAL_HEALTH_STATUSES:
                return False
            if status != "cooldown":
                return True
            delay = self._cooldown_delay(record)
            if delay > 0:
                await self._append_runtime_event(
                    "provider.cooldown_wait_started",
                    provider.name,
                    {
                        "provider": provider.name,
                        "delay_seconds": round(delay, 6),
                        "cooldown_until": record["cooldown_until"],
                    },
                )
                try:
                    await asyncio.wait_for(
                        self.cancel_event.wait(),
                        timeout=delay,
                    )
                    return False
                except asyncio.TimeoutError:
                    pass
            next_status = await self._check_provider(
                provider,
                reset_failures=False,
            )
            if next_status in TERMINAL_HEALTH_STATUSES:
                return False
        return False

    async def ensure_provider_ready(self, provider_name: str) -> bool:
        return await self._ensure_provider_ready(self.providers[provider_name])

    def _agent_owner_id(self, provider: ProviderConfig) -> str:
        return f"qinyuan-bat:{self.store.project_id}:{provider.name}"

    def _agent_work_dir(self, provider: ProviderConfig) -> Path:
        return self.project_dir / "agent-workspaces" / provider.name

    def _provider_workspace(self, provider: ProviderConfig) -> Path | None:
        if provider.project_workspace or provider.agent_lifecycle is not None:
            return self._agent_work_dir(provider)
        return None

    async def _release_agent_lifecycle(self, provider: ProviderConfig) -> None:
        if (
            provider.agent_lifecycle is None
            or provider.name in self._released_agent_lifecycles
        ):
            return
        lock = self._agent_lifecycle_locks[provider.name]
        async with lock:
            if provider.name in self._released_agent_lifecycles:
                return
            try:
                result = await release_agent_lifecycle(
                    provider,
                    self._agent_owner_id(provider),
                    self._agent_work_dir(provider),
                    self.project_dir
                    / "agent-lifecycle"
                    / provider.name
                    / f"release-{uuid4().hex}",
                    max_log_bytes=self.audit_config.max_log_bytes,
                )
                payload = {
                    "provider": provider.name,
                    "owner_id": self._agent_owner_id(provider),
                    "status": result.status,
                    "exit_code": result.exit_code,
                    "duration_seconds": result.duration_seconds,
                    "stdout_path": result.stdout_path,
                    "stderr_path": result.stderr_path,
                    "detail": result.detail,
                }
                if result.status == "released":
                    self._released_agent_lifecycles.add(provider.name)
            except Exception as exc:
                payload = {
                    "provider": provider.name,
                    "owner_id": self._agent_owner_id(provider),
                    "status": "failed",
                    "error_type": type(exc).__name__,
                    "detail": str(exc)[:500],
                }
            await self._append_runtime_event(
                "agent.lifecycle_release_finished",
                provider.name,
                payload,
            )

    async def _release_agent_lifecycles(self) -> None:
        await asyncio.gather(
            *(
                self._release_agent_lifecycle(provider)
                for provider in self.providers.values()
                if provider.agent_lifecycle is not None
            )
        )

    def _schedule_agent_lifecycle_cleanup(self) -> None:
        if not self._agent_lifecycle_locks:
            return
        if (
            self._lifecycle_cleanup_task is not None
            and not self._lifecycle_cleanup_task.done()
        ):
            return

        async def cleanup_after_tasks() -> None:
            current = asyncio.current_task()
            active = tuple(self.tasks.values()) + tuple(self.manual_tasks.values())
            pending = [
                task
                for task in active
                if task is not current and not task.done()
            ]
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            await self._release_agent_lifecycles()

        self._lifecycle_cleanup_task = asyncio.create_task(cleanup_after_tasks())

    async def _run_provider(
        self,
        provider: ProviderConfig,
        request: str,
        work_dir: Path,
        *,
        kind: str,
        on_chunk=None,
        on_permit: PermitCallback | None = None,
    ) -> WorkerResult:
        async def run() -> WorkerResult:
            kwargs = {
                "on_chunk": on_chunk,
                "cancel_event": self.cancel_event,
            }
            if self._execute_accepts_log_limit:
                kwargs["max_log_bytes"] = self.audit_config.max_log_bytes
            if self._execute_accepts_origin_host:
                kwargs["origin_host"] = self._origin_host()
            workspace = self._provider_workspace(provider)
            if workspace is not None:
                workspace.mkdir(parents=True, exist_ok=True)
                if self._execute_accepts_agent_work_dir:
                    kwargs["agent_work_dir"] = workspace
            if provider.agent_lifecycle is not None:
                if not (
                    self._execute_accepts_agent_owner_id
                    and self._execute_accepts_agent_work_dir
                ):
                    raise RuntimeError(
                        "execution backend does not accept Agent lifecycle arguments"
                    )
                kwargs["agent_owner_id"] = self._agent_owner_id(provider)
            return await self.execute(
                provider,
                request,
                self.challenge_dir,
                work_dir,
                **kwargs,
            )

        async def acquired(lease: PermitLease) -> None:
            if on_permit is None:
                return
            callback_result = on_permit(lease)
            if inspect.isawaitable(callback_result):
                await callback_result

        def with_lease(result: WorkerResult, lease: PermitLease) -> WorkerResult:
            current = self.store.provider_health_for(provider.name)
            cli_version = result.cli_version
            if cli_version == "unknown":
                cli_version = str(current.get("cli_version") or "unknown")
            return replace(
                result,
                queue_seconds=lease.queue_seconds,
                wait_reasons=lease.wait_reasons,
                cli_version=cli_version,
            )

        async def scheduled() -> WorkerResult:
            try:
                if self.coordinator is not None:
                    async with self.coordinator.permit(
                        project_id=self.store.project_id,
                        provider=provider.name,
                        kind=kind,
                        priority=provider.priority,
                        provider_limit=provider.max_running,
                        cancel_event=self.cancel_event,
                    ) as lease:
                        await acquired(lease)
                        if self.cancel_event.is_set():
                            return self._cancelled_result(
                                provider,
                                work_dir,
                                lease=lease,
                                reason="project stop requested",
                            )
                        return with_lease(await run(), lease)
                assert self.global_semaphore is not None
                async with self.global_semaphore:
                    lease = PermitLease(0.0, ())
                    await acquired(lease)
                    return with_lease(await run(), lease)
            except PermitCancelled as exc:
                return self._cancelled_result(
                    provider,
                    work_dir,
                    lease=PermitLease(exc.queue_seconds, exc.wait_reasons),
                    reason="scheduler permit wait cancelled",
                )

        lifecycle_lock = self._agent_lifecycle_locks.get(provider.name)
        if lifecycle_lock is None:
            return await scheduled()
        async with lifecycle_lock:
            return await scheduled()

    @staticmethod
    def _cancelled_result(
        provider: ProviderConfig,
        work_dir: Path,
        *,
        lease: PermitLease | None = None,
        reason: str = "project stop requested",
    ) -> WorkerResult:
        lease = lease or PermitLease(0.0, ())
        return WorkerResult(
            provider=provider.name,
            priority=provider.priority,
            status="cancelled",
            exit_code=None,
            duration_seconds=0.0,
            output="",
            stdout_path=str((work_dir / "stdout.txt").resolve()),
            stderr_path=str((work_dir / "stderr.txt").resolve()),
            work_dir=str(work_dir.resolve()),
            queue_seconds=lease.queue_seconds,
            wait_reasons=lease.wait_reasons,
            cancel_reason=reason,
            execution_backend="not_started",
            isolation="not_started",
            cleanup_status="not_started",
            backend_metadata={"reason": "scheduler_cancelled"},
        )

    async def _mark_planning_started(
        self,
        attempt_id: str,
        lease: PermitLease,
    ) -> None:
        async with self.write_lock:
            sequence = int(self.store.snapshot()["sequence"])
            self.store.mark_planning_started(
                attempt_id,
                lease.queue_seconds,
                lease.wait_reasons,
            )
            events = self.store.events_after(sequence)
        for event in events:
            await self.hub.publish(event)

    async def _finish_planning_attempt(
        self,
        attempt_id: str,
        status: str,
        decision_reason: str,
        result: WorkerResult,
        *,
        intent_id: str | None = None,
    ) -> None:
        async with self.write_lock:
            sequence = int(self.store.snapshot()["sequence"])
            self.store.finish_planning_attempt(
                attempt_id,
                status,
                decision_reason,
                intent_id=intent_id,
                queue_seconds=result.queue_seconds,
                duration_seconds=result.duration_seconds,
                wait_reasons=result.wait_reasons,
                model=result.model,
                cli_version=result.cli_version,
                token_usage=result.token_usage,
                    execution_backend=result.execution_backend,
                    isolation=result.isolation,
                    backend_run_id=result.backend_run_id,
                    cleanup_policy=result.cleanup_policy,
                    cleanup_status=result.cleanup_status,
                    cleanup_detail=result.cleanup_detail,
                    backend_metadata=result.backend_metadata,
            )
            events = self.store.events_after(sequence)
        for event in events:
            await self.hub.publish(event)
        await self._prune_finished_logs()

    async def _mark_execution_started(
        self,
        execution_id: str,
        lease: PermitLease,
    ) -> None:
        async with self.write_lock:
            sequence = int(self.store.snapshot()["sequence"])
            self.store.mark_execution_started(
                execution_id,
                lease.queue_seconds,
                lease.wait_reasons,
            )
            events = self.store.events_after(sequence)
        for event in events:
            await self.hub.publish(event)

    async def _prune_finished_logs(self) -> None:
        async with self.write_lock:
            pruned = prune_audit_logs(
                self.store,
                self.project_dir,
                self.audit_config.max_retained_log_sets,
            )
            event = None
            if pruned:
                event = self.store.append_event(
                    "audit.logs_pruned",
                    "orchestrator",
                    {"record_ids": list(pruned)},
                )
        if event is not None:
            await self.hub.publish(event)

    async def _mark_protocol_failure(self, provider: str) -> None:
        self._failures[provider] += 1
        await self._set_agent_status(provider, "failed")
        if self._failures[provider] >= self.failure_threshold:
            await self._set_project_status("degraded")

    async def run_agent_once(
        self,
        provider_name: str,
        *,
        execution_provider_name: str | None = None,
    ) -> str:
        provider = self.providers[provider_name]
        execution_provider = self.providers[
            execution_provider_name or provider_name
        ]
        if self.cancel_event.is_set():
            await self._set_agent_status(provider_name, "cancelled")
            return "cancelled"

        await self._set_agent_status(provider_name, "planning")
        snapshot = self.store.snapshot()
        planning_node_types = {
            str(node["id"]): str(node["type"])
            for node in snapshot["nodes"]
            if node["state"] == "active"
        }
        planning_dir = (
            self.project_dir
            / "executions"
            / f"plan-{provider_name}-{uuid4().hex}"
        )
        async with self.write_lock:
            sequence = int(self.store.snapshot()["sequence"])
            planning_attempt_id = self.store.start_planning_attempt(
                provider_name,
                int(snapshot["sequence"]),
                str((planning_dir / "stdout.txt").resolve()),
                str((planning_dir / "stderr.txt").resolve()),
                str(planning_dir.resolve()),
            )
            events = self.store.events_after(sequence)
        for event in events:
            await self.hub.publish(event)
        planning_logs = _BufferedLogPublisher(
            self.store,
            self.hub,
            self.write_lock,
            "planning.log_chunk",
            provider.name,
            {"planning_attempt_id": planning_attempt_id},
            planning_dir / "combined.txt",
        )
        try:
            planning = await self._run_provider(
                replace(
                    provider,
                    timeout_seconds=provider.planning_timeout_seconds,
                ),
                render_intent_request(
                    snapshot,
                    self.challenge_dir,
                    self._provider_workspace(provider),
                ),
                planning_dir,
                kind="planning",
                on_chunk=planning_logs.add,
                on_permit=lambda lease: self._mark_planning_started(
                    planning_attempt_id,
                    lease,
                ),
            )
        finally:
            await planning_logs.close()
        if planning.status != "success":
            planning_detail = worker_failure_detail(planning)
            planning_category = classify_worker_failure(
                planning,
                planning_detail,
                get_provider_adapter(provider.name),
            )
            await self._finish_planning_attempt(
                planning_attempt_id,
                planning.status,
                planning.cancel_reason
                or summarize_failure(
                    planning_category,
                    planning.status,
                    planning_detail,
                ),
                planning,
            )
            if planning.status == "cancelled":
                await self._set_agent_status(provider_name, "cancelled")
                return "cancelled"
            await self._record_worker_failure(provider, planning)
            return "health_failure"

        parsed_intent = parse_intent(
            planning.protocol_text,
            planning_node_types,
        )
        if parsed_intent.status != "valid" or parsed_intent.decision is None:
            await self._finish_planning_attempt(
                planning_attempt_id,
                "protocol_error",
                parsed_intent.error or "invalid intent protocol",
                planning,
            )
            await self._mark_protocol_failure(provider_name)
            return "failed"
        if (
            self.policy.check_stale_plans
            and self.store.node_types() != planning_node_types
        ):
            await self._finish_planning_attempt(
                planning_attempt_id,
                "stale",
                "blackboard changed after planning snapshot",
                planning,
            )
            return "stale"
        if parsed_intent.decision.action == "idle":
            await self._finish_planning_attempt(
                planning_attempt_id,
                "idle",
                parsed_intent.decision.reason or "provider chose idle",
                planning,
            )
            self._failures[provider_name] = 0
            await self._record_provider_success(provider)
            await self._set_agent_status(provider_name, "idle")
            return "idle"
        assert parsed_intent.decision.intent is not None

        commit_error: str | None = None
        async with self.write_lock:
            stale = (
                self.policy.check_stale_plans
                and self.store.node_types() != planning_node_types
            )
            if stale:
                intent = None
                events = ()
            else:
                sequence = int(self.store.snapshot()["sequence"])
                try:
                    intent = self.store.create_or_merge_intent(
                        provider_name,
                        parsed_intent.decision.intent,
                    )
                except ValueError as exc:
                    intent = None
                    commit_error = str(exc)
                events = self.store.events_after(sequence)
        if stale:
            await self._finish_planning_attempt(
                planning_attempt_id,
                "stale",
                "blackboard changed before intent commit",
                planning,
            )
            return "stale"
        if commit_error is not None:
            await self._finish_planning_attempt(
                planning_attempt_id,
                "invalid",
                commit_error,
                planning,
            )
            await self._set_agent_status(provider_name, "failed")
            return "failed"
        assert intent is not None
        for event in events:
            await self.hub.publish(event)
        await self._finish_planning_attempt(
            planning_attempt_id,
            "valid",
            "valid intent created or merged",
            planning,
            intent_id=intent.id,
        )
        result_status = await self._execute_intent(execution_provider, intent)
        if result_status == "completed":
            self._failures[provider_name] = 0
            await self._record_provider_success(provider)
            await self._set_agent_status(provider_name, "completed")
            if execution_provider.name != provider_name:
                self._failures[execution_provider.name] = 0
                await self._record_provider_success(execution_provider)
                await self._set_agent_status(
                    execution_provider.name,
                    "completed",
                )
            return "completed"
        if result_status == "protocol_error":
            await self._mark_protocol_failure(execution_provider.name)
            return "failed"
        if result_status == "cancelled":
            await self._set_agent_status(provider_name, "cancelled")
            if execution_provider.name != provider_name:
                await self._set_agent_status(
                    execution_provider.name,
                    "cancelled",
                )
        if result_status == "execution_limit":
            await self._set_agent_status(provider_name, "idle")
        return result_status

    async def _execute_intent(
        self,
        provider: ProviderConfig,
        intent: IntentRecord,
    ) -> str:
        protocol_error: str | None = None
        rejected_result_line: str | None = None
        maximum_attempts = 1 + self.policy.protocol_correction_attempts
        for attempt_number in range(maximum_attempts):
            prepared = await self._create_execution_record(
                provider,
                intent,
                retry_count=attempt_number,
                enforce_policy=True,
            )
            if prepared is None:
                return "execution_limit"
            attempt = await self._execute_intent_attempt(
                provider,
                intent,
                retry_count=attempt_number,
                protocol_error=protocol_error,
                rejected_result_line=rejected_result_line,
                prepared=prepared,
            )
            if attempt.status == "completed":
                return "completed"
            if attempt.status != "protocol_error":
                return attempt.status
            if attempt_number + 1 >= maximum_attempts:
                return "protocol_error"
            protocol_error = attempt.protocol_error
            rejected_result_line = attempt.rejected_result_line
        raise AssertionError("unreachable intent attempt loop")

    async def _create_execution_record(
        self,
        provider: ProviderConfig,
        intent: IntentRecord,
        *,
        retry_count: int,
        retry_of_execution_id: str | None = None,
        enforce_policy: bool = False,
    ) -> tuple[ExecutionRecord, Path] | None:
        execution_dir = (
            self.project_dir
            / "executions"
            / f"run-{provider.name}-{uuid4().hex}"
        )
        async with self.write_lock:
            sequence = int(self.store.snapshot()["sequence"])
            limit = self.policy.max_executions_per_intent
            if (
                enforce_policy
                and limit is not None
                and self.store.execution_count(intent.id) >= limit
            ):
                execution = None
                self.store.append_event(
                    "intent.execution_skipped",
                    provider.name,
                    {
                        "intent_id": intent.id,
                        "reason": "execution limit reached",
                        "max_executions_per_intent": limit,
                    },
                )
            else:
                execution = self.store.start_execution(
                    intent.id,
                    provider.name,
                    str((execution_dir / "stdout.txt").resolve()),
                    str((execution_dir / "stderr.txt").resolve()),
                    str(execution_dir.resolve()),
                    retry_count=retry_count,
                    retry_of_execution_id=retry_of_execution_id,
                    defer_start=True,
                )
            events = self.store.events_after(sequence)
        for event in events:
            await self.hub.publish(event)
        if execution is None:
            return None
        return execution, execution_dir

    @staticmethod
    def _bounded_timeout_output(output: str) -> str:
        if len(output) <= TIMEOUT_SUMMARY_MAX_CHARS:
            return output
        head_size = TIMEOUT_SUMMARY_MAX_CHARS // 4
        tail_size = TIMEOUT_SUMMARY_MAX_CHARS - head_size
        return (
            output[:head_size]
            + "\n...[middle of captured output omitted]...\n"
            + output[-tail_size:]
        )

    @staticmethod
    def _recovered_timeout_result(
        timed_out: WorkerResult,
        summary: WorkerResult | None,
        *,
        mode: str,
    ) -> WorkerResult:
        summary_metadata: dict[str, object] = {"mode": mode}
        if summary is not None:
            summary_metadata.update(
                {
                    "status": summary.status,
                    "duration_seconds": summary.duration_seconds,
                    "queue_seconds": summary.queue_seconds,
                    "stdout_path": summary.stdout_path,
                    "stderr_path": summary.stderr_path,
                    "work_dir": summary.work_dir,
                    "execution_backend": summary.execution_backend,
                    "isolation": summary.isolation,
                    "backend_run_id": summary.backend_run_id,
                    "cleanup_policy": summary.cleanup_policy,
                    "cleanup_status": summary.cleanup_status,
                    "cleanup_detail": summary.cleanup_detail,
                    "backend_metadata": summary.backend_metadata,
                }
            )
        metadata = {
            **timed_out.backend_metadata,
            "timeout_recovery": summary_metadata,
        }
        if summary is None:
            return replace(
                timed_out,
                status="success",
                backend_metadata=metadata,
            )
        return replace(
            summary,
            status="success",
            duration_seconds=round(
                timed_out.duration_seconds + summary.duration_seconds,
                3,
            ),
            output=summary.output,
            stdout_path=timed_out.stdout_path,
            stderr_path=timed_out.stderr_path,
            work_dir=timed_out.work_dir,
            queue_seconds=timed_out.queue_seconds + summary.queue_seconds,
            wait_reasons=tuple(
                dict.fromkeys(timed_out.wait_reasons + summary.wait_reasons)
            ),
            model=(
                summary.model
                if summary.model != "unknown"
                else timed_out.model
            ),
            cli_version=(
                summary.cli_version
                if summary.cli_version != "unknown"
                else timed_out.cli_version
            ),
            token_usage={
                "timed_out_run": timed_out.token_usage,
                "timeout_summary": summary.token_usage,
            },
            execution_backend=timed_out.execution_backend,
            isolation=timed_out.isolation,
            backend_run_id=timed_out.backend_run_id,
            cleanup_policy=timed_out.cleanup_policy,
            cleanup_status=timed_out.cleanup_status,
            cleanup_detail=timed_out.cleanup_detail,
            backend_metadata=metadata,
        )

    async def _recover_timeout_result(
        self,
        provider: ProviderConfig,
        intent: IntentRecord,
        execution: ExecutionRecord,
        execution_dir: Path,
        execution_logs: _BufferedLogPublisher,
        timed_out: WorkerResult,
    ) -> WorkerResult | None:
        existing_node_ids = set(self.store.node_types())
        parsed = parse_result(
            timed_out.protocol_text,
            assigned_intent_id=intent.id,
            existing_node_ids=existing_node_ids,
            node_types=self.store.node_types(),
            current_goal_node_id=self.store.current_goal_node_id(),
            existing_addresses=self.store.existing_addresses(),
        )
        if (
            parsed.status == "valid"
            and parsed.update is not None
            and (
                parsed.update.facts
                or parsed.update.edges
                or parsed.update.goal_achieved
            )
        ):
            await self._append_runtime_event(
                "intent.timeout_result_recovered",
                provider.name,
                {
                    "intent_id": intent.id,
                    "execution_id": execution.id,
                    "mode": "captured_result",
                },
            )
            return self._recovered_timeout_result(
                timed_out,
                None,
                mode="captured_result",
            )

        if self.cancel_event.is_set():
            return None

        await self._append_runtime_event(
            "intent.timeout_summary_started",
            provider.name,
            {
                "intent_id": intent.id,
                "execution_id": execution.id,
            },
        )
        summary_timeout = max(
            0.1,
            min(TIMEOUT_SUMMARY_SECONDS, provider.planning_timeout_seconds),
        )
        try:
            summary = await self._run_provider(
                replace(provider, timeout_seconds=summary_timeout),
                render_timeout_summary_request(
                    self.store.snapshot(),
                    {
                        "id": intent.id,
                        "source_node_id": intent.source_node_id,
                        "target_node_id": intent.target_node_id,
                        "content": intent.content,
                        "generation": intent.generation,
                    },
                    self.challenge_dir,
                    self._bounded_timeout_output(timed_out.output),
                    artifact_dir=self._provider_workspace(provider),
                ),
                execution_dir / "timeout-summary",
                kind="execution",
                on_chunk=execution_logs.add,
            )
        except Exception as exc:
            await self._append_runtime_event(
                "intent.timeout_summary_finished",
                provider.name,
                {
                    "intent_id": intent.id,
                    "execution_id": execution.id,
                    "status": "failed",
                    "error_type": type(exc).__name__,
                },
            )
            return None
        parsed = parse_result(
            summary.protocol_text,
            assigned_intent_id=intent.id,
            existing_node_ids=set(self.store.node_types()),
            node_types=self.store.node_types(),
            current_goal_node_id=self.store.current_goal_node_id(),
            existing_addresses=self.store.existing_addresses(),
        )
        recovered = (
            parsed.status == "valid"
            and parsed.update is not None
            and (
                parsed.update.facts
                or parsed.update.edges
                or parsed.update.goal_achieved
            )
        )
        await self._append_runtime_event(
            "intent.timeout_summary_finished",
            provider.name,
            {
                "intent_id": intent.id,
                "execution_id": execution.id,
                "status": "completed" if recovered else summary.status,
                "result_protocol": parsed.status,
            },
        )
        if not recovered:
            return None
        return self._recovered_timeout_result(
            timed_out,
            summary,
            mode="summary_process",
        )

    async def _execute_intent_attempt(
        self,
        provider: ProviderConfig,
        intent: IntentRecord,
        retry_count: int,
        protocol_error: str | None,
        rejected_result_line: str | None,
        prepared: tuple[ExecutionRecord, Path],
    ) -> IntentAttemptResult:
        execution, execution_dir = prepared

        execution_logs = _BufferedLogPublisher(
            self.store,
            self.hub,
            self.write_lock,
            "intent.log_chunk",
            provider.name,
            {"intent_id": intent.id, "execution_id": execution.id},
            execution_dir / "combined.txt",
        )

        await self._set_agent_status(provider.name, "running")
        snapshot = self.store.snapshot()
        try:
            result = await self._run_provider(
                provider,
                render_execution_request(
                    snapshot,
                    {
                        "id": intent.id,
                        "source_node_id": intent.source_node_id,
                        "target_node_id": intent.target_node_id,
                        "content": intent.content,
                        "generation": intent.generation,
                    },
                    self.challenge_dir,
                    protocol_error=protocol_error,
                    rejected_result_line=rejected_result_line,
                    artifact_dir=self._provider_workspace(provider),
                ),
                execution_dir,
                kind="execution",
                on_chunk=execution_logs.add,
                on_permit=lambda lease: self._mark_execution_started(
                    execution.id,
                    lease,
                ),
            )
            if result.status == "timeout":
                recovered = await self._recover_timeout_result(
                    provider,
                    intent,
                    execution,
                    execution_dir,
                    execution_logs,
                    result,
                )
                if recovered is not None:
                    result = recovered
        finally:
            await execution_logs.close()
        if result.status != "success":
            result_detail = worker_failure_detail(result)
            result_category = classify_worker_failure(
                result,
                result_detail,
                get_provider_adapter(provider.name),
            )
            failure_message = summarize_failure(
                result_category,
                result.status,
                result_detail,
            )
            async with self.write_lock:
                sequence = int(self.store.snapshot()["sequence"])
                self.store.fail_execution(
                    execution.id,
                    result.status,
                    failure_message,
                    result.exit_code,
                    queue_seconds=result.queue_seconds,
                    duration_seconds=result.duration_seconds,
                    cancel_reason=result.cancel_reason,
                    wait_reasons=result.wait_reasons,
                    model=result.model,
                    cli_version=result.cli_version,
                    token_usage=result.token_usage,
                    execution_backend=result.execution_backend,
                    isolation=result.isolation,
                    backend_run_id=result.backend_run_id,
                    cleanup_policy=result.cleanup_policy,
                    cleanup_status=result.cleanup_status,
                    cleanup_detail=result.cleanup_detail,
                    backend_metadata=result.backend_metadata,
                )
                events = self.store.events_after(sequence)
            for event in events:
                await self.hub.publish(event)
            await self._prune_finished_logs()
            if result.status == "cancelled":
                return IntentAttemptResult("cancelled")
            await self._record_worker_failure(provider, result)
            return IntentAttemptResult("health_failure")

        parsed_result = parse_result(
            result.protocol_text,
            assigned_intent_id=intent.id,
            existing_node_ids=set(self.store.node_types()),
            node_types=self.store.node_types(),
            current_goal_node_id=self.store.current_goal_node_id(),
            existing_addresses=self.store.existing_addresses(),
        )
        if parsed_result.status != "valid" or parsed_result.update is None:
            error = parsed_result.error or "invalid result protocol"
            async with self.write_lock:
                sequence = int(self.store.snapshot()["sequence"])
                self.store.fail_execution(
                    execution.id,
                    "protocol_error",
                    error,
                    result.exit_code,
                    queue_seconds=result.queue_seconds,
                    duration_seconds=result.duration_seconds,
                    wait_reasons=result.wait_reasons,
                    model=result.model,
                    cli_version=result.cli_version,
                    token_usage=result.token_usage,
                    execution_backend=result.execution_backend,
                    isolation=result.isolation,
                    backend_run_id=result.backend_run_id,
                    cleanup_policy=result.cleanup_policy,
                    cleanup_status=result.cleanup_status,
                    cleanup_detail=result.cleanup_detail,
                    backend_metadata=result.backend_metadata,
                )
                events = self.store.events_after(sequence)
            for event in events:
                await self.hub.publish(event)
            await self._prune_finished_logs()
            return IntentAttemptResult(
                status="protocol_error",
                protocol_error=error,
                rejected_result_line=last_result_line(result.protocol_text),
            )

        async with self.write_lock:
            sequence = int(self.store.snapshot()["sequence"])
            self.store.finish_execution(
                execution.id,
                parsed_result.update,
                result.exit_code,
                "completed",
                queue_seconds=result.queue_seconds,
                duration_seconds=result.duration_seconds,
                wait_reasons=result.wait_reasons,
                model=result.model,
                cli_version=result.cli_version,
                token_usage=result.token_usage,
                    execution_backend=result.execution_backend,
                    isolation=result.isolation,
                    backend_run_id=result.backend_run_id,
                    cleanup_policy=result.cleanup_policy,
                    cleanup_status=result.cleanup_status,
                    cleanup_detail=result.cleanup_detail,
                    backend_metadata=result.backend_metadata,
            )
            goal_result = None
            if parsed_result.update.protocol_version == 2:
                goal_result = self.store.goal_result(
                    self.answer_quorum_provider_count
                )
            consensus = (
                None
                if parsed_result.update.protocol_version == 2
                else self.store.consensus_details(
                    self.answer_quorum_provider_count
                )
            )
            if goal_result is not None:
                self.store.append_event(
                    "goal.result_selected",
                    "orchestrator",
                    {
                        "goal_version": goal_result.goal_version,
                        "fact_node_id": goal_result.fact.id,
                        "supporting_providers": list(
                            goal_result.supporting_providers
                        ),
                        "threshold": goal_result.completion_threshold,
                    },
                )
                self._write_project_status("achieved", goal_result.fact.content)
                self.cancel_event.set()
            elif (
                parsed_result.update.protocol_version == 2
                and self.store.goal_result_conflict(
                    self.answer_quorum_provider_count
                )
            ):
                self.store.append_event(
                    "goal.result_conflict",
                    "orchestrator",
                    {
                        "goal_version": self.store.snapshot()["project"][
                            "goal_version"
                        ],
                        "reason_code": "result_conflict",
                    },
                )
                self._write_project_status("degraded", None)
                self.cancel_event.set()
            elif consensus is not None:
                self.store.append_event(
                    "answer.consensus_reached",
                    "orchestrator",
                    consensus,
                )
                self._write_project_status(
                    "achieved",
                    str(consensus["answer"]),
                )
                self.cancel_event.set()
            events = self.store.events_after(sequence)
        for event in events:
            await self.hub.publish(event)
        await self._prune_finished_logs()
        self.wake_event.set()
        if self.fact_compactor is not None:
            self.fact_compactor.wake()
        project_status = str(self.store.snapshot()["project"]["status"])
        if self.cancel_event.is_set() and project_status in {
            "degraded",
            "stopped",
        }:
            self._schedule_agent_lifecycle_cleanup()
        return IntentAttemptResult("completed")

    async def queue_retry(
        self,
        source_execution_id: str,
    ) -> dict[str, object]:
        source = self.store.execution(source_execution_id)
        if source["status"] in {"planned", "running"}:
            raise ValueError("an active execution cannot be retried")
        if self.store.snapshot()["project"]["status"] == "achieved":
            raise ValueError("reopen the achieved project before retrying")
        provider_name = str(source["provider"])
        provider = self.providers.get(provider_name)
        if provider is None:
            raise ValueError(f"provider {provider_name!r} is not configured")
        health = self.store.provider_health_for(provider_name)
        health_status = str(health["status"])
        if health_status in TERMINAL_HEALTH_STATUSES:
            raise ValueError(f"provider {provider_name!r} is {health_status}")
        if health_status == "cooldown" and self._cooldown_delay(health) > 0:
            raise ValueError(f"provider {provider_name!r} is in cooldown")

        self.cancel_event.clear()
        intent = self.store.intent(str(source["intent_edge_id"]))
        retry_count = int(source.get("retry_count") or 0) + 1
        prepared = await self._create_execution_record(
            provider,
            intent,
            retry_count=retry_count,
            retry_of_execution_id=source_execution_id,
        )
        assert prepared is not None
        await self._set_project_status("running")
        execution = self.store.execution(prepared[0].id)
        task = asyncio.create_task(
            self._run_manual_retry(
                provider,
                intent,
                prepared,
                retry_count,
                source,
            )
        )
        self.manual_tasks[prepared[0].id] = task
        task.add_done_callback(
            lambda _task, execution_id=prepared[0].id: (
                self.manual_tasks.pop(execution_id, None)
            )
        )
        return execution

    async def _run_manual_retry(
        self,
        provider: ProviderConfig,
        intent: IntentRecord,
        prepared: tuple[ExecutionRecord, Path],
        retry_count: int,
        source: dict[str, object],
    ) -> None:
        try:
            source_error = (
                str(source["error_message"])
                if source.get("status") == "protocol_error"
                and source.get("error_message")
                else None
            )
            result = await self._execute_intent_attempt(
                provider,
                intent,
                retry_count=retry_count,
                protocol_error=source_error,
                rejected_result_line=None,
                prepared=prepared,
            )
            if result.status == "completed":
                self._failures[provider.name] = 0
                await self._record_provider_success(provider)
                await self._set_agent_status(provider.name, "completed")
            elif result.status == "protocol_error":
                await self._mark_protocol_failure(provider.name)
            elif result.status == "cancelled":
                await self._set_agent_status(provider.name, "cancelled")
        except Exception as exc:
            async with self.write_lock:
                sequence = int(self.store.snapshot()["sequence"])
                current = self.store.execution(prepared[0].id)
                if current["status"] in {"planned", "running"}:
                    self.store.fail_execution(
                        prepared[0].id,
                        "failed",
                        f"manual retry raised {type(exc).__name__}: {exc}",
                    )
                events = self.store.events_after(sequence)
            for event in events:
                await self.hub.publish(event)
            await self._set_agent_status(provider.name, "failed")
            if self.store.snapshot()["project"]["status"] == "running":
                await self._set_project_status("degraded")
        finally:
            active_agents = any(not task.done() for task in self.tasks.values())
            active_manual = any(
                execution_id != prepared[0].id and not task.done()
                for execution_id, task in self.manual_tasks.items()
            )
            project_status = str(self.store.snapshot()["project"]["status"])
            if (
                not active_agents
                and not active_manual
                and project_status == "running"
            ):
                await self._set_project_status("idle")

    async def _agent_loop(self, provider_name: str) -> None:
        iterations = 0
        while not self.cancel_event.is_set():
            if self.max_iterations is not None and iterations >= self.max_iterations:
                break
            provider = self.providers[provider_name]
            if not await self._ensure_provider_ready(provider):
                break
            graph_sequence = self.store.graph_sequence()
            evidence_signature = (
                self._evidence_signature()
                if provider_name == "qinyuan-agent"
                else None
            )
            status = await self.run_agent_once(provider_name)
            if status == "stale":
                continue
            iterations += 1
            if status in {"failed", "cancelled"}:
                if self._failures[provider_name] >= self.failure_threshold:
                    break
            if (
                provider_name == "qinyuan-agent"
                and status == "completed"
                and self.store.snapshot()["project"]["status"] == "running"
                and self._evidence_signature() == evidence_signature
            ):
                await self._set_agent_status(provider_name, "idle")
                if self.max_iterations is not None:
                    break
                await self._wait_for_graph_change(self.store.graph_sequence())
                continue
            if status == "idle":
                if self.max_iterations is not None:
                    break
                await self._wait_for_graph_change(graph_sequence)

    def _evidence_signature(self) -> tuple[frozenset[tuple[object, ...]], ...]:
        snapshot = self.store.snapshot()
        nodes = frozenset(
            (
                node["id"],
                node["type"],
                node["content"],
                node["state"],
                node["generation"],
            )
            for node in snapshot["nodes"]
        )
        relations = frozenset(
            (
                edge["id"],
                edge["type"],
                edge["from_node"],
                edge["to_node"],
                edge["generation"],
            )
            for edge in snapshot["edges"]
            if edge["type"] != "intent"
        )
        return nodes, relations

    async def _wait_for_graph_change(self, observed_sequence: int) -> None:
        while not self.cancel_event.is_set():
            if self.store.graph_sequence() != observed_sequence:
                return
            self.wake_event.clear()
            if self.store.graph_sequence() != observed_sequence:
                return
            if self.cancel_event.is_set():
                return
            try:
                await asyncio.wait_for(
                    self.wake_event.wait(),
                    timeout=self.idle_poll_interval,
                )
            except asyncio.TimeoutError:
                pass

    async def start(self) -> None:
        if any(not task.done() for task in self.tasks.values()):
            return
        if self._lifecycle_cleanup_task is not None:
            await asyncio.gather(
                self._lifecycle_cleanup_task,
                return_exceptions=True,
            )
            self._lifecycle_cleanup_task = None
        self._released_agent_lifecycles.clear()
        self.cancel_event.clear()
        runnable: list[str] = []
        for name, provider in self.providers.items():
            current = self.store.provider_health_for(name)
            if (
                current["status"] == "cooldown"
                and self._cooldown_delay(current) > 0
            ):
                health_status = "cooldown"
            else:
                health_status = await self._check_provider(provider)
            if health_status not in TERMINAL_HEALTH_STATUSES:
                runnable.append(name)
        desired_status = (
            "degraded"
            if len(runnable) != len(self.providers)
            else "running"
        )
        current_status = str(self.store.snapshot()["project"]["status"])
        if current_status != desired_status:
            await self._set_project_status(desired_status)
        self.tasks = {
            name: asyncio.create_task(self._agent_loop(name))
            for name in runnable
        }
        if self.fact_compactor is not None:
            await self.fact_compactor.start()

    async def wait_until_idle(self, timeout: float | None = None) -> None:
        if self.tasks:
            await asyncio.wait_for(
                asyncio.gather(*self.tasks.values()),
                timeout=timeout,
            )
        project = self.store.snapshot()["project"]
        if project["status"] == "running":
            await self._set_project_status("idle")
        if self.fact_compactor is not None:
            await self.fact_compactor.wait_until_idle(timeout=timeout)

    async def stop(self, *, release_lifecycle: bool = True) -> None:
        self.cancel_event.set()
        self.wake_event.set()
        if self.tasks:
            await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        if self.manual_tasks:
            await asyncio.gather(
                *self.manual_tasks.values(),
                return_exceptions=True,
            )
        if self.fact_compactor is not None:
            await self.fact_compactor.stop()
        if self.store.snapshot()["project"]["status"] != "achieved":
            await self._set_project_status("stopped")
        if release_lifecycle:
            await self._release_agent_lifecycles()
            cleanup_task = self._lifecycle_cleanup_task
            if cleanup_task is not None and cleanup_task is not asyncio.current_task():
                await asyncio.gather(cleanup_task, return_exceptions=True)
