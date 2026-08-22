from dataclasses import asdict, dataclass, field
from typing import Literal
from urllib.parse import urlsplit
import re


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


_FACT_COMPACTION_MODEL_PATTERN = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$"
)


@dataclass(frozen=True, slots=True, repr=False)
class FactCompactionLLMConfig:
    base_url: str
    api_key: str
    model: str
    timeout_seconds: float = 120.0

    def __post_init__(self) -> None:
        base_url = self.base_url.strip().rstrip("/")
        if base_url.endswith("/chat/completions"):
            base_url = base_url[: -len("/chat/completions")]
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("fact compaction llm base_url must be an absolute HTTP URL")
        api_key = self.api_key.strip()
        if not api_key:
            raise ValueError("fact compaction llm api_key must be non-empty")
        model = self.model.strip()
        if not model or _FACT_COMPACTION_MODEL_PATTERN.fullmatch(model) is None:
            raise ValueError("fact compaction llm model is invalid")
        if self.timeout_seconds <= 0 or self.timeout_seconds > 1800:
            raise ValueError("fact compaction llm timeout_seconds must be between 0 and 1800")
        object.__setattr__(self, "base_url", base_url)
        object.__setattr__(self, "api_key", api_key)
        object.__setattr__(self, "model", model)

    @property
    def chat_completions_url(self) -> str:
        return f"{self.base_url}/chat/completions"

    def __repr__(self) -> str:
        return (
            "FactCompactionLLMConfig("
            f"base_url={self.base_url!r}, model={self.model!r}, "
            f"timeout_seconds={self.timeout_seconds!r}, api_key='[redacted]')"
        )


@dataclass(frozen=True, slots=True)
class FactCompactionConfig:
    enabled: bool = False
    provider: ProviderConfig | None = None
    llm: FactCompactionLLMConfig | None = None
    min_facts: int = 3
    max_facts: int = 16
    poll_interval_seconds: float = 1.0
    max_summary_chars: int = 4_000

    def __post_init__(self) -> None:
        if self.enabled and self.provider is None and self.llm is None:
            raise ValueError("enabled fact compaction requires a provider or llm")
        if self.provider is not None and self.llm is not None:
            raise ValueError("fact compaction cannot configure both provider and llm")
        if self.min_facts < 2:
            raise ValueError("fact compaction min_facts must be at least 2")
        if self.max_facts < self.min_facts:
            raise ValueError("fact compaction max_facts must be at least min_facts")
        if self.poll_interval_seconds <= 0:
            raise ValueError("fact compaction poll interval must be positive")
        if self.max_summary_chars <= 0:
            raise ValueError("fact compaction max_summary_chars must be positive")


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
