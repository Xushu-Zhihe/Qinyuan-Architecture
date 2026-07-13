import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import uuid4

from orchestrator.events import ProjectEventHub
from orchestrator.protocol import (
    last_result_line,
    parse_intent,
    parse_result,
    render_execution_request,
    render_intent_request,
)
from orchestrator.store import BlackboardStore, IntentRecord
from worker.executor import execute_provider
from worker.models import LogChunk, ProviderConfig, WorkerResult


Executor = Callable[..., Awaitable[WorkerResult]]


@dataclass(frozen=True, slots=True)
class IntentAttemptResult:
    status: str
    protocol_error: str | None = None
    rejected_result_line: str | None = None


class ProjectSupervisor:
    def __init__(
        self,
        store: BlackboardStore,
        providers: tuple[ProviderConfig, ...],
        challenge_dir: Path | None,
        project_dir: Path,
        hub: ProjectEventHub | None = None,
        execute: Executor = execute_provider,
        global_semaphore: asyncio.Semaphore | None = None,
        max_iterations: int | None = None,
        failure_threshold: int = 3,
        idle_poll_interval: float = 1.0,
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
        self.global_semaphore = global_semaphore or asyncio.Semaphore(1024)
        self.max_iterations = max_iterations
        self.failure_threshold = failure_threshold
        self.idle_poll_interval = idle_poll_interval
        self.cancel_event = asyncio.Event()
        self.wake_event = asyncio.Event()
        self.write_lock = asyncio.Lock()
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self._agent_status = {name: "idle" for name in self.providers}
        self._failures = {name: 0 for name in self.providers}

    def agent_statuses(self) -> dict[str, str]:
        return dict(self._agent_status)

    async def _set_agent_status(self, provider: str, status: str) -> None:
        self._agent_status[provider] = status
        event = self.store.append_event(
            "agent.status_changed",
            provider,
            {"provider": provider, "status": status},
        )
        await self.hub.publish(event)

    async def _run_provider(
        self,
        provider: ProviderConfig,
        request: str,
        work_dir: Path,
        on_chunk=None,
    ) -> WorkerResult:
        async with self.global_semaphore:
            return await self.execute(
                provider,
                request,
                self.challenge_dir,
                work_dir,
                on_chunk=on_chunk,
                cancel_event=self.cancel_event,
            )

    async def _mark_failure(self, provider: str) -> None:
        self._failures[provider] += 1
        await self._set_agent_status(provider, "failed")
        if self._failures[provider] >= self.failure_threshold:
            self.store.set_project_status("degraded")

    async def run_agent_once(self, provider_name: str) -> str:
        provider = self.providers[provider_name]
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
        planning = await self._run_provider(
            replace(
                provider,
                timeout_seconds=provider.planning_timeout_seconds,
            ),
            render_intent_request(snapshot, self.challenge_dir),
            planning_dir,
        )
        if planning.status != "success":
            await self._mark_failure(provider_name)
            return "failed"

        parsed_intent = parse_intent(
            planning.output,
            planning_node_types,
        )
        if parsed_intent.status != "valid" or parsed_intent.decision is None:
            await self._mark_failure(provider_name)
            return "failed"
        if self.store.node_types() != planning_node_types:
            return "stale"
        if parsed_intent.decision.action == "idle":
            self._failures[provider_name] = 0
            await self._set_agent_status(provider_name, "idle")
            return "idle"
        assert parsed_intent.decision.intent is not None

        async with self.write_lock:
            if self.store.node_types() != planning_node_types:
                return "stale"
            sequence = int(self.store.snapshot()["sequence"])
            intent = self.store.create_or_merge_intent(
                provider_name,
                parsed_intent.decision.intent,
            )
            events = self.store.events_after(sequence)
        for event in events:
            await self.hub.publish(event)
        result_status = await self._execute_intent(provider, intent)
        if result_status == "completed":
            self._failures[provider_name] = 0
            await self._set_agent_status(provider_name, "completed")
        else:
            await self._mark_failure(provider_name)
        return result_status

    async def _execute_intent(
        self,
        provider: ProviderConfig,
        intent: IntentRecord,
    ) -> str:
        protocol_error: str | None = None
        rejected_result_line: str | None = None
        for attempt_number in range(2):
            attempt = await self._execute_intent_attempt(
                provider,
                intent,
                protocol_error=protocol_error,
                rejected_result_line=rejected_result_line,
            )
            if attempt.status == "completed":
                return "completed"
            if attempt.status != "protocol_error" or attempt_number == 1:
                return "failed"
            protocol_error = attempt.protocol_error
            rejected_result_line = attempt.rejected_result_line
        raise AssertionError("unreachable intent attempt loop")

    async def _execute_intent_attempt(
        self,
        provider: ProviderConfig,
        intent: IntentRecord,
        protocol_error: str | None,
        rejected_result_line: str | None,
    ) -> IntentAttemptResult:
        execution_dir = (
            self.project_dir
            / "executions"
            / f"run-{provider.name}-{uuid4().hex}"
        )
        async with self.write_lock:
            sequence = int(self.store.snapshot()["sequence"])
            execution = self.store.start_execution(
                intent.id,
                provider.name,
                str((execution_dir / "stdout.txt").resolve()),
                str((execution_dir / "stderr.txt").resolve()),
                str(execution_dir.resolve()),
            )
            events = self.store.events_after(sequence)
        for event in events:
            await self.hub.publish(event)

        offsets = {"stdout": 0, "stderr": 0}

        async def on_chunk(chunk: LogChunk) -> None:
            offset = offsets[chunk.stream]
            byte_length = len(chunk.text.encode("utf-8"))
            offsets[chunk.stream] += byte_length
            metadata = {
                "intent_id": intent.id,
                "execution_id": execution.id,
                "stream": chunk.stream,
                "byte_offset": offset,
                "byte_length": byte_length,
            }
            async with self.write_lock:
                event = self.store.append_event(
                    "intent.log_chunk",
                    provider.name,
                    metadata,
                )
            await self.hub.publish(
                {**event, "payload": {**metadata, "text": chunk.text}}
            )

        await self._set_agent_status(provider.name, "running")
        snapshot = self.store.snapshot()
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
            ),
            execution_dir,
            on_chunk=on_chunk,
        )
        if result.status != "success":
            async with self.write_lock:
                self.store.fail_execution(
                    execution.id,
                    result.status,
                    f"provider process status was {result.status}",
                    result.exit_code,
                )
            return IntentAttemptResult("failed")

        parsed_result = parse_result(
            result.output,
            assigned_intent_id=intent.id,
            existing_node_ids=set(self.store.node_types()),
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
                )
                events = self.store.events_after(sequence)
            for event in events:
                await self.hub.publish(event)
            return IntentAttemptResult(
                status="protocol_error",
                protocol_error=error,
                rejected_result_line=last_result_line(result.output),
            )

        async with self.write_lock:
            sequence = int(self.store.snapshot()["sequence"])
            self.store.finish_execution(
                execution.id,
                parsed_result.update,
                result.exit_code,
                "completed",
            )
            answer = self.store.consensus(len(self.providers))
            if answer is not None:
                self.store.set_project_status("achieved", answer)
                self.cancel_event.set()
            events = self.store.events_after(sequence)
        for event in events:
            await self.hub.publish(event)
        self.wake_event.set()
        return IntentAttemptResult("completed")

    async def _agent_loop(self, provider_name: str) -> None:
        iterations = 0
        while not self.cancel_event.is_set():
            if self.max_iterations is not None and iterations >= self.max_iterations:
                break
            graph_sequence = self.store.graph_sequence()
            status = await self.run_agent_once(provider_name)
            if status == "stale":
                continue
            iterations += 1
            if status in {"failed", "cancelled"}:
                if self._failures[provider_name] >= self.failure_threshold:
                    break
            if status == "idle":
                if self.max_iterations is not None:
                    break
                await self._wait_for_graph_change(graph_sequence)

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
        self.cancel_event.clear()
        self.store.set_project_status("running")
        self.tasks = {
            name: asyncio.create_task(self._agent_loop(name))
            for name in self.providers
        }

    async def wait_until_idle(self, timeout: float | None = None) -> None:
        if self.tasks:
            await asyncio.wait_for(
                asyncio.gather(*self.tasks.values()),
                timeout=timeout,
            )
        project = self.store.snapshot()["project"]
        if project["status"] == "running":
            self.store.set_project_status("idle")

    async def stop(self) -> None:
        self.cancel_event.set()
        self.wake_event.set()
        if self.tasks:
            await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        if self.store.snapshot()["project"]["status"] != "achieved":
            self.store.set_project_status("stopped")
