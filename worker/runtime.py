from __future__ import annotations

import asyncio
from pathlib import Path

from sandbox.artifacts import ArtifactStore
from sandbox.bridge import SandboxBridgeServer
from sandbox.docker_runtime import DockerRuntimeError, DockerSandboxRuntime
from sandbox.service import SandboxService
from sandbox.settings import SandboxSettings
from worker.backend import ExecutionBackend
from worker.models import ProviderConfig, ToolSandboxConfig, WorkerResult
from worker.qinyuan_provider import QinyuanMainProvider


class ProviderRuntime:
    def __init__(
        self,
        backend: ExecutionBackend,
        tool_config: ToolSandboxConfig,
        artifact_root: Path,
    ) -> None:
        self.backend = backend
        self.bridge: SandboxBridgeServer | None = None
        self.sandbox: SandboxService | None = None
        self.docker_runtime: DockerSandboxRuntime | None = None
        if tool_config.enabled:
            settings = SandboxSettings(
                image=tool_config.image,
                image_digest=tool_config.image_digest,
                max_output_bytes=tool_config.max_output_bytes,
                max_artifact_bytes=tool_config.max_artifact_bytes,
                max_command_timeout_seconds=tool_config.max_command_timeout_seconds,
                docker_api_timeout_seconds=tool_config.docker_api_timeout_seconds,
                kill_grace_seconds=tool_config.kill_grace_seconds,
                artifact_root=artifact_root,
                network=tool_config.network,
                memory_limit=tool_config.memory_limit,
                cpu_limit=tool_config.cpu_limit,
                pids_limit=tool_config.pids_limit,
            )
            runtime = DockerSandboxRuntime(settings, runtime_command=tool_config.runtime_command)
            self.docker_runtime = runtime
            artifacts = ArtifactStore(artifact_root, max_bytes=tool_config.max_artifact_bytes)
            self.sandbox = SandboxService(runtime, settings, artifact_store=artifacts)
            self.bridge = SandboxBridgeServer(self.sandbox)
        self.provider = QinyuanMainProvider(
            backend=backend,
            bridge=self.bridge,
            sandbox=self.sandbox,
        )

    async def start(self) -> None:
        if self.bridge is not None:
            assert self.docker_runtime is not None
            try:
                await self.docker_runtime.preflight()
                await self.docker_runtime.cleanup_orphans()
            except DockerRuntimeError:
                self.provider.set_tool_unavailable("docker_unavailable")
            else:
                self.provider.set_tool_unavailable(None)
            await self.bridge.start()

    async def close(self) -> tuple[str, ...]:
        if self.bridge is None:
            return ()
        return await self.bridge.close()

    async def execute(
        self,
        provider: ProviderConfig,
        request: str,
        challenge_dir: Path | None,
        work_dir: Path,
        on_chunk=None,
        cancel_event: asyncio.Event | None = None,
        max_log_bytes: int = 8 * 1024 * 1024,
        *,
        execution_kind: str = "execution",
        thread_id: str | None = None,
        tool_profile_override: str | None = None,
    ) -> WorkerResult:
        return await self.provider.execute(
            provider,
            request,
            challenge_dir,
            work_dir,
            on_chunk=on_chunk,
            cancel_event=cancel_event,
            max_log_bytes=max_log_bytes,
            execution_kind=execution_kind,
            thread_id=thread_id,
            tool_profile_override=tool_profile_override,
        )
