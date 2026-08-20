from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from worker.commands import lifecycle_release_command
from worker.executor import _terminate_process_tree
from worker.models import ProviderConfig
from worker.redaction import redact_text


@dataclass(frozen=True, slots=True)
class AgentLifecycleReleaseResult:
    status: str
    exit_code: int | None
    duration_seconds: float
    stdout_path: str
    stderr_path: str
    detail: str | None = None


def _resolve_executable(command: tuple[str, ...]) -> tuple[str, ...]:
    if os.name != "nt":
        return command
    executable = shutil.which(command[0])
    return (executable, *command[1:]) if executable else command


def _bounded_text(raw: bytes, maximum_bytes: int) -> str:
    decoded = redact_text(raw.decode("utf-8", errors="replace"))
    return decoded.encode("utf-8")[:maximum_bytes].decode(
        "utf-8",
        errors="ignore",
    )


async def release_agent_lifecycle(
    provider: ProviderConfig,
    agent_owner_id: str,
    agent_work_dir: Path,
    log_dir: Path,
    *,
    max_log_bytes: int = 8 * 1024 * 1024,
) -> AgentLifecycleReleaseResult:
    lifecycle = provider.agent_lifecycle
    if lifecycle is None:
        raise ValueError(f"provider {provider.name!r} has no Agent lifecycle")

    log_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = log_dir / "stdout.txt"
    stderr_path = log_dir / "stderr.txt"
    command = lifecycle_release_command(
        provider,
        agent_owner_id,
        agent_work_dir,
    )
    started = time.monotonic()
    process: asyncio.subprocess.Process | None = None
    communicate_task: asyncio.Task[tuple[bytes, bytes]] | None = None
    stdout = b""
    stderr = b""
    status = "startup_failed"
    exit_code: int | None = None
    detail: str | None = None
    kwargs: dict[str, object] = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True

    try:
        process = await asyncio.create_subprocess_exec(
            *_resolve_executable(command),
            cwd=log_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **kwargs,
        )
        communicate_task = asyncio.create_task(process.communicate())
        try:
            stdout, stderr = await asyncio.wait_for(
                asyncio.shield(communicate_task),
                timeout=lifecycle.release_timeout_seconds,
            )
            exit_code = process.returncode
            status = "released" if exit_code == 0 else "failed"
        except asyncio.TimeoutError:
            await _terminate_process_tree(process)
            stdout, stderr = await communicate_task
            status = "timeout"
            detail = (
                "Agent lifecycle release exceeded "
                f"{lifecycle.release_timeout_seconds:g} seconds"
            )
    except asyncio.CancelledError:
        if process is not None:
            await _terminate_process_tree(process)
        if communicate_task is not None:
            await asyncio.gather(communicate_task, return_exceptions=True)
        raise
    except OSError as exc:
        detail = f"cannot start Agent lifecycle release: {type(exc).__name__}: {exc}"
        stderr = detail.encode("utf-8", errors="replace")

    stdout_text = _bounded_text(stdout, max_log_bytes)
    stderr_text = _bounded_text(stderr, max_log_bytes)
    stdout_path.write_text(stdout_text, encoding="utf-8")
    stderr_path.write_text(stderr_text, encoding="utf-8")
    if detail is None and status != "released":
        detail = (
            stderr_text
            or stdout_text
            or f"release exited with {exit_code}"
        ).strip()
        detail = detail[:500]
    return AgentLifecycleReleaseResult(
        status=status,
        exit_code=exit_code,
        duration_seconds=round(time.monotonic() - started, 3),
        stdout_path=str(stdout_path.resolve()),
        stderr_path=str(stderr_path.resolve()),
        detail=detail,
    )
