import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from orchestrator.models import AgentUpdate, EdgeProposal, NodeProposal
from orchestrator.store import BlackboardStore


class StoreTestCase(unittest.TestCase):
    def make_store(
        self,
        *,
        goal: str = "Goal",
        origin: str = "Origin",
        hints: tuple[str, ...] = (),
    ) -> tuple[BlackboardStore, Path]:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        store = BlackboardStore.create(
            root / "blackboard.db",
            run_id="run-1",
            max_rounds=5,
            goal=goal,
            origin=origin,
            hints=hints,
        )
        self.addCleanup(store.close)
        return store, root


class BlackboardStoreTests(StoreTestCase):
    def test_create_initializes_schema_and_human_nodes(self) -> None:
        store, _ = self.make_store(
            goal="Recover the flag",
            origin="The binary reads config.dat",
            hints=("Inspect config.dat first",),
        )

        snapshot = store.snapshot()

        self.assertEqual(snapshot["run"]["status"], "running")
        self.assertEqual(snapshot["run"]["max_rounds"], 5)
        self.assertEqual(
            [node["type"] for node in snapshot["nodes"]],
            ["goal", "origin", "hint"],
        )
        self.assertTrue(
            all(item["sources"] == ["human"] for item in snapshot["nodes"])
        )
        self.assertEqual(snapshot["edges"][0]["type"], "addresses")

    def test_schema_enforces_edge_foreign_keys(self) -> None:
        store, _ = self.make_store()

        with self.assertRaises(sqlite3.IntegrityError):
            store.connection.execute(
                """
                INSERT INTO edges(
                    id, run_id, type, from_node, to_node, source, round_number
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "edge-bad",
                    "run-1",
                    "supports",
                    "missing-a",
                    "missing-b",
                    "codex",
                    1,
                ),
            )

    def test_export_snapshot_writes_stable_json(self) -> None:
        store, root = self.make_store()

        path = store.export_snapshot(0, root / "snapshots")
        payload = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(path.name, "round-000.json")
        self.assertEqual(payload, store.snapshot())

    def test_consensus_requires_two_agents_when_multiple_are_available(self) -> None:
        store, _ = self.make_store()
        store.record_agent_result(
            1,
            "codex",
            "success",
            "valid",
            0,
            1.0,
            "a",
            "b",
            "c",
            "flag{same}",
        )

        self.assertIsNone(store.consensus(1, available_agents=3))

        store.record_agent_result(
            1,
            "claude",
            "success",
            "valid",
            0,
            1.0,
            "d",
            "e",
            "f",
            " flag{same} ",
        )
        self.assertEqual(
            store.consensus(1, available_agents=3),
            "flag{same}",
        )

    def test_conflicting_answers_are_returned_without_priority_tiebreak(self) -> None:
        store, _ = self.make_store()
        store.record_agent_result(
            1,
            "codex",
            "success",
            "valid",
            0,
            1.0,
            "a",
            "b",
            "c",
            "flag{a}",
        )
        store.record_agent_result(
            1,
            "claude",
            "success",
            "valid",
            0,
            1.0,
            "d",
            "e",
            "f",
            "flag{b}",
        )

        self.assertIsNone(store.consensus(1, available_agents=2))
        self.assertEqual(store.candidates(), ("flag{a}", "flag{b}"))

    def test_add_hint_creates_human_node_addressing_goal(self) -> None:
        store, _ = self.make_store()

        hint_id = store.add_hint("Check the alternate key", round_number=1)
        snapshot = store.snapshot()
        hint = next(node for node in snapshot["nodes"] if node["id"] == hint_id)
        edge = next(
            edge
            for edge in snapshot["edges"]
            if edge["from_node"] == hint_id
        )

        self.assertEqual(hint["sources"], ["human"])
        self.assertEqual(edge["type"], "addresses")
        self.assertEqual(edge["source"], "human")

    def test_finish_persists_run_status_and_final_answer(self) -> None:
        store, _ = self.make_store()

        store.finish("achieved", "flag{done}", round_number=2)

        run = store.snapshot()["run"]
        self.assertEqual(run["status"], "achieved")
        self.assertEqual(run["final_answer"], "flag{done}")
        event = store.connection.execute(
            """
            SELECT round_number, type
            FROM events
            WHERE type = 'run_finished'
            """
        ).fetchone()
        self.assertEqual(tuple(event), (2, "run_finished"))


class BlackboardMergeTests(StoreTestCase):
    def test_identical_facts_share_one_node_and_keep_both_sources(self) -> None:
        store, _ = self.make_store()
        first = AgentUpdate(
            nodes=(NodeProposal("f1", "fact", "Binary  uses  AES-CBC"),),
            edges=(),
            goal_achieved=False,
            final_answer=None,
        )
        second = AgentUpdate(
            nodes=(NodeProposal("f2", "fact", "Binary uses AES-CBC"),),
            edges=(),
            goal_achieved=False,
            final_answer=None,
        )

        store.apply_round(1, (("codex", first), ("claude", second)))
        facts = [
            node for node in store.snapshot()["nodes"] if node["type"] == "fact"
        ]

        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]["sources"], ["claude", "codex"])

    def test_similar_facts_remain_distinct_and_are_linked(self) -> None:
        store, _ = self.make_store()
        cbc = AgentUpdate(
            nodes=(NodeProposal("cbc", "fact", "The cipher mode is AES-CBC"),),
            edges=(),
            goal_achieved=False,
            final_answer=None,
        )
        ecb = AgentUpdate(
            nodes=(NodeProposal("ecb", "fact", "The cipher mode is AES-ECB"),),
            edges=(),
            goal_achieved=False,
            final_answer=None,
        )

        store.apply_round(1, (("codex", cbc), ("claude", ecb)))
        snapshot = store.snapshot()
        facts = [node for node in snapshot["nodes"] if node["type"] == "fact"]

        self.assertEqual(len(facts), 2)
        self.assertIn("similar_to", [edge["type"] for edge in snapshot["edges"]])

    def test_new_intent_supersedes_same_agents_old_intent(self) -> None:
        store, _ = self.make_store()
        first = AgentUpdate(
            nodes=(NodeProposal("i1", "intent", "Inspect imports"),),
            edges=(),
            goal_achieved=False,
            final_answer=None,
        )
        second = AgentUpdate(
            nodes=(NodeProposal("i2", "intent", "Trace decryption"),),
            edges=(),
            goal_achieved=False,
            final_answer=None,
        )

        store.apply_round(1, (("codex", first),))
        store.apply_round(2, (("codex", second),))
        snapshot = store.snapshot()
        intents = [
            node for node in snapshot["nodes"] if node["type"] == "intent"
        ]

        self.assertEqual(
            [node["state"] for node in intents],
            ["superseded", "active"],
        )
        self.assertIn("supersedes", [edge["type"] for edge in snapshot["edges"]])

    def test_invalid_edge_rolls_back_whole_round(self) -> None:
        store, _ = self.make_store()
        update = AgentUpdate(
            nodes=(NodeProposal("f1", "fact", "A useful fact"),),
            edges=(EdgeProposal("supports", "f1", "missing-node"),),
            goal_achieved=False,
            final_answer=None,
        )

        with self.assertRaisesRegex(ValueError, "unknown node reference"):
            store.apply_round(1, (("codex", update),))

        self.assertEqual(
            [
                node
                for node in store.snapshot()["nodes"]
                if node["type"] == "fact"
            ],
            [],
        )


if __name__ == "__main__":
    unittest.main()
