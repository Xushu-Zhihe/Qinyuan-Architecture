import asyncio
import inspect
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from orchestrator.coordinator import ExecutionCoordinator, PermitCancelled
from orchestrator.events import ProjectEventHub
from orchestrator.fact_compaction_candidates import FactCandidate
from orchestrator.fact_compaction_llm import execute_fact_compaction_llm
from orchestrator.fact_compaction_repository import FactCompactionRepository
from orchestrator.store import BlackboardStore
from worker.executor import execute_provider
from worker.models import FactCompactionConfig, ProviderConfig, WorkerResult


Executor = Callable[..., Awaitable[WorkerResult]]


@dataclass(frozen=True, slots=True)
class FactCompactionGroup:
    source_ids: tuple[str, ...]
    summary: str


def render_fact_compaction_request(
    facts: tuple[FactCandidate, ...],
    max_summary_chars: int,
) -> str:
    payload = [
        {"id": fact.id, "content": fact.content}
        for fact in facts
    ]
    return (
        "You are an independent blackboard Fact compactor. Merge only facts "
        "that express the same durable information. Preserve disagreements, "
        "specific values, qualifications, and uncertainty; do not invent new "
        "claims. A candidate Fact may contain several independent important "
        "points. Split those points into separate single-topic summaries. The "
        "same source Fact may appear in multiple groups when it supports more "
        "than one point. Do not combine unrelated points into one summary. "
        "Leave conflicts and unsupported singletons unmerged. Return exactly "
        "one JSON object and no Markdown: "
        '{"action":"merge","groups":[{"source_ids":['
        '"fact-id","fact-id"],"summary":"one atomic point"}]} or '
        '{"action":"skip","reason":"..."}. '
        f"Each summary must be at most {max_summary_chars} characters.\n"
        "Candidate facts:\n"
        + json.dumps(payload, ensure_ascii=False)
    )


