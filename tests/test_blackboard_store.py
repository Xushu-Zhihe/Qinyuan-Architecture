import sqlite3
import tempfile
import unittest
from pathlib import Path

from orchestrator.models import (
    ExecutionUpdate,
    FactProposal,
    IntentProposal,
    RelationProposal,
)
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

    def make_store_and_intent(self):
        store, fact_id = self.make_store_with_fact()
        intent = store.create_or_merge_intent(
            "codex",
            IntentProposal(
                fact_id,
                store.node_id("goal"),
                "Verify the key source",
            ),
        )
        return store, intent

    @staticmethod
    def execution_paths(provider: str = "codex") -> tuple[str, str, str]:
        return (
            f"logs/{provider}/stdout.txt",
            f"logs/{provider}/stderr.txt",
            f"logs/{provider}/work",
        )


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


class BlackboardExecutionTests(StoreTestCase):
    def test_provider_health_is_persisted_redacted_and_evented(self) -> None:
        store = self.make_store()
        store.ensure_provider_health(("codex",))
        sequence = int(store.snapshot()["sequence"])

        record = store.set_provider_health(
            "codex",
            "auth_failed",
            consecutive_failures=1,
            last_error="token=sk-secret 403 Forbidden",
        )

        self.assertEqual(record["status"], "auth_failed")
        self.assertNotIn("sk-secret", str(record["last_error"]))
        self.assertEqual(
            store.snapshot()["provider_health"][0]["status"],
            "auth_failed",
        )
        events = store.events_after(sequence)
        self.assertEqual(events[-1]["type"], "provider.health_changed")
        self.assertNotIn("sk-secret", str(events[-1]["payload"]))

    def test_open_adds_provider_health_table_to_existing_database(self) -> None:
        store = self.make_store()
        path = store.path
        with store.connection:
            store.connection.execute("DROP TABLE provider_health")
        store.close()

        reopened = BlackboardStore.open(path, store.project_id)
        self.addCleanup(reopened.close)
        reopened.ensure_provider_health(("codex",))

        self.assertEqual(
            reopened.provider_health_for("codex")["status"],
            "unknown",
        )

    def test_open_migrates_pre_audit_database(self) -> None:
        store = self.make_store()
        path = store.path
        with store.connection:
            store.connection.execute("DROP TABLE planning_attempts")
            for column in (
                "queued_at",
                "queue_seconds",
                "duration_seconds",
                "retry_count",
                "cancel_reason",
                "wait_reasons_json",
                "model",
                "cli_version",
                "token_usage_json",
                "logs_retained",
                "retry_of_execution_id",
            ):
                store.connection.execute(
                    f"ALTER TABLE intent_executions DROP COLUMN {column}"
                )
            store.connection.execute(
                "ALTER TABLE provider_health DROP COLUMN cli_version"
            )
        store.close()

        reopened = BlackboardStore.open(path, "project-1")
        self.addCleanup(reopened.close)
        execution_columns = {
            str(row[1])
            for row in reopened.connection.execute(
                "PRAGMA table_info(intent_executions)"
            )
        }

        self.assertIn("queue_seconds", execution_columns)
        self.assertIn("token_usage_json", execution_columns)
        self.assertIn("retry_of_execution_id", execution_columns)
        self.assertEqual(reopened.planning_attempts(), ())
        self.assertIn(
            "cli_version",
            {
                str(row[1])
                for row in reopened.connection.execute(
                    "PRAGMA table_info(provider_health)"
                )
            },
        )

    def test_planning_attempt_records_decision_metrics_and_intent(self) -> None:
        store = self.make_store()
        attempt_id = store.start_planning_attempt(
            "codex",
            7,
            "logs/plan/stdout.txt",
            "logs/plan/stderr.txt",
            "logs/plan",
        )
        store.mark_planning_started(
            attempt_id,
            1.25,
            ("global_planning_limit",),
        )
        intent = store.create_or_merge_intent(
            "codex",
            IntentProposal(
                store.node_id("origin"),
                store.node_id("goal"),
                "Inspect the challenge",
            ),
        )
        store.finish_planning_attempt(
            attempt_id,
            "valid",
            "token=sk-planning-secret accepted",
            intent_id=intent.id,
            queue_seconds=1.25,
            duration_seconds=2.5,
            wait_reasons=("global_planning_limit",),
            model="unknown",
            cli_version="codex 1.2.3",
            token_usage={"input_tokens": 12},
        )

        attempt = store.planning_attempt(attempt_id)

        self.assertEqual(attempt["status"], "valid")
        self.assertEqual(attempt["intent_edge_id"], intent.id)
        self.assertEqual(attempt["queue_seconds"], 1.25)
        self.assertEqual(attempt["duration_seconds"], 2.5)
        self.assertEqual(attempt["wait_reasons"], ["global_planning_limit"])
        self.assertEqual(attempt["token_usage"], {"input_tokens": 12})
        self.assertNotIn("sk-planning-secret", attempt["decision_reason"])

    def test_restart_recovery_is_transactional_and_idempotent(self) -> None:
        store, intent = self.make_store_and_intent()
        running = store.start_execution(
            intent.id,
            "codex",
            *self.execution_paths("running"),
        )
        planned = store.start_execution(
            intent.id,
            "claude",
            *self.execution_paths("planned"),
        )
        completed = store.start_execution(
            intent.id,
            "kimi",
            *self.execution_paths("completed"),
        )
        with store.connection:
            store.connection.execute(
                """
                UPDATE intent_executions SET status = 'planned'
                WHERE id = ?
                """,
                (planned.id,),
            )
        store.fail_execution(completed.id, "failed", "provider failed")
        store.set_project_status("running")
        sequence = int(store.snapshot()["sequence"])

        recovered = store.mark_interrupted()

        self.assertEqual(recovered, 2)
        self.assertEqual(store.active_execution_count(), 0)
        self.assertEqual(store.snapshot()["project"]["status"], "idle")
        for execution_id in (running.id, planned.id):
            execution = store.execution(execution_id)
            self.assertEqual(execution["status"], "interrupted")
            self.assertEqual(execution["error_message"], "server restarted")
            self.assertIsNotNone(execution["finished_at"])
        self.assertEqual(store.execution(completed.id)["status"], "failed")

        events = store.events_after(sequence)
        interrupted_events = [
            event
            for event in events
            if event["type"] == "intent.execution_finished"
        ]
        self.assertEqual(len(interrupted_events), 2)
        self.assertTrue(
            all(
                event["payload"]["error_message"] == "server restarted"
                for event in interrupted_events
            )
        )
        self.assertEqual(events[-1]["type"], "project.status_changed")
        self.assertEqual(events[-1]["payload"]["status"], "idle")
        self.assertEqual(events[-1]["payload"]["reason"], "server restarted")

        recovered_sequence = int(store.snapshot()["sequence"])
        self.assertEqual(store.mark_interrupted(), 0)
        self.assertEqual(store.snapshot()["sequence"], recovered_sequence)
        self.assertEqual(
            store.recovery_summary()["executions"],
            2,
        )

    def test_manual_retry_preserves_source_execution(self) -> None:
        store, intent = self.make_store_and_intent()
        source = store.start_execution(
            intent.id,
            "codex",
            *self.execution_paths("source"),
        )
        store.fail_execution(source.id, "failed", "provider failed")

        retried = store.start_execution(
            intent.id,
            "codex",
            *self.execution_paths("retry"),
            retry_count=1,
            retry_of_execution_id=source.id,
            defer_start=True,
        )

        self.assertNotEqual(retried.id, source.id)
        self.assertEqual(store.execution(source.id)["status"], "failed")
        retry_record = store.execution(retried.id)
        self.assertEqual(retry_record["status"], "planned")
        self.assertEqual(retry_record["retry_count"], 1)
        self.assertEqual(retry_record["retry_of_execution_id"], source.id)
        self.assertTrue(
            any(
                event["type"] == "intent.execution_retried"
                and event["payload"]["execution_id"] == retried.id
                for event in store.events_after(0)
            )
        )

    def test_reopen_increments_goal_version_and_isolates_old_answers(self) -> None:
        store = self.make_store()
        store.record_answer("codex", "flag{old}")
        store.record_answer("claude", "flag{old}")
        store.set_project_status("achieved", "flag{old}")

        goal_version = store.reopen_project()
        snapshot = store.snapshot()

        self.assertEqual(goal_version, 2)
        self.assertEqual(snapshot["project"]["status"], "idle")
        self.assertEqual(snapshot["project"]["goal_version"], 2)
        self.assertIsNone(snapshot["project"]["final_answer"])
        self.assertEqual(store.candidates(), ())
        self.assertIsNone(store.consensus(available_providers=2))
        with self.assertRaisesRegex(ValueError, "only an achieved"):
            store.reopen_project()

    def test_execution_output_links_fact_to_intent(self) -> None:
        store, intent = self.make_store_and_intent()
        stdout, stderr, work = self.execution_paths()
        execution = store.start_execution(
            intent.id,
            "codex",
            stdout,
            stderr,
            work,
        )
        update = ExecutionUpdate(
            intent_id=intent.id,
            facts=(FactProposal("fact-new", "JWT secret is in config"),),
            edges=(),
            goal_achieved=False,
            final_answer=None,
        )

        store.finish_execution(execution.id, update, 0, "completed")

        snapshot = store.snapshot()
        fact = next(
            node
            for node in snapshot["nodes"]
            if node["content"] == "JWT secret is in config"
        )
        self.assertEqual(fact["generation"], intent.generation)
        self.assertEqual(fact["produced_by"]["intent_id"], intent.id)
        self.assertEqual(
            fact["produced_by"]["execution_id"],
            execution.id,
        )

    def test_invalid_relation_rolls_back_result_and_status(self) -> None:
        store, intent = self.make_store_and_intent()
        execution = store.start_execution(
            intent.id,
            "codex",
            *self.execution_paths(),
        )
        update = ExecutionUpdate(
            intent_id=intent.id,
            facts=(FactProposal("fact-new", "A transient fact"),),
            edges=(
                RelationProposal("supports", "fact-new", "missing"),
            ),
            goal_achieved=False,
            final_answer=None,
        )

        with self.assertRaisesRegex(ValueError, "unknown node reference"):
            store.finish_execution(execution.id, update, 0, "completed")

        snapshot = store.snapshot()
        self.assertNotIn(
            "A transient fact",
            [node["content"] for node in snapshot["nodes"]],
        )
        self.assertEqual(store.execution(execution.id)["status"], "running")

    def test_intent_status_aggregates_independent_executions(self) -> None:
        store, intent = self.make_store_and_intent()
        first = store.start_execution(
            intent.id,
            "codex",
            *self.execution_paths("codex"),
        )
        second = store.start_execution(
            intent.id,
            "claude",
            *self.execution_paths("claude"),
        )

        store.fail_execution(first.id, "failed", "process failed")
        self.assertEqual(store.intent_status(intent.id), "running")
        store.finish_execution(
            second.id,
            ExecutionUpdate(intent.id, (), (), False, None),
            0,
            "completed",
        )

        self.assertEqual(store.intent_status(intent.id), "completed")
        edge = next(
            edge
            for edge in store.snapshot()["edges"]
            if edge["id"] == intent.id
        )
        self.assertEqual(len(edge["executions"]), 2)

    def test_consensus_spans_asynchronous_executions(self) -> None:
        store = self.make_store()

        store.record_answer("codex", "flag{same}")
        self.assertIsNone(store.consensus(available_providers=2))
        store.record_answer("claude", " flag{same} ")

        self.assertEqual(
            store.consensus(available_providers=2),
            "flag{same}",
        )
        self.assertEqual(store.candidates(), ("flag{same}",))

    def test_events_after_returns_monotonic_sequence(self) -> None:
        store, intent = self.make_store_and_intent()
        first_sequence = store.snapshot()["sequence"]
        store.start_execution(
            intent.id,
            "codex",
            *self.execution_paths(),
        )

        events = store.events_after(first_sequence)

        self.assertGreater(len(events), 0)
        self.assertTrue(
            all(event["sequence"] > first_sequence for event in events)
        )
        self.assertEqual(
            [event["sequence"] for event in events],
            sorted(event["sequence"] for event in events),
        )

    def test_graph_sequence_ignores_runtime_and_layout_events(self) -> None:
        store = self.make_store()
        initial = store.graph_sequence()

        store.append_event(
            "agent.status_changed",
            "codex",
            {"provider": "codex", "status": "planning"},
        )
        store.append_event(
            "graph.layout_updated",
            "human",
            {"revision": 1},
        )

        self.assertEqual(store.graph_sequence(), initial)
        store.add_human_node("fact", "New evidence")
        self.assertGreater(store.graph_sequence(), initial)


if __name__ == "__main__":
    unittest.main()
