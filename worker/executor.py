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
) -> str:
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    parts: list[str] = []
    with path.open("w", encoding="utf-8", newline="") as handle:
        while True:
            raw = await stream.read(4096)
            if not raw:
                break
            text = decoder.decode(raw)
            if text:
                handle.write(text)
                handle.flush()
                parts.append(text)
                await _emit(on_chunk, LogChunk(stream_name, text))
        tail = decoder.decode(b"", final=True)
        if tail:
            handle.write(tail)
            handle.flush()
            parts.append(tail)
            await _emit(on_chunk, LogChunk(stream_name, tail))
    return "".join(parts)


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
) -> None:
    with path.open("a", encoding="utf-8", newline="") as handle:
        handle.write(text)
        handle.flush()
    await _emit(on_chunk, LogChunk("stderr", text))


async def execute_provider(
    provider: ProviderConfig,
    request: str,
    challenge_dir: Path | None,
    work_dir: Path,
    on_chunk: ChunkCallback | None = None,
    cancel_event: asyncio.Event | None = None,
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
        process.stdin.write(request.encode("utf-8"))
        await process.stdin.drain()
        process.stdin.close()

        stdout_task = asyncio.create_task(
            _read_stream(process.stdout, "stdout", stdout_path, on_chunk)
        )
        stderr_task = asyncio.create_task(
            _read_stream(process.stderr, "stderr", stderr_path, on_chunk)
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
        if cancel_task is not None and not cancel_task.done():
            cancel_task.cancel()
            await asyncio.gather(cancel_task, return_exceptions=True)
        stdout_text, _ = await asyncio.gather(stdout_task, stderr_task)
        if diagnostic is not None:
            await _append_stderr(stderr_path, diagnostic, on_chunk)
    except OSError as exc:
        message = f"cannot start provider command {command[0]!r}: {exc}\n"
        await _append_stderr(stderr_path, message, on_chunk)

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
    )
