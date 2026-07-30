from dataclasses import asdict, dataclass


@dataclass(frozen=True, slots=True)
class CoordinatorConfig:
    max_global_running: int = 16
    max_project_running: int = 4
    max_global_planning: int = 4
    max_project_planning: int = 2
    aging_seconds: float = 30.0


@dataclass(frozen=True, slots=True)
class AuditConfig:
    max_log_bytes: int = 8 * 1024 * 1024
    max_retained_log_sets: int = 500
    max_retained_exports: int = 5
    default_log_page_bytes: int = 256 * 1024
    max_log_page_bytes: int = 1024 * 1024


@dataclass(frozen=True, slots=True)
class HealthCheckConfig:
    command: tuple[str, ...]
    timeout_seconds: float
    cooldown_seconds: float
    max_cooldown_seconds: float = 300.0
    failure_threshold: int = 3


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    name: str
    enabled: bool
    priority: int
    command: tuple[str, ...]
    timeout_seconds: float
    planning_timeout_seconds: float = 120.0
    health_check: HealthCheckConfig | None = None
    max_running: int = 4


@dataclass(frozen=True, slots=True)
class LogChunk:
    stream: str
    text: str


@dataclass(frozen=True, slots=True)
class WorkerResult:
    provider: str
    priority: int
    status: str
    exit_code: int | None
    duration_seconds: float
    output: str
    stdout_path: str
    stderr_path: str
    work_dir: str
    queue_seconds: float = 0.0
    wait_reasons: tuple[str, ...] = ()
    cancel_reason: str | None = None
    model: str = "unknown"
    cli_version: str = "unknown"
    token_usage: object = "unknown"

    def to_dict(self) -> dict[str, object]:
        return asdict(self)
