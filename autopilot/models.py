from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Mapping


@dataclass(frozen=True, slots=True, repr=False)
class ControllerConfig:
    base_url: str
    model: str
    timeout_seconds: float = 300.0
    max_rounds: int = 20
    temperature: float | None = None
    max_output_tokens: int | None = None
    reasoning_effort: str | None = None
    extra_headers: tuple[tuple[str, str], ...] = ()
    extra_body: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        base = self.base_url.strip().rstrip("/")
        if base.endswith("/chat/completions"):
            base = base[: -len("/chat/completions")]
        if not base or not self.model.strip():
            raise ValueError("controller base_url and model must be non-empty")
        if self.timeout_seconds <= 0 or self.timeout_seconds > 1800:
            raise ValueError("controller timeout_seconds must be between 0 and 1800")
        if self.max_rounds < 1 or self.max_rounds > 50:
            raise ValueError("controller max_rounds must be between 1 and 50")
        object.__setattr__(self, "base_url", base)
        object.__setattr__(self, "model", self.model.strip())

    @property
    def chat_completions_url(self) -> str:
        return f"{self.base_url}/chat/completions"

    def __repr__(self) -> str:
        return (
            "ControllerConfig("
            f"base_url={self.base_url!r}, model={self.model!r}, "
            f"timeout_seconds={self.timeout_seconds!r}, max_rounds={self.max_rounds!r})"
        )


@dataclass(slots=True, repr=False)
class RuntimeControllerSecret:
    api_key: str

    def __repr__(self) -> str:
        return "RuntimeControllerSecret(api_key='[redacted]')"

    __str__ = __repr__

    def clear(self) -> None:
        self.api_key = ""


@dataclass(slots=True, repr=False)
class RuntimePlatformSecret:
    prompt: str = ""
    token: str = ""

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
        if self.controller is None:
            if not self.platform_prompt.strip():
                raise ValueError("platform_prompt must be non-empty")
            if not self.controller_provider.strip():
                raise ValueError("controller_provider must be non-empty")
        elif self.mode == "prompt" and not isinstance(self.platform, PromptPlatformConfig):
            raise ValueError("prompt mode requires PromptPlatformConfig")
        elif self.mode == "structured" and self.platform is None:
            raise ValueError("structured mode requires platform configuration")
        if not self.solver_providers:
            raise ValueError("solver_providers must not be empty")
        if self.agent_limit <= 0:
            raise ValueError("agent_limit must be positive")
        if self.max_tasks <= 0:
            raise ValueError("max_tasks must be positive")
        if self.task_timeout_seconds <= 0:
            raise ValueError("task_timeout_seconds must be positive")
        if self.controller_timeout_seconds <= 0:
            raise ValueError("controller_timeout_seconds must be positive")
        if self.poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        if not self.is_legacy and self.agent_limit > len(self.solver_providers):
            raise ValueError("agent_limit cannot exceed solver provider count")
        if self.rejected_action not in {"replan", "skip", "fail"}:
            raise ValueError("invalid rejected_action")
        if self.max_replans_after_rejection < 0 or self.max_replans_after_rejection > 10:
            raise ValueError("max_replans_after_rejection must be between 0 and 10")
        if self.max_transport_retries < 0 or self.max_transport_retries > 10:
            raise ValueError("max_transport_retries must be between 0 and 10")

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
