from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from orchestrator.coordinator import ExecutionCoordinator
from orchestrator.models import OrchestrationResult
from orchestrator.store import BlackboardStore
from orchestrator.supervisor import ProjectSupervisor
from worker.executor import execute_provider
from worker.models import (
    AuditConfig,
    CoordinatorConfig,
    ProviderConfig,
    WorkerResult,
)


Executor = Callable[..., Awaitable[WorkerResult]]


def _new_project_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{uuid4().hex[:8]}"


async def run_orchestration(
    providers: tuple[ProviderConfig, ...],
    goal: str,
    origin: str,
    hints: tuple[str, ...],
    challenge_dir: Path | None,
    runs_root: Path,
    max_iterations: int = 5,
    execute: Executor = execute_provider,
    coordinator_config: CoordinatorConfig | None = None,
    audit_config: AuditConfig | None = None,
) -> OrchestrationResult:
    project_id = _new_project_id()
    run_dir = runs_root / project_id
    run_dir.mkdir(parents=True, exist_ok=False)
    store = BlackboardStore.create(
        run_dir / "blackboard.db",
        project_id,
        goal,
        origin,
        hints,
    )
    enabled = tuple(provider for provider in providers if provider.enabled)
    if not enabled:
        store.set_project_status("no_usable_providers")
        snapshot = store.snapshot()
        store.close()
        return _result(run_dir, snapshot)

    supervisor = ProjectSupervisor(
        store=store,
        providers=enabled,
        challenge_dir=challenge_dir,
        project_dir=run_dir,
        execute=execute,
        coordinator=ExecutionCoordinator(coordinator_config),
        audit_config=audit_config,
        max_iterations=max_iterations,
    )
    try:
        await supervisor.start()
        await supervisor.wait_until_idle()
        snapshot = store.snapshot()
        if snapshot["project"]["status"] == "idle" and len(
            snapshot["candidates"]
        ) > 1:
            store.set_project_status("unresolved")
            snapshot = store.snapshot()
        store.export_snapshot(run_dir / "snapshots")
        return _result(run_dir, snapshot)
    finally:
        store.close()


def _result(
    run_dir: Path,
    snapshot: dict[str, object],
) -> OrchestrationResult:
    project = snapshot["project"]
    return OrchestrationResult(
        project_id=str(project["id"]),
        run_dir=str(run_dir.resolve()),
        status=str(project["status"]),
        highest_generation=int(snapshot["highest_generation"]),
        final_answer=(
            str(project["final_answer"])
            if project["final_answer"] is not None
            else None
        ),
        candidates=tuple(str(item) for item in snapshot["candidates"]),
    )
