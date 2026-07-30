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
    replacements = {
        "{challenge_dir}": str(challenge_dir.resolve()) if challenge_dir else "",
        "{work_dir}": str(work_dir.resolve()),
    }
    expanded: list[str] = []
    for configured in provider.command:
        argument = configured
        for placeholder, value in replacements.items():
            argument = argument.replace(placeholder, value)
        expanded.append(argument)
    return tuple(expanded)


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


async def execute_provider(
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
    command = _resolve_executable(
        _expand_command(provider, challenge_dir, work_dir)
    )
    started = time.monotonic()
    status = "unavailable"
    exit_code: int | None = None
    stdout_text = ""

    kwargs: dict[str, object] = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True

    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=work_dir,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **kwargs,
        )
        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None
        stdin_task = asyncio.create_task(_write_stdin(process.stdin, request))
        stdout_task = asyncio.create_task(
            _read_stream(
                process.stdout,
                "stdout",
                stdout_path,
                on_chunk,
                max_log_bytes,
            )
        )
        stderr_task = asyncio.create_task(
            _read_stream(
                process.stderr,
                "stderr",
                stderr_path,
                on_chunk,
                max_log_bytes,
            )
        )
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
            timeout=provider.timeout_seconds,
            return_when=asyncio.FIRST_COMPLETED,
        )

        diagnostic: str | None = None
        if wait_task in done:
            exit_code = wait_task.result()
            status = "success" if exit_code == 0 else "failed"
        elif cancel_task is not None and cancel_task in done:
            await _terminate_process_tree(process)
            status = "cancelled"
            diagnostic = "provider execution was cancelled\n"
        else:
            await _terminate_process_tree(process)
            status = "timeout"
            diagnostic = (
                "provider exceeded timeout of "
                f"{provider.timeout_seconds:g} seconds\n"
            )

        if not wait_task.done():
            await wait_task
        await asyncio.gather(stdin_task, return_exceptions=True)
        if cancel_task is not None and not cancel_task.done():
            cancel_task.cancel()
            await asyncio.gather(cancel_task, return_exceptions=True)
        stdout_text, _ = await asyncio.gather(stdout_task, stderr_task)
        if diagnostic is not None:
            await _append_stderr(
                stderr_path,
                diagnostic,
                on_chunk,
                max_log_bytes,
            )
    except OSError as exc:
        message = f"cannot start provider command {command[0]!r}: {exc}\n"
        await _append_stderr(
            stderr_path,
            message,
            on_chunk,
            max_log_bytes,
        )

    return WorkerResult(
        provider=provider.name,
        priority=provider.priority,
        status=status,
        exit_code=exit_code,
        duration_seconds=round(time.monotonic() - started, 3),
        output=stdout_text.strip(),
        stdout_path=str(stdout_path.resolve()),
        stderr_path=str(stderr_path.resolve()),
        work_dir=str(work_dir.resolve()),
        cancel_reason=(
            "project stop requested" if status == "cancelled" else None
        ),
    )
