import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from autopilot.manager import AutoManager
from autopilot.models import AutoStartConfig, ControllerConfig, RuntimeControllerSecret, RuntimePlatformSecret
from autopilot.platform_models import EndpointConfig, FieldMapping, StructuredPlatformConfig
from autopilot.solver_result import SubmissionPlan
from orchestrator.models import GoalResult, GoalResultFact
from autopilot.platform import PlatformResult, StructuredPlatformAdapter
from worker.models import ProviderCapabilities, ProviderConfig


class _FakeStore:
    def __init__(self) -> None:
        self.status = "idle"

    def snapshot(self) -> dict[str, object]:
        return {"project": {"status": self.status}}


class _FakeService:
    def __init__(self, root: Path) -> None:
        self.registry = SimpleNamespace(data_root=root)
        self.coordinator = SimpleNamespace()
        self.handles: dict[str, object] = {}
        self.events: list[str] = []

    def create_project(self, **_kwargs):
        project_id = f"project-{len(self.handles) + 1}"
        handle = SimpleNamespace(id=project_id, store=_FakeStore())
        self.handles[project_id] = handle
        return handle

    async def start_project(self, project_id: str):
        self.events.append(f"solve:{project_id}")
        self.handles[project_id].store.status = "achieved"

    async def stop_project(self, _project_id: str):
        return None

    def get_handle(self, project_id: str):
        return self.handles[project_id]

    def get_goal_result(self, project_id: str) -> GoalResult:
        fact = GoalResultFact(f"{project_id}-fact", f"result for {project_id}")
        return GoalResult(
            project_id=project_id,
            goal_version=1,
            goal_node_id=f"{project_id}-goal",
            result_ref=f"{project_id}:1:{fact.id}",
            fact=fact,
        )


class _FakeController:
    def __init__(self, *_args, **_kwargs) -> None:
        self.plans: list[str] = []

    async def preflight(self):
        return {"status": "valid", "model": "fake"}

    async def plan_submission(self, _task, goal_result, **_kwargs):
        self.plans.append(goal_result.result_ref)
        return SubmissionPlan(
            result_ref=goal_result.result_ref,
            source_fact_ids=(goal_result.fact.id,),
            content_type="text",
            text=goal_result.fact.content,
        )

    async def close(self):
        return None


class _FakePlatform(StructuredPlatformAdapter):
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.index = 0
        self.submitted: list[str] = []

    async def fetch_task(self):
        tasks = (
            ("one", "First"),
            ("two", "Second"),
        )
        if self.index >= len(tasks):
            self.events.append("fetch:finished")
            return PlatformResult("finished")
        external_id, name = tasks[self.index]
        self.index += 1
        self.events.append(f"fetch:{external_id}")
        from autopilot.models import AutoTask

        return PlatformResult(
            "task",
            task=AutoTask(
                external_id=external_id,
                name=name,
                goal=f"Solve {name}",
                origin="fixture",
            ),
        )

    async def submit_result(self, task, _plan, _goal_result):
        self.events.append(f"submit:{task.external_id}")
        self.submitted.append(task.external_id)
        return PlatformResult("accepted")

    async def close(self):
        return None


class AutopilotSection13Tests(unittest.IsolatedAsyncioTestCase):
    async def test_modern_manager_completes_two_tasks_in_order_from_goal_result(self) -> None:
        provider = ProviderConfig(
            "fake",
            True,
            1,
            ("unused",),
            10,
            max_running=1,
            capabilities=ProviderCapabilities(
                request_protocol_versions=(2,),
                result_protocol_versions=(2,),
                skill_mode="required",
                available_skills=("blackboard-result-protocol",),
                validates_result_locally=True,
                supports_result_fact=True,
                schema_version="2",
            ),
        )
        platform_config = StructuredPlatformConfig(
            base_url="https://platform.example/api",
            fetch_task=EndpointConfig("GET", "/tasks"),
            submit_result=EndpointConfig("POST", "/submit"),
            mapping=FieldMapping(),
        )
        with tempfile.TemporaryDirectory() as directory:
            service = _FakeService(Path(directory))
            controller = _FakeController()
            platform = _FakePlatform(service.events)
            manager = AutoManager(
                service=service,
                provider_configs=(provider,),
                controller_factory=lambda *_args, **_kwargs: controller,
                platform_adapter_factory=lambda *_args, **_kwargs: platform,
            )
            config = AutoStartConfig(
                mode="structured",
                controller=ControllerConfig("https://controller.example/v1", "model"),
                platform=platform_config,
                solver_providers=("fake",),
                agent_limit=1,
                max_tasks=2,
                poll_interval_seconds=0.001,
            )
            with patch(
                "autopilot.manager.probe_provider_capabilities",
                return_value={"auto_ready": True, "status": "ready"},
            ):
                await manager.start(
                    config,
                    controller_secret=RuntimeControllerSecret("controller-key"),
                    platform_secret=RuntimePlatformSecret(token="platform-token"),
                )
                await manager._runner
            snapshot = await manager.snapshot()
            self.assertEqual(snapshot["session"]["status"], "finished", snapshot)
            self.assertEqual(snapshot["session"]["completed_tasks"], 2)
            self.assertEqual(platform.submitted, ["one", "two"])
            self.assertEqual(
                service.events[:4],
                ["fetch:one", "solve:project-1", "submit:one", "fetch:two"],
            )
            self.assertEqual(len(controller.plans), 2)
            await manager.aclose()


if __name__ == "__main__":
    unittest.main()
