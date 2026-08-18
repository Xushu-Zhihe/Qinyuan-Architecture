from dataclasses import dataclass, field
from pathlib import Path
import json
import re
from typing import Literal, Mapping
from urllib.parse import urlsplit

from autopilot.platform_models import StructuredPlatformConfig


CONTROLLER_MODEL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")
CONTROLLER_EXTRA_BODY_FIELDS = frozenset(
    {
        "top_p",
        "stop",
        "presence_penalty",
        "frequency_penalty",
        "seed",
        "response_format",
        "user",
        "logprobs",
        "top_logprobs",
        "parallel_tool_calls",
        "service_tier",
    }
)
CONTROLLER_RESERVED_BODY_FIELDS = frozenset(
    {"model", "messages", "tools", "tool_choice", "temperature", "max_tokens", "reasoning_effort"}
)
MAX_AGENT_LIMIT = 16
MAX_AUTOPILOT_TASKS = 1000
MAX_TASK_TIMEOUT_SECONDS = 24 * 60 * 60
MAX_POLL_INTERVAL_SECONDS = 60 * 60
MAX_RETRY_COUNT = 10
MAX_RESULT_FACT_CONTENT_LENGTH = 1_000_000
MAX_SUPPORTING_FACT_CONTENT_LENGTH = 20_000


@dataclass(frozen=True, slots=True, repr=False)
class ControllerConfig:
    base_url: str
    model: str
    timeout_seconds: float = 300.0
    max_rounds: int = 20
    max_transport_retries: int = 2
    temperature: float | None = None
    max_output_tokens: int | None = None
    reasoning_effort: str | None = None
    extra_headers: tuple[tuple[str, str], ...] = ()
    extra_body: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        base = self.base_url.strip().rstrip("/")
        if base.endswith("/chat/completions"):
            base = base[: -len("/chat/completions")]
        parsed = urlsplit(base)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("controller base_url must be an absolute HTTP URL")
        model = self.model.strip()
        if not model:
            raise ValueError("controller model must be non-empty")
        if len(model) > 200 or CONTROLLER_MODEL_PATTERN.fullmatch(model) is None:
            raise ValueError(
                "controller model must be 1-200 characters using letters, numbers, '.', '_', ':', '/', or '-'"
            )
        if self.timeout_seconds <= 0 or self.timeout_seconds > 1800:
            raise ValueError("controller timeout_seconds must be between 0 and 1800")
        if self.max_rounds < 1 or self.max_rounds > 50:
            raise ValueError("controller max_rounds must be between 1 and 50")
        if self.max_transport_retries < 0 or self.max_transport_retries > MAX_RETRY_COUNT:
            raise ValueError(
                f"controller max_transport_retries must be between 0 and {MAX_RETRY_COUNT}"
            )
        if self.temperature is not None and not 0 <= self.temperature <= 2:
            raise ValueError("controller temperature must be between 0 and 2")
        if self.max_output_tokens is not None and not 1 <= self.max_output_tokens <= 200000:
            raise ValueError("controller max_output_tokens must be between 1 and 200000")
        if self.reasoning_effort is not None:
            effort = self.reasoning_effort.strip()
            if not effort or len(effort) > 40:
                raise ValueError("controller reasoning_effort must be 1-40 characters")
            object.__setattr__(self, "reasoning_effort", effort)
        if not isinstance(self.extra_body, Mapping):
            raise ValueError("controller extra_body must be an object")
        body = dict(self.extra_body)
        unknown = sorted(set(body) - CONTROLLER_EXTRA_BODY_FIELDS)
        if unknown:
            raise ValueError(f"unsupported controller extra_body fields: {unknown}")
        reserved = sorted(set(body) & CONTROLLER_RESERVED_BODY_FIELDS)
        if reserved:
            raise ValueError(f"controller extra_body cannot override request fields: {reserved}")
        try:
            json.dumps(body, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("controller extra_body must be JSON serializable") from exc
        for header in self.extra_headers:
            if not isinstance(header, tuple) or len(header) != 2:
                raise ValueError("controller extra_headers must contain name/value pairs")
            if not str(header[0]).strip():
                raise ValueError("controller extra header names must be non-empty")
        object.__setattr__(self, "base_url", base)
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "extra_body", body)

    @property
    def chat_completions_url(self) -> str:
        return f"{self.base_url}/chat/completions"

    def __repr__(self) -> str:
        return (
            "ControllerConfig("
            f"base_url={self.base_url!r}, model={self.model!r}, "
            f"timeout_seconds={self.timeout_seconds!r}, max_rounds={self.max_rounds!r})"
        )


