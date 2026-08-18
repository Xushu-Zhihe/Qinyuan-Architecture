import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from web.app import create_app
from autopilot.models import AutoTask
from autopilot.platform_models import PlatformResult
from worker.models import ProviderCapabilities, ProviderConfig, WorkerResult


class AutopilotWebTests(unittest.TestCase):
    @staticmethod
    def auto_provider() -> ProviderConfig:
        return ProviderConfig(
            "fake",
            True,
            1,
            ("unused",),
            10,
            max_running=1,
            capabilities=ProviderCapabilities(
                result_protocol_versions=(1, 2),
                skill_mode="required",
                available_skills=("blackboard-result-protocol",),
                validates_result_locally=True,
                supports_result_fact=True,
                schema_version="2",
                fixture_digest="fixture",
            ),
        )

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

    def test_modern_prompt_and_structured_payloads_are_accepted_without_controller_provider(self) -> None:
        class FakeController:
            def __init__(self, *_args, **_kwargs):
                pass

            async def preflight(self):
                return {"status": "valid", "model": "fake-model"}

            async def prompt_phase(self, *_args, **_kwargs):
                return PlatformResult("wait", retry_after_seconds=60)

            async def close(self):
                return None

        class FakeStructuredAdapter:
            def __init__(self, *_args, **_kwargs):
                pass

            async def fetch_task(self):
                return PlatformResult("wait", retry_after_seconds=60)

            async def close(self):
                return None

        with tempfile.TemporaryDirectory() as directory, patch(
            "autopilot.manager.DirectLLMController", FakeController
        ), patch(
            "autopilot.manager.StructuredPlatformAdapter", FakeStructuredAdapter
        ):
            with TestClient(
                create_app(Path(directory), provider_configs=(self.auto_provider(),))
            ) as client:
                common = {
                    "controller": {
                        "api_key": "controller-secret",
                        "base_url": "https://controller.example/v1",
                        "model": "fake-model",
                    },
                    "solver_providers": ["fake"],
                    "agent_limit": 1,
                    "max_tasks": 1,
                    "task_timeout_minutes": 1,
                }
                prompt = client.post(
                    "/api/autopilot/start",
                    json={
                        **common,
                        "mode": "prompt",
                        "platform": {"prompt": "fetch, submit, accepted, rejected"},
                    },
                )
                self.assertEqual(prompt.status_code, 202, prompt.text)
                self.assertNotIn("controller-secret", prompt.text)
                self.assertNotIn("fetch, submit", prompt.text)
                self.assertNotIn("controller_provider", json.dumps(prompt.json()))
                client.post("/api/autopilot/stop")

                structured_payload = {
                    **common,
                    "mode": "structured",
                    "platform": {
                        "base_url": "https://platform.example/api",
                        "auth": {"token": "platform-secret"},
                        "fetch_task": {"method": "GET", "path": "/tasks"},
                        "submit_result": {
                            "method": "POST",
                            "path": "/tasks/{{external_id}}/submit",
                            "body": {"value": "{{submission.value}}"},
                        },
                        "outcomes": {
                            "submit": {"accepted": {"path": "accepted", "equals": True}}
                        },
                    },
                }
                structured = client.post("/api/autopilot/start", json=structured_payload)
                self.assertEqual(structured.status_code, 202, structured.text)
                self.assertNotIn("controller-secret", structured.text)
                self.assertNotIn("platform-secret", structured.text)
                client.post("/api/autopilot/stop")

                forbidden = client.post(
                    "/api/autopilot/start",
                    json={**structured_payload, "controller_provider": "fake"},
                )
                self.assertEqual(forbidden.status_code, 422)

    def test_platform_health_requires_and_uses_explicit_health_endpoint(self) -> None:
        calls: list[str] = []

        class FakeHealthAdapter:
            def __init__(self, config, **_kwargs):
                calls.append(config.health_check.path if config.health_check else "")

            async def health_check(self):
                return PlatformResult("healthy", metadata={"http_status": 204})

            async def close(self):
                return None

        base_platform = {
            "base_url": "https://platform.example/api",
            "auth": {"token": "platform-secret"},
            "fetch_task": {"method": "GET", "path": "/tasks"},
            "submit_result": {"method": "POST", "path": "/submit"},
        }
        with tempfile.TemporaryDirectory() as directory, patch(
            "web.app.StructuredPlatformAdapter", FakeHealthAdapter
        ):
            with TestClient(create_app(Path(directory))) as client:
                missing = client.post(
                    "/api/autopilot/test-platform-health",
                    json={"mode": "structured", "platform": base_platform},
                )
                self.assertEqual(missing.status_code, 400)
                healthy = client.post(
                    "/api/autopilot/test-platform-health",
                    json={
                        "mode": "structured",
                        "platform": {
                            **base_platform,
                            "health_check": {"method": "GET", "path": "/health"},
                        },
                    },
                )
                self.assertEqual(healthy.status_code, 200, healthy.text)
                self.assertEqual(calls, ["/health"])
                self.assertNotIn("platform-secret", healthy.text)

    def test_current_task_requires_endpoint_and_returns_mapped_task(self) -> None:
        calls: list[str] = []

        class FakeCurrentTaskAdapter:
            def __init__(self, config, **_kwargs):
                calls.append(config.current_task.path if config.current_task else "")

            async def inspect_current_task(self):
                return PlatformResult(
                    "task",
                    task=AutoTask(
                        external_id="attempt-1",
                        name="Current challenge",
                        goal="Recover the flag",
                        origin="Challenge statement",
                        hints=("Inspect the response",),
                        lease_id="lease-1",
                    ),
                )

            async def close(self):
                return None

        base_platform = {
            "base_url": "https://platform.example/api",
            "auth": {"token": "platform-secret"},
            "fetch_task": {"method": "POST", "path": "/next"},
            "submit_result": {"method": "POST", "path": "/submit"},
        }
        with tempfile.TemporaryDirectory() as directory, patch(
            "web.app.StructuredPlatformAdapter", FakeCurrentTaskAdapter
        ):
            with TestClient(create_app(Path(directory))) as client:
                missing = client.post(
                    "/api/autopilot/current-task",
                    json={"mode": "structured", "platform": base_platform},
                )
                self.assertEqual(missing.status_code, 400)
                current = client.post(
                    "/api/autopilot/current-task",
                    json={
                        "mode": "structured",
                        "platform": {
                            **base_platform,
                            "current_task": {"method": "GET", "path": "/current"},
                        },
                    },
                )
                self.assertEqual(current.status_code, 200, current.text)
                self.assertEqual(current.json()["task"]["external_id"], "attempt-1")
                self.assertEqual(calls, ["/current"])
                self.assertNotIn("platform-secret", current.text)


if __name__ == "__main__":
    unittest.main()
