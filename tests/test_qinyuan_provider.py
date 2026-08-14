import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from sandbox.bridge import SandboxBridgeServer
from sandbox.docker_runtime import DockerRuntimeError
from sandbox.models import RuntimeContainer
from sandbox.service import SandboxService
from worker.models import ProviderConfig, ToolSandboxConfig, WorkerResult
from worker.qinyuan_provider import QinyuanMainProvider
from worker.runtime import ProviderRuntime
from tests.test_sandbox_contracts import FakeRuntime


class FakeBackend:
    def __init__(self) -> None:
        self.environments: list[dict[str, str] | None] = []

    async def execute(
        self, provider, request, challenge_dir, work_dir,
        on_chunk=None, cancel_event=None, max_log_bytes=0,
        environment=None, execution_context=None,
    ) -> WorkerResult:
        self.environments.append(dict(environment) if environment is not None else None)
        stdout = work_dir / "stdout.txt"
        stderr = work_dir / "stderr.txt"
        work_dir.mkdir(parents=True, exist_ok=True)
        stdout.write_text("BLACKBOARD_RESULT: {}\n", encoding="utf-8")
        stderr.write_text("", encoding="utf-8")
        return WorkerResult(
            provider=provider.name, priority=provider.priority, status="success",
            exit_code=0, duration_seconds=0.01, output="BLACKBOARD_RESULT: {}",
            stdout_path=str(stdout), stderr_path=str(stderr), work_dir=str(work_dir),
            execution_backend="local", isolation="unsafe_local",
        )


class ToolCallingBackend(FakeBackend):
    def __init__(self, bridge: SandboxBridgeServer) -> None:
        super().__init__()
        self.bridge = bridge

    async def execute(self, *args, environment=None, **kwargs) -> WorkerResult:
        assert environment is not None
        token = Path(environment["QINYUAN_SANDBOX_TOKEN_FILE"]).read_text(encoding="utf-8").strip()
        for call_id in ("scan-1", "scan-2"):
            await self.bridge.execute(token, {
                "protocol_version": 1,
                "call_id": call_id,
                "tool": "nmap",
                "arguments": ["-sT", "127.0.0.1"],
                "timeout_seconds": 5,
            })
        return await super().execute(*args, environment=environment, **kwargs)


def provider() -> ProviderConfig:
    return ProviderConfig(
        "qinyuan-main", True, 1, ("qinyuan-main",), 5,
        tool_sandbox_profile="interactive_unrestricted", provider_isolation="host_process",
        host_tools_disabled=False,
    )


def sandbox_provider() -> ProviderConfig:
    return ProviderConfig(
        "qinyuan-main", True, 1, ("qinyuan-main",), 5,
        tool_sandbox_profile="nmap", provider_isolation="host_process",
        host_tools_disabled=True,
    )


class QinyuanProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_unrestricted_planning_does_not_create_container(self) -> None:
        backend = FakeBackend()
        wrapper = QinyuanMainProvider(backend=backend)
        with tempfile.TemporaryDirectory() as directory:
            result = await wrapper.execute(provider(), "plan", None, Path(directory), execution_kind="planning")
            self.assertEqual(result.status, "success")
            self.assertEqual(backend.environments[0]["QINYUAN_RUN_CONTEXT_FILE"], str(Path(directory, "run_context.json").resolve()))
            self.assertNotIn("QINYUAN_SANDBOX_TOKEN_FILE", backend.environments[0])
            self.assertEqual(json.loads(Path(directory, "run_context.json").read_text())['tool_profile'], "interactive_unrestricted")
            self.assertEqual(result.backend_metadata["tool_isolation"], "host_process")
            self.assertFalse(result.backend_metadata["tool_sandbox_active"])
            self.assertFalse(Path(directory, "sandbox.token").exists())

    async def test_records_qinyuan_tool_usage_from_state_database(self) -> None:
        backend = FakeBackend()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "state" / "state.sqlite3"
            database.parent.mkdir()
            connection = sqlite3.connect(database)
            try:
                connection.execute(
                    "CREATE TABLE tool_calls (name TEXT NOT NULL)"
                )
                connection.executemany(
                    "INSERT INTO tool_calls(name) VALUES (?)",
                    [("powershell",), ("powershell",), ("list_skills",)],
                )
                connection.commit()
            finally:
                connection.close()

            result = await QinyuanMainProvider(backend=backend).execute(
                provider(),
                "execute",
                None,
                root,
                execution_kind="execution",
            )

        self.assertEqual(result.backend_metadata["tool_call_count"], 3)
        self.assertEqual(
            result.backend_metadata["tool_names"],
            {"list_skills": 1, "powershell": 2},
        )
        self.assertTrue(result.backend_metadata["observed"])

    async def test_execution_issues_lease_and_cleans_tool_sandbox(self) -> None:
        backend = FakeBackend()
        runtime = FakeRuntime()
        service = SandboxService(runtime)
        bridge = SandboxBridgeServer(service)
        await bridge.start()
        wrapper = QinyuanMainProvider(backend=backend, bridge=bridge)
        with tempfile.TemporaryDirectory() as directory:
            result = await wrapper.execute(sandbox_provider(), "execute", None, Path(directory), execution_kind="execution")
            self.assertEqual(result.status, "success")
            self.assertIn("QINYUAN_SANDBOX_TOKEN_FILE", backend.environments[0])
            self.assertEqual(runtime.create_calls, 0)
            self.assertEqual(result.backend_metadata["tool_profile"], "nmap_optional")
            self.assertEqual(result.backend_metadata["tool_isolation"], "container")
            self.assertTrue(result.backend_metadata["tool_sandbox_active"])
            self.assertEqual(result.backend_metadata["tool_cleanup_status"], "not_required")
            self.assertFalse(Path(directory, "sandbox.token").exists())
        await bridge.close()

    async def test_two_tool_calls_reuse_one_container(self) -> None:
        runtime = FakeRuntime()
        service = SandboxService(runtime)
        bridge = SandboxBridgeServer(service)
        await bridge.start()
        backend = ToolCallingBackend(bridge)
        wrapper = QinyuanMainProvider(backend=backend, bridge=bridge)
        with tempfile.TemporaryDirectory() as directory:
            result = await wrapper.execute(sandbox_provider(), "execute", None, Path(directory), execution_kind="execution")
        self.assertEqual(runtime.create_calls, 1)
        self.assertEqual(runtime.execute_calls, 2)
        self.assertEqual(runtime.destroy_calls, 1)
        self.assertEqual(result.backend_metadata["tool_cleanup_status"], "succeeded")
        self.assertEqual(result.backend_metadata["tool_sandbox_id"], result.backend_metadata["agent_run_id"].join(("sandbox-", "")))
        await bridge.close()

    async def test_docker_preflight_failure_does_not_block_other_providers(self) -> None:
        backend = FakeBackend()
        with tempfile.TemporaryDirectory() as directory:
            runtime = ProviderRuntime(
                backend,
                ToolSandboxConfig(enabled=True),
                Path(directory) / "artifacts",
            )
            assert runtime.docker_runtime is not None

            async def unavailable() -> str:
                raise DockerRuntimeError("Docker daemon is unavailable")

            runtime.docker_runtime.preflight = unavailable
            await runtime.start()
            self.assertIsNotNone(runtime.bridge)
            assert runtime.bridge is not None
            self.assertTrue(runtime.bridge.running)

            qinyuan_result = await runtime.execute(
                sandbox_provider(), "execute", None, Path(directory) / "qinyuan"
            )
            self.assertEqual(qinyuan_result.status, "unavailable")
            self.assertEqual(
                qinyuan_result.backend_metadata["health_category"],
                "docker_unavailable",
            )
            self.assertEqual(backend.environments, [])

            codex = ProviderConfig("codex", True, 1, ("codex",), 5)
            codex_result = await runtime.execute(
                codex, "execute", None, Path(directory) / "codex"
            )
            self.assertEqual(codex_result.status, "success")
            self.assertEqual(len(backend.environments), 1)
            await runtime.close()


if __name__ == "__main__":
    unittest.main()
