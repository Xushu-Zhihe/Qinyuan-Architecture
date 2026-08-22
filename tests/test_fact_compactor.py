import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from orchestrator.fact_compactor import (
    FactCompactor,
    parse_fact_compaction_response,
)
from orchestrator.fact_compaction_repository import FactCompactionRepository
from orchestrator.store import BlackboardStore
from worker.models import (
    FactCompactionConfig,
    FactCompactionLLMConfig,
    ProviderConfig,
    WorkerResult,
)


class FactCompactionRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = BlackboardStore.create(
            self.root / "blackboard.db",
            "project-1",
            "Recover the flag",
            "Challenge statement",
            (),
        )
        self.addCleanup(self.store.close)

    def test_archives_agent_duplicates_and_preserves_relations(self) -> None:
        goal_id = self.store.current_goal_node_id()
        fact_ids = tuple(
            self.store.add_fact(f"agent-{index}", content, 1)
            for index, content in enumerate(
                (
                    "The service uses JWT with the HS256 algorithm",
                    "The service uses JWT with HS256 algorithm",
                    "The service uses JWT using the HS256 algorithm",
                )
            )
        )
        self.store.add_relation("addresses", fact_ids[0], goal_id, "agent-0")
        repository = FactCompactionRepository(
            self.store.connection,
            self.store.project_id,
        )

        groups = repository.candidates(min_facts=3)
        self.assertEqual(len(groups), 1)
        self.assertEqual({item.id for item in groups[0]}, set(fact_ids))

        summary_id = repository.apply(
            fact_ids,
            "The service uses JWT with HS256.",
        )
        snapshot = self.store.snapshot()
        summary = next(node for node in snapshot["nodes"] if node["id"] == summary_id)
        self.assertEqual(summary["state"], "active")
        self.assertEqual(summary["sources"], ["fact-summarizer"])
        archived = {
            node["id"]
            for node in snapshot["nodes"]
            if node["state"] == "archived"
        }
        self.assertTrue(set(fact_ids).issubset(archived))
        self.assertTrue(
            any(
                edge["type"] == "addresses"
                and edge["from_node"] == summary_id
                and edge["to_node"] == goal_id
                for edge in snapshot["edges"]
            )
        )
        self.assertEqual(
            sum(
                edge["type"] == "derived_from"
                and edge["from_node"] == summary_id
                for edge in snapshot["edges"]
            ),
            3,
        )

    def test_excludes_human_facts(self) -> None:
        self.store.add_human_node("fact", "The service uses JWT with HS256")
        self.store.add_fact("codex", "The service uses JWT using HS256", 1)
        self.store.add_fact(
            "claude",
            "The service uses JWT with the HS256 algorithm",
            1,
        )

        self.assertEqual(
            FactCompactionRepository(
                self.store.connection,
                self.store.project_id,
            ).candidates(min_facts=3),
            (),
        )

    def test_splits_multiple_topics_and_allows_overlapping_sources(self) -> None:
        fact_ids = tuple(
            self.store.add_fact("agent", content, 1)
            for content in (
                "The report is stored at /srv/report.txt and uses AES-256-GCM",
                "The report is stored at /srv/report.txt",
                "The report uses AES-256-GCM",
            )
        )
        repository = FactCompactionRepository(
            self.store.connection,
            self.store.project_id,
        )

        summary_ids = repository.apply_groups(
            (
                ((fact_ids[0], fact_ids[1]), "The report is stored at /srv/report.txt."),
                ((fact_ids[0], fact_ids[2]), "The report uses AES-256-GCM."),
            )
        )

        self.assertEqual(len(summary_ids), 2)
        active_summaries = [
            node for node in self.store.snapshot()["nodes"]
            if node["id"] in summary_ids and node["state"] == "active"
        ]
        self.assertEqual(len(active_summaries), 2)
        edges = self.store.snapshot()["edges"]
        self.assertEqual(
            sum(
                edge["type"] == "derived_from"
                and edge["from_node"] == summary_ids[0]
                and edge["to_node"] in fact_ids
                for edge in edges
            ),
            2,
        )
        self.assertEqual(
            sum(
                edge["type"] == "derived_from"
                and edge["from_node"] == summary_ids[1]
                and edge["to_node"] in fact_ids
                for edge in edges
            ),
            2,
        )
        self.assertTrue(
            all(
                node["state"] == "archived"
                for node in self.store.snapshot()["nodes"]
                if node["id"] in fact_ids
            )
        )


