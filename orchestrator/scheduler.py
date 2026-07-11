import asyncio
import sys
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from orchestrator.models import (
    AgentRoundResult,
    OrchestrationResult,
    ProtocolResult,
)
from orchestrator.protocol import parse_update, render_request
from orchestrator.store import BlackboardStore
from worker.executor import execute_provider
from worker.models import ProviderConfig, WorkerResult


Executor = Callable[
    [ProviderConfig, str, Path | None, Path],
    Awaitable[WorkerResult],
]


def _new_run_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{uuid4().hex[:8]}"


def _node_ids(snapshot: dict[str, object]) -> set[str]:
    return {node["id"] for node in snapshot["nodes"]}


async def _run_agent(
    config: ProviderConfig,
    request: str,
    challenge_dir: Path | None,
    work_dir: Path,
    existing_node_ids: set[str],
    execute: Executor,
) -> AgentRoundResult:
    result = await execute(config, request, challenge_dir, work_dir)
    protocol = (
        parse_update(result.output, existing_node_ids)
        if result.status == "success"
        else ProtocolResult(
            status="not_attempted",
            update=None,
            error=f"process status was {result.status}",
        )
    )
    return AgentRoundResult(
        provider=config.name,
        process_status=result.status,
        protocol_status=protocol.status,
        exit_code=result.exit_code,
        duration_seconds=result.duration_seconds,
        output=result.output,
        stdout_path=result.stdout_path,
        stderr_path=result.stderr_path,
        work_dir=result.work_dir,
        update=protocol.update,
        protocol_error=protocol.error,
    )


async def run_orchestration(
    providers: tuple[ProviderConfig, ...],
    goal: str,
    origin: str,
    hints: tuple[str, ...],
    challenge_dir: Path | None,
    runs_root: Path,
    max_rounds: int = 5,
    execute: Executor = execute_provider,
    hint_reader: Callable[[str], str] = input,
    interactive: bool | None = None,
) -> OrchestrationResult:
    run_id = _new_run_id()
    run_dir = runs_root / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    store = BlackboardStore.create(
        run_dir / "blackboard.db",
        run_id,
        max_rounds,
        goal,
        origin,
        hints,
    )
    enabled = tuple(provider for provider in providers if provider.enabled)
    is_interactive = sys.stdin.isatty() if interactive is None else interactive
    rounds_completed = 0
    final_answer: str | None = None
    status = "no_usable_providers" if not enabled else "running"

    try:
        store.export_snapshot(0, run_dir / "snapshots")
        for round_number in range(1, max_rounds + 1):
            if not enabled:
                break

            store.export_snapshot(round_number, run_dir / "snapshots")
            snapshot = store.snapshot()
            request = render_request(snapshot, round_number, challenge_dir)
            existing_node_ids = _node_ids(snapshot)
            results = await asyncio.gather(
                *(
                    _run_agent(
                        config,
                        request,
                        challenge_dir,
                        run_dir
                        / "rounds"
                        / f"{round_number:03d}"
                        / config.name,
                        existing_node_ids,
                        execute,
                    )
                    for config in enabled
                )
            )
            rounds_completed = round_number
            available_agents = sum(
                result.process_status != "unavailable" for result in results
            )
            valid_updates = []
            for result in results:
                answer = (
                    result.update.final_answer
                    if result.update and result.update.goal_achieved
                    else None
                )
                store.record_agent_result(
                    round_number,
                    result.provider,
                    result.process_status,
                    result.protocol_status,
                    result.exit_code,
                    result.duration_seconds,
                    result.stdout_path,
                    result.stderr_path,
                    result.work_dir,
                    answer,
                )
                if result.update is not None:
                    valid_updates.append((result.provider, result.update))

            store.apply_round(round_number, tuple(valid_updates))
            if available_agents == 0:
                status = "no_usable_providers"
                break

            final_answer = store.consensus(round_number, available_agents)
            if final_answer is not None:
                status = "achieved"
                break

            if round_number < max_rounds and is_interactive:
                hint = hint_reader("Hint (blank to continue): ").strip()
                if hint:
                    store.add_hint(hint, round_number)

        if status == "running":
            status = (
                "unresolved"
                if len(store.candidates()) > 1
                else "max_rounds_reached"
            )
        store.finish(status, final_answer, rounds_completed)
        return OrchestrationResult(
            run_id=run_id,
            run_dir=str(run_dir.resolve()),
            status=status,
            rounds_completed=rounds_completed,
            final_answer=final_answer,
            candidates=store.candidates(),
        )
    finally:
        store.close()
