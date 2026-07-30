import math
from collections import Counter, defaultdict
from datetime import datetime
from statistics import fmean, pvariance

from orchestrator.store import BlackboardStore


def _timestamp(value: object) -> float | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except ValueError:
        return None


def _token_counts(records: tuple[dict[str, object], ...]) -> tuple[int, int, int]:
    input_tokens = 0
    output_tokens = 0
    known_calls = 0
    for record in records:
        usage = record.get("token_usage")
        if not isinstance(usage, dict):
            continue
        raw_input = usage.get("input_tokens", 0)
        raw_output = usage.get("output_tokens", 0)
        if isinstance(raw_input, int) and not isinstance(raw_input, bool):
            input_tokens += max(0, raw_input)
        if isinstance(raw_output, int) and not isinstance(raw_output, bool):
            output_tokens += max(0, raw_output)
        known_calls += 1
    return input_tokens, output_tokens, known_calls


def jain_fairness(values: list[float]) -> float | None:
    usable = [max(0.0, value) for value in values]
    if not usable or not any(usable):
        return None
    numerator = sum(usable) ** 2
    denominator = len(usable) * sum(value * value for value in usable)
    return numerator / denominator if denominator else None


def collect_run_metrics(
    store: BlackboardStore,
    *,
    condition: str,
    task_id: str,
    repeat: int,
    seed: int,
    expected_answer: str,
    elapsed_seconds: float,
    actual_model_calls: int,
    max_model_calls: int,
    input_cost_per_1k_tokens: float,
    output_cost_per_1k_tokens: float,
    timed_out: bool,
) -> dict[str, object]:
    snapshot = store.snapshot()
    project = snapshot["project"]
    planning = store.planning_attempts()
    executions = store.executions()
    events = store.events_after(0)
    records = planning + executions
    input_tokens, output_tokens, known_token_calls = _token_counts(records)

    created_at = _timestamp(project["created_at"])
    fact_times = [
        timestamp
        for node in snapshot["nodes"]
        if node["type"] == "fact"
        for timestamp in [_timestamp(node["created_at"])]
        if timestamp is not None
    ]
    achieved_times = [
        timestamp
        for event in events
        if event["type"] == "project.status_changed"
        and event["payload"].get("status") == "achieved"
        for timestamp in [_timestamp(event["created_at"])]
        if timestamp is not None
    ]

    intent_edges = [edge for edge in snapshot["edges"] if edge["type"] == "intent"]
    duplicate_intents = sum(
        max(0, len(edge.get("sources", [])) - 1) for edge in intent_edges
    )
    invalid_statuses = {
        "failed",
        "protocol_error",
        "timeout",
        "unavailable",
        "interrupted",
    }
    invalid_executions = sum(
        execution["status"] in invalid_statuses for execution in executions
    )
    protocol_errors = sum(
        record["status"] == "protocol_error" for record in records
    )
    stale_plans = sum(attempt["status"] == "stale" for attempt in planning)
    skipped_executions = sum(
        event["type"] == "intent.execution_skipped" for event in events
    )

    failure_times = [
        _timestamp(event["created_at"])
        for event in events
        if event["type"] == "provider.health_changed"
        and event["payload"].get("status") in {
            "cooldown",
            "unhealthy",
            "auth_failed",
            "missing",
        }
    ]
    recovery_times = [
        _timestamp(event["created_at"])
        for event in events
        if event["type"] == "provider.recovered"
    ]
    valid_failures = [value for value in failure_times if value is not None]
    valid_recoveries = [value for value in recovery_times if value is not None]
    recovery_seconds = None
    if valid_failures and valid_recoveries:
        first_failure = min(valid_failures)
        after = [value for value in valid_recoveries if value >= first_failure]
        if after:
            recovery_seconds = min(after) - first_failure

    final_answer = project.get("final_answer")
    final_rows = store.connection.execute(
        """
        SELECT provider, trim(answer) AS answer
        FROM final_answers
        WHERE goal_version = ?
        ORDER BY created_at, id
        """,
        (project["goal_version"],),
    ).fetchall()
    answer_counts = Counter(str(row["answer"]) for row in final_rows)
    completed_executions = sum(
        execution["status"] == "completed" for execution in executions
    )
    remaining_after_failure = None
    if valid_failures:
        first_failure = min(valid_failures)
        later = [
            execution
            for execution in executions
            if (_timestamp(execution.get("finished_at")) or 0) >= first_failure
        ]
        if later:
            remaining_after_failure = sum(
                execution["status"] == "completed" for execution in later
            ) / len(later)

    estimated_cost = (
        input_tokens / 1000 * input_cost_per_1k_tokens
        + output_tokens / 1000 * output_cost_per_1k_tokens
    )
    return {
        "condition": condition,
        "task_id": task_id,
        "repeat": repeat,
        "seed": seed,
        "status": project["status"],
        "timed_out": timed_out,
        "success": project["status"] == "achieved",
        "answer_correct": final_answer == expected_answer,
        "final_answer": final_answer,
        "expected_answer": expected_answer,
        "total_seconds": elapsed_seconds,
        "first_fact_seconds": (
            max(0.0, min(fact_times) - created_at)
            if fact_times and created_at is not None
            else None
        ),
        "goal_seconds": (
            max(0.0, min(achieved_times) - created_at)
            if achieved_times and created_at is not None
            else None
        ),
        "model_calls": actual_model_calls,
        "model_call_budget": max_model_calls,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "known_token_calls": known_token_calls,
        "estimated_cost": estimated_cost,
        "planning_attempts": len(planning),
        "execution_attempts": len(executions),
        "completed_executions": completed_executions,
        "duplicate_intents": duplicate_intents,
        "invalid_executions": invalid_executions,
        "protocol_errors": protocol_errors,
        "stale_plans": stale_plans,
        "skipped_executions": skipped_executions,
        "provider_recovery_seconds": recovery_seconds,
        "remaining_task_completion_rate": remaining_after_failure,
        "throughput_executions_per_second": (
            completed_executions / elapsed_seconds if elapsed_seconds > 0 else None
        ),
        "answer_signal": {
            "proposal_count": len(final_rows),
            "unique_answers": len(answer_counts),
            "largest_agreement": max(answer_counts.values(), default=0),
            "consensus_reached": project["status"] == "achieved",
            "is_correct": final_answer == expected_answer,
        },
    }


