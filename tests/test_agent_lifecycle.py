from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from orchestrator.projects import ProjectService
from orchestrator.store import BlackboardStore
from orchestrator.supervisor import ProjectSupervisor
from worker.agent_lifecycle import (
    AgentLifecycleReleaseResult,
    release_agent_lifecycle,
)
from worker.models import AgentLifecycleConfig, ProviderConfig, WorkerResult


def lifecycle_provider(command: tuple[str, ...]) -> ProviderConfig:
    return ProviderConfig(
        "persistent",
        True,
        10,
        (
            "unused",
            "--sandbox-owner-id",
            "{agent_owner_id}",
            "--sandbox-work-dir",
            "{agent_work_dir}",
        ),
        5,
        agent_lifecycle=AgentLifecycleConfig(command, 5),
    )


class AgentLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_release_command_expands_owner_and_writes_audit_logs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            provider = lifecycle_provider(
                (
                    sys.executable,
                    "-c",
                    "import sys; print('released:' + sys.argv[1])",
                    "{agent_owner_id}",
                )
            )

            result = await release_agent_lifecycle(
                provider,
                "qinyuan-bat:project-1:persistent",
                root / "workspace",
                root / "release-log",
            )

            self.assertEqual(result.status, "released")
            self.assertEqual(result.exit_code, 0)
            self.assertIn(
                "released:qinyuan-bat:project-1:persistent",
                Path(result.stdout_path).read_text(encoding="utf-8"),
            )

    async def test_supervisor_reuses_identity_serially_and_releases_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = BlackboardStore.create(
                root / "blackboard.db",
                "project-1",
                "Recover the flag",
                "Challenge",
                (),
            )
            self.addAsyncCleanup(asyncio.to_thread, store.close)
            provider = lifecycle_provider(("unused", "{agent_owner_id}"))
            calls: list[tuple[str, Path]] = []
            active = 0
            maximum_active = 0

            async def execute(config, request, challenge_dir, work_dir, **kwargs):
                nonlocal active, maximum_active
                active += 1
                maximum_active = max(maximum_active, active)
                try:
                    calls.append(
                        (kwargs["agent_owner_id"], kwargs["agent_work_dir"])
                    )
                    await asyncio.sleep(0.01)
                    work_dir.mkdir(parents=True, exist_ok=True)
                    stdout = work_dir / "stdout.txt"
                    stderr = work_dir / "stderr.txt"
                    stdout.write_text("ok", encoding="utf-8")
                    stderr.write_text("", encoding="utf-8")
                    return WorkerResult(
                        provider=config.name,
                        priority=config.priority,
                        status="success",
                        exit_code=0,
                        duration_seconds=0.01,
                        output="ok",
                        stdout_path=str(stdout),
                        stderr_path=str(stderr),
                        work_dir=str(work_dir),
                    )
                finally:
                    active -= 1

            release_result = AgentLifecycleReleaseResult(
                "released",
                0,
                0.01,
                str(root / "release-stdout.txt"),
                str(root / "release-stderr.txt"),
            )
            with patch(
                "orchestrator.supervisor.release_agent_lifecycle",
                new=AsyncMock(return_value=release_result),
            ) as release:
                supervisor = ProjectSupervisor(
                    store=store,
                    providers=(provider,),
                    challenge_dir=None,
                    project_dir=root,
                    execute=execute,
                )
                await asyncio.gather(
                    supervisor._run_provider(
                        provider,
                        "first",
                        root / "executions" / "first",
                        kind="execution",
                    ),
                    supervisor._run_provider(
                        provider,
                        "second",
                        root / "executions" / "second",
                        kind="execution",
                    ),
                )
                await supervisor.stop()
                await supervisor.stop()

            expected_owner = "qinyuan-bat:project-1:persistent"
            expected_workspace = root / "agent-workspaces" / "persistent"
            self.assertEqual(calls, [(expected_owner, expected_workspace)] * 2)
            self.assertEqual(maximum_active, 1)
            release.assert_awaited_once()
            self.assertEqual(release.await_args.args[1], expected_owner)
            self.assertEqual(release.await_args.args[2], expected_workspace)

    async def test_stop_releases_lifecycle_without_an_active_supervisor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            provider = lifecycle_provider(("unused", "{agent_owner_id}"))
            service = ProjectService(
                root / "data",
                provider_configs=(provider,),
            )
            self.addAsyncCleanup(service.aclose)
            project = service.create_project(
                name="Persistent Agent",
                goal="Recover the flag",
                origin="Challenge",
                hints=(),
                mode="run",
                provider_names=(provider.name,),
                agent_limit=1,
                challenge_path=None,
            )
            release_result = AgentLifecycleReleaseResult(
                "released",
                0,
                0.01,
                str(root / "release-stdout.txt"),
                str(root / "release-stderr.txt"),
            )
            with patch(
                "orchestrator.supervisor.release_agent_lifecycle",
                new=AsyncMock(return_value=release_result),
            ) as release:
                stopped = await service.stop_project(project.id)

            self.assertEqual(stopped.status, "stopped")
            release.assert_awaited_once()
            self.assertEqual(
                release.await_args.args[1],
                f"qinyuan-bat:{project.id}:persistent",
            )


if __name__ == "__main__":
    unittest.main()
