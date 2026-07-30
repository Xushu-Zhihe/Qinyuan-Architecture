import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from worker.container_backend import ContainerExecutionBackend
from worker.models import ContainerBackendConfig, ProviderConfig


RUN_DOCKER_TESTS = os.environ.get("QINYUAN_RUN_DOCKER_TESTS") == "1"
DOCKER_IMAGE = os.environ.get(
    "QINYUAN_TEST_CONTAINER_IMAGE",
    "python:3.12-alpine",
)


@unittest.skipUnless(RUN_DOCKER_TESTS, "set QINYUAN_RUN_DOCKER_TESTS=1")
@unittest.skipIf(os.name == "nt", "Linux container integration runs in Linux CI")
class DockerContainerIntegrationTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        docker = shutil.which("docker")
        if docker is None:
            raise RuntimeError("Docker CLI is required when container tests are enabled")
        info = subprocess.run(
            [docker, "info"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if info.returncode != 0:
            raise RuntimeError(f"Docker daemon is unavailable: {info.stderr.strip()}")
        pull = subprocess.run(
            [docker, "pull", DOCKER_IMAGE],
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
        if pull.returncode != 0:
            raise RuntimeError(f"cannot pull {DOCKER_IMAGE}: {pull.stderr.strip()}")
        cls.docker = docker

    def backend(self, cleanup: str = "remove") -> ContainerExecutionBackend:
        return ContainerExecutionBackend(
            ContainerBackendConfig(
                runtime_command=(self.docker,),
                default_image=DOCKER_IMAGE,
                network="none",
                environment=(("EXPLICIT_VALUE", "configured"),),
                environment_allowlist=("ALLOWED_VALUE",),
                cleanup_policy=cleanup,
                challenge_mount_path="/challenge",
                work_mount_path="/work",
                read_only_root=True,
                memory_limit="256m",
                cpu_limit=1.0,
                pids_limit=64,
                stop_timeout_seconds=1.0,
                setup_timeout_seconds=600.0,
            )
        )

    @staticmethod
    def provider(command: tuple[str, ...], timeout: float = 30.0) -> ProviderConfig:
        return ProviderConfig(
            name="docker-fake",
            enabled=True,
            priority=1,
            command=command,
            timeout_seconds=timeout,
            container_image=DOCKER_IMAGE,
        )

    def inspect(self, container_id: str, template: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.docker, "inspect", "--format", template, container_id],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    def force_remove(self, container_id: str | None) -> None:
        if container_id:
            subprocess.run(
                [self.docker, "rm", "--force", container_id],
                capture_output=True,
                timeout=30,
                check=False,
            )

    async def test_readonly_challenge_writable_work_and_environment_allowlist(self) -> None:
        script = r'''
import json
import os
from pathlib import Path
challenge = Path("/challenge/input.txt")
blocked = False
try:
    challenge.write_text("modified", encoding="utf-8")
except OSError:
    blocked = True
Path("/work/written.txt").write_text("work-ok", encoding="utf-8")
print(json.dumps({
    "challenge_read_only": blocked,
    "challenge_value": challenge.read_text(encoding="utf-8"),
    "explicit": os.environ.get("EXPLICIT_VALUE"),
    "allowed": os.environ.get("ALLOWED_VALUE"),
    "host_secret_present": "HOST_SECRET_SHOULD_NOT_PASS" in os.environ,
}, sort_keys=True))
'''
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {
                "ALLOWED_VALUE": "allowed",
                "HOST_SECRET_SHOULD_NOT_PASS": "secret",
            },
            clear=False,
        ):
            root = Path(directory)
            challenge = root / "challenge"
            work = root / "work"
            challenge.mkdir()
            work.mkdir()
            (challenge / "input.txt").write_text("original", encoding="utf-8")
            result = await self.backend().execute(
                self.provider(("python", "-c", script)),
                "",
                challenge,
                work,
            )
            payload = json.loads(result.output)

            self.assertEqual(result.status, "success")
            self.assertTrue(payload["challenge_read_only"])
            self.assertEqual(payload["challenge_value"], "original")
            self.assertEqual((challenge / "input.txt").read_text(encoding="utf-8"), "original")
            self.assertEqual((work / "written.txt").read_text(encoding="utf-8"), "work-ok")
            self.assertEqual(payload["explicit"], "configured")
            self.assertEqual(payload["allowed"], "allowed")
            self.assertFalse(payload["host_secret_present"])
            self.assertEqual(result.cleanup_status, "removed")
            assert result.backend_run_id is not None
            self.assertNotEqual(
                self.inspect(result.backend_run_id, "{{.Id}}").returncode,
                0,
            )

    async def test_timeout_and_cancel_remove_containers(self) -> None:
        sleep = ("python", "-c", "import time; time.sleep(30)")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            timed_out = await self.backend().execute(
                self.provider(sleep, timeout=0.5),
                "",
                None,
                root / "timeout",
            )
            self.assertEqual(timed_out.status, "timeout")
            self.assertEqual(timed_out.cleanup_status, "removed")
            assert timed_out.backend_run_id is not None
            self.assertNotEqual(
                self.inspect(timed_out.backend_run_id, "{{.Id}}").returncode,
                0,
            )

            cancel_event = asyncio.Event()
            asyncio.get_running_loop().call_later(0.5, cancel_event.set)
            cancelled = await self.backend().execute(
                self.provider(sleep, timeout=30.0),
                "",
                None,
                root / "cancelled",
                cancel_event=cancel_event,
            )
            self.assertEqual(cancelled.status, "cancelled")
            self.assertEqual(cancelled.cleanup_status, "removed")
            assert cancelled.backend_run_id is not None
            self.assertNotEqual(
                self.inspect(cancelled.backend_run_id, "{{.Id}}").returncode,
                0,
            )

    async def test_keep_and_stop_policies_match_runtime_state(self) -> None:
        command = ("python", "-c", "print('ok')")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for policy, cleanup_status in (("keep", "kept"), ("stop", "stopped")):
                result = await self.backend(policy).execute(
                    self.provider(command),
                    "",
                    None,
                    root / policy,
                )
                try:
                    self.assertEqual(result.status, "success")
                    self.assertEqual(result.cleanup_status, cleanup_status)
                    assert result.backend_run_id is not None
                    inspected = self.inspect(
                        result.backend_run_id,
                        "{{.State.Running}}",
                    )
                    self.assertEqual(inspected.returncode, 0)
                    self.assertEqual(inspected.stdout.strip().lower(), "false")
                finally:
                    self.force_remove(result.backend_run_id)


if __name__ == "__main__":
    unittest.main()
