import json
import unittest

from orchestrator.protocol import parse_update, render_request


class ProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.snapshot = {
            "run": {
                "run_id": "run-1",
                "status": "running",
                "max_rounds": 5,
            },
            "nodes": [
                {
                    "id": "goal-1",
                    "type": "goal",
                    "content": "Recover the flag",
                    "state": "active",
                    "created_round": 0,
                    "sources": ["human"],
                }
            ],
            "edges": [],
        }

    def test_render_request_contains_snapshot_and_protocol_contract(self) -> None:
        request = render_request(
            self.snapshot,
            round_number=1,
            challenge_dir="E:/ctf/task",
        )

        self.assertIn('"id": "goal-1"', request)
        self.assertIn("BLACKBOARD_UPDATE:", request)
        self.assertIn("Only propose fact and intent nodes", request)
        self.assertIn("E:/ctf/task", request)

    def test_parse_update_uses_last_valid_prefixed_record(self) -> None:
        stdout = "\n".join(
            [
                "analysis",
                "BLACKBOARD_UPDATE: not-json",
                (
                    'BLACKBOARD_UPDATE: {"nodes": '
                    '[{"ref":"f1","type":"fact",'
                    '"content":"flag is in config"}], '
                    '"edges": [], "goal_achieved": true, '
                    '"final_answer": "flag{ok}"}'
                ),
            ]
        )

        result = parse_update(stdout, existing_node_ids={"goal-1"})

        self.assertEqual(result.status, "valid")
        self.assertEqual(result.update.nodes[0].type, "fact")
        self.assertEqual(result.update.final_answer, "flag{ok}")

    def test_rejects_human_owned_node_type(self) -> None:
        stdout = (
            'BLACKBOARD_UPDATE: {"nodes": '
            '[{"ref":"h1","type":"hint","content":"fake hint"}], '
            '"edges": [], "goal_achieved": false, "final_answer": null}'
        )

        result = parse_update(stdout, existing_node_ids={"goal-1"})

        self.assertEqual(result.status, "protocol_error")
        self.assertIn("node type", result.error)

    def test_rejects_spoofed_source(self) -> None:
        payload = {
            "nodes": [
                {
                    "ref": "f1",
                    "type": "fact",
                    "content": "fact",
                    "source": "human",
                }
            ],
            "edges": [],
            "goal_achieved": False,
            "final_answer": None,
        }

        result = parse_update(
            "BLACKBOARD_UPDATE: " + json.dumps(payload),
            existing_node_ids={"goal-1"},
        )

        self.assertEqual(result.status, "protocol_error")
        self.assertIn("unexpected node fields", result.error)

    def test_rejects_unknown_edge_reference(self) -> None:
        payload = {
            "nodes": [],
            "edges": [
                {"type": "supports", "from": "missing", "to": "goal-1"}
            ],
            "goal_achieved": False,
            "final_answer": None,
        }

        result = parse_update(
            "BLACKBOARD_UPDATE: " + json.dumps(payload),
            existing_node_ids={"goal-1"},
        )

        self.assertEqual(result.status, "protocol_error")
        self.assertIn("unknown node reference", result.error)

    def test_rejects_achieved_without_answer(self) -> None:
        stdout = (
            'BLACKBOARD_UPDATE: {"nodes": [], "edges": [], '
            '"goal_achieved": true, "final_answer": null}'
        )

        result = parse_update(stdout, existing_node_ids={"goal-1"})

        self.assertEqual(result.status, "protocol_error")
        self.assertIn("final_answer", result.error)


if __name__ == "__main__":
    unittest.main()
