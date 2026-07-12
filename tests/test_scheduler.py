import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from orchestrator.scheduler import run_orchestration
from worker.models import ProviderConfig, WorkerResult


def provider(name: str) -> ProviderConfig:
    return ProviderConfig(name, True, 10, ("unused",), 5)


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def worker_result(name: str, work_dir: Path, output: str) -> WorkerResult:
        work_dir.mkdir(parents=True, exist_ok=True)
        stdout = work_dir / "stdout.txt"
        stderr = work_dir / "stderr.txt"
        stdout.write_text(output, encoding="utf-8")
        stderr.write_text("", encoding="utf-8")
        return WorkerResult(
            name,
            10,
            "success",
            0,
            0.01,
            output,
            str(stdout),
            str(stderr),
            str(work_dir),
        )

    async def test_cli_wrapper_uses_intent_edge_runtime(self) -> None:
        async def execute(config, request, challenge_dir, work_dir, **kwargs):
            if "BLACKBOARD_INTENT:" in request:
                snapshot = json.loads(request.split("Graph snapshot:\n", 1)[1])
                origin = next(node["id"] for node in snapshot["nodes"] if node["type"] == "origin")
                goal = next(node["id"] for node in snapshot["nodes"] if node["type"] == "goal")
                output = "BLACKBOARD_INTENT: " + json.dumps({
                    "action": "plan",
                    "source_node_id": origin,
                    "target_node_id": goal,
                    "content": "Solve the challenge",
                })
            else:
                intent_text = request.split("Assigned intent:\n", 1)[1].split("\n\nGraph snapshot:", 1)[0]
                intent = json.loads(intent_text)
                output = "BLACKBOARD_RESULT: " + json.dumps({
                    "intent_id": intent["id"],
                    "facts": [],
                    "edges": [],
                    "goal_achieved": True,
                    "final_answer": "flag{same}",
                })
            return self.worker_result(config.name, work_dir, output)

        with tempfile.TemporaryDirectory() as directory:
            result = await run_orchestration(
                providers=(provider("codex"), provider("claude")),
                goal="Goal",
                origin="Origin",
                hints=(),
                challenge_dir=None,
                runs_root=Path(directory),
                max_iterations=1,
                execute=execute,
            )

            self.assertEqual(result.status, "achieved")
            self.assertEqual(result.final_answer, "flag{same}")
            self.assertEqual(result.highest_generation, 1)
            database = Path(result.run_dir) / "blackboard.db"
            connection = sqlite3.connect(database)
            try:
                intent_nodes = connection.execute(
                    "SELECT COUNT(*) FROM nodes WHERE type = 'intent'"
                ).fetchone()[0]
                intent_edges = connection.execute(
                    "SELECT COUNT(*) FROM intents"
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(intent_nodes, 0)
            self.assertEqual(intent_edges, 1)

    async def test_no_enabled_provider_returns_no_usable_providers(self) -> None:
        disabled = ProviderConfig("off", False, 10, ("unused",), 5)
        with tempfile.TemporaryDirectory() as directory:
            result = await run_orchestration(
                providers=(disabled,),
                goal="Goal",
                origin="Origin",
                hints=(),
                challenge_dir=None,
                runs_root=Path(directory),
                max_iterations=1,
            )
        self.assertEqual(result.status, "no_usable_providers")


if __name__ == "__main__":
    unittest.main()
