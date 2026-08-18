import dataclasses
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import httpx

from autopilot.direct_controller import DirectLLMController
from autopilot.manager import AutoManager
from autopilot.models import (
    AutoSession,
    AutoStartConfig,
    AutoTask,
    ControllerConfig,
    PromptPlatformConfig,
    RuntimeControllerSecret,
    RuntimePlatformSecret,
)
from web.schemas import AuthPayload, ControllerPayload


class SecretBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def test_config_and_pydantic_serialization_redact_fixed_secrets(self) -> None:
        controller = ControllerConfig("https://controller.example/v1", "model")
        self.assertNotIn("FIXED-CONTROLLER-KEY", repr(controller))
        self.assertNotIn("FIXED-CONTROLLER-KEY", repr(dataclasses.asdict(controller)))

        payload = ControllerPayload(
            api_key="FIXED-CONTROLLER-KEY",
            base_url="https://controller.example/v1",
            model="model",
        )
        auth = AuthPayload(token="FIXED-PLATFORM-TOKEN")
        self.assertNotIn("FIXED-CONTROLLER-KEY", repr(payload.model_dump()))
        self.assertNotIn("FIXED-CONTROLLER-KEY", payload.model_dump_json())
        self.assertNotIn("FIXED-PLATFORM-TOKEN", repr(auth.model_dump()))
        self.assertNotIn("FIXED-PLATFORM-TOKEN", auth.model_dump_json())

        secret = RuntimeControllerSecret("FIXED-CONTROLLER-KEY")
        platform_secret = RuntimePlatformSecret(
            prompt="platform prompt", token="FIXED-PLATFORM-TOKEN"
        )
        self.assertNotIn("FIXED-CONTROLLER-KEY", repr(secret))
        self.assertNotIn("FIXED-PLATFORM-TOKEN", repr(platform_secret))
        secret.clear()
        platform_secret.clear()
        self.assertEqual(secret.api_key, "")
        self.assertEqual(platform_secret.token, "")

    async def test_modern_task_fields_are_scrubbed_before_project_creation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = SimpleNamespace(
                registry=SimpleNamespace(data_root=root),
                coordinator=SimpleNamespace(),
            )
            manager = AutoManager(service=service, provider_configs=())
            config = AutoStartConfig(
                mode="prompt",
                controller=ControllerConfig("https://controller.example/v1", "model"),
                platform=PromptPlatformConfig(),
                solver_providers=("solver",),
                agent_limit=1,
            )
            session = AutoSession("auto-test", config, root / "runtime")
            manager._controller_secret = RuntimeControllerSecret("FIXED-CONTROLLER-KEY")
            manager._platform_secret = RuntimePlatformSecret(
                prompt="platform contract", token="FIXED-PLATFORM-TOKEN"
            )
            task = manager._scrub_task(
                session,
                AutoTask(
                    external_id="task-1",
                    name="name FIXED-PLATFORM-TOKEN",
                    goal="goal FIXED-CONTROLLER-KEY",
                    origin="origin FIXED-PLATFORM-TOKEN",
                    hints=("hint FIXED-CONTROLLER-KEY",),
                ),
            )
            encoded = json.dumps(
                {"name": task.name, "goal": task.goal, "origin": task.origin, "hints": task.hints}
            )
            self.assertNotIn("FIXED-CONTROLLER-KEY", encoded)
            self.assertNotIn("FIXED-PLATFORM-TOKEN", encoded)
            await manager._dispose_modern_runtime()

    async def test_runtime_disposal_clears_controller_and_platform_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = SimpleNamespace(
                registry=SimpleNamespace(data_root=Path(directory)),
                coordinator=SimpleNamespace(),
            )
            manager = AutoManager(service=service, provider_configs=())

            class Closable:
                async def close(self):
                    return None

            controller_secret = RuntimeControllerSecret("FIXED-CONTROLLER-KEY")
            platform_secret = RuntimePlatformSecret(token="FIXED-PLATFORM-TOKEN")
            manager._controller = Closable()
            manager._adapter = Closable()
            manager._controller_secret = controller_secret
            manager._platform_secret = platform_secret
            await manager._dispose_modern_runtime()
            self.assertIsNone(manager._controller)
            self.assertIsNone(manager._adapter)
            self.assertEqual(controller_secret.api_key, "")
            self.assertEqual(platform_secret.token, "")

    async def test_controller_call_root_is_removed_without_persisting_auth_header(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "tool_calls": [
                                    {
                                        "id": "call-1",
                                        "function": {
                                            "name": "controller_readiness",
                                            "arguments": "{}",
                                        },
                                    }
                                ]
                            }
                        }
                    ]
                },
                request=request,
            )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            controller = DirectLLMController(
                ControllerConfig("https://controller.example/v1", "model"),
                RuntimeControllerSecret("FIXED-CONTROLLER-KEY"),
                client=client,
            )
            controller.set_runtime_root(root)
            try:
                result = await controller.preflight()
                self.assertEqual(result["status"], "valid")
            finally:
                await controller.close()
            self.assertEqual(requests[0].headers["Authorization"], "Bearer FIXED-CONTROLLER-KEY")
            self.assertEqual(list(root.rglob("messages.json")), [])


if __name__ == "__main__":
    unittest.main()
