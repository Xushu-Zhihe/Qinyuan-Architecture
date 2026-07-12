import sqlite3
import tempfile
import unittest
from pathlib import Path

from orchestrator.models import IntentProposal
from orchestrator.store import BlackboardStore


class StoreTestCase(unittest.TestCase):
    def make_store(
        self,
        *,
        goal: str = "Recover the flag",
        origin: str = "Challenge statement",
        hints: tuple[str, ...] = (),
    ) -> BlackboardStore:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        store = BlackboardStore.create(
            Path(temporary.name) / "blackboard.db",
            project_id="project-1",
            goal=goal,
            origin=origin,
            hints=hints,
        )
        self.addCleanup(store.close)
        return store

    def make_store_with_fact(self) -> tuple[BlackboardStore, str]:
        store = self.make_store()
        fact_id = store.add_human_node("fact", "JWT uses HS256")
        return store, fact_id


class BlackboardStoreTests(StoreTestCase):
    def test_create_initializes_generation_zero_human_nodes(self) -> None:
        store = self.make_store(hints=("Inspect config",))

        snapshot = store.snapshot()

        self.assertEqual(snapshot["project"]["status"], "idle")
        self.assertEqual(snapshot["highest_generation"], 0)
        self.assertEqual(
            [node["type"] for node in snapshot["nodes"]],
            ["goal", "origin", "hint"],
        )
        self.assertTrue(
            all(node["generation"] == 0 for node in snapshot["nodes"])
        )
        self.assertTrue(
            all(node["sources"] == ["human"] for node in snapshot["nodes"])
        )
        self.assertEqual(snapshot["edges"][0]["type"], "addresses")

    def test_schema_rejects_intent_nodes(self) -> None:
        store = self.make_store()

        with self.assertRaises(sqlite3.IntegrityError):
            store.connection.execute(
                """
                INSERT INTO nodes(
                    id, project_id, type, content, state,
                    generation, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "intent-bad",
                    store.project_id,
                    "intent",
                    "invalid",
                    "active",
                    1,
                    "now",
                ),
            )

    def test_schema_enforces_edge_foreign_keys(self) -> None:
        store = self.make_store()

        with self.assertRaises(sqlite3.IntegrityError):
            store.connection.execute(
                """
                INSERT INTO edges(
                    id, project_id, type, from_node, to_node,
                    generation, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "edge-bad",
                    store.project_id,
                    "supports",
                    "missing-a",
                    "missing-b",
                    1,
                    "now",
                ),
            )

    def test_bootstrap_intent_uses_generation_one(self) -> None:
        store = self.make_store()
        origin = store.node_id("origin")
        goal = store.node_id("goal")

        intent = store.create_or_merge_intent(
            "codex",
            IntentProposal(origin, goal, "Inspect the challenge"),
        )

        self.assertEqual(intent.generation, 1)
        self.assertEqual(intent.source_node_id, origin)
        self.assertEqual(store.snapshot()["highest_generation"], 1)

    def test_origin_cannot_be_source_after_fact_exists(self) -> None:
        store, _ = self.make_store_with_fact()
        origin = store.node_id("origin")
        goal = store.node_id("goal")

        with self.assertRaisesRegex(ValueError, "bootstrap"):
            store.create_or_merge_intent(
                "codex",
                IntentProposal(origin, goal, "Ignore the fact"),
            )

    def test_same_semantic_intent_merges_edge_and_sources(self) -> None:
        store, fact_id = self.make_store_with_fact()
        goal = store.node_id("goal")

        first = store.create_or_merge_intent(
            "codex",
            IntentProposal(fact_id, goal, "Verify key source"),
        )
        second = store.create_or_merge_intent(
            "claude",
            IntentProposal(fact_id, goal, "  Verify   key source "),
        )

        self.assertEqual(first.id, second.id)
        self.assertEqual(
            store.intent_sources(first.id),
            ("claude", "codex"),
        )
        intents = [
            edge
            for edge in store.snapshot()["edges"]
            if edge["type"] == "intent"
        ]
        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0]["content"], "Verify key source")

    def test_identical_facts_share_node_and_keep_sources(self) -> None:
        store = self.make_store()

        first = store.add_fact("codex", "Binary  uses  AES-CBC", 1)
        second = store.add_fact("claude", "Binary uses AES-CBC", 1)

        self.assertEqual(first, second)
        fact = next(
            node
            for node in store.snapshot()["nodes"]
            if node["id"] == first
        )
        self.assertEqual(fact["sources"], ["claude", "codex"])


if __name__ == "__main__":
    unittest.main()
