from __future__ import annotations

import hashlib
import os
from pathlib import Path

from sandbox.contracts import BridgeProtocolError


class ArtifactStore:
    """Run-owned, bounded artifact files with opaque references."""

    def __init__(self, root: Path, *, max_bytes: int = 8 * 1024 * 1024) -> None:
        self.root = root.resolve()
        self.max_bytes = max_bytes
        self.root.mkdir(parents=True, exist_ok=True)

    def _run_root(self, agent_run_id: str) -> Path:
        path = (self.root / agent_run_id).resolve()
        if self.root not in path.parents:
            raise BridgeProtocolError("artifact scope escapes root")
        path.mkdir(parents=True, exist_ok=True)
        return path

    def write_stream(self, agent_run_id: str, call_id: str, stream: str, data: bytes) -> tuple[str, bool]:
        if stream not in {"stdout", "stderr"}:
            raise ValueError("unsupported artifact stream")
        payload = data[: self.max_bytes]
        truncated = len(payload) < len(data)
        run_root = self._run_root(agent_run_id)
        digest = hashlib.sha256(f"{agent_run_id}:{call_id}:{stream}".encode()).hexdigest()[:24]
        target = run_root / call_id / f"{stream}.bin"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
        return f"artifact:{agent_run_id}:{call_id}:{stream}", truncated

    def read(self, agent_run_id: str, reference: str) -> bytes:
        prefix = f"artifact:{agent_run_id}:"
        if not reference.startswith(prefix):
            raise PermissionError("artifact does not belong to run")
        parts = reference.split(":")
        if len(parts) != 4 or parts[3] not in {"stdout", "stderr"}:
            raise PermissionError("invalid artifact reference")
        call_id = parts[2]
        if not call_id or any(item in call_id for item in ("/", "\\", "..")):
            raise PermissionError("invalid artifact reference")
        path = self._run_root(agent_run_id) / call_id / f"{parts[3]}.bin"
        if not path.is_file():
            raise FileNotFoundError(reference)
        return path.read_bytes()

    def remove_run(self, agent_run_id: str) -> None:
        target = (self.root / agent_run_id).resolve()
        if target == self.root or self.root not in target.parents:
            return
        for child in sorted(target.rglob("*"), reverse=True):
            if child.is_file():
                child.unlink(missing_ok=True)
            elif child.is_dir():
                child.rmdir()
        target.rmdir()
