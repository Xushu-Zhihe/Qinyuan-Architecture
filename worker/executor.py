import asyncio
import os
import signal
import shutil
import subprocess
import time
from pathlib import Path

from worker.models import ProviderConfig, WorkerResult


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
    for argument in provider.command:
        for placeholder, value in replacements.items():
            argument = argument.replace(placeholder, value)
        expanded.append(argument)
    return tuple(expanded)


async def _terminate_process_tree(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    if os.name == "nt":
        killer = await asyncio.create_subprocess_exec(
            "taskkill",
            "/PID",
            str(process.pid),
            "/T",
            "/F",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await killer.communicate()
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
        await process.wait()
    except ProcessLookupError:
        pass


def _write_logs(
    work_dir: Path,
    stdout_text: str,
    stderr_text: str,
) -> tuple[Path, Path]:
    stdout_path = work_dir / "stdout.txt"
    stderr_path = work_dir / "stderr.txt"
    stdout_path.write_text(stdout_text, encoding="utf-8")
    stderr_path.write_text(stderr_text, encoding="utf-8")
    return stdout_path, stderr_path


async def execute_provider(
    provider: ProviderConfig,
    request: str,
    challenge_dir: Path | None,
    work_dir: Path,
) -> WorkerResult:
    work_dir.mkdir(parents=True, exist_ok=True)
    command = _resolve_executable(
        _expand_command(provider, challenge_dir, work_dir)
    )
    payload = request.encode("utf-8")
    started = time.monotonic()
    stdout_text = ""
    stderr_text = ""
    status = "unavailable"
    exit_code: int | None = None

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
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(payload),
                timeout=provider.timeout_seconds,
            )
            stdout_text = stdout.decode("utf-8", errors="replace")
            stderr_text = stderr.decode("utf-8", errors="replace")
            exit_code = process.returncode
            status = "success" if exit_code == 0 else "failed"
        except asyncio.TimeoutError:
            await _terminate_process_tree(process)
            status = "timeout"
            stderr_text = (
                f"provider exceeded timeout of {provider.timeout_seconds:g} seconds"
            )
    except OSError as exc:
        stderr_text = f"cannot start provider command {command[0]!r}: {exc}"

    stdout_path, stderr_path = _write_logs(work_dir, stdout_text, stderr_text)
    duration = round(time.monotonic() - started, 3)
    return WorkerResult(
        provider=provider.name,
        priority=provider.priority,
        status=status,
        exit_code=exit_code,
        duration_seconds=duration,
        output=stdout_text.strip(),
        stdout_path=str(stdout_path.resolve()),
        stderr_path=str(stderr_path.resolve()),
        work_dir=str(work_dir.resolve()),
    )
