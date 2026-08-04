import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from web.app import create_app
from worker.models import ProviderConfig, WorkerResult


class AutopilotWebTests(unittest.TestCase):
    def test_start_is_single_session_and_never_returns_platform_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            provider = ProviderConfig("fake", True, 1, ("unused",), 10, max_running=1)

            async def execute(config, request, challenge_dir, work_dir, **kwargs):
                work_dir.mkdir(parents=True, exist_ok=True)
                output = 'AUTOPILOT_RESULT: {"action":"wait","message":"empty"}'
                stdout = work_dir / "stdout.txt"
                stderr = work_dir / "stderr.txt"
                stdout.write_text(output, encoding="utf-8")
                stderr.write_text("", encoding="utf-8")
                return WorkerResult(
                    provider=config.name,
                    priority=config.priority,
                    status="success",
                    exit_code=0,
                    duration_seconds=0.01,
                    output=output,
                    stdout_path=str(stdout),
                    stderr_path=str(stderr),
                    work_dir=str(work_dir),
                )

            context = TestClient(
                create_app(root, provider_configs=(provider,), execute=execute)
            )
            with context as client:
                payload = {
                    "platform_prompt": "token=secret-token; call the challenge API",
                    "controller_provider": "fake",
                    "solver_providers": ["fake"],
                    "agent_limit": 1,
                    "max_tasks": 2,
                    "task_timeout_minutes": 1,
                    "controller_timeout_seconds": 5,
                }
                started = client.post("/api/autopilot/start", json=payload)
                self.assertEqual(started.status_code, 202, started.text)
                self.assertNotIn("secret-token", started.text)
                conflict = client.post("/api/autopilot/start", json=payload)
                self.assertEqual(conflict.status_code, 409, conflict.text)
                status = client.get("/api/autopilot")
                self.assertEqual(status.status_code, 200)
                self.assertNotIn("secret-token", status.text)
                stopped = client.post("/api/autopilot/stop")
                self.assertEqual(stopped.status_code, 200, stopped.text)
                self.assertEqual(stopped.json()["session"]["status"], "stopped")


if __name__ == "__main__":
    unittest.main()
