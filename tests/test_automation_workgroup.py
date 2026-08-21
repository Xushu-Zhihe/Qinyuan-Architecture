from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock

from automation.workgroup import BlackboardWorkgroup, WorkgroupQuestion
from orchestrator.models import GoalResult, GoalResultFact
from orchestrator.projects import ProjectService


class FakeSupervisor:
    async def wait_until_idle(self, timeout: float | None = None) -> None:
        self.timeout = timeout


class FakeWorkgroupService:
    def __init__(self, result: GoalResult) -> None:
        self.result = result
        self.created: dict[str, object] | None = None
        self.supervisor = FakeSupervisor()

    def create_project(self, **kwargs):
        self.created = kwargs
        return SimpleNamespace(id="project-question")

    async def start_project(self, project_id: str):
        return self.supervisor

    def get_goal_result(self, project_id: str) -> GoalResult:
        return self.result


class WorkgroupTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_question_is_mapped_to_one_blackboard_project(self) -> None:
        result = GoalResult(
            project_id="project-question",
            goal_version=1,
            goal_node_id="goal-1",
            result_ref="project-question:1:fact-1",
            fact=GoalResultFact("fact-1", "FLAG{answer}", sources=("solver-a",)),
        )
        service = FakeWorkgroupService(result)
        workgroup = BlackboardWorkgroup(
            service,
            solver_providers=("solver-a", "solver-b"),
            agent_limit=2,
            timeout_seconds=30,
        )

        solution = await workgroup.solve(
            WorkgroupQuestion(
                external_id="question-1",
                name="Question",
                goal="Recover the answer",
                origin="Manual",
                hints=("Inspect the attachment",),
                attachments_dir=Path("attachments"),
            )
        )

        self.assertEqual(solution.project_id, "project-question")
        self.assertEqual(
            solution.dispatcher_context()["fact"]["content"],
            "FLAG{answer}",
        )
        self.assertEqual(service.created["agent_limit"], 2)
        self.assertEqual(service.created["challenge_path"], Path("attachments"))


class ProjectGoalResultThresholdTests(unittest.TestCase):
    def test_service_uses_actual_agent_limit_for_goal_result(self) -> None:
        service = ProjectService.__new__(ProjectService)
        store = Mock()
        store.goal_result.return_value = None
        handle = SimpleNamespace(
            record=SimpleNamespace(agent_limit=1, providers=("solver-a", "solver-b")),
            store=store,
        )
        service.get_handle = lambda project_id: handle

        ProjectService.get_goal_result(service, "project-1")

        store.goal_result.assert_called_once_with(1)
