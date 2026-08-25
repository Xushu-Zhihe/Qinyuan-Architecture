import unittest

from orchestrator.models import GoalResult, GoalResultFact
from orchestrator.protocol import parse_result


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


if __name__ == "__main__":
    unittest.main()
