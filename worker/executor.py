import asyncio
import codecs
import inspect
import os
import shutil
import signal
import subprocess
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from worker.backend import (
    BackendHandle,
    BackendOutcome,
    CleanupOutcome,
    ExecutionBackend,
)
from worker.commands import local_command
from worker.models import LogChunk, ProviderConfig, WorkerResult
from worker.redaction import redact_text


ChunkCallback = Callable[[LogChunk], Awaitable[None] | None]


def _resolve_executable(command: tuple[str, ...]) -> tuple[str, ...]:
    if os.name != "nt":
        return command
    executable = shutil.which(command[0])
    return (executable, *command[1:]) if executable else command


def _expand_command(
    provider: ProviderConfig,
    challenge_dir: Path | None,
    work_dir: Path,
) -> tuple[str, ...]:
    # Compatibility wrapper retained for callers/tests that used the old helper.
    return local_command(provider, challenge_dir, work_dir)


async def _emit(on_chunk: ChunkCallback | None, chunk: LogChunk) -> None:
    if on_chunk is None:
        return
    result = on_chunk(chunk)
    if inspect.isawaitable(result):
        await result


async def _read_stream(
    stream: asyncio.StreamReader,
    stream_name: str,
    path: Path,
    on_chunk: ChunkCallback | None,
    max_log_bytes: int,
) -> str:
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    parts: list[str] = []
    pending = ""
    written_bytes = 0

    async def write_safe(text: str, handle) -> None:
        nonlocal written_bytes
        if not text or written_bytes >= max_log_bytes:
            return
        safe = redact_text(text)
        encoded = safe.encode("utf-8")
        remaining = max_log_bytes - written_bytes
        if len(encoded) > remaining:
            safe = encoded[:remaining].decode("utf-8", errors="ignore")
            encoded = safe.encode("utf-8")
        if not safe:
            return
        written_bytes += len(encoded)
        handle.write(safe)
        handle.flush()
        parts.append(safe)
        await _emit(on_chunk, LogChunk(stream_name, safe))

    with path.open("w", encoding="utf-8", newline="") as handle:
        while True:
            raw = await stream.read(4096)
            if not raw:
                break
            text = decoder.decode(raw)
            pending += text
            if written_bytes >= max_log_bytes:
                pending = ""
                continue
            lines = pending.splitlines(keepends=True)
            if lines and not lines[-1].endswith(("\n", "\r")):
                pending = lines.pop()
            else:
                pending = ""
            for line in lines:
                await write_safe(line, handle)
            if len(pending.encode("utf-8")) >= max_log_bytes:
                await write_safe(pending, handle)
                pending = ""
        pending += decoder.decode(b"", final=True)
        await write_safe(pending, handle)
    return "".join(parts)


async def _write_stdin(
    stream: asyncio.StreamWriter,
    request: str,
) -> None:
    try:
        stream.write(request.encode("utf-8"))
        await stream.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        stream.close()
        try:
            await stream.wait_closed()
        except (BrokenPipeError, ConnectionResetError):
            pass


async def _terminate_process_tree(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    if os.name == "nt":
        try:
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(process.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                await asyncio.wait_for(killer.communicate(), timeout=0.75)
            except asyncio.TimeoutError:
                try:
                    killer.kill()
                except ProcessLookupError:
                    pass
                await killer.wait()
        except OSError:
            pass
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        await asyncio.wait_for(process.wait(), timeout=1.0)
    except (asyncio.TimeoutError, ProcessLookupError):
        pass


async def _append_stderr(
    path: Path,
    text: str,
    on_chunk: ChunkCallback | None,
    max_log_bytes: int,
) -> None:
    safe = redact_text(text)
    try:
        written_bytes = path.stat().st_size
    except OSError:
        written_bytes = 0
    remaining = max(0, max_log_bytes - written_bytes)
    encoded = safe.encode("utf-8")[:remaining]
    safe = encoded.decode("utf-8", errors="ignore")
    if not safe:
        return
    with path.open("a", encoding="utf-8", newline="") as handle:
        handle.write(safe)
        handle.flush()
    await _emit(on_chunk, LogChunk("stderr", safe))


class LocalExecutionBackend(ExecutionBackend):
    name = "local"
    isolation = "unsafe_local"

    async def start(
        self,
        provider: ProviderConfig,
        command: tuple[str, ...],
        request: str,
        challenge_dir: Path | None,
        work_dir: Path,
    ) -> BackendHandle:
        kwargs: dict[str, object] = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        process = await asyncio.create_subprocess_exec(
            *_resolve_executable(command),
            cwd=work_dir,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **kwargs,
        )
        return BackendHandle(process=process, run_id=str(process.pid))

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

    async def wait(
        self,
        handle: BackendHandle,
        timeout_seconds: float,
        cancel_event: asyncio.Event | None,
    ) -> BackendOutcome:
        process = handle.process
        wait_task = asyncio.create_task(process.wait())
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
            exit_code = wait_task.result()
            outcome = BackendOutcome(
                "success" if exit_code == 0 else "failed",
                exit_code,
            )
        elif cancel_task is not None and cancel_task in done:
            await self.cancel(handle)
            outcome = BackendOutcome(
                "cancelled",
                None,
                "provider execution was cancelled\n",
            )
        else:
            await self.cancel(handle)
            outcome = BackendOutcome(
                "timeout",
                None,
                f"provider exceeded timeout of {timeout_seconds:g} seconds\n",
            )
        if not wait_task.done():
            try:
                await asyncio.wait_for(wait_task, timeout=1.0)
            except asyncio.TimeoutError:
                pass
        if cancel_task is not None and not cancel_task.done():
            cancel_task.cancel()
            await asyncio.gather(cancel_task, return_exceptions=True)
        return outcome

    async def cancel(self, handle: BackendHandle) -> None:
        await _terminate_process_tree(handle.process)

    async def cleanup(
        self,
        handle: BackendHandle,
        outcome: BackendOutcome,
    ) -> CleanupOutcome:
        return CleanupOutcome(None, "not_required", "local process exited")

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
        command = local_command(provider, challenge_dir, work_dir)
        started = time.monotonic()
        outcome = BackendOutcome("unavailable", None)
        cleanup = CleanupOutcome(None, "not_applicable")
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
            message = f"cannot start provider command {command[0]!r}: {exc}\n"
            await _append_stderr(
                stderr_path,
                message,
                on_chunk,
                max_log_bytes,
            )
            if handle is not None:
                cleanup = await self.cleanup(handle, outcome)

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
            backend_metadata={"challenge_enforcement": "prompt_only"},
        )


_LOCAL_BACKEND = LocalExecutionBackend()


async def execute_provider(
    provider: ProviderConfig,
    request: str,
    challenge_dir: Path | None,
    work_dir: Path,
    on_chunk: ChunkCallback | None = None,
    cancel_event: asyncio.Event | None = None,
    max_log_bytes: int = 8 * 1024 * 1024,
) -> WorkerResult:
    """Backward-compatible local executor.

    Local mode is deliberately labelled ``unsafe_local``.  A challenge path in
    the prompt is not an enforced read-only boundary.
    """
    return await _LOCAL_BACKEND.execute(
        provider,
        request,
        challenge_dir,
        work_dir,
        on_chunk=on_chunk,
        cancel_event=cancel_event,
        max_log_bytes=max_log_bytes,
    )