def parse_fact_compaction_response(
    text: str,
    max_summary_chars: int,
    available_fact_ids: set[str],
) -> tuple[FactCompactionGroup, ...]:
    stripped = text.strip()
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        start, end = stripped.find("{"), stripped.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("fact compactor did not return a JSON object")
        try:
            payload = json.loads(stripped[start : end + 1])
        except json.JSONDecodeError as exc:
            raise ValueError("fact compactor returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("fact compactor result must be an object")
    action = payload.get("action")
    if action == "skip":
        return ()
    if action != "merge":
        raise ValueError("fact compactor action must be merge or skip")
    raw_groups = payload.get("groups")
    if not isinstance(raw_groups, list) or not raw_groups:
        raise ValueError("fact compactor merge requires non-empty groups")
    groups: list[FactCompactionGroup] = []
    seen: set[tuple[tuple[str, ...], str]] = set()
    for raw_group in raw_groups:
        if not isinstance(raw_group, dict):
            raise ValueError("fact compactor group must be an object")
        summary = raw_group.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            raise ValueError("fact compactor group requires a summary")
        summary = " ".join(summary.strip().split())
        if len(summary) > max_summary_chars:
            raise ValueError(
                "fact compactor summary exceeds the configured limit"
            )
        source_ids = raw_group.get("source_ids")
        if not isinstance(source_ids, list) or any(
            not isinstance(item, str) or not item.strip()
            for item in source_ids
        ):
            raise ValueError("fact compactor group requires source_ids")
        selected = tuple(dict.fromkeys(item.strip() for item in source_ids))
        if len(selected) < 2:
            raise ValueError(
                "fact compactor group must select at least two source Facts"
            )
        if not set(selected).issubset(available_fact_ids):
            raise ValueError("fact compactor selected an unknown source Fact")
        key = (tuple(sorted(selected)), summary.casefold())
        if key in seen:
            raise ValueError("fact compactor returned a duplicate group")
        seen.add(key)
        groups.append(FactCompactionGroup(selected, summary))
    return tuple(groups)


class FactCompactor:
    """Asynchronously compact similar Facts without joining solver consensus."""

    def __init__(
        self,
        *,
        store: BlackboardStore,
        project_dir: Path,
        config: FactCompactionConfig,
        hub: ProjectEventHub | None = None,
        coordinator: ExecutionCoordinator | None = None,
        execute: Executor = execute_provider,
        write_lock: asyncio.Lock | None = None,
        on_graph_changed: Callable[[], None] | None = None,
    ) -> None:
        if not config.enabled or (config.provider is None and config.llm is None):
            raise ValueError("FactCompactor requires an enabled configuration")
        self.store = store
        self.repository = FactCompactionRepository(
            store.connection,
            store.project_id,
        )
        self.project_dir = project_dir
        self.config = config
        self.provider = config.provider
        self.llm = config.llm
        self.hub = hub or ProjectEventHub()
        self.coordinator = coordinator or ExecutionCoordinator()
        self.execute = execute
        self._execute_accepts_cancel = self._accepts_keyword(
            execute,
            "cancel_event",
        )
        self.write_lock = write_lock or asyncio.Lock()
        self.on_graph_changed = on_graph_changed
        self.cancel_event = asyncio.Event()
        self.wake_event = asyncio.Event()
        self.task: asyncio.Task[None] | None = None
        self._rejected_batches: set[tuple[str, ...]] = set()
        self._busy = False
        self._last_error: str | None = None

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

    async def start(self) -> None:
        if self.task is not None and not self.task.done():
            return
        self.cancel_event.clear()
        self.task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self.cancel_event.set()
        self.wake_event.set()
        if self.task is not None:
            await asyncio.gather(self.task, return_exceptions=True)

    def wake(self) -> None:
        self.wake_event.set()

    def status(self) -> dict[str, object]:
        task = self.task
        if task is None or task.done():
            state = "stopped"
        elif self._busy:
            state = "compacting"
        elif self._last_error:
            state = "error"
        else:
            state = "waiting"
        pending = self.repository.candidates(
            min_facts=self.config.min_facts,
            max_facts=self.config.max_facts,
        )
        return {
            "enabled": True,
            "status": state,
            "provider": self.provider.name if self.provider else "summary-llm",
            "model": self.provider.name if self.provider else self.llm.model,
            "pending_groups": len(pending),
            "rejected_batches": len(self._rejected_batches),
            "last_error": self._last_error,
        }

    def _pending_group(self) -> tuple[FactCandidate, ...] | None:
        groups = self.repository.candidates(
            min_facts=self.config.min_facts,
            max_facts=self.config.max_facts,
        )
        return next(
            (
                group
                for group in groups
                if tuple(sorted(item.id for item in group))
                not in self._rejected_batches
            ),
            None,
        )

    async def wait_until_idle(self, timeout: float | None = None) -> None:
        async def wait() -> None:
            while self._busy or self._pending_group() is not None:
                self.wake()
                await asyncio.sleep(0.05)

        await asyncio.wait_for(wait(), timeout=timeout)

    async def _publish_events(self, sequence: int) -> None:
        for event in self.store.events_after(sequence):
            await self.hub.publish(event)

    async def _record_event(
        self,
        event_type: str,
        payload: dict[str, object],
    ) -> None:
        async with self.write_lock:
            event = self.store.append_event(
                event_type,
                self.provider.name if self.provider else "summary-llm",
                payload,
            )
        await self.hub.publish(event)

    async def _run_provider(
        self,
        request: str,
        work_dir: Path,
    ) -> WorkerResult:
        try:
            if self.llm is not None:
                async with self.coordinator.permit(
                    project_id=self.store.project_id,
                    provider="summary-llm",
                    kind="execution",
                    priority=-100,
                    provider_limit=1,
                    cancel_event=self.cancel_event,
                ):
                    return await execute_fact_compaction_llm(
                        self.llm,
                        request,
                        work_dir,
                        cancel_event=self.cancel_event,
                    )
            assert self.provider is not None
            async with self.coordinator.permit(
                project_id=self.store.project_id,
                provider=self.provider.name,
                kind="execution",
                priority=self.provider.priority,
                provider_limit=self.provider.max_running,
                cancel_event=self.cancel_event,
            ):
                kwargs = (
                    {"cancel_event": self.cancel_event}
                    if self._execute_accepts_cancel
                    else {}
                )
                return await self.execute(
                    self.provider,
                    request,
                    None,
                    work_dir,
                    **kwargs,
                )
        except PermitCancelled:
            raise asyncio.CancelledError

    async def _compact(self, facts: tuple[FactCandidate, ...]) -> None:
        ids = tuple(sorted(fact.id for fact in facts))
        if ids in self._rejected_batches:
            return
        work_dir = (
            self.project_dir
            / "fact-compactions"
            / f"run-{self.provider.name if self.provider else 'summary-llm'}-{uuid4().hex}"
        )
        await self._record_event(
            "graph.fact_compaction_started",
            {"source_node_ids": list(ids), "source_count": len(ids)},
        )
        result = await self._run_provider(
            render_fact_compaction_request(
                facts,
                self.config.max_summary_chars,
            ),
            work_dir,
        )
        if result.status != "success":
            self._last_error = f"provider returned {result.status}"
            await self._record_event(
                "graph.fact_compaction_failed",
                {
                    "source_node_ids": list(ids),
                    "status": result.status,
                    "exit_code": result.exit_code,
                },
            )
            return
        try:
            decision = parse_fact_compaction_response(
                result.protocol_text,
                self.config.max_summary_chars,
                set(ids),
            )
        except ValueError as exc:
            self._last_error = str(exc)
            self._rejected_batches.add(ids)
            await self._record_event(
                "graph.fact_compaction_failed",
                {"source_node_ids": list(ids), "error": str(exc)},
            )
            return
        if not decision:
            self._rejected_batches.add(ids)
            await self._record_event(
                "graph.fact_compaction_skipped",
                {"source_node_ids": list(ids)},
            )
            return
        async with self.write_lock:
            sequence = int(self.store.snapshot()["sequence"])
            try:
                summary_ids = self.repository.apply_groups(
                    tuple(
                        (group.source_ids, group.summary)
                        for group in decision
                    ),
                )
            except ValueError as exc:
                self._last_error = str(exc)
                summary_ids = ()
                self.store.append_event(
                    "graph.fact_compaction_stale",
                    self.provider.name if self.provider else "summary-llm",
                    {"source_node_ids": list(ids), "error": str(exc)},
                )
        await self._publish_events(sequence)
        if summary_ids:
            self._last_error = None
            self.wake_event.set()
            if self.on_graph_changed is not None:
                self.on_graph_changed()

    async def _run(self) -> None:
        while not self.cancel_event.is_set():
            pending = self._pending_group()
            if pending is not None:
                try:
                    self._busy = True
                    await self._compact(pending)
                except asyncio.CancelledError:
                    return
                except Exception as exc:
                    self._last_error = str(exc)[:500]
                    await self._record_event(
                        "graph.fact_compaction_failed",
                        {"error_type": type(exc).__name__, "error": str(exc)[:500]},
                    )
                finally:
                    self._busy = False
                continue
            self.wake_event.clear()
            try:
                await asyncio.wait_for(
                    self.wake_event.wait(),
                    timeout=self.config.poll_interval_seconds,
                )
            except asyncio.TimeoutError:
                pass
