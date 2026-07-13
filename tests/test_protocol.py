import inspect
import json
import unittest

import orchestrator.protocol as protocol
from orchestrator.models import RELATION_EDGE_TYPES
from orchestrator.protocol import (
    parse_intent,
    parse_result,
    render_execution_request,
    render_intent_request,
)


class IntentProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.node_types = {
            "origin-1": "origin",
            "goal-1": "goal",
            "fact-1": "fact",
            "hint-1": "hint",
        }

    def test_parses_plan_record(self) -> None:
        output = (
            'BLACKBOARD_INTENT: {"action":"plan",'
            '"source_node_id":"fact-1","target_node_id":"goal-1",'
            '"content":"Verify key"}'
        )

        result = parse_intent(output, self.node_types)

        self.assertEqual(result.status, "valid")
        self.assertEqual(result.decision.action, "plan")
        self.assertEqual(result.decision.intent.content, "Verify key")

    def test_allows_origin_only_when_no_fact_exists(self) -> None:
        output = (
            'BLACKBOARD_INTENT: {"action":"plan",'
            '"source_node_id":"origin-1","target_node_id":"goal-1",'
            '"content":"Bootstrap"}'
        )

        valid = parse_intent(
            output,
            {"origin-1": "origin", "goal-1": "goal"},
        )
        invalid = parse_intent(output, self.node_types)

        self.assertEqual(valid.status, "valid")
        self.assertEqual(invalid.status, "protocol_error")
        self.assertIn("bootstrap", invalid.error)

    def test_parses_explicit_idle(self) -> None:
        output = (
            'BLACKBOARD_INTENT: {"action":"idle",'
            '"reason":"No unresolved work"}'
        )

        result = parse_intent(output, self.node_types)

        self.assertEqual(result.status, "valid")
        self.assertEqual(result.decision.action, "idle")
        self.assertEqual(result.decision.reason, "No unresolved work")

    def test_rejects_spoofed_system_fields(self) -> None:
        payload = {
            "action": "plan",
            "source_node_id": "fact-1",
            "target_node_id": "goal-1",
            "content": "Verify key",
            "source": "human",
        }

        result = parse_intent(
            "BLACKBOARD_INTENT: " + json.dumps(payload),
            self.node_types,
        )

        self.assertEqual(result.status, "protocol_error")
        self.assertIn("unexpected intent fields", result.error)

    def test_render_intent_request_contains_snapshot_and_contract(self) -> None:
        request = render_intent_request(
            {"nodes": [{"id": "goal-1", "type": "goal"}]},
            "E:/ctf/task",
        )

        self.assertIn("BLACKBOARD_INTENT:", request)
        self.assertIn('"id": "goal-1"', request)
        self.assertIn("E:/ctf/task", request)


