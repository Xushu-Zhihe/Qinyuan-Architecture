import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path

from autopilot.manager import AutoManager, AutoManagerError
from autopilot.models import (
    AutoSession,
    AutoStartConfig,
    AutoTask,
    ControllerConfig,
    PromptPlatformConfig,
    RuntimeControllerSecret,
    StructuredPlatformConfig,
)
from autopilot.platform_models import AuthConfig, EndpointConfig
from autopilot.solver_result import SubmissionPlan
from orchestrator.models import GoalResult, GoalResultFact
from orchestrator.protocol import parse_result


def structured_platform(*, with_skip: bool = True) -> StructuredPlatformConfig:
    return StructuredPlatformConfig(
        base_url="https://platform.example/api/",
        auth=AuthConfig(),
        fetch_task=EndpointConfig("GET", "/tasks"),
        submit_result=EndpointConfig("POST", "/tasks/{{external_id}}/submit"),
        skip_task=(EndpointConfig("POST", "/tasks/{{external_id}}/skip") if with_skip else None),
    )


class ControllerModelTests(unittest.TestCase):
    def test_controller_url_and_model_are_normalized(self) -> None:
        config = ControllerConfig("https://llm.example/v1/chat/completions///", "  openai/gpt-4o  ")
        self.assertEqual(config.base_url, "https://llm.example/v1")
        self.assertEqual(config.chat_completions_url, "https://llm.example/v1/chat/completions")
        self.assertEqual(config.model, "openai/gpt-4o")

    def test_controller_rejects_invalid_model_and_extra_body(self) -> None:
        with self.assertRaises(ValueError):
            ControllerConfig("https://llm.example/v1", "bad model")
        with self.assertRaises(ValueError):
            ControllerConfig("https://llm.example/v1", "model", extra_body={"messages": []})
        config = ControllerConfig("https://llm.example/v1", "model", extra_body={"top_p": 0.5})
        self.assertEqual(config.extra_body["top_p"], 0.5)

    def test_runtime_secret_is_redacted_and_clearable(self) -> None:
        secret = RuntimeControllerSecret("controller-key")
        self.assertNotIn("controller-key", repr(secret))
        with self.assertRaises(TypeError):
            asdict(secret)
        secret.clear()
        self.assertEqual(secret.api_key, "")


class AutoStartModelTests(unittest.TestCase):
    def modern_config(self, **kwargs) -> AutoStartConfig:
        values = {
            "mode": "structured",
            "controller": ControllerConfig("https://llm.example/v1", "model"),
            "platform": structured_platform(),
            "solver_providers": ("solver-a",),
            "agent_limit": 1,
        }
        values.update(kwargs)
        return AutoStartConfig(**values)

    def test_mode_and_platform_types_must_match(self) -> None:
        with self.assertRaises(ValueError):
            self.modern_config(mode="prompt", platform=structured_platform())
        with self.assertRaises(ValueError):
            self.modern_config(platform=PromptPlatformConfig("platform contract"))

    def test_provider_and_numeric_limits_are_domain_validated(self) -> None:
        with self.assertRaises(ValueError):
            self.modern_config(solver_providers=("solver-a", "solver-a"))
        with self.assertRaises(ValueError):
            self.modern_config(max_tasks=1001)
        with self.assertRaises(ValueError):
            self.modern_config(poll_interval_seconds=3601)
        with self.assertRaises(ValueError):
            self.modern_config(agent_limit=2)

    def test_skip_policy_requires_structured_skip_endpoint(self) -> None:
        with self.assertRaises(ValueError):
            self.modern_config(
                platform=structured_platform(with_skip=False),
                solver_failure_action="skip",
            )

    def test_external_task_id_is_required(self) -> None:
        with self.assertRaises(ValueError):
            AutoTask("  ", "name", "goal", "origin")
        self.assertEqual(AutoTask(" task-1 ", "name", "goal", "origin").external_id, "task-1")


class GoalResultAndTransitionTests(unittest.TestCase):
    def test_goal_result_ref_is_immutable_and_supporting_facts_are_bounded(self) -> None:
        fact = GoalResultFact("fact-1", "complete result")
        result = GoalResult("project-1", 2, "goal-2", "project-1:2:fact-1", fact)
        self.assertEqual(result.result_ref, "project-1:2:fact-1")
        with self.assertRaises(ValueError):
            GoalResult("project-1", 2, "goal-2", "project-1:1:fact-1", fact)
        with self.assertRaises(ValueError):
            GoalResult(
                "project-1",
                2,
                "goal-2",
                "project-1:2:fact-1",
                fact,
                supporting_facts=tuple(GoalResultFact(str(index), "evidence") for index in range(21)),
            )

    def test_v2_parser_requires_current_graph_context(self) -> None:
        payload = (
            '{"protocol_version":2,"intent_id":"intent-1",'
            '"facts":[{"ref":"result","content":"done"}],'
            '"edges":[{"type":"addresses","from":"result","to":"goal-1"}],'
            '"goal_achieved":true,"goal_result_ref":"result"}'
        )
        missing_context = parse_result("BLACKBOARD_RESULT: " + payload, assigned_intent_id="intent-1", existing_node_ids={"goal-1"})
        self.assertEqual(missing_context.status, "protocol_error")
        valid = parse_result(
            "BLACKBOARD_RESULT: " + payload,
            assigned_intent_id="intent-1",
            existing_node_ids={"goal-1"},
            node_types={"goal-1": "goal"},
            current_goal_node_id="goal-1",
            existing_addresses=set(),
        )
        self.assertEqual(valid.status, "valid")

    def test_transition_rejects_illegal_path_and_unknown_reason(self) -> None:
        config = AutoStartConfig(
            platform_prompt="platform contract",
            controller_provider="legacy-controller",
            solver_providers=("solver",),
            agent_limit=1,
        )
        with tempfile.TemporaryDirectory() as directory:
            session = AutoSession("auto-1", config, Path(directory))
            AutoManager._transition(session, "queued")
            AutoManager._transition(session, "fetched")
            with self.assertRaises(AutoManagerError):
                AutoManager._transition(session, "accepted")
            with self.assertRaises(AutoManagerError):
                AutoManager._transition(session, "solving", "unknown_reason")

    def test_submission_plan_still_requires_one_payload(self) -> None:
        plan = SubmissionPlan(
            result_ref="project-1:1:fact-1",
            source_fact_ids=("fact-1",),
            content_type="text",
            text="complete result",
        )
        self.assertTrue(plan.fingerprint.startswith("sha256:"))


if __name__ == "__main__":
    unittest.main()
