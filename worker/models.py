from dataclasses import asdict, dataclass


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    name: str
    enabled: bool
    priority: int
    command: tuple[str, ...]
    timeout_seconds: float


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

    def to_dict(self) -> dict[str, object]:
        return asdict(self)