class RuntimeControllerSecret:
    __slots__ = ("api_key",)

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    def __repr__(self) -> str:
        return "RuntimeControllerSecret(api_key='[redacted]')"

    __str__ = __repr__

    def clear(self) -> None:
        self.api_key = ""


class RuntimePlatformSecret:
    __slots__ = ("prompt", "token")

    def __init__(self, prompt: str = "", token: str = "") -> None:
        self.prompt = prompt
        self.token = token

    def __repr__(self) -> str:
        return "RuntimePlatformSecret(prompt='[redacted]', token='[redacted]')"

    __str__ = __repr__

    def clear(self) -> None:
        self.prompt = ""
        self.token = ""


@dataclass(frozen=True, slots=True, repr=False)
class PromptPlatformConfig:
    prompt: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "prompt", self.prompt.strip())

    def __repr__(self) -> str:
        return "PromptPlatformConfig(prompt='[redacted]')"


@dataclass(frozen=True, slots=True, repr=False)
class AutoStartConfig:
    # The first three fields retain source compatibility with the legacy API.
    platform_prompt: str = ""
    controller_provider: str = ""
    solver_providers: tuple[str, ...] = ()
    agent_limit: int = 2
    max_tasks: int = 10
    task_timeout_seconds: float = 3600.0
    controller_timeout_seconds: float = 300.0
    poll_interval_seconds: float = 1.0
    mode: Literal["prompt", "structured"] = "prompt"
    controller: ControllerConfig | None = None
    platform: object | None = None
    rejected_action: Literal["replan", "skip", "fail"] = "fail"
    solver_failure_action: Literal["skip", "fail"] = "fail"
    stop_active_task_action: Literal["leave", "skip"] = "leave"
    max_replans_after_rejection: int = 1
    max_transport_retries: int = 2

    def __post_init__(self) -> None:
        if self.mode not in {"prompt", "structured"}:
            raise ValueError("mode must be prompt or structured")
        if self.controller is None:
            if self.mode != "prompt" or self.platform is not None:
                raise ValueError("legacy configuration cannot declare modern platform fields")
            if not self.platform_prompt.strip():
                raise ValueError("platform_prompt must be non-empty")
            if not self.controller_provider.strip():
                raise ValueError("controller_provider must be non-empty")
        else:
            if self.mode == "prompt" and not isinstance(self.platform, PromptPlatformConfig):
                raise ValueError("prompt mode requires PromptPlatformConfig")
            if self.mode == "structured" and not isinstance(self.platform, StructuredPlatformConfig):
                raise ValueError("structured mode requires StructuredPlatformConfig")
            if self.controller_timeout_seconds != 300.0:
                raise ValueError(
                    "modern AutoStartConfig uses controller.timeout_seconds; "
                    "controller_timeout_seconds is legacy-only"
                )
        try:
            providers = tuple(provider.strip() for provider in self.solver_providers)
        except AttributeError as exc:
            raise ValueError("solver_providers must contain strings") from exc
        if any(not provider for provider in providers):
            raise ValueError("solver_providers must contain non-empty names")
        if len(set(providers)) != len(providers):
            raise ValueError("solver_providers must be unique")
        object.__setattr__(self, "solver_providers", providers)
        object.__setattr__(self, "controller_provider", self.controller_provider.strip())
        if not self.solver_providers:
            raise ValueError("solver_providers must not be empty")
        if self.agent_limit < 1 or self.agent_limit > MAX_AGENT_LIMIT:
            raise ValueError(f"agent_limit must be between 1 and {MAX_AGENT_LIMIT}")
        if self.max_tasks < 1 or self.max_tasks > MAX_AUTOPILOT_TASKS:
            raise ValueError(f"max_tasks must be between 1 and {MAX_AUTOPILOT_TASKS}")
        if self.task_timeout_seconds <= 0 or self.task_timeout_seconds > MAX_TASK_TIMEOUT_SECONDS:
            raise ValueError(
                f"task_timeout_seconds must be between 0 and {MAX_TASK_TIMEOUT_SECONDS}"
            )
        if self.controller_timeout_seconds <= 0 or self.controller_timeout_seconds > 1800:
            raise ValueError("controller_timeout_seconds must be between 0 and 1800")
        if self.poll_interval_seconds <= 0 or self.poll_interval_seconds > MAX_POLL_INTERVAL_SECONDS:
            raise ValueError(
                f"poll_interval_seconds must be between 0 and {MAX_POLL_INTERVAL_SECONDS}"
            )
        if not self.is_legacy and self.agent_limit > len(self.solver_providers):
            raise ValueError("agent_limit cannot exceed solver provider count")
        if self.rejected_action not in {"replan", "skip", "fail"}:
            raise ValueError("invalid rejected_action")
        if self.solver_failure_action not in {"skip", "fail"}:
            raise ValueError("invalid solver_failure_action")
        if self.stop_active_task_action not in {"leave", "skip"}:
            raise ValueError("invalid stop_active_task_action")
        if self.max_replans_after_rejection < 0 or self.max_replans_after_rejection > MAX_RETRY_COUNT:
            raise ValueError(
                f"max_replans_after_rejection must be between 0 and {MAX_RETRY_COUNT}"
            )
        if self.max_transport_retries < 0 or self.max_transport_retries > MAX_RETRY_COUNT:
            raise ValueError(
                f"max_transport_retries must be between 0 and {MAX_RETRY_COUNT}"
            )
        skip_requested = (
            self.rejected_action == "skip"
            or self.solver_failure_action == "skip"
            or self.stop_active_task_action == "skip"
        )
        if (
            skip_requested
            and self.mode == "structured"
            and isinstance(self.platform, StructuredPlatformConfig)
            and self.platform.skip_task is None
        ):
            raise ValueError(
                "skip policies require a structured platform skip_task endpoint"
            )

    @property
    def is_legacy(self) -> bool:
        return self.controller is None

    def __repr__(self) -> str:
        return (
            "AutoStartConfig("
            f"mode={self.mode!r}, solver_providers={self.solver_providers!r}, "
            f"agent_limit={self.agent_limit!r}, max_tasks={self.max_tasks!r}, "
            f"legacy={self.is_legacy!r})"
        )


