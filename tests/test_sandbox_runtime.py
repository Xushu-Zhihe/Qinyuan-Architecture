import os
import unittest
from pathlib import Path
from uuid import uuid4

from sandbox.contracts import CommandRequest
from sandbox.docker_runtime import DockerSandboxRuntime
from sandbox.settings import SandboxSettings


@unittest.skipUnless(
    os.environ.get("QINYUAN_RUN_DOCKER_SMOKE") == "1",
    "set QINYUAN_RUN_DOCKER_SMOKE=1 to run real Docker Sandbox smoke tests",
)
class DockerSandboxSmokeTests(unittest.IsolatedAsyncioTestCase):
    async def test_nmap_version_and_authorized_target(self) -> None:
        image = os.environ.get("QINYUAN_SANDBOX_IMAGE", "qinyuan-tools:nmap-1")
        target = os.environ.get("QINYUAN_SANDBOX_TEST_TARGET")
        network = os.environ.get("QINYUAN_SANDBOX_TEST_NETWORK", "none")
        if not target:
            self.skipTest("QINYUAN_SANDBOX_TEST_TARGET is required for an authorized scan")
        settings = SandboxSettings(
            image=image,
            network=network,
            artifact_root=Path.cwd() / ".pytest-sandbox-artifacts",
        )
        runtime = DockerSandboxRuntime(settings)
        container = None
        try:
            await runtime.preflight()
            container = await runtime.create(f"smoke-{uuid4()}")
            version = await runtime.execute(
                container,
                CommandRequest(1, "version", "nmap", ("--version",), 30),
            )
            self.assertEqual(version.status, "completed")
            self.assertIn("Nmap version", version.stdout)
            scan = await runtime.execute(
                container,
                CommandRequest(
                    1,
                    "authorized-scan",
                    "nmap",
                    ("-sT", "-Pn", "-n", "-p", "80,443", target),
                    60,
                ),
            )
            self.assertEqual(scan.status, "completed")
        finally:
            try:
                if container is not None:
                    await runtime.destroy(container)
            finally:
                await runtime.close()


if __name__ == "__main__":
    unittest.main()
