import asyncio
import hashlib
import json
import platform
import shutil
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from experiments.config import ExperimentConfig, ExperimentTask
from experiments.fake_provider import DeterministicFakeExecutor, fake_health_probe
from experiments.metrics import collect_run_metrics, summarize_results
from orchestrator.coordinator import ExecutionCoordinator
from orchestrator.store import BlackboardStore
from orchestrator.supervisor import ProjectSupervisor, SupervisorPolicy
from worker.executor import execute_provider
from worker.models import (
    CoordinatorConfig,
    HealthCheckConfig,
    ProviderConfig,
    WorkerResult,
)


class ModelCallBudget:
    def __init__(self, maximum: int, execute) -> None:
        self.maximum = maximum
        self.execute = execute
        self.used = 0
        self._lock = asyncio.Lock()

    @property
    def exhausted(self) -> bool:
        return self.used >= self.maximum

    async def __call__(
        self,
        provider: ProviderConfig,
        request: str,
        challenge_dir: Path | None,
        work_dir: Path,
        **kwargs,
    ) -> WorkerResult:
        async with self._lock:
            if self.used >= self.maximum:
                allowed = False
            else:
                self.used += 1
                allowed = True
        if allowed:
            return await self.execute(
                provider,
                request,
                challenge_dir,
                work_dir,
                **kwargs,
            )
        work_dir.mkdir(parents=True, exist_ok=True)
        stdout = work_dir / "stdout.txt"
        stderr = work_dir / "stderr.txt"
        stdout.write_text("", encoding="utf-8")
        stderr.write_text("experiment model-call budget exhausted\n", encoding="utf-8")
        return WorkerResult(
            provider=provider.name,
            priority=provider.priority,
            status="cancelled",
            exit_code=None,
            duration_seconds=0.0,
            output="",
            stdout_path=str(stdout.resolve()),
            stderr_path=str(stderr.resolve()),
            work_dir=str(work_dir.resolve()),
            cancel_reason="experiment model-call budget exhausted",
            model="experiment-budget-exhausted",
            cli_version="not-called",
        )


def _fake_providers(names: tuple[str, ...]) -> tuple[ProviderConfig, ...]:
    return tuple(
        ProviderConfig(
            name=name,
            enabled=True,
            priority=100 - index,
            command=("deterministic-fake",),
            timeout_seconds=5,
            planning_timeout_seconds=5,
            health_check=HealthCheckConfig(
                command=("deterministic-fake", "--version"),
                timeout_seconds=1,
                cooldown_seconds=0.001,
                max_cooldown_seconds=0.01,
                failure_threshold=3,
            ),
            max_running=4,
        )
        for index, name in enumerate(names)
    )


def _selected_providers(
    condition: str,
    providers: tuple[ProviderConfig, ...],
) -> tuple[ProviderConfig, ...]:
    if condition == "single_agent_serial":
        return providers[:1]
    if condition == "fixed_roles":
        return providers[:2]
    return providers


def _policy(condition: str) -> SupervisorPolicy:
    return SupervisorPolicy(
        check_stale_plans=condition != "no_stale_check",
        protocol_correction_attempts=(
            0 if condition == "no_protocol_correction" else 1
        ),
        max_executions_per_intent=(
            1 if condition == "single_execution_per_intent" else None
        ),
    )


async def _manual_driver(
    supervisor: ProjectSupervisor,
    condition: str,
    budget: ModelCallBudget,
) -> None:
    names = tuple(supervisor.providers)
    supervisor.store.set_project_status("running")
    for step in range(budget.maximum):
        if supervisor.cancel_event.is_set() or budget.exhausted:
            break
        if condition == "single_agent_serial":
            planner_name = names[0]
            execution_name = names[0]
        elif condition == "fixed_roles":
            planner_name = names[0]
            execution_name = names[1]
        else:
            planner_name = names[step % len(names)]
            execution_name = planner_name
        if not await supervisor.ensure_provider_ready(planner_name):
            continue
        if (
            execution_name != planner_name
            and not await supervisor.ensure_provider_ready(execution_name)
        ):
            continue
        status = await supervisor.run_agent_once(
            planner_name,
            execution_provider_name=execution_name,
        )
        if status == "idle" and condition in {
            "single_agent_serial",
            "fixed_roles",
        }:
            break
    if supervisor.store.snapshot()["project"]["status"] == "running":
        supervisor.store.set_project_status("idle")


async def _drive(
    supervisor: ProjectSupervisor,
    condition: str,
    budget: ModelCallBudget,
) -> None:
    if condition in {
        "single_agent_serial",
        "fixed_roles",
        "round_robin",
    }:
        await _manual_driver(supervisor, condition, budget)
        return
    await supervisor.start()
    await supervisor.wait_until_idle()


def _coordinator_config(config: ExperimentConfig) -> CoordinatorConfig:
    provider_count = max(1, len(config.providers))
    project_limit = min(provider_count, 2)
    global_limit = max(project_limit, config.budget.concurrent_projects * 2)
    planning_limit = max(1, config.budget.concurrent_projects)
    return CoordinatorConfig(
        max_global_running=global_limit,
        max_project_running=project_limit,
        max_global_planning=min(global_limit, planning_limit),
        max_project_planning=min(project_limit, 2),
        aging_seconds=0.01,
    )


def _source_hashes() -> dict[str, str]:
    root = Path(__file__).resolve().parents[1]
    paths = (
        root / "experiments" / "analyze.py",
        root / "experiments" / "config.py",
        root / "experiments" / "fake_provider.py",
        root / "experiments" / "metrics.py",
        root / "experiments" / "runner.py",
        root / "orchestrator" / "supervisor.py",
        root / "orchestrator" / "store.py",
    )
    return {
        str(path.relative_to(root)).replace("\\", "/"): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in paths
    }


