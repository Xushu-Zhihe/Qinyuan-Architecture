import asyncio
import os
import re
import shutil
import subprocess
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from worker.executor import _terminate_process_tree
from worker.models import ProviderConfig, WorkerResult


HEALTH_STATUSES = frozenset(
    {
        "healthy",
        "unhealthy",
        "auth_failed",
        "missing",
        "cooldown",
        "unknown",
    }
)
TERMINAL_HEALTH_STATUSES = frozenset(
    {"unhealthy", "auth_failed", "missing"}
)


@dataclass(frozen=True, slots=True)
class ProbeResult:
    status: str
    detail: str = ""


HealthProbe = Callable[[ProviderConfig], Awaitable[ProbeResult]]


_AUTH_PATTERN = re.compile(
    r"(?i)(?:\b401\b|\b403\b|unauthori[sz]ed|forbidden|not[ -]?logged[ -]?in|"
    r"login required|authentication (?:failed|required)|请(?:先)?登录|未登录|认证失败)"
)
_MISSING_PATTERN = re.compile(
    r"(?i)(?:no such file|not found|cannot find|is not recognized|winerror 2|"
    r"找不到指定的文件|不是内部或外部命令)"
)
_TEMPORARY_PATTERN = re.compile(
    r"(?i)(?:\b5\d\d\b|timed? out|timeout|temporar(?:y|ily)|connection "
    r"(?:reset|refused|aborted)|network|dns|service unavailable|rate limit|"
    r"网络|连接.*(?:重置|拒绝|中断)|服务不可用)"
)
_SECRET_PATTERN = re.compile(
    r"(?i)\b(api[ _-]?key|access[ _-]?token|refresh[ _-]?token|token|"
    r"authorization|bearer|secret|password)\b\s*[:=]?\s*"
    r"(?:bearer\s+)?[^\s,;]+"
)


def redact_error(value: str, limit: int = 400) -> str:
    normalized = " ".join(value.replace("\x00", "").split())
    redacted = _SECRET_PATTERN.sub(lambda match: f"{match.group(1)}=[redacted]", normalized)
    if len(redacted) > limit:
        return redacted[: limit - 3] + "..."
    return redacted


def classify_probe_failure(result: ProbeResult) -> str:
    if result.status == "missing":
        return "missing"
    if _AUTH_PATTERN.search(result.detail):
        return "auth_failed"
    if result.status == "timeout" or _TEMPORARY_PATTERN.search(result.detail):
        return "cooldown"
    return "unhealthy"


def classify_worker_failure(result: WorkerResult, detail: str) -> str:
    if _AUTH_PATTERN.search(detail):
        return "auth_failed"
    if result.status == "unavailable":
        return "missing" if _MISSING_PATTERN.search(detail) else "unhealthy"
    if result.status in {"timeout", "failed"}:
        return "cooldown"
    return "unhealthy"


def summarize_failure(category: str, status: str, detail: str) -> str:
    if category == "auth_failed":
        code = "403" if re.search(r"\b403\b", detail) else "401"
        return f"authentication failed ({code}); sign in before retrying"
    if category == "missing":
        return "provider executable is missing"
    if category == "cooldown":
        if status == "timeout":
            return "provider timed out"
        if _TEMPORARY_PATTERN.search(detail):
            return "temporary provider or network failure"
        return "provider process failed"
    safe = redact_error(detail)
    return safe or f"provider health check failed ({status})"


def worker_failure_detail(result: WorkerResult) -> str:
    parts = [result.output]
    stderr_path = Path(result.stderr_path)
    if stderr_path.is_file():
        try:
            parts.append(stderr_path.read_text(encoding="utf-8", errors="replace")[-4096:])
        except OSError:
            pass
    return "\n".join(part for part in parts if part)


def replace_auth_failure_logs(result: WorkerResult, summary: str) -> None:
    for path_value, content in (
        (result.stdout_path, ""),
        (result.stderr_path, summary + "\n"),
    ):
        path = Path(path_value)
        try:
            path.write_text(content, encoding="utf-8")
        except OSError:
            pass


async def probe_provider(provider: ProviderConfig) -> ProbeResult:
    health = provider.health_check
    if health is None:
        return ProbeResult("skipped")

    executable = shutil.which(health.command[0])
    if executable is None and Path(health.command[0]).is_file():
        executable = str(Path(health.command[0]).resolve())
    if executable is None:
        return ProbeResult("missing", "provider executable was not found")
    command = (executable, *health.command[1:])
    kwargs: dict[str, object] = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True

    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **kwargs,
        )
    except OSError as exc:
        category = "missing" if isinstance(exc, FileNotFoundError) else "failed"
        return ProbeResult(category, str(exc))

    communicate = asyncio.create_task(process.communicate())
    done, _ = await asyncio.wait({communicate}, timeout=health.timeout_seconds)
    if communicate not in done:
        await _terminate_process_tree(process)
        stdout, stderr = await communicate
        detail = (stdout + stderr).decode("utf-8", errors="replace")[-4096:]
        return ProbeResult("timeout", detail)

    stdout, stderr = communicate.result()
    detail = (stdout + stderr).decode("utf-8", errors="replace")[-4096:]
    if process.returncode == 0:
        return ProbeResult("success")
    return ProbeResult("failed", detail)
