import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from autopilot.manager import AutoManager, AutoManagerError
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
        self.manual_facts: dict[str, str] = {}

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

    def get_submission_result(self, project_id: str, fact_id: str) -> GoalResult:
        content = self.manual_facts.get(fact_id)
        if content is None:
            raise ValueError("selected Fact is not active in the current project")
        fact = GoalResultFact(fact_id, content, sources=("human",))
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
        self.plan_gate: asyncio.Event | None = None

    async def preflight(self):
        return {"status": "valid", "model": "fake"}

    async def plan_submission(self, _task, goal_result, **_kwargs):
        if self.plan_gate is not None:
            await self.plan_gate.wait()
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
    def __init__(self, events: list[str], outcomes: tuple[str, ...] = ()) -> None:
        self.events = events
        self.index = 0
        self.submitted: list[str] = []
        self.submitted_facts: list[str] = []
        self.outcomes = list(outcomes)
        self.skip_calls = 0

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

    async def submit_result(self, task, _plan, goal_result):
        self.events.append(f"submit:{task.external_id}")
        self.submitted.append(task.external_id)
        self.submitted_facts.append(goal_result.fact.id)
        status = self.outcomes.pop(0) if self.outcomes else "accepted"
        return PlatformResult(status)

    async def skip_task(self, _task, _reason):
        self.skip_calls += 1
        return PlatformResult("skipped")

    async def close(self):
        return None


class AutopilotSection13Tests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _provider() -> ProviderConfig:
        return ProviderConfig(
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

    @staticmethod
    def _platform_config() -> StructuredPlatformConfig:
        return StructuredPlatformConfig(
            base_url="https://platform.example/api",
            fetch_task=EndpointConfig("GET", "/tasks"),
            submit_result=EndpointConfig("POST", "/submit"),
            mapping=FieldMapping(),
        )

    async def _wait_for_status(self, manager: AutoManager, status: str) -> dict[str, object]:
        for _ in range(200):
            snapshot = await manager.snapshot()
            if snapshot["session"]["current_task_status"] == status:
                return snapshot
            await asyncio.sleep(0.01)
        self.fail(f"automatic session did not reach {status}")

    async def test_modern_manager_completes_two_tasks_in_order_from_goal_result(self) -> None:
        provider = self._provider()
        platform_config = self._platform_config()
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

    async def test_rejection_pauses_and_selected_fact_acceptance_continues(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = _FakeService(Path(directory))
            service.manual_facts["human-fact"] = "NSSCTF{corrected}"
            controller = _FakeController()
            platform = _FakePlatform(service.events, ("rejected", "accepted", "accepted"))
            manager = AutoManager(
                service=service,
                provider_configs=(self._provider(),),
                controller_factory=lambda *_args, **_kwargs: controller,
                platform_adapter_factory=lambda *_args, **_kwargs: platform,
            )
            config = AutoStartConfig(
                mode="structured",
                controller=ControllerConfig("https://controller.example/v1", "model"),
                platform=self._platform_config(),
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
                paused = await self._wait_for_status(manager, "awaiting_manual_review")
                self.assertEqual(platform.index, 1)
                await asyncio.sleep(0.03)
                self.assertEqual(platform.index, 1, "paused task must not fetch the next task")

                controller.plan_gate = asyncio.Event()
                queued = await manager.submit_current_task("human-fact")
                self.assertEqual(queued["session"]["current_task_status"], "submit_pending")
                with self.assertRaisesRegex(AutoManagerError, "not awaiting"):
                    await manager.submit_current_task("human-fact")
                controller.plan_gate.set()
                await manager._runner

            snapshot = await manager.snapshot()
            self.assertEqual(snapshot["session"]["status"], "finished", snapshot)
            self.assertEqual(snapshot["session"]["completed_tasks"], 2)
            self.assertEqual(
                platform.submitted_facts,
                ["project-1-fact", "human-fact", "project-2-fact"],
            )
            self.assertIn("fetch:two", service.events)
            await manager.aclose()

    async def test_manual_rejection_and_unknown_stay_paused_then_finish_without_skip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = _FakeService(Path(directory))
            service.manual_facts.update(
                {
                    "fact-one": "NSSCTF{wrong}",
                    "fact-two": "NSSCTF{unknown}",
                    "fact-three": "NSSCTF{correct}",
                }
            )
            controller = _FakeController()
            platform = _FakePlatform(
                service.events,
                ("rejected", "rejected", "fatal_error", "accepted"),
            )
            manager = AutoManager(
                service=service,
                provider_configs=(self._provider(),),
                controller_factory=lambda *_args, **_kwargs: controller,
                platform_adapter_factory=lambda *_args, **_kwargs: platform,
            )
            config = AutoStartConfig(
                mode="structured",
                controller=ControllerConfig("https://controller.example/v1", "model"),
                platform=self._platform_config(),
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
                await self._wait_for_status(manager, "awaiting_manual_review")
                await manager.submit_current_task("fact-one")
                await self._wait_for_status(manager, "awaiting_manual_review")
                self.assertEqual(platform.index, 1)

                await manager.submit_current_task("fact-two")
                await self._wait_for_status(manager, "awaiting_manual_review")
                self.assertEqual(platform.index, 1)

                await manager.submit_current_task("fact-three", finish_after_accept=True)
                await manager._runner

            snapshot = await manager.snapshot()
            self.assertEqual(snapshot["session"]["status"], "finished", snapshot)
            self.assertEqual(snapshot["session"]["completed_tasks"], 1)
            self.assertEqual(platform.index, 1)
            self.assertEqual(platform.skip_calls, 0)
            self.assertIn("finished by user", snapshot["session"]["last_message"])
            await manager.aclose()


if __name__ == "__main__":
    unittest.main()
