from __future__ import annotations

import asyncio
import inspect
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Awaitable, Callable, Mapping
from uuid import uuid4

from sandbox.bridge import SandboxBridgeServer
from sandbox.contracts import PROTOCOL_VERSION, RunContext
from sandbox.service import SandboxService
from worker.backend import ExecutionBackend
from worker.executor import LocalExecutionBackend
from worker.models import LogChunk, ProviderConfig, ProviderRunContext, WorkerResult


ChunkCallback = Callable[[LogChunk], Awaitable[None] | None]


def _deadline(timeout_seconds: float) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=max(1.0, timeout_seconds))


def _write_private(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


class QinyuanMainProvider:
    """External-process provider wrapper with a separate tool Sandbox lease."""

    def __init__(
        self,
        *,
        backend: ExecutionBackend | None = None,
        bridge: SandboxBridgeServer | None = None,
        sandbox: SandboxService | None = None,
    ) -> None:
        self.backend = backend or LocalExecutionBackend()
        self.bridge = bridge
        self.sandbox = sandbox or (bridge.service if bridge is not None else None)
        self._tool_unavailable_category: str | None = None

    def set_tool_unavailable(self, category: str | None) -> None:
        """Fail closed for Nmap without taking unrelated providers offline."""
        self._tool_unavailable_category = category

    @property
    def available(self) -> bool:
        return self.bridge is not None

    async def execute(
        self,
        provider: ProviderConfig,
        request: str,
        challenge_dir: Path | None,
        work_dir: Path,
        on_chunk: ChunkCallback | None = None,
        cancel_event: asyncio.Event | None = None,
        max_log_bytes: int = 8 * 1024 * 1024,
        *,
        execution_kind: str = "execution",
        thread_id: str | None = None,
        tool_profile_override: str | None = None,
    ) -> WorkerResult:
        if provider.name.casefold() not in {"qinyuan-main", "qinyuan_main"}:
            execute = self.backend.execute
            kwargs = {
                "on_chunk": on_chunk,
                "cancel_event": cancel_event,
                "max_log_bytes": max_log_bytes,
            }
            if "environment" in inspect.signature(execute).parameters:
                kwargs["environment"] = None
            return await execute(provider, request, challenge_dir, work_dir, **kwargs)

        run_id = str(uuid4())
        thread = thread_id or work_dir.name
        profile = tool_profile_override or ("no_tools" if execution_kind == "planning" else (
            "nmap_optional" if provider.tool_sandbox_profile == "nmap" else "no_tools"
        ))
        host_tools_blocked = profile == "nmap_optional" and not provider.host_tools_disabled
        deadline = _deadline(provider.timeout_seconds)
        endpoint = self.bridge.endpoint if self.bridge is not None else "disabled://sandbox"
        context = RunContext(
            protocol_version=PROTOCOL_VERSION,
            agent_run_id=run_id,
            thread_id=thread,
            tool_profile=profile,
            authorized_targets=provider.authorized_targets,
            deadline=deadline.isoformat(),
            work_dir=str(work_dir.resolve()),
            artifact_scope=str((work_dir / "artifacts").resolve()),
            result_contract={
                "prefix": "BLACKBOARD_RESULT:",
                "version": 1,
                "exactly_one": True,
            },
            sandbox_endpoint=endpoint,
        )
        work_dir.mkdir(parents=True, exist_ok=True)
        context_path = work_dir / "run_context.json"
        _write_private(context_path, __import__("json").dumps(context.to_dict(), ensure_ascii=False, indent=2) + "\n")
        token_path = work_dir / "sandbox.token"
        lease = None
        bridge_ready = self.bridge is not None and self.bridge.running
        if host_tools_blocked:
            (work_dir / "stdout.txt").write_text("", encoding="utf-8")
            (work_dir / "stderr.txt").write_text("host tools are not confirmed disabled\n", encoding="utf-8")
            return WorkerResult(
                provider=provider.name, priority=provider.priority,
                status="unavailable", exit_code=None, duration_seconds=0.0,
                output="", stdout_path=str((work_dir / "stdout.txt").resolve()),
                stderr_path=str((work_dir / "stderr.txt").resolve()),
                work_dir=str(work_dir.resolve()), execution_backend="qinyuan-main",
                isolation="host_process", cleanup_status="not_started",
                cleanup_detail="host tools are not confirmed disabled",
                backend_metadata={"health_category": "unsafe_host_tools", "tool_profile": "nmap_optional"},
            )
        if profile != "no_tools" and self._tool_unavailable_category is not None:
            (work_dir / "stdout.txt").write_text("", encoding="utf-8")
            (work_dir / "stderr.txt").write_text("tool sandbox runtime is unavailable\n", encoding="utf-8")
            return WorkerResult(
                provider=provider.name,
                priority=provider.priority,
                status="unavailable",
                exit_code=None,
                duration_seconds=0.0,
                output="",
                stdout_path=str((work_dir / "stdout.txt").resolve()),
                stderr_path=str((work_dir / "stderr.txt").resolve()),
                work_dir=str(work_dir.resolve()),
                execution_backend="qinyuan-main",
                isolation="host_process",
                cleanup_status="not_started",
                cleanup_detail="tool sandbox runtime is unavailable",
                backend_metadata={
                    "health_category": self._tool_unavailable_category,
                    "tool_profile": profile,
                },
            )
        if bridge_ready and self.bridge is not None:
            lease = self.bridge.issue_token(run_id, profile, deadline, token_file=token_path)
        elif profile != "no_tools":
            (work_dir / "stdout.txt").write_text("", encoding="utf-8")
            (work_dir / "stderr.txt").write_text("sandbox bridge is not configured\n", encoding="utf-8")
            return WorkerResult(
                provider=provider.name,
                priority=provider.priority,
                status="unavailable",
                exit_code=None,
                duration_seconds=0.0,
                output="",
                stdout_path=str((work_dir / "stdout.txt").resolve()),
                stderr_path=str((work_dir / "stderr.txt").resolve()),
                work_dir=str(work_dir.resolve()),
                execution_backend="qinyuan-main",
                isolation="host_process",
                cleanup_status="not_started",
                cleanup_detail="sandbox bridge is not running",
                backend_metadata={"health_category": "bridge_unavailable", "tool_profile": profile},
            )
        environment: dict[str, str] = {
            "QINYUAN_RUN_CONTEXT_FILE": str(context_path.resolve()),
        }
        if bridge_ready and self.bridge is not None and lease is not None:
            environment["QINYUAN_SANDBOX_TOKEN_FILE"] = str(token_path.resolve())
            environment["QINYUAN_SANDBOX_ENDPOINT"] = endpoint
        result: WorkerResult | None = None
        cleanup_status = "not_required"
        cleanup_detail: str | None = None
        created_session = None
        try:
            execute = self.backend.execute
            kwargs: dict[str, object] = {
                "on_chunk": on_chunk,
                "cancel_event": cancel_event,
                "max_log_bytes": max_log_bytes,
                "environment": environment,
                "execution_context": ProviderRunContext(
                    run_id, thread, execution_kind, profile,
                    context.deadline, context.work_dir,
                    context.artifact_scope, endpoint,
                ),
            }
            parameters = inspect.signature(execute).parameters
            kwargs = {key: value for key, value in kwargs.items() if key in parameters or any(item.kind is inspect.Parameter.VAR_KEYWORD for item in parameters.values())}
            result = await execute(provider, request, challenge_dir, work_dir, **kwargs)
        finally:
            if self.sandbox is not None:
                created_session = self.sandbox.sessions.get(run_id)
            if self.bridge is not None:
                self.bridge.revoke_token(run_id)
            if self.sandbox is not None:
                session = self.sandbox.sessions.get(run_id)
                if session is not None and session.active_call_id:
                    await self.sandbox.cancel(run_id, session.active_call_id)
                if session is not None:
                    if not await self.sandbox.stop_for_agent(run_id):
                        cleanup_status = "failed"
                        cleanup_detail = "tool sandbox destroy failed"
                    else:
                        cleanup_status = "succeeded"
            try:
                token_path.unlink(missing_ok=True)
            except OSError:
                pass
        assert result is not None
        session_metadata = created_session
        metadata = {
            **result.backend_metadata,
            "provider_isolation": "host_process",
            "tool_isolation": "container",
            "agent_run_id": run_id,
            "tool_profile": profile,
            "tool_sandbox_id": session_metadata.sandbox_id if session_metadata else None,
            "tool_sandbox_image": session_metadata.container.image if session_metadata and session_metadata.container else None,
            "tool_cleanup_status": cleanup_status,
            "run_context_file": str(context_path.resolve()),
        }
        return replace(
            result,
            execution_backend="qinyuan-main",
            isolation="host_process",
            cleanup_status=cleanup_status,
            cleanup_detail=cleanup_detail or result.cleanup_detail,
            backend_metadata=metadata,
        )


def build_qinyuan_executor(
    backend: ExecutionBackend,
    bridge: SandboxBridgeServer | None,
) -> QinyuanMainProvider:
    return QinyuanMainProvider(backend=backend, bridge=bridge)