SUMMARY_NUMBERS = (
    "total_seconds",
    "first_fact_seconds",
    "goal_seconds",
    "model_calls",
    "input_tokens",
    "output_tokens",
    "estimated_cost",
    "planning_attempts",
    "execution_attempts",
    "completed_executions",
    "duplicate_intents",
    "invalid_executions",
    "protocol_errors",
    "stale_plans",
    "skipped_executions",
    "provider_recovery_seconds",
    "remaining_task_completion_rate",
    "throughput_executions_per_second",
)


def summarize_results(
    results: list[dict[str, object]],
    *,
    concurrent_projects: int,
    mode: str,
) -> dict[str, object]:
    grouped: dict[str, list[dict[str, object]]] = defaultdict(list)
    for result in results:
        grouped[str(result["condition"])].append(result)
    conditions: dict[str, object] = {}
    for condition, rows in grouped.items():
        numeric: dict[str, object] = {}
        for key in SUMMARY_NUMBERS:
            values = [
                float(row[key])
                for row in rows
                if isinstance(row.get(key), (int, float))
                and not isinstance(row.get(key), bool)
                and math.isfinite(float(row[key]))
            ]
            numeric[key] = {
                "count": len(values),
                "mean": fmean(values) if values else None,
                "variance": pvariance(values) if values else None,
            }
        completed_by_run = [float(row["completed_executions"]) for row in rows]
        conditions[condition] = {
            "runs": len(rows),
            "success_rate": sum(bool(row["success"]) for row in rows) / len(rows),
            "answer_accuracy": sum(bool(row["answer_correct"]) for row in rows)
            / len(rows),
            "failure_distribution": dict(
                sorted(Counter(str(row["status"]) for row in rows).items())
            ),
            "project_fairness_jain": jain_fairness(completed_by_run),
            "metrics": numeric,
        }
    return {
        "format": "qinyuan-experiment-summary-v1",
        "mode": mode,
        "run_count": len(results),
        "concurrent_projects": concurrent_projects,
        "conditions": conditions,
        "evidence_boundary": (
            "deterministic fake-provider results validate reproducibility and "
            "metric collection only; they are not evidence of real-model quality"
            if mode == "fake"
            else "real-provider results depend on the recorded model and CLI environment"
        ),
    }