async def _run_one(
    *,
    config: ExperimentConfig,
    condition: str,
    task: ExperimentTask,
    repeat: int,
    seed: int,
    output_root: Path,
    providers: tuple[ProviderConfig, ...],
    coordinator: ExecutionCoordinator,
    semaphore: asyncio.Semaphore,
) -> dict[str, object]:
    async with semaphore:
        run_dir = (
            output_root
            / "runs"
            / condition
            / task.id
            / f"repeat-{repeat:02d}-seed-{seed}"
        )
        run_dir.mkdir(parents=True, exist_ok=False)
        project_id = f"experiment-{condition}-{task.id}-{repeat}-{seed}"
        store = BlackboardStore.create(
            run_dir / "blackboard.db",
            project_id,
            task.goal,
            task.origin,
            task.hints,
        )
        selected = _selected_providers(condition, providers)
        if config.mode == "fake":
            underlying_execute = DeterministicFakeExecutor(task)
            health_probe = fake_health_probe
        else:
            underlying_execute = execute_provider
            health_probe = None
        budget = ModelCallBudget(config.budget.max_model_calls, underlying_execute)
        kwargs = {"health_probe": health_probe} if health_probe is not None else {}
        supervisor = ProjectSupervisor(
            store=store,
            providers=selected,
            challenge_dir=None,
            project_dir=run_dir,
            execute=budget,
            coordinator=coordinator,
            max_iterations=config.budget.max_model_calls,
            policy=_policy(condition),
            answer_quorum_provider_count=(1 if condition == "fixed_roles" else None),
            **kwargs,
        )
        started = time.monotonic()
        timed_out = False
        runner_error: str | None = None
        try:
            await asyncio.wait_for(
                _drive(supervisor, condition, budget),
                timeout=config.budget.timeout_seconds,
            )
        except asyncio.TimeoutError:
            timed_out = True
            await supervisor.stop()
        except Exception as exc:
            runner_error = f"{type(exc).__name__}: {exc}"
            store.append_event(
                "experiment.runner_error",
                "experiment",
                {"error": runner_error},
            )
            if store.snapshot()["project"]["status"] == "running":
                store.set_project_status("degraded")
        elapsed = time.monotonic() - started
        try:
            store.export_snapshot(run_dir / "snapshots")
            events = store.events_after(0)
            (run_dir / "events.json").write_text(
                json.dumps(events, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            result = collect_run_metrics(
                store,
                condition=condition,
                task_id=task.id,
                repeat=repeat,
                seed=seed,
                expected_answer=task.expected_answer,
                elapsed_seconds=elapsed,
                actual_model_calls=budget.used,
                max_model_calls=budget.maximum,
                input_cost_per_1k_tokens=config.input_cost_per_1k_tokens,
                output_cost_per_1k_tokens=config.output_cost_per_1k_tokens,
                timed_out=timed_out,
            )
            result["runner_error"] = runner_error
            result["artifact_dir"] = str(run_dir.relative_to(output_root)).replace(
                "\\", "/"
            )
            (run_dir / "result.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
                + "\n",
                encoding="utf-8",
            )
            return result
        finally:
            store.close()


async def run_experiment_suite(
    config: ExperimentConfig,
    output_root: Path,
    *,
    provider_configs: tuple[ProviderConfig, ...] = (),
    allow_real_providers: bool = False,
) -> dict[str, object]:
    if config.mode == "real" and not allow_real_providers:
        raise PermissionError(
            "real provider experiments require allow_real_providers=True"
        )
    if config.mode == "fake":
        providers = _fake_providers(config.providers)
    else:
        by_name = {provider.name: provider for provider in provider_configs}
        missing = set(config.providers) - set(by_name)
        if missing:
            raise ValueError(f"missing real provider configs: {sorted(missing)}")
        providers = tuple(by_name[name] for name in config.providers)

    output_root = output_root.resolve()
    output_root.mkdir(parents=True, exist_ok=False)
    config_payload = asdict(config)
    manifest = {
        "format": "qinyuan-experiment-v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "config": config_payload,
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
        },
        "source_sha256": _source_hashes(),
        "claim_boundary": (
            "fake runs validate the framework and metrics, not real-model quality"
            if config.mode == "fake"
            else "real runs may incur provider cost and require environment review"
        ),
    }
    (output_root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (output_root / "config.json").write_text(
        json.dumps(config_payload, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    shutil.copy2(
        Path(__file__).with_name("analyze.py"),
        output_root / "analysis.py",
    )

    results: list[dict[str, object]] = []
    semaphore = asyncio.Semaphore(config.budget.concurrent_projects)
    for condition in config.conditions:
        coordinator = ExecutionCoordinator(_coordinator_config(config))
        jobs = [
            _run_one(
                config=config,
                condition=condition,
                task=task,
                repeat=repeat,
                seed=config.seed + repeat,
                output_root=output_root,
                providers=providers,
                coordinator=coordinator,
                semaphore=semaphore,
            )
            for repeat in range(config.repetitions)
            for task in config.tasks
        ]
        results.extend(await asyncio.gather(*jobs))

    (output_root / "raw_results.jsonl").write_text(
        "".join(
            json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n"
            for result in results
        ),
        encoding="utf-8",
    )
    summary = summarize_results(
        results,
        concurrent_projects=config.budget.concurrent_projects,
        mode=config.mode,
    )
    (output_root / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return summary