class FactCompactionProtocolTests(unittest.TestCase):
    def test_parser_accepts_overlapping_groups(self) -> None:
        groups = parse_fact_compaction_response(
            json.dumps(
                {
                    "action": "merge",
                    "groups": [
                        {"source_ids": ["a", "b"], "summary": "location"},
                        {"source_ids": ["a", "c"], "summary": "algorithm"},
                    ],
                }
            ),
            100,
            {"a", "b", "c"},
        )
        self.assertEqual([group.source_ids for group in groups], [("a", "b"), ("a", "c")])

    def test_parser_rejects_invalid_groups(self) -> None:
        for payload in (
            {"action": "merge", "groups": [{"source_ids": ["a"], "summary": "one"}]},
            {"action": "merge", "groups": [{"source_ids": ["a", "x"], "summary": "unknown"}]},
            {
                "action": "merge",
                "groups": [
                    {"source_ids": ["a", "b"], "summary": "same"},
                    {"source_ids": ["b", "a"], "summary": "same"},
                ],
            },
        ):
            with self.assertRaises(ValueError):
                parse_fact_compaction_response(json.dumps(payload), 100, {"a", "b"})


class FactCompactorTests(unittest.IsolatedAsyncioTestCase):
    async def test_direct_llm_configuration_bypasses_cli_executor(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        store = BlackboardStore.create(
            root / "blackboard.db",
            "project-1",
            "Recover the flag",
            "Challenge statement",
            (),
        )
        self.addCleanup(store.close)
        result = WorkerResult(
            provider="summary-llm",
            priority=-100,
            status="success",
            exit_code=0,
            duration_seconds=0.01,
            output='{"action":"skip"}',
            stdout_path=str(root / "stdout.txt"),
            stderr_path=str(root / "stderr.txt"),
            work_dir=str(root),
        )
        config = FactCompactionLLMConfig(
            base_url="https://llm.example/v1",
            api_key="secret-key",
            model="compact-model",
        )
        compactor = FactCompactor(
            store=store,
            project_dir=root,
            config=FactCompactionConfig(enabled=True, llm=config),
        )
        direct = AsyncMock(return_value=result)
        with patch("orchestrator.fact_compactor.execute_fact_compaction_llm", direct):
            actual = await compactor._run_provider("request", root / "run")

        self.assertIs(actual, result)
        direct.assert_awaited_once()
        self.assertEqual(compactor.status()["provider"], "summary-llm")
        self.assertEqual(compactor.status()["model"], "compact-model")

    async def test_background_compactor_uses_dedicated_provider(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        store = BlackboardStore.create(
            root / "blackboard.db",
            "project-1",
            "Recover the flag",
            "Challenge statement",
            (),
        )
        self.addCleanup(store.close)
        for provider, content in (
            ("codex", "The binary uses AES CBC with a static IV"),
            ("claude", "The binary uses AES-CBC with a static IV"),
            ("kimi", "The binary uses AES CBC and a static IV"),
        ):
            store.add_fact(provider, content, 1)

        calls = []
        repository = FactCompactionRepository(
            store.connection,
            store.project_id,
        )

        async def execute(provider, request, challenge_dir, work_dir):
            calls.append((provider.name, request, challenge_dir))
            return WorkerResult(
                provider=provider.name,
                priority=provider.priority,
                status="success",
                exit_code=0,
                duration_seconds=0.01,
                output=(
                    '{"action":"merge","groups":[{"source_ids":['
                    + ",".join(
                        json.dumps(fact.id)
                        for fact in repository.candidates(min_facts=3)[0]
                    )
                    + '],"summary":'
                    '"The binary uses AES-CBC with a static IV."}]}'
                ),
                stdout_path=str(work_dir / "stdout.txt"),
                stderr_path=str(work_dir / "stderr.txt"),
                work_dir=str(work_dir),
            )

        provider = ProviderConfig(
            name="summary-llm",
            enabled=True,
            priority=-100,
            command=("summary-llm",),
            timeout_seconds=30,
            max_running=1,
        )
        compactor = FactCompactor(
            store=store,
            project_dir=root,
            config=FactCompactionConfig(
                enabled=True,
                provider=provider,
                min_facts=3,
                max_facts=8,
                poll_interval_seconds=0.01,
            ),
            execute=execute,
        )
        await compactor.start()
        await compactor.wait_until_idle(timeout=2)
        await compactor.stop()

        self.assertEqual(len(calls), 1)
        self.assertEqual(compactor.status()["status"], "stopped")
        self.assertEqual(compactor.status()["provider"], "summary-llm")
        self.assertEqual(calls[0][0], "summary-llm")
        self.assertIsNone(calls[0][2])
        active_facts = [
            node
            for node in store.snapshot()["nodes"]
            if node["type"] == "fact" and node["state"] == "active"
        ]
        self.assertEqual(len(active_facts), 1)
        self.assertEqual(
            active_facts[0]["content"],
            "The binary uses AES-CBC with a static IV.",
        )


if __name__ == "__main__":
    unittest.main()
