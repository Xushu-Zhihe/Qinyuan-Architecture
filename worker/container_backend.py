import asyncio
import math
import os
import re
import subprocess
import time
from pathlib import Path
from uuid import uuid4

from worker.backend import (
    BackendHandle,
    BackendOutcome,
    BackendStartError,
    CleanupOutcome,
    ExecutionBackend,
)
from worker.commands import container_command
from worker.executor import (
    ChunkCallback,
    _append_stderr,
    _read_stream,
    _resolve_executable,
    _terminate_process_tree,
    _write_stdin,
)
from worker.models import ContainerBackendConfig, ProviderConfig, WorkerResult
from worker.redaction import redact_text


_CONTAINER_NAME_RE = re.compile(r"[^a-zA-Z0-9_.-]+")


class ContainerExecutionBackend(ExecutionBackend):
    name = "container"
    isolation = "container"

    def __init__(self, config: ContainerBackendConfig) -> None:
        self.config = config

    @staticmethod
    def _container_name(provider: str) -> str:
        safe = _CONTAINER_NAME_RE.sub("-", provider).strip("-.") or "provider"
        return f"qinyuan-{safe[:32]}-{uuid4().hex[:12]}".lower()

    async def _runtime(
        self,
        *arguments: str,
        timeout: float | None = None,
    ) -> tuple[int, str, str]:
        command = (*self.config.runtime_command, *arguments)
        kwargs: dict[str, object] = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        try:
            process = await asyncio.create_subprocess_exec(
                *_resolve_executable(command),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **kwargs,
            )
        except OSError as exc:
            raise BackendStartError(
                f"container runtime {self.config.runtime_command[0]!r} is unavailable: {exc}"
            ) from exc
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=timeout or self.config.setup_timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            await _terminate_process_tree(process)
            raise BackendStartError(
                f"container runtime command timed out after "
                f"{timeout or self.config.setup_timeout_seconds:g} seconds"
            ) from exc
        return (
            int(process.returncode or 0),
            stdout.decode("utf-8", errors="replace").strip(),
            stderr.decode("utf-8", errors="replace").strip(),
        )

    def _environment(self) -> tuple[tuple[str, str], ...]:
        values = dict(self.config.environment)
        for name in self.config.environment_allowlist:
            if name in os.environ:
                values[name] = os.environ[name]
        return tuple(sorted(values.items()))

    def _create_arguments(
        self,
        provider: ProviderConfig,
        challenge_dir: Path | None,
        work_dir: Path,
        command: tuple[str, ...],
        name: str,
    ) -> tuple[str, ...]:
        image = provider.container_image or self.config.default_image
        if not image:
            raise BackendStartError(
                f"provider {provider.name!r} has no container image configured"
            )
        arguments: list[str] = [
            "create",
            "--name",
            name,
            "--interactive",
            "--network",
            self.config.network,
            "--workdir",
            self.config.work_mount_path,
            "--mount",
            (
                "type=bind,source="
                f"{work_dir.resolve()},target={self.config.work_mount_path}"
            ),
        ]
        if challenge_dir is not None:
            arguments.extend(
                [
                    "--mount",
                    (
                        "type=bind,source="
                        f"{challenge_dir.resolve()},"
                        f"target={self.config.challenge_mount_path},readonly"
                    ),
                ]
            )
        if self.config.read_only_root:
            arguments.append("--read-only")
        for capability in self.config.capabilities:
            arguments.extend(("--cap-add", capability))
        for key, value in self._environment():
            arguments.extend(("--env", f"{key}={value}"))
        if self.config.memory_limit:
            arguments.extend(("--memory", self.config.memory_limit))
        if self.config.cpu_limit is not None:
            arguments.extend(("--cpus", f"{self.config.cpu_limit:g}"))
        if self.config.pids_limit is not None:
            arguments.extend(("--pids-limit", str(self.config.pids_limit)))
        arguments.append(image)
        arguments.extend(command)
        return tuple(arguments)

    async def start(
        self,
        provider: ProviderConfig,
        command: tuple[str, ...],
        request: str,
        challenge_dir: Path | None,
        work_dir: Path,
    ) -> BackendHandle:
        name = self._container_name(provider.name)
        arguments = self._create_arguments(
            provider,
            challenge_dir,
            work_dir,
            command,
            name,
        )
        code, stdout, stderr = await self._runtime(*arguments)
        if code != 0:
            detail = redact_text(stderr or stdout or "unknown runtime error")
            raise BackendStartError(f"cannot create provider container: {detail}")
        container_id = stdout.splitlines()[-1].strip() if stdout else name
        kwargs: dict[str, object] = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        attach_command = (
            *self.config.runtime_command,
            "start",
            "--attach",
            "--interactive",
            container_id,
        )
        try:
            process = await asyncio.create_subprocess_exec(
                *_resolve_executable(attach_command),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **kwargs,
            )
        except OSError as exc:
            await self._runtime("rm", "--force", container_id, timeout=30.0)
            raise BackendStartError(f"cannot attach provider container: {exc}") from exc
        image = provider.container_image or self.config.default_image
        return BackendHandle(
            process=process,
            run_id=container_id,
            metadata={
                "container_name": name,
                "image": image or "",
                "network": self.config.network,
                "capabilities": list(self.config.capabilities),
                "environment_names": [key for key, _ in self._environment()],
                "read_only_root": self.config.read_only_root,
                "challenge_mount": (
                    self.config.challenge_mount_path
                    if challenge_dir is not None
                    else None
                ),
                "challenge_read_only": challenge_dir is not None,
                "work_mount": self.config.work_mount_path,
            },
        )

    async def stream(
        self,
        handle: BackendHandle,
        request: str,
        stdout_path: Path,
        stderr_path: Path,
        on_chunk: ChunkCallback | None,
        max_log_bytes: int,
    ) -> tuple[asyncio.Task[None], asyncio.Task[str], asyncio.Task[str]]:
        process = handle.process
        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None
        return (
            asyncio.create_task(_write_stdin(process.stdin, request)),
            asyncio.create_task(
                _read_stream(
                    process.stdout,
                    "stdout",
                    stdout_path,
                    on_chunk,
                    max_log_bytes,
                )
            ),
            asyncio.create_task(
                _read_stream(
                    process.stderr,
                    "stderr",
                    stderr_path,
                    on_chunk,
                    max_log_bytes,
                )
            ),
        )

    async def _container_exit_code(self, container_id: str) -> int | None:
        code, stdout, _ = await self._runtime(
            "inspect",
            "--format",
            "{{.State.ExitCode}}",
            container_id,
            timeout=30.0,
        )
        if code != 0:
            return None
        try:
            return int(stdout.strip())
        except ValueError:
            return None

    async def wait(
        self,
        handle: BackendHandle,
        timeout_seconds: float,
        cancel_event: asyncio.Event | None,
    ) -> BackendOutcome:
        wait_task = asyncio.create_task(handle.process.wait())
        cancel_task = (
            asyncio.create_task(cancel_event.wait())
            if cancel_event is not None
            else None
        )
        wait_set = {wait_task}
        if cancel_task is not None:
            wait_set.add(cancel_task)
        done, _ = await asyncio.wait(
            wait_set,
            timeout=timeout_seconds,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if wait_task in done:
            attach_exit = wait_task.result()
            container_exit = (
                await self._container_exit_code(handle.run_id)
                if handle.run_id is not None
                else None
            )
            exit_code = container_exit if container_exit is not None else attach_exit
            outcome = BackendOutcome(
                "success" if exit_code == 0 else "failed",
                exit_code,
            )
        elif cancel_task is not None and cancel_task in done:
            await self.cancel(handle)
            outcome = BackendOutcome(
                "cancelled",
                None,
                "provider container was cancelled\n",
            )
        else:
            await self.cancel(handle)
            outcome = BackendOutcome(
                "timeout",
                None,
                f"provider container exceeded timeout of {timeout_seconds:g} seconds\n",
            )
        if not wait_task.done():
            try:
                await asyncio.wait_for(wait_task, timeout=2.0)
            except asyncio.TimeoutError:
                await _terminate_process_tree(handle.process)
        if cancel_task is not None and not cancel_task.done():
            cancel_task.cancel()
            await asyncio.gather(cancel_task, return_exceptions=True)
        return outcome

    async def cancel(self, handle: BackendHandle) -> None:
        if handle.run_id is not None:
            try:
                await self._runtime("kill", handle.run_id, timeout=30.0)
            except BackendStartError:
                pass
        await _terminate_process_tree(handle.process)

    async def cleanup(
        self,
        handle: BackendHandle,
        outcome: BackendOutcome,
    ) -> CleanupOutcome:
        policy = self.config.cleanup_policy
        container_id = handle.run_id
        if container_id is None:
            return CleanupOutcome(policy, "failed", "container id is unavailable")
        try:
            if policy == "keep":
                return CleanupOutcome(policy, "kept", "container retained")
            if policy == "stop":
                code, stdout, stderr = await self._runtime(
                    "stop",
                    "--time",
                    str(max(0, math.ceil(self.config.stop_timeout_seconds))),
                    container_id,
                    timeout=max(30.0, self.config.stop_timeout_seconds + 5.0),
                )
                if code == 0:
                    return CleanupOutcome(policy, "stopped", stdout or "container stopped")
                # An already stopped container is still a successful stop outcome.
                inspect_code, state, _ = await self._runtime(
                    "inspect",
                    "--format",
                    "{{.State.Running}}",
                    container_id,
                    timeout=30.0,
                )
                if inspect_code == 0 and state.strip().lower() == "false":
                    return CleanupOutcome(policy, "stopped", "container already stopped")
                return CleanupOutcome(policy, "failed", redact_text(stderr or stdout))
            code, stdout, stderr = await self._runtime(
                "rm",
                "--force",
                container_id,
                timeout=30.0,
            )
            if code == 0:
                return CleanupOutcome(policy, "removed", stdout or "container removed")
            return CleanupOutcome(policy, "failed", redact_text(stderr or stdout))
        except BackendStartError as exc:
            return CleanupOutcome(policy, "failed", redact_text(str(exc)))

    async def execute(
        self,
        provider: ProviderConfig,
        request: str,
        challenge_dir: Path | None,
        work_dir: Path,
        on_chunk: ChunkCallback | None = None,
        cancel_event: asyncio.Event | None = None,
        max_log_bytes: int = 8 * 1024 * 1024,
    ) -> WorkerResult:
        work_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = work_dir / "stdout.txt"
        stderr_path = work_dir / "stderr.txt"
        stdout_path.write_text("", encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")
        command = container_command(
            provider,
            self.config.challenge_mount_path,
            self.config.work_mount_path,
            has_challenge=challenge_dir is not None,
        )
        started = time.monotonic()
        outcome = BackendOutcome("unavailable", None)
        cleanup = CleanupOutcome(self.config.cleanup_policy, "not_started")
        stdout_text = ""
        handle: BackendHandle | None = None
        try:
            handle = await self.start(
                provider,
                command,
                request,
                challenge_dir,
                work_dir,
            )
            stdin_task, stdout_task, stderr_task = await self.stream(
                handle,
                request,
                stdout_path,
                stderr_path,
                on_chunk,
                max_log_bytes,
            )
            outcome = await self.wait(
                handle,
                provider.timeout_seconds,
                cancel_event,
            )
            await asyncio.gather(stdin_task, return_exceptions=True)
            stdout_text, _ = await asyncio.gather(stdout_task, stderr_task)
            if outcome.diagnostic is not None:
                await _append_stderr(
                    stderr_path,
                    outcome.diagnostic,
                    on_chunk,
                    max_log_bytes,
                )
            cleanup = await self.cleanup(handle, outcome)
        except OSError as exc:
            await _append_stderr(
                stderr_path,
                f"cannot start provider container: {exc}\n",
                on_chunk,
                max_log_bytes,
            )
        finally:
            if handle is not None and cleanup.status == "not_started":
                cleanup = await self.cleanup(handle, outcome)
        if cleanup.status == "failed" and cleanup.detail:
            await _append_stderr(
                stderr_path,
                f"container cleanup failed: {cleanup.detail}\n",
                on_chunk,
                max_log_bytes,
            )

        metadata = dict(handle.metadata) if handle is not None else {
            "network": self.config.network,
            "capabilities": list(self.config.capabilities),
            "environment_names": [key for key, _ in self._environment()],
            "read_only_root": self.config.read_only_root,
            "challenge_read_only": challenge_dir is not None,
            "work_mount": self.config.work_mount_path,
        }
        return WorkerResult(
            provider=provider.name,
            priority=provider.priority,
            status=outcome.status,
            exit_code=outcome.exit_code,
            duration_seconds=round(time.monotonic() - started, 3),
            output=stdout_text.strip(),
            stdout_path=str(stdout_path.resolve()),
            stderr_path=str(stderr_path.resolve()),
            work_dir=str(work_dir.resolve()),
            cancel_reason=(
                "project stop requested" if outcome.status == "cancelled" else None
            ),
            execution_backend=self.name,
            isolation=self.isolation,
            backend_run_id=handle.run_id if handle is not None else None,
            cleanup_policy=cleanup.policy,
            cleanup_status=cleanup.status,
            cleanup_detail=cleanup.detail,
            backend_metadata=metadata,
        )
