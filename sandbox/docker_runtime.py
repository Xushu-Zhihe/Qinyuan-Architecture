from __future__ import annotations

import asyncio
import codecs
import os
import re
import shutil
import signal
import subprocess
from pathlib import Path
from uuid import uuid4

from sandbox.contracts import CommandRequest, CommandResult
from sandbox.models import RuntimeContainer
from sandbox.settings import SandboxSettings
from worker.redaction import redact_text


_SAFE_NAME = re.compile(r"[^a-zA-Z0-9_.-]+")


class DockerRuntimeError(RuntimeError):
    pass


class _BoundedBytes:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.total = 0
        self.head_limit = max(1, limit * 3 // 4)
        self.tail_limit = max(0, limit - self.head_limit)
        self.head = bytearray()
        self.tail = bytearray()

    def add(self, data: bytes) -> None:
        self.total += len(data)
        head_room = max(0, self.head_limit - len(self.head))
        self.head.extend(data[:head_room])
        remainder = data[head_room:]
        if self.tail_limit and remainder:
            self.tail.extend(remainder)
            if len(self.tail) > self.tail_limit:
                del self.tail[: len(self.tail) - self.tail_limit]

    @property
    def truncated(self) -> bool:
        return self.total > self.limit

    def text(self) -> str:
        if not self.truncated:
            raw = bytes(self.head + self.tail)
        else:
            raw = bytes(self.head) + b"\n...[output truncated]...\n" + bytes(self.tail)
        return redact_text(raw.decode("utf-8", errors="ignore"))


class _RedactingArtifact:
    def __init__(self, path: Path, budget: list[int]) -> None:
        self.handle = path.open("wb")
        self.budget = budget
        self.pending = ""
        self.decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    def feed(self, data: bytes) -> None:
        self.pending += self.decoder.decode(data)
        if len(self.pending) <= 512:
            return
        visible = redact_text(self.pending[:-256])
        self.pending = self.pending[-256:]
        self._write(visible)

    def _write(self, text: str) -> None:
        if self.budget[0] <= 0:
            return
        encoded = text.encode("utf-8")[: self.budget[0]]
        self.budget[0] -= len(encoded)
        if encoded:
            self.handle.write(encoded)
            self.handle.flush()

    def finish(self) -> None:
        self.pending += self.decoder.decode(b"", final=True)
        self._write(redact_text(self.pending))
        self.pending = ""
        self.handle.close()


async def _terminate(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    if os.name == "nt":
        killer = await asyncio.create_subprocess_exec(
            "taskkill", "/PID", str(process.pid), "/T", "/F",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await killer.communicate()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        await asyncio.wait_for(process.wait(), timeout=2)
    except TimeoutError:
        pass


class DockerSandboxRuntime:
    """Docker CLI adapter for the dedicated Nmap tool image."""

    def __init__(
        self,
        settings: SandboxSettings | None = None,
        *,
        runtime_command: tuple[str, ...] = ("docker",),
        instance_id: str | None = None,
    ) -> None:
        self.settings = settings or SandboxSettings()
        self.runtime_command = runtime_command
        self.instance_id = instance_id or uuid4().hex
        self._processes: dict[tuple[str, str], asyncio.subprocess.Process] = {}
        self._containers: dict[str, RuntimeContainer] = {}
        self._closed = False
        self._cancelled: set[tuple[str, str]] = set()

    async def _run(self, *arguments: str, timeout: float = 30.0) -> tuple[int, str, str]:
        executable = shutil.which(self.runtime_command[0]) or self.runtime_command[0]
        kwargs: dict[str, object] = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        try:
            process = await asyncio.create_subprocess_exec(
                executable, *self.runtime_command[1:], *arguments,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **kwargs,
            )
        except OSError as exc:
            raise DockerRuntimeError("Docker runtime is unavailable") from exc
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except TimeoutError as exc:
            await _terminate(process)
            raise DockerRuntimeError("Docker API operation timed out") from exc
        return int(process.returncode or 0), stdout.decode("utf-8", errors="replace").strip(), redact_text(stderr.decode("utf-8", errors="replace").strip())

    def _container_name(self, agent_run_id: str) -> str:
        safe = _SAFE_NAME.sub("-", agent_run_id).strip("-.") or "run"
        return f"qinyuan-tool-{safe[:24]}-{uuid4().hex[:10]}".lower()

    async def create(self, agent_run_id: str) -> RuntimeContainer:
        if self._closed:
            raise DockerRuntimeError("runtime is closed")
        image = self.settings.image_digest or self.settings.image
        name = self._container_name(agent_run_id)
        arguments = [
            "create", "--name", name,
            "--label", f"io.qinyuan.namespace={self.settings.namespace}",
            "--label", f"io.qinyuan.instance={self.instance_id}",
            "--label", f"io.qinyuan.agent-run={agent_run_id}",
            "--network", self.settings.network,
            "--read-only", "--tmpfs", "/tmp:rw,noexec,nosuid,size=16m",
            "--tmpfs", "/workspace:rw,noexec,nosuid,size=16m",
            "--security-opt", "no-new-privileges",
            "--cap-drop", "ALL", "--user", "65532:65532",
            "--pids-limit", str(self.settings.pids_limit),
            "--memory", self.settings.memory_limit,
            "--cpus", f"{self.settings.cpu_limit:g}",
            image,
        ]
        code, container_id, detail = await self._run(*arguments)
        if code != 0 or not container_id:
            raise DockerRuntimeError(f"Docker create failed: {detail or 'unknown error'}")
        container_id = container_id.splitlines()[-1]
        try:
            code, _, detail = await self._run("start", container_id)
            if code != 0:
                raise DockerRuntimeError(f"Docker start failed: {detail or 'unknown error'}")
            code, image_id, detail = await self._run("inspect", "--format", "{{.Image}}", container_id)
            if code != 0:
                raise DockerRuntimeError(f"Docker inspect failed: {detail or 'unknown error'}")
        except Exception:
            await self._run("rm", "--force", container_id, timeout=30)
            raise
        container = RuntimeContainer(container_id, self.settings.image, image_id or None)
        self._containers[container_id] = container
        return container

    async def preflight(self) -> str:
        code, _, detail = await self._run("version", "--format", "{{.Server.Version}}", timeout=10)
        if code != 0:
            raise DockerRuntimeError(f"Docker daemon is unavailable: {detail or 'version check failed'}")
        image = self.settings.image_digest or self.settings.image
        code, image_id, detail = await self._run("image", "inspect", image, "--format", "{{.Id}}", timeout=15)
        if code != 0 or not image_id:
            raise DockerRuntimeError(f"tool image is unavailable: {detail or image}")
        return image_id

    async def _read(self, reader: asyncio.StreamReader, bounded: _BoundedBytes, artifact) -> None:
        while True:
            chunk = await reader.read(64 * 1024)
            if not chunk:
                break
            bounded.add(chunk)
            if artifact is not None:
                artifact.feed(chunk)

    async def execute(self, container: RuntimeContainer, request: CommandRequest, *, artifact_dir: Path | None = None) -> CommandResult:
        if request.tool not in self.settings.allowed_tools:
            raise DockerRuntimeError("tool is not allowed")
        if request.timeout_seconds > self.settings.max_command_timeout_seconds:
            raise DockerRuntimeError("command timeout exceeds runtime policy")
        if artifact_dir is not None:
            artifact_dir.mkdir(parents=True, exist_ok=True)
        kwargs: dict[str, object] = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        command = (
            *self.runtime_command, "exec", container.container_id,
            "timeout", "--signal=KILL", f"{request.timeout_seconds:g}s",
            request.tool, *request.arguments,
        )
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **kwargs,
            )
        except OSError as exc:
            raise DockerRuntimeError("Docker exec failed to start") from exc
        self._processes[(container.container_id, request.call_id)] = process
        assert process.stdout is not None and process.stderr is not None
        stdout = _BoundedBytes(self.settings.max_output_bytes)
        stderr = _BoundedBytes(self.settings.max_output_bytes)
        out_path = artifact_dir / "stdout.bin" if artifact_dir else None
        err_path = artifact_dir / "stderr.bin" if artifact_dir else None
        budget = [self.settings.max_artifact_bytes]
        out_handle = _RedactingArtifact(out_path, budget) if out_path else None
        err_handle = _RedactingArtifact(err_path, budget) if err_path else None
        timed_out = False
        unknown = False
        try:
            readers = (
                asyncio.create_task(self._read(process.stdout, stdout, out_handle)),
                asyncio.create_task(self._read(process.stderr, stderr, err_handle)),
            )
            try:
                await asyncio.wait_for(process.wait(), timeout=request.timeout_seconds + self.settings.kill_grace_seconds)
            except TimeoutError:
                timed_out = True
                await _terminate(process)
                kill_code, _, _ = await self._run("kill", container.container_id, timeout=30)
                unknown = kill_code != 0
            await asyncio.gather(*readers, return_exceptions=True)
        finally:
            self._processes.pop((container.container_id, request.call_id), None)
            if out_handle:
                out_handle.finish()
            if err_handle:
                err_handle.finish()
        exit_code = process.returncode
        key = (container.container_id, request.call_id)
        cancelled = key in self._cancelled
        self._cancelled.discard(key)
        status = "cancelled" if cancelled else "outcome_unknown" if unknown else "timeout" if timed_out or exit_code == 124 else "completed" if exit_code == 0 else "failed"
        refs: list[str] = []
        if artifact_dir and (stdout.truncated or stderr.truncated):
            if stdout.total:
                refs.append(f"artifact:{request.call_id}:stdout")
            if stderr.total:
                refs.append(f"artifact:{request.call_id}:stderr")
        return CommandResult(
            call_id=request.call_id,
            status=status,
            exit_code=exit_code,
            stdout=stdout.text(),
            stderr=stderr.text(),
            timed_out=status == "timeout",
            stdout_truncated=stdout.truncated,
            stderr_truncated=stderr.truncated,
            artifact_refs=tuple(refs),
        )

    async def cancel(self, container: RuntimeContainer, call_id: str) -> bool:
        process = self._processes.get((container.container_id, call_id))
        if process is None:
            return False
        self._cancelled.add((container.container_id, call_id))
        await _terminate(process)
        await self._run("kill", container.container_id, timeout=30)
        return True

    async def destroy(self, container: RuntimeContainer) -> None:
        code, _, detail = await self._run("rm", "--force", container.container_id, timeout=30)
        if code != 0:
            raise DockerRuntimeError(f"Docker destroy failed: {detail or 'unknown error'}")
        self._containers.pop(container.container_id, None)

    async def cleanup_orphans(self) -> tuple[str, ...]:
        label_args = (
            "--filter", f"label=io.qinyuan.namespace={self.settings.namespace}",
            "--filter", f"label=io.qinyuan.instance={self.instance_id}",
        )
        code, output, detail = await self._run("ps", "-aq", *label_args)
        if code != 0:
            raise DockerRuntimeError(f"orphan scan failed: {detail}")
        removed: list[str] = []
        for container_id in output.splitlines():
            if container_id.strip():
                rm_code, _, _ = await self._run("rm", "--force", container_id.strip())
                if rm_code == 0:
                    removed.append(container_id.strip())
        return tuple(removed)

    async def close(self) -> None:
        self._closed = True
        for process in tuple(self._processes.values()):
            await _terminate(process)
