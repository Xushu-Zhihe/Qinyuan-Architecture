from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True, slots=True)
class AutoStartConfig:
    platform_prompt: str
    controller_provider: str
    solver_providers: tuple[str, ...]
    agent_limit: int = 2
    max_tasks: int = 10
    task_timeout_seconds: float = 3600.0
    controller_timeout_seconds: float = 300.0
    poll_interval_seconds: float = 1.0

    def __post_init__(self) -> None:
        if not self.platform_prompt.strip():
            raise ValueError("platform_prompt must be non-empty")
        if not self.controller_provider.strip():
            raise ValueError("controller_provider must be non-empty")
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


@dataclass(frozen=True, slots=True)
class AutoTask:
    external_id: str
    name: str
    goal: str
    origin: str
    hints: tuple[str, ...] = ()
    challenge_path: Path | None = None


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

    def to_dict(self) -> dict[str, object]:
        return {
            "external_id": self.external_id,
            "name": self.name,
            "project_id": self.project_id,
            "answer": self.answer,
            "status": self.status,
            "message": self.message,
        }


@dataclass(slots=True)
class AutoSession:
    id: str
    config: AutoStartConfig
    runtime_root: Path
    status: str = "starting"
    completed_tasks: int = 0
    current_external_id: str | None = None
    current_task_name: str | None = None
    current_project_id: str | None = None
    last_message: str | None = None
    last_error: str | None = None
    history: list[AutoHistoryEntry] = field(default_factory=list)

    def snapshot(self, providers: tuple[str, ...]) -> dict[str, object]:
        return {
            "id": self.id,
            "status": self.status,
            "controller_provider": self.config.controller_provider,
            "solver_providers": list(self.config.solver_providers),
            "available_providers": list(providers),
            "agent_limit": self.config.agent_limit,
            "max_tasks": self.config.max_tasks,
            "completed_tasks": self.completed_tasks,
            "current_external_id": self.current_external_id,
            "current_task_name": self.current_task_name,
            "current_project_id": self.current_project_id,
            "last_message": self.last_message,
            "last_error": self.last_error,
            "history": [entry.to_dict() for entry in self.history],
        }
