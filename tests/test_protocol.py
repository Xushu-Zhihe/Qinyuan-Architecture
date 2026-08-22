import inspect
import json
import unittest

import orchestrator.protocol as protocol
from orchestrator.models import (
    MAX_FACT_CONTENT_LENGTH,
    MAX_FACTS_PER_RESULT,
    MAX_NEW_RESULT_FACT_CONTENT_LENGTH,
    RELATION_EDGE_TYPES,
)
from orchestrator.protocol import (
    MAX_PROMPT_FACTS,
    MAX_PROMPT_FACT_TOTAL_LENGTH,
    build_prompt_snapshot,
    parse_intent,
    parse_result,
    render_execution_request,
    render_intent_request,
    render_timeout_summary_request,
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
        self.assertIn('"id":"goal-1"', request)
        self.assertIn("E:/ctf/task", request)
        self.assertIn("authoritative target endpoint", request)
        self.assertIn("Do not call tools", request)
        self.assertIn("Do not execute the action", request)


class ResultProtocolTests(unittest.TestCase):
    @staticmethod
    def v2_payload(
        facts: list[dict[str, str]],
        *,
        achieved: bool = False,
        result_ref: str | None = None,
    ) -> dict[str, object]:
        edges = (
            [{"type": "addresses", "from": result_ref, "to": "goal-1"}]
            if achieved and result_ref is not None
            else []
        )
        return {
            "protocol_version": 2,
            "intent_id": "intent-1",
            "facts": facts,
            "edges": edges,
            "goal_achieved": achieved,
            "goal_result_ref": result_ref,
        }

    def parse_v2(self, payload: dict[str, object]):
        return parse_result(
            "BLACKBOARD_RESULT: " + json.dumps(payload),
            assigned_intent_id="intent-1",
            existing_node_ids={"goal-1"},
            node_types={"goal-1": "goal"},
            current_goal_node_id="goal-1",
            existing_addresses=set(),
        )

    def test_prompt_snapshot_is_active_relevant_and_bounded(self) -> None:
        nodes: list[dict[str, object]] = [
            {"id": "goal-1", "type": "goal", "content": "goal", "state": "active"},
            {"id": "origin-1", "type": "origin", "content": "origin", "state": "active"},
            {"id": "archived", "type": "fact", "content": "old", "state": "archived"},
        ]
        nodes.extend(
            {
                "id": f"fact-{index}",
                "type": "fact",
                "content": str(index) * 1_500,
                "state": "active",
                "generation": index,
            }
            for index in range(14)
        )
        edges = [
            {
                "id": f"edge-{index}",
                "type": "supports",
                "from_node": f"fact-{index}",
                "to_node": "goal-1",
            }
            for index in range(14)
        ]
        edges.append(
            {
                "id": "similar",
                "type": "similar_to",
                "from_node": "fact-0",
                "to_node": "fact-1",
            }
        )
        compact = build_prompt_snapshot(
            {
                "project": {"id": "project-1", "status": "running", "secret": "drop"},
                "nodes": nodes,
                "edges": edges,
                "planning_attempts": [{"large": "drop"}],
                "provider_health": [{"large": "drop"}],
                "highest_generation": 13,
            },
            {"source_node_id": "fact-1", "target_node_id": "goal-1"},
        )

        fact_nodes = [node for node in compact["nodes"] if node["type"] == "fact"]
        self.assertLessEqual(len(fact_nodes), MAX_PROMPT_FACTS)
        self.assertLessEqual(
            sum(len(str(node["content"])) for node in fact_nodes),
            MAX_PROMPT_FACT_TOTAL_LENGTH,
        )
        self.assertIn("fact-1", {node["id"] for node in fact_nodes})
        self.assertNotIn("fact-0", {node["id"] for node in fact_nodes})
        self.assertNotIn("archived", {node["id"] for node in compact["nodes"]})
        self.assertNotIn("planning_attempts", compact)
        self.assertNotIn("provider_health", compact)
        self.assertEqual(compact["project"], {"id": "project-1", "status": "running"})
        self.assertGreater(compact["context_omissions"]["facts"], 0)

    def test_rejects_too_many_facts(self) -> None:
        payload = self.v2_payload(
            [
                {"ref": f"fact-{index}", "content": "verified"}
                for index in range(MAX_FACTS_PER_RESULT + 1)
            ]
        )
        result = self.parse_v2(payload)
        self.assertEqual(result.status, "protocol_error")
        self.assertIn("more than", result.error)

    def test_rejects_oversized_evidence_but_allows_bounded_result(self) -> None:
        oversized = self.v2_payload(
            [{"ref": "evidence", "content": "x" * (MAX_FACT_CONTENT_LENGTH + 1)}]
        )
        self.assertIn("exceeds", self.parse_v2(oversized).error)

        bounded_result = self.v2_payload(
            [{"ref": "result", "content": "x" * MAX_NEW_RESULT_FACT_CONTENT_LENGTH}],
            achieved=True,
            result_ref="result",
        )
        self.assertEqual(self.parse_v2(bounded_result).status, "valid")

    def test_rejects_total_fact_content_over_budget(self) -> None:
        payload = self.v2_payload(
            [
                {"ref": "result", "content": "r" * 4_000},
                {"ref": "evidence-1", "content": "a" * 700},
                {"ref": "evidence-2", "content": "b" * 700},
                {"ref": "evidence-3", "content": "c" * 700},
            ],
            achieved=True,
            result_ref="result",
        )
        result = self.parse_v2(payload)
        self.assertEqual(result.status, "protocol_error")
        self.assertIn("total fact content", result.error)

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

    def test_parses_explicit_answer_key(self) -> None:
        payload = {
            "intent_id": "intent-1",
            "facts": [],
            "edges": [],
            "goal_achieved": True,
            "answer_key": "flag{same}",
            "final_answer": "Verified answer: flag{same}",
        }

        result = parse_result(
            "BLACKBOARD_RESULT: " + json.dumps(payload),
            assigned_intent_id="intent-1",
            existing_node_ids=set(),
        )

        self.assertEqual(result.status, "valid")
        self.assertEqual(result.update.answer_key, "flag{same}")
        self.assertEqual(result.update.final_answer, "Verified answer: flag{same}")

    def test_legacy_result_falls_back_to_final_answer_key(self) -> None:
        payload = {
            "intent_id": "intent-1",
            "facts": [],
            "edges": [],
            "goal_achieved": True,
            "final_answer": "flag{legacy}",
        }

        result = parse_result(
            "BLACKBOARD_RESULT: " + json.dumps(payload),
            assigned_intent_id="intent-1",
            existing_node_ids=set(),
        )

        self.assertEqual(result.status, "valid")
        self.assertEqual(result.update.answer_key, "flag{legacy}")

    def test_render_execution_request_binds_intent(self) -> None:
        request = render_execution_request(
            snapshot={"nodes": []},
            intent={"id": "intent-1", "content": "Verify key"},
            challenge_dir=None,
        )

        self.assertIn("BLACKBOARD_RESULT:", request)
        self.assertIn("intent-1", request)
        self.assertIn("Verify key", request)
        self.assertIn("authoritative target endpoint", request)

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
        self.assertIn('"answer_key":"verified-answer"', request)
        self.assertIn('"final_answer":"verified answer"', request)
        for edge_type in RELATION_EDGE_TYPES:
            self.assertIn(edge_type, request)
        self.assertIn("environment or provider limitations", request)
        self.assertIn("diagnostics, not challenge facts", request)

    def test_render_timeout_summary_request_is_immediate_and_data_only(self) -> None:
        request = render_timeout_summary_request(
            snapshot={"nodes": [{"id": "goal-1", "type": "goal"}]},
            intent={"id": "intent-1", "content": "Recover the flag"},
            challenge_dir=None,
            partial_output="verified prefix\nBLACKBOARD_RESULT: partial",
        )

        self.assertIn("immediate timeout-recovery pass", request)
        self.assertIn("Do not continue investigating", request)
        self.assertIn("<captured-output>", request)
        self.assertIn("verified prefix", request)
        self.assertIn("intent-1", request)
        self.assertIn("BLACKBOARD_RESULT:", request)

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

    def test_render_execution_request_bounds_rejected_result(self) -> None:
        request = render_execution_request(
            snapshot={"nodes": []},
            intent={"id": "intent-1", "content": "Recover the flag"},
            challenge_dir=None,
            protocol_error="fact content exceeds budget",
            rejected_result_line="x" * 10_000,
        )

        self.assertIn("...[omitted]...", request)
        self.assertNotIn("x" * 2_049, request)

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