@dataclass(frozen=True, slots=True)
class AutoTask:
    external_id: str
    name: str
    goal: str
    origin: str
    hints: tuple[str, ...] = ()
    challenge_path: Path | None = None
    raw_metadata: Mapping[str, object] = field(default_factory=dict)
    lease_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.external_id, str) or not self.external_id.strip():
            raise ValueError("task external_id must be non-empty")
        external_id = self.external_id.strip()
        if len(external_id) > 500:
            raise ValueError("task external_id is too long")
        object.__setattr__(self, "external_id", external_id)


@dataclass(frozen=True, slots=True)
class AutoControllerResult:
    action: str
    external_id: str | None = None
    name: str | None = None
    goal: str | None = None
    origin: str | None = None
    hints: tuple[str, ...] = ()
    message: str | None = None


@dataclass(slots=True)
class AutoHistoryEntry:
    external_id: str
    name: str
    project_id: str
    answer: str
    status: str = "accepted"
    message: str | None = None
    result_ref: str | None = None
    result_fact_id: str | None = None
    reason_code: str | None = None
    attempt_count: int = 0
    plan_fingerprints: tuple[str, ...] = ()
    attempts: tuple[dict[str, object], ...] = ()
    accepted_at: str | None = None
    finished_at: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "external_id": self.external_id,
            "name": self.name,
            "project_id": self.project_id,
            "answer": self.answer,
            "status": self.status,
            "message": self.message,
            "result_ref": self.result_ref,
            "result_fact_id": self.result_fact_id,
            "reason_code": self.reason_code,
            "attempt_count": self.attempt_count,
            "plan_fingerprints": list(self.plan_fingerprints),
            "attempts": [dict(item) for item in self.attempts],
            "accepted_at": self.accepted_at,
            "finished_at": self.finished_at,
        }


