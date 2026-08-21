import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from automation.http import OperationResult
from automation.llm import LLMConfig
from automation.manager import AutomationManager
from automation.manifest import (
    AnswerField,
    AnswerMapping,
    Manifest,
    Operation,
    PlatformInfo,
    SuccessCondition,
)
from automation.secrets import SecretRedactor
from automation.workgroup import WorkgroupSolution
from orchestrator.models import GoalResult, GoalResultFact


def _tool_response(operation_id: str) -> dict[str, object]:
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": f"call-{operation_id}",
                            "function": {
                                "name": "execute_operation",
                                "arguments": json.dumps(
                                    {"operation_id": operation_id}
                                ),
                            },
                        }
                    ],
                }
            }
        ]
    }


class FakeManagerLLM:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self.responses = [
            _tool_response("fetch-question"),
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(
                                {
                                    "status": "question",
                                    "message": "fetched",
                                    "question": {
                                        "external_id": "q-1",
                                        "name": "Question",
                                        "goal": "Solve the question",
                                        "origin": "fixture",
                                        "hints": [],
                                    },
                                    "metadata": {"question_id": "q-1"},
                                }
                            ),
                        }
                    }
                ]
            },
            _tool_response("submit-answer"),
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(
                                {
                                    "status": "submitted",
                                    "message": "accepted",
                                    "metadata": {},
                                }
                            ),
                        }
                    }
                ]
            },
        ]

    async def complete(self, *args: object, **kwargs: object) -> dict[str, object]:
        del args, kwargs
        if not self.responses:
            raise AssertionError("manager requested more LLM turns than expected")
        return self.responses.pop(0)

    async def close(self) -> None:
        return None


class FakeManagerExecutor:
    def __init__(
        self,
        manifest: Manifest,
        secret: object,
        *,
        timeout_seconds: float,
        redactor: SecretRedactor,
    ) -> None:
        del manifest, timeout_seconds
        token = getattr(secret, "token", "")
        self.redactor = redactor
        self.token = token

    async def execute(
        self,
        operation_id: str,
        *,
        variables: dict[str, object] | None = None,
        download_to: Path | None = None,
    ) -> OperationResult:
        del variables
        if operation_id == "fetch-question":
            return OperationResult(
                operation_id=operation_id,
                method="GET",
                url="https://isolated.example/question",
                status_code=200,
                response_headers={},
                payload={"id": "q-1"},
                success=True,
                message="fetched",
                downloaded_path=download_to,
            )
        return OperationResult(
            operation_id=operation_id,
            method="POST",
            url="https://isolated.example/submit",
            status_code=200,
            response_headers={},
            payload={"accepted": True},
            success=True,
            message="accepted",
            downloaded_path=download_to,
        )

    async def close(self) -> None:
        return None


class FakeManagerWorkgroup:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        return None

    async def solve(self, _question: object) -> WorkgroupSolution:
        return WorkgroupSolution(
            project_id="blackboard-question-q-1",
            goal_result=GoalResult(
                project_id="blackboard-question-q-1",
                goal_version=1,
                goal_node_id="goal-q-1",
                result_ref="blackboard-question-q-1:1:fact-q-1",
                fact=GoalResultFact(
                    "fact-q-1",
                    "FLAG{manager-e2e}",
                    sources=("solver-fixture",),
                ),
            ),
        )


class AutomationManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_manager_runs_question_workgroup_and_submission_lifecycle(
        self,
    ) -> None:
        manifest = Manifest(
            platform=PlatformInfo(name="Isolated fixture"),
            operations=(
                Operation(
                    id="fetch-question",
                    name="Fetch question",
                    kind="fetch",
                    method="GET",
                    url="https://isolated.example/question",
                ),
                Operation(
                    id="submit-answer",
                    name="Submit answer",
                    kind="submit",
                    method="POST",
                    url="https://isolated.example/submit",
                    body={"answer": "{{answer}}"},
                    answer_mapping=AnswerMapping(
                        mode="json",
                        fields=(
                            AnswerField(
                                target="answer",
                                source_path="fact.content",
                            ),
                        ),
                    ),
                    success_condition=SuccessCondition(
                        kind="json_path_equals",
                        path="accepted",
                        expected=True,
                    ),
                ),
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = AutomationManager(object(), root)
            manager.manifests.save(manifest)
            try:
                with (
                    patch("automation.manager.LLMClient", FakeManagerLLM),
                    patch(
                        "automation.manager.ManifestOperationExecutor",
                        FakeManagerExecutor,
                    ),
                    patch(
                        "automation.manager.BlackboardWorkgroup",
                        FakeManagerWorkgroup,
                    ),
                ):
                    await manager.start(
                        manifest_id=manifest.manifest_id,
                        dispatcher_config=LLMConfig(
                            "https://dispatcher.example/v1",
                            "dispatcher-model",
                        ),
                        dispatcher_api_key="dispatcher-secret",
                        platform_token="platform-secret",
                        solver_providers=("solver-fixture",),
                        agent_limit=1,
                        max_tasks=1,
                        task_timeout_seconds=30,
                        poll_interval_seconds=0.01,
                    )
                    run = manager._run
                    self.assertIsNotNone(run)
                    self.assertIsNotNone(run.task)
                    await asyncio.wait_for(run.task, timeout=5)

                self.assertEqual(run.status, "completed")
                self.assertEqual(run.processed_tasks, 1)
                self.assertEqual(
                    run.workgroup_project_id,
                    "blackboard-question-q-1",
                )
                self.assertTrue(run.event_log_path.is_file())
                events = run.event_log_path.read_text(encoding="utf-8")
                self.assertIn("submit-answer", events)
                self.assertNotIn("platform-secret", events)
                self.assertNotIn("dispatcher-secret", events)
            finally:
                await manager.aclose()


if __name__ == "__main__":
    unittest.main()
