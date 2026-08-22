from dataclasses import asdict, dataclass, field
from typing import Literal


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
class CapabilityProbeConfig:
    command: tuple[str, ...]
    timeout_seconds: float


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    request_protocol_versions: tuple[int, ...] = (1,)
    result_protocol_versions: tuple[int, ...] = (1,)
    skill_mode: Literal["none", "inline", "required", "tool"] = "inline"
    available_skills: tuple[str, ...] = ()
    validates_result_locally: bool = False
    supports_result_fact: bool = False
    schema_version: str | None = None
    fixture_digest: str | None = None


@dataclass(frozen=True, slots=True)
class AgentLifecycleConfig:
    release_command: tuple[str, ...]
    release_timeout_seconds: float = 30.0


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
    container_image: str | None = None
    container_command: tuple[str, ...] | None = None
    stream_format: str = "text"
    streaming_supported: bool = True
    capabilities: ProviderCapabilities = field(default_factory=ProviderCapabilities)
    capability_probe: CapabilityProbeConfig | None = None
    protocol_arguments: tuple[str, ...] = ()
    agent_lifecycle: AgentLifecycleConfig | None = None


@dataclass(frozen=True, slots=True)
class ContainerBackendConfig:
    runtime_command: tuple[str, ...] = ("docker",)
    default_image: str | None = None
    network: str = "none"
    capabilities: tuple[str, ...] = ()
    environment: tuple[tuple[str, str], ...] = ()
    environment_allowlist: tuple[str, ...] = ()
    cleanup_policy: str = "remove"
    challenge_mount_path: str = "/challenge"
    work_mount_path: str = "/work"
    read_only_root: bool = True
    memory_limit: str | None = None
    cpu_limit: float | None = None
    pids_limit: int | None = None
    stop_timeout_seconds: float = 2.0
    setup_timeout_seconds: float = 300.0


@dataclass(frozen=True, slots=True)
class ExecutionConfig:
    backend: str = "local"
    container: ContainerBackendConfig | None = None


@dataclass(frozen=True, slots=True)
class LogChunk:
    stream: str
    text: str


class StreamOutput(str):
    """Rendered stream text with a redacted view and a protocol view.

    Subclassing ``str`` keeps the historical ``_read_stream`` return contract
    for callers that only need the audit text while exposing the semantic
    protocol text to execution backends.
    """

    def __new__(cls, audit: str, protocol: str) -> "StreamOutput":
        value = super().__new__(cls, audit)
        value.audit = audit
        value.protocol = protocol
        return value


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
    execution_backend: str = "custom"
    isolation: str = "unspecified"
    backend_run_id: str | None = None
    cleanup_policy: str | None = None
    cleanup_status: str = "not_applicable"
    cleanup_detail: str | None = None
    backend_metadata: dict[str, object] = field(default_factory=dict)
    protocol_output: str | None = field(default=None, repr=False)

    @property
    def protocol_text(self) -> str:
        return self.protocol_output or self.output

    def to_dict(self) -> dict[str, object]:
        serialized = asdict(self)
        serialized.pop("protocol_output", None)
        return serialized