@dataclass(frozen=True, slots=True)
class ControllerDecision:
    """Validated modern controller output before manager-specific handling."""

    protocol_version: int
    phase: Literal["fetch", "plan", "submit", "skip"]
    action: str
    task: AutoTask | None = None
    message: str | None = None
    retry_after_seconds: float | None = None
    submission_plan: object | None = None

    def __post_init__(self) -> None:
        if self.protocol_version != 2:
            raise ValueError("controller protocol_version must be 2")
        if self.action not in {
            "task",
            "wait",
            "finished",
            "accepted",
            "rejected",
            "retryable_error",
            "fatal_error",
            "skipped",
            "skip_failed",
        }:
            raise ValueError("unsupported controller action")
        if self.retry_after_seconds is not None and not 0.1 <= self.retry_after_seconds <= 300:
            raise ValueError("retry_after_seconds must be between 0.1 and 300")
@dataclass(slots=True)
class AutoSession:
    id: str
    config: AutoStartConfig
    runtime_root: Path
    status: str = "starting"
    completed_tasks: int = 0
    processed_tasks: int = 0
    current_external_id: str | None = None
    current_task_name: str | None = None
    current_project_id: str | None = None
    last_message: str | None = None
    last_error: str | None = None
    history: list[AutoHistoryEntry] = field(default_factory=list)
    current_task_status: str | None = None
    current_reason_code: str | None = None
    current_result_ref: str | None = None
    current_result_fact_id: str | None = None
    current_result_preview: str | None = None
    current_attempt_phase: str | None = None
    current_retry_count: int = 0
    current_replan_count: int = 0
    current_plan_fingerprint: str | None = None
    current_task: AutoTask | None = None
    consecutive_empty_fetches: int = 0
    consecutive_fetch_errors: int = 0
    consecutive_duplicate_ids: int = 0
    stop_release_attempted: bool = False

    def snapshot(self, providers: tuple[str, ...]) -> dict[str, object]:
        result = {
            "id": self.id,
            "status": self.status,
            "mode": self.config.mode,
            "controller": (
                {
                    "base_url": self.config.controller.base_url,
                    "model": self.config.controller.model,
                }
                if self.config.controller is not None else None
            ),
            "solver_providers": list(self.config.solver_providers),
            "available_providers": list(providers),
            "agent_limit": self.config.agent_limit,
            "max_tasks": self.config.max_tasks,
            "completed_tasks": self.completed_tasks,
            "processed_tasks": self.processed_tasks,
            "current_external_id": self.current_external_id,
            "current_task_name": self.current_task_name,
            "current_project_id": self.current_project_id,
            "last_message": self.last_message,
            "last_error": self.last_error,
            "current_task_status": self.current_task_status,
            "reason_code": self.current_reason_code,
            "current_goal_result": (
                {
                    "result_ref": self.current_result_ref,
                    "fact_id": self.current_result_fact_id,
                    "preview": self.current_result_preview,
                }
                if self.current_result_ref is not None
                else None
            ),
            "current_attempt": {
                "phase": self.current_attempt_phase,
                "retry_count": self.current_retry_count,
                "replan_count": self.current_replan_count,
                "plan_fingerprint": self.current_plan_fingerprint,
            },
            "history": [entry.to_dict() for entry in self.history],
        }
        if self.config.is_legacy:
            result["controller_provider"] = self.config.controller_provider
        return result
