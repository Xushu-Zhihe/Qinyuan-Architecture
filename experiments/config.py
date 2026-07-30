import json
from dataclasses import dataclass, field
from pathlib import Path


CONDITIONS = (
    "single_agent_serial",
    "fixed_roles",
    "round_robin",
    "role_free_async",
    "no_stale_check",
    "no_protocol_correction",
    "single_execution_per_intent",
)


class ExperimentConfigError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class FakeBehavior:
    requires_protocol_correction: bool = False
    execution_failures_each: int = 0
    planning_delay_ms: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ExperimentTask:
    id: str
    goal: str
    origin: str
    expected_answer: str
    hints: tuple[str, ...] = ()
    fake: FakeBehavior = field(default_factory=FakeBehavior)


@dataclass(frozen=True, slots=True)
class ExperimentBudget:
    max_model_calls: int
    timeout_seconds: float
    concurrent_projects: int


@dataclass(frozen=True, slots=True)
class ExperimentConfig:
    name: str
    mode: str
    seed: int
    repetitions: int
    providers: tuple[str, ...]
    conditions: tuple[str, ...]
    budget: ExperimentBudget
    tasks: tuple[ExperimentTask, ...]
    input_cost_per_1k_tokens: float = 0.0
    output_cost_per_1k_tokens: float = 0.0


def _non_empty(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExperimentConfigError(f"{field_name} must be a non-empty string")
    return value.strip()


def _positive_int(value: object, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ExperimentConfigError(f"{field_name} must be a positive integer")
    return value


def _positive_number(value: object, field_name: str) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or value <= 0
    ):
        raise ExperimentConfigError(f"{field_name} must be positive")
    return float(value)


def _non_negative_number(value: object, field_name: str) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or value < 0
    ):
        raise ExperimentConfigError(f"{field_name} must be non-negative")
    return float(value)


def _string_list(value: object, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ExperimentConfigError(
            f"{field_name} must be a list of non-empty strings"
        )
    return tuple(item.strip() for item in value)


def _parse_fake(value: object, field_name: str) -> FakeBehavior:
    if value is None:
        return FakeBehavior()
    if not isinstance(value, dict):
        raise ExperimentConfigError(f"{field_name} must be an object")
    correction = value.get("requires_protocol_correction", False)
    if not isinstance(correction, bool):
        raise ExperimentConfigError(
            f"{field_name}.requires_protocol_correction must be a boolean"
        )
    failures = value.get("execution_failures_each", 0)
    if (
        not isinstance(failures, int)
        or isinstance(failures, bool)
        or failures < 0
    ):
        raise ExperimentConfigError(
            f"{field_name}.execution_failures_each must be non-negative"
        )
    delays = value.get("planning_delay_ms", {})
    if not isinstance(delays, dict) or any(
        not isinstance(name, str)
        or not isinstance(delay, int)
        or isinstance(delay, bool)
        or delay < 0
        for name, delay in delays.items()
    ):
        raise ExperimentConfigError(
            f"{field_name}.planning_delay_ms must map providers to "
            "non-negative integers"
        )
    return FakeBehavior(correction, failures, dict(delays))


def _parse_task(value: object, index: int) -> ExperimentTask:
    if not isinstance(value, dict):
        raise ExperimentConfigError(f"tasks[{index}] must be an object")
    prefix = f"tasks[{index}]"
    hints = _string_list(value.get("hints", []), f"{prefix}.hints")
    return ExperimentTask(
        id=_non_empty(value.get("id"), f"{prefix}.id"),
        goal=_non_empty(value.get("goal"), f"{prefix}.goal"),
        origin=_non_empty(value.get("origin"), f"{prefix}.origin"),
        expected_answer=_non_empty(
            value.get("expected_answer"),
            f"{prefix}.expected_answer",
        ),
        hints=hints,
        fake=_parse_fake(value.get("fake"), f"{prefix}.fake"),
    )


def load_experiment_config(path: Path) -> ExperimentConfig:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ExperimentConfigError(f"cannot read experiment config: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ExperimentConfigError(f"invalid experiment JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise ExperimentConfigError("experiment config must be an object")

    mode = payload.get("mode", "fake")
    if mode not in {"fake", "real"}:
        raise ExperimentConfigError("mode must be fake or real")
    providers = _string_list(payload.get("providers"), "providers")
    conditions = _string_list(
        payload.get("conditions", list(CONDITIONS)),
        "conditions",
    )
    unknown = set(conditions) - set(CONDITIONS)
    if unknown:
        raise ExperimentConfigError(f"unknown conditions: {sorted(unknown)}")
    if len(set(conditions)) != len(conditions):
        raise ExperimentConfigError("conditions must be unique")
    if "fixed_roles" in conditions and len(providers) < 2:
        raise ExperimentConfigError("fixed_roles requires at least two providers")

    raw_budget = payload.get("budget")
    if not isinstance(raw_budget, dict):
        raise ExperimentConfigError("budget must be an object")
    budget = ExperimentBudget(
        max_model_calls=_positive_int(
            raw_budget.get("max_model_calls"),
            "budget.max_model_calls",
        ),
        timeout_seconds=_positive_number(
            raw_budget.get("timeout_seconds"),
            "budget.timeout_seconds",
        ),
        concurrent_projects=_positive_int(
            raw_budget.get("concurrent_projects", 1),
            "budget.concurrent_projects",
        ),
    )

    raw_tasks = payload.get("tasks")
    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise ExperimentConfigError("tasks must be a non-empty list")
    tasks = tuple(_parse_task(value, index) for index, value in enumerate(raw_tasks))
    task_ids = [task.id for task in tasks]
    if len(task_ids) != len(set(task_ids)):
        raise ExperimentConfigError("task ids must be unique")

    return ExperimentConfig(
        name=_non_empty(payload.get("name"), "name"),
        mode=mode,
        seed=int(payload.get("seed", 0)),
        repetitions=_positive_int(payload.get("repetitions"), "repetitions"),
        providers=providers,
        conditions=conditions,
        budget=budget,
        tasks=tasks,
        input_cost_per_1k_tokens=_non_negative_number(
            payload.get("input_cost_per_1k_tokens", 0),
            "input_cost_per_1k_tokens",
        ),
        output_cost_per_1k_tokens=_non_negative_number(
            payload.get("output_cost_per_1k_tokens", 0),
            "output_cost_per_1k_tokens",
        ),
    )