class ResultProtocolTests(unittest.TestCase):
    def test_parses_fact_and_relation_result(self) -> None:
        payload = {
            "intent_id": "intent-1",
            "facts": [
                {"ref": "fact-new", "content": "JWT uses HS256"}
            ],
            "edges": [
                {"type": "supports", "from": "fact-new", "to": "goal-1"}
            ],
            "goal_achieved": False,
            "final_answer": None,
        }

        result = parse_result(
            "BLACKBOARD_RESULT: " + json.dumps(payload),
            assigned_intent_id="intent-1",
            existing_node_ids={"goal-1"},
        )

        self.assertEqual(result.status, "valid")
        self.assertEqual(result.update.facts[0].content, "JWT uses HS256")
        self.assertEqual(result.update.edges[0].type, "supports")

    def test_result_must_reference_assigned_intent(self) -> None:
        payload = {
            "intent_id": "intent-wrong",
            "facts": [],
            "edges": [],
            "goal_achieved": False,
            "final_answer": None,
        }

        result = parse_result(
            "BLACKBOARD_RESULT: " + json.dumps(payload),
            assigned_intent_id="intent-expected",
            existing_node_ids={"goal-1"},
        )

        self.assertEqual(result.status, "protocol_error")
        self.assertIn("assigned intent", result.error)

    def test_rejects_unknown_relation_reference(self) -> None:
        payload = {
            "intent_id": "intent-1",
            "facts": [],
            "edges": [
                {"type": "supports", "from": "missing", "to": "goal-1"}
            ],
            "goal_achieved": False,
            "final_answer": None,
        }

        result = parse_result(
            "BLACKBOARD_RESULT: " + json.dumps(payload),
            assigned_intent_id="intent-1",
            existing_node_ids={"goal-1"},
        )

        self.assertEqual(result.status, "protocol_error")
        self.assertIn("unknown node reference", result.error)

    def test_rejects_goal_achieved_without_answer(self) -> None:
        payload = {
            "intent_id": "intent-1",
            "facts": [],
            "edges": [],
            "goal_achieved": True,
            "final_answer": None,
        }

        result = parse_result(
            "BLACKBOARD_RESULT: " + json.dumps(payload),
            assigned_intent_id="intent-1",
            existing_node_ids=set(),
        )

        self.assertEqual(result.status, "protocol_error")
        self.assertIn("final_answer", result.error)

    def test_render_execution_request_binds_intent(self) -> None:
        request = render_execution_request(
            snapshot={"nodes": []},
            intent={"id": "intent-1", "content": "Verify key"},
            challenge_dir=None,
        )

        self.assertIn("BLACKBOARD_RESULT:", request)
        self.assertIn("intent-1", request)
        self.assertIn("Verify key", request)

    def test_render_execution_request_contains_exact_result_contract(self) -> None:
        request = render_execution_request(
            snapshot={
                "nodes": [
                    {"id": "goal-1", "type": "goal", "state": "active"}
                ]
            },
            intent={
                "id": "intent-1",
                "source_node_id": "origin-1",
                "target_node_id": "goal-1",
                "content": "Recover the flag",
                "generation": 1,
            },
            challenge_dir=None,
        )

        self.assertIn("use $blackboard-result-protocol", request)
        self.assertIn("If the Skill is unavailable", request)
        self.assertIn(
            '{"ref":"fact-ref","content":"verified fact"}',
            request,
        )
        self.assertIn(
            '{"type":"supports","from":"fact-ref","to":"goal-1"}',
            request,
        )
        self.assertIn('"goal_achieved":true', request)
        self.assertIn('"final_answer":"verified answer"', request)
        for edge_type in RELATION_EDGE_TYPES:
            self.assertIn(edge_type, request)

    def test_render_execution_request_contains_correction_context(self) -> None:
        parameters = inspect.signature(render_execution_request).parameters
        self.assertIn("protocol_error", parameters)
        self.assertIn("rejected_result_line", parameters)
        if "protocol_error" not in parameters:
            return

        rejected = (
            'BLACKBOARD_RESULT: {"intent_id":"intent-1",'
            '"facts":["flag found"],"edges":[],"goal_achieved":true,'
            '"final_answer":"flag{same}"}'
        )
        request = render_execution_request(
            snapshot={"nodes": []},
            intent={
                "id": "intent-1",
                "source_node_id": "origin-1",
                "target_node_id": "goal-1",
                "content": "Recover the flag",
                "generation": 1,
            },
            challenge_dir=None,
            protocol_error="fact proposal must be an object",
            rejected_result_line=rejected,
        )

        self.assertIn("correct the previous result", request)
        self.assertIn("fact proposal must be an object", request)
        self.assertIn(json.dumps(rejected), request)
        self.assertIn("Do not create or plan another Intent", request)

    def test_last_result_line_returns_last_prefixed_line(self) -> None:
        self.assertTrue(hasattr(protocol, "last_result_line"))
        if not hasattr(protocol, "last_result_line"):
            return

        output = "\n".join(
            (
                'BLACKBOARD_RESULT: {"first":true}',
                "analysis",
                'BLACKBOARD_RESULT: {"second":true}',
            )
        )

        self.assertEqual(
            protocol.last_result_line(output),
            'BLACKBOARD_RESULT: {"second":true}',
        )
        self.assertIsNone(protocol.last_result_line("analysis only"))


if __name__ == "__main__":
    unittest.main()
