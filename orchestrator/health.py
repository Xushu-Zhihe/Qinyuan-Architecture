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
from worker.provider_adapters import ProviderAdapter, get_provider_adapter
from worker.redaction import redact_error


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
    cli_version: str = "unknown"


HealthProbe = Callable[[ProviderConfig], Awaitable[ProbeResult]]


_PROBE_AUTH_PATTERN = re.compile(
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
_DOCKER_PATTERN = re.compile(
    r"(?i)(?:docker (?:daemon|desktop)|cannot connect to (?:the )?docker"
    r"|docker is not running)"
)
_PERMISSION_PATTERN = re.compile(
    r"(?i)(?:permission denied|access is denied|winerror 5|拒绝访问|权限)"
)


def classify_probe_failure(result: ProbeResult) -> str:
    if result.status == "missing":
        return "missing"
    if _PROBE_AUTH_PATTERN.search(result.detail):
        return "auth_failed"
    if (
        result.status == "timeout"
        or _TEMPORARY_PATTERN.search(result.detail)
        or _DOCKER_PATTERN.search(result.detail)
    ):
        return "cooldown"
    return "unhealthy"


def classify_worker_failure(
    result: WorkerResult,
    detail: str,
    adapter: ProviderAdapter | None = None,
) -> str:
    adapter = adapter or get_provider_adapter(result.provider)
    if adapter.is_auth_failure(detail):
        return "auth_failed"
    if result.status == "unavailable":
        return "missing" if _MISSING_PATTERN.search(detail) else "unhealthy"
    if result.status in {"timeout", "failed"}:
        return "cooldown"
    return "unhealthy"


def summarize_failure(category: str, status: str, detail: str) -> str:
    if category == "auth_failed":
        match = re.search(r"\b(401|403)\b", detail)
        if match:
            return f"authentication failed ({match.group(1)}); sign in before retrying"
        return "authentication failed; sign in before retrying"
    if category == "missing":
        return "provider executable is missing"
    if category == "cooldown":
        if status == "timeout" or re.search(
            r"(?i)timed? out|timeout",
            detail,
        ):
            suffix = redact_error(detail, limit=240)
            return "provider timed out" + (f": {suffix}" if suffix else "")
        if _TEMPORARY_PATTERN.search(detail) or _DOCKER_PATTERN.search(detail):
            suffix = redact_error(detail, limit=240)
            return "temporary provider or network failure" + (
                f": {suffix}" if suffix else ""
            )
        suffix = redact_error(detail, limit=240)
        return "provider process failed" + (f": {suffix}" if suffix else "")
    safe = redact_error(detail)
    if _PERMISSION_PATTERN.search(detail):
        return "provider workspace or permission failure" + (
            f": {safe}" if safe else ""
        )
    return safe or f"provider health check failed ({status})"


def worker_failure_detail(result: WorkerResult) -> str:
    parts = [result.output]
    metadata_error = result.backend_metadata.get("startup_error")
    if metadata_error:
        parts.append(str(metadata_error))
    if result.cleanup_detail:
        parts.append(str(result.cleanup_detail))
    stderr_path = Path(result.stderr_path)
    if stderr_path.is_file():
        try:
            parts.append(stderr_path.read_text(encoding="utf-8", errors="replace")[-4096:])
        except OSError:
            pass
    return redact_error("\n".join(part for part in parts if part), limit=4096)


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
        try:
            stdout, stderr = await asyncio.wait_for(communicate, timeout=2.0)
        except asyncio.TimeoutError:
            communicate.cancel()
            await asyncio.gather(communicate, return_exceptions=True)
            stdout, stderr = b"", b"health probe process did not close pipes"
        detail = (stdout + stderr).decode("utf-8", errors="replace")[-4096:]
        return ProbeResult("timeout", detail)

    stdout, stderr = communicate.result()
    detail = (stdout + stderr).decode("utf-8", errors="replace")[-4096:]
    if process.returncode == 0:
        version = redact_error(detail, limit=200) or "unknown"
        return ProbeResult("success", cli_version=version)
    return ProbeResult("failed", detail)
