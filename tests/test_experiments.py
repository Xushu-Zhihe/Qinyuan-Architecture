import asyncio
import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from experiments.analyze import analyze_results
from experiments.config import (
    CONDITIONS,
    ExperimentConfigError,
    load_experiment_config,
)
from experiments.metrics import jain_fairness
from experiments.runner import run_experiment_suite


class ExperimentConfigTests(unittest.TestCase):
    def test_rejects_unknown_condition(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "experiment.json"
            path.write_text(
                json.dumps(
                    {
                        "name": "invalid",
                        "mode": "fake",
                        "seed": 1,
                        "repetitions": 1,
                        "providers": ["a", "b"],
                        "conditions": ["unknown"],
                        "budget": {
                            "max_model_calls": 4,
                            "timeout_seconds": 2,
                            "concurrent_projects": 1,
                        },
                        "tasks": [
                            {
                                "id": "task",
                                "goal": "goal",
                                "origin": "origin",
                                "expected_answer": "answer",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ExperimentConfigError, "unknown conditions"):
                load_experiment_config(path)

    def test_jain_fairness_handles_equal_and_empty_throughput(self) -> None:
        self.assertEqual(jain_fairness([2, 2, 2]), 1.0)
        self.assertAlmostEqual(jain_fairness([1, 0]), 0.5)
        self.assertIsNone(jain_fairness([0, 0]))


class ExperimentRunnerTests(unittest.IsolatedAsyncioTestCase):
    def make_config(self, directory: str):
        path = Path(directory) / "experiment.json"
        path.write_text(
            json.dumps(
                {
                    "name": "test-suite",
                    "mode": "fake",
                    "seed": 100,
                    "repetitions": 1,
                    "providers": ["agent-a", "agent-b", "agent-c"],
                    "conditions": list(CONDITIONS),
                    "budget": {
                        "max_model_calls": 12,
                        "timeout_seconds": 3,
                        "concurrent_projects": 2,
                    },
                    "tasks": [
                        {
                            "id": "direct",
                            "goal": "Return the deterministic answer",
                            "origin": "offline fixture",
                            "expected_answer": "answer{direct}",
                        },
                        {
                            "id": "correction",
                            "goal": "Correct the protocol and answer",
                            "origin": "offline correction fixture",
                            "expected_answer": "answer{correction}",
                            "fake": {
                                "requires_protocol_correction": True
                            },
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        return load_experiment_config(path)

    async def test_fake_suite_saves_replayable_artifacts_and_ablations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = self.make_config(directory)
            output = Path(directory) / "results"

            summary = await run_experiment_suite(config, output)

            self.assertEqual(summary["run_count"], len(CONDITIONS) * 2)
            self.assertEqual(summary["mode"], "fake")
            self.assertIn("not evidence of real-model quality", summary["evidence_boundary"])
            for name in CONDITIONS:
                self.assertIn(name, summary["conditions"])
            self.assertEqual(
                summary["conditions"]["round_robin"]["success_rate"],
                1.0,
            )
            self.assertEqual(
                summary["conditions"]["role_free_async"]["success_rate"],
                1.0,
            )
            self.assertEqual(
                summary["conditions"]["no_protocol_correction"]["success_rate"],
                0.5,
            )

            for name in (
                "manifest.json",
                "config.json",
                "analysis.py",
                "raw_results.jsonl",
                "summary.json",
            ):
                self.assertTrue((output / name).is_file(), name)
            manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["config"]["seed"], 100)
            self.assertIn("experiments/runner.py", manifest["source_sha256"])

            raw = [
                json.loads(line)
                for line in (output / "raw_results.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertTrue(all(item["runner_error"] is None for item in raw))
            for item in raw:
                run_dir = output / item["artifact_dir"]
                self.assertTrue((run_dir / "blackboard.db").is_file())
                self.assertTrue((run_dir / "events.json").is_file())
                self.assertTrue((run_dir / "result.json").is_file())

            limited = [
                item
                for item in raw
                if item["condition"] == "single_execution_per_intent"
            ]
            for item in limited:
                database = output / item["artifact_dir"] / "blackboard.db"
                connection = sqlite3.connect(database)
                try:
                    maximum = connection.execute(
                        """
                        SELECT COALESCE(MAX(attempts), 0)
                        FROM (
                            SELECT COUNT(*) AS attempts
                            FROM intent_executions
                            GROUP BY intent_edge_id
                        )
                        """
                    ).fetchone()[0]
                finally:
                    connection.close()
                self.assertLessEqual(maximum, 1)

            rebuilt = await asyncio.to_thread(analyze_results, output)
            self.assertEqual(rebuilt["run_count"], summary["run_count"])
            self.assertEqual(
                rebuilt["conditions"]["no_protocol_correction"]["success_rate"],
                0.5,
            )

    async def test_real_mode_requires_explicit_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fake = self.make_config(directory)
            real = replace(fake, mode="real")
            output = Path(directory) / "real-results"

            with self.assertRaisesRegex(PermissionError, "require"):
                await run_experiment_suite(real, output)

            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
