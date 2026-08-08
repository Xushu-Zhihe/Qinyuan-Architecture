from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True, slots=True)
class SandboxSettings:
    image: str = "qinyuan-tools:nmap-1"
    image_digest: str | None = None
    allowed_tools: frozenset[str] = field(default_factory=lambda: frozenset({"nmap"}))
    max_output_bytes: int = 256 * 1024
    max_artifact_bytes: int = 8 * 1024 * 1024
    max_command_timeout_seconds: float = 900.0
    docker_api_timeout_seconds: float = 960.0
    kill_grace_seconds: float = 3.0
    artifact_root: Path | None = None
    network: str = "none"
    memory_limit: str = "256m"
    cpu_limit: float = 1.0
    pids_limit: int = 64
    namespace: str = "qinyuan-bat"

    def __post_init__(self) -> None:
        if self.allowed_tools != frozenset({"nmap"}):
            raise ValueError("first-version Sandbox only allows nmap")
        if self.max_output_bytes <= 0 or self.max_artifact_bytes < self.max_output_bytes:
            raise ValueError("invalid output/artifact limits")
        if not 0 < self.max_command_timeout_seconds <= 900:
            raise ValueError("max command timeout must be in (0, 900]")
        if self.docker_api_timeout_seconds <= self.max_command_timeout_seconds + self.kill_grace_seconds:
            raise ValueError("docker API timeout must include command and kill grace")
        if self.network == "host":
            raise ValueError("host network is not permitted")

    def artifact_path(self, agent_run_id: str, call_id: str) -> Path:
        if self.artifact_root is None:
            raise RuntimeError("artifact_root is not configured")
        return self.artifact_root / agent_run_id / call_id
