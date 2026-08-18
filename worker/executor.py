import asyncio
import codecs
import inspect
import os
import shutil
import signal
import subprocess
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import nullcontext
from pathlib import Path

from worker.backend import (
    BackendHandle,
    BackendOutcome,
    CleanupOutcome,
    ExecutionBackend,
)
from worker.commands import local_command
from worker.models import LogChunk, ProviderConfig, WorkerResult
from worker.output_adapters import OutputAdapter, build_output_adapter
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
    adapter: OutputAdapter | None = None,
    raw_path: Path | None = None,
) -> str:
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    parts: list[str] = []
    pending = ""
    written_bytes = 0
    raw_written_bytes = 0
    transform = adapter or build_output_adapter("text")

    def write_raw(text: str, handle) -> None:
        nonlocal raw_written_bytes
        if handle is None or not text or raw_written_bytes >= max_log_bytes:
            return
        safe = redact_text(text)
        encoded = safe.encode("utf-8")[: max_log_bytes - raw_written_bytes]
        safe = encoded.decode("utf-8", errors="ignore")
        if not safe:
            return
        raw_written_bytes += len(safe.encode("utf-8"))
        handle.write(safe)
        handle.flush()

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

    raw_context = (
        raw_path.open("w", encoding="utf-8", newline="")
        if raw_path is not None
        else nullcontext(None)
    )
    with path.open("w", encoding="utf-8", newline="") as handle, raw_context as raw_handle:
        read_task: asyncio.Task[bytes] | None = None
        while True:
            if read_task is None:
                read_task = asyncio.create_task(stream.read(4096))
            try:
                raw = await asyncio.wait_for(
                    asyncio.shield(read_task),
                    timeout=0.1,
                )
            except TimeoutError:
                if pending:
                    for visible in transform.feed(pending):
                        await write_safe(visible, handle)
                    pending = ""
                continue
            read_task = None
            if not raw:
                break
            text = decoder.decode(raw)
            write_raw(text, raw_handle)
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
                for visible in transform.feed(line):
                    await write_safe(visible, handle)
            if len(pending.encode("utf-8")) >= 16 * 1024:
                for visible in transform.feed(pending):
                    await write_safe(visible, handle)
                pending = ""
        final_text = decoder.decode(b"", final=True)
        write_raw(final_text, raw_handle)
        pending += final_text
        if pending:
            for visible in transform.feed(pending):
                await write_safe(visible, handle)
        for visible in transform.finish():
            await write_safe(visible, handle)
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

    # Give providers a short chance to close MCP clients and external
    # resources before falling back to the existing forceful termination.
    try:
        if os.name == "nt":
            process.send_signal(getattr(signal, "CTRL_BREAK_EVENT", signal.SIGTERM))
        else:
            os.killpg(process.pid, signal.SIGTERM)
        await asyncio.wait_for(process.wait(), timeout=5.0)
        return
    except (OSError, ProcessLookupError, asyncio.TimeoutError, ValueError):
        pass

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
        cancel_file = work_dir / f".provider-cancel-{uuid.uuid4().hex}"
        environment = os.environ.copy()
        environment["QINYUAN_PROVIDER_CANCEL_FILE"] = str(cancel_file.resolve())
        process = await asyncio.create_subprocess_exec(
            *_resolve_executable(command),
            cwd=work_dir,
            env=environment,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **kwargs,
        )
        return BackendHandle(
            process=process,
            run_id=str(process.pid),
            metadata={"cancel_file": str(cancel_file.resolve())},
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
                    build_output_adapter(
                        str(handle.metadata.get("stream_format", "text"))
                    ),
                    raw_path=(
                        stdout_path.with_name("stdout.raw.jsonl")
                        if handle.metadata.get("stream_format") == "claude-stream-json"
                        else None
                    ),
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
        cancel_file_value = handle.metadata.get("cancel_file")
        cancel_file = (
            Path(str(cancel_file_value)) if cancel_file_value is not None else None
        )
        try:
            if cancel_file is not None:
                try:
                    cancel_file.write_text("cancel\n", encoding="utf-8")
                except OSError:
                    pass
                # Providers can use the cooperative marker to flush their own
                # clients before the process-tree signal is sent.
                await asyncio.sleep(0.1)
            await _terminate_process_tree(handle.process)
        finally:
            if cancel_file is not None:
                try:
                    cancel_file.unlink(missing_ok=True)
                except OSError:
                    pass

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
        origin_host: str | None = None,
    ) -> WorkerResult:
        stdout_path = work_dir / "stdout.txt"
        stderr_path = work_dir / "stderr.txt"
        started = time.monotonic()
        outcome = BackendOutcome("unavailable", None)
        cleanup = CleanupOutcome(None, "not_applicable")
        stdout_text = ""
        handle: BackendHandle | None = None
        command: tuple[str, ...] = ()
        startup_error: str | None = None

        try:
            work_dir.mkdir(parents=True, exist_ok=True)
            stdout_path.write_text("", encoding="utf-8")
            stderr_path.write_text("", encoding="utf-8")
            command = local_command(
                provider,
                challenge_dir,
                work_dir,
                origin_host=origin_host,
            )
            handle = await self.start(
                provider,
                command,
                request,
                challenge_dir,
                work_dir,
            )
            handle.metadata["stream_format"] = provider.stream_format
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
        except Exception as exc:
            command_name = command[0] if command else provider.command[0]
            startup_error = (
                f"cannot start provider command {command_name!r}: "
                f"{type(exc).__name__}: {exc}"
            )
            try:
                await _append_stderr(
                    stderr_path,
                    startup_error + "\n",
                    on_chunk,
                    max_log_bytes,
                )
            except OSError:
                pass
            if handle is not None:
                cleanup = await self.cleanup(handle, outcome)

        return WorkerResult(
            provider=provider.name,
            priority=provider.priority,
            status=outcome.status,
            exit_code=outcome.exit_code,
            duration_seconds=round(time.monotonic() - started, 3),
            output=(stdout_text.strip() or startup_error or ""),
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
            backend_metadata={
                "challenge_enforcement": "prompt_only",
                **(
                    {
                        "startup_error": redact_text(startup_error),
                        "command": [redact_text(str(item)) for item in command],
                    }
                    if startup_error
                    else {}
                ),
                **(
                    {"raw_stdout_path": str(stdout_path.with_name("stdout.raw.jsonl").resolve())}
                    if provider.stream_format == "claude-stream-json"
                    else {}
                ),
            },
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
    origin_host: str | None = None,
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
        origin_host=origin_host,
    )
