import asyncio
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from orchestrator.scheduler import run_orchestration
from worker.models import ProviderConfig, WorkerResult


def provider(name: str, priority: int = 10) -> ProviderConfig:
    return ProviderConfig(name, True, priority, ("unused",), 5)


def update_line(
    *,
    answer: str | None = None,
    nodes: list[dict[str, str]] | None = None,
) -> str:
    payload = {
        "nodes": nodes or [],
        "edges": [],
        "goal_achieved": answer is not None,
        "final_answer": answer,
    }
    return "BLACKBOARD_UPDATE: " + json.dumps(payload)


def worker_result(
    name: str,
    work_dir: Path,
    output: str,
    *,
    status: str = "success",
) -> WorkerResult:
    work_dir.mkdir(parents=True, exist_ok=True)
    stdout = work_dir / "stdout.txt"
    stderr = work_dir / "stderr.txt"
    stdout.write_text(output, encoding="utf-8")
    stderr.write_text("", encoding="utf-8")
    return WorkerResult(
        provider=name,
        priority=10,
        status=status,
        exit_code=0 if status == "success" else None,
        duration_seconds=0.1,
        output=output,
        stdout_path=str(stdout),
        stderr_path=str(stderr),
        work_dir=str(work_dir),
    )


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def test_runs_agents_concurrently_with_identical_snapshot(self) -> None:
        requests: list[str] = []

        async def execute(config, request, challenge_dir, work_dir):
            requests.append(request)
            await asyncio.sleep(0.2)
            return worker_result(config.name, work_dir, update_line())

        with tempfile.TemporaryDirectory() as directory:
            started = time.monotonic()
            result = await run_orchestration(
                providers=(provider("codex"), provider("claude")),
                goal="Goal",
                origin="Origin",
                hints=(),
                challenge_dir=None,
                runs_root=Path(directory),
                max_rounds=1,
                execute=execute,
                hint_reader=lambda _: "",
                interactive=False,
            )
            elapsed = time.monotonic() - started

            self.assertLess(elapsed, 0.35)
            self.assertEqual(requests[0], requests[1])
            self.assertEqual(result.status, "max_rounds_reached")

    async def test_reaches_consensus_without_provider_priority(self) -> None:
        async def execute(config, request, challenge_dir, work_dir):
            return worker_result(
                config.name,
                work_dir,
                update_line(answer="flag{same}"),
            )

        with tempfile.TemporaryDirectory() as directory:
            result = await run_orchestration(
                providers=(provider("codex", 1), provider("claude", 999)),
                goal="Goal",
                origin="Origin",
                hints=(),
                challenge_dir=None,
                runs_root=Path(directory),
                max_rounds=3,
                execute=execute,
                hint_reader=lambda _: "",
                interactive=False,
            )

            self.assertEqual(result.status, "achieved")
            self.assertEqual(result.final_answer, "flag{same}")
            self.assertEqual(result.rounds_completed, 1)

    async def test_conflict_continues_and_hint_reaches_next_snapshot(self) -> None:
        second_round_requests: list[str] = []

        async def execute(config, request, challenge_dir, work_dir):
            round_number = int(work_dir.parent.name)
            if round_number == 2:
                second_round_requests.append(request)
                answer = "flag{resolved}"
            else:
                answer = "flag{a}" if config.name == "codex" else "flag{b}"
            return worker_result(
                config.name,
                work_dir,
                update_line(answer=answer),
            )

        hints = iter(("Check the alternate key",))
        with tempfile.TemporaryDirectory() as directory:
            result = await run_orchestration(
                providers=(provider("codex"), provider("claude")),
                goal="Goal",
                origin="Origin",
                hints=(),
                challenge_dir=None,
                runs_root=Path(directory),
                max_rounds=3,
                execute=execute,
                hint_reader=lambda _: next(hints, ""),
                interactive=True,
            )

            self.assertEqual(result.status, "achieved")
            self.assertEqual(result.rounds_completed, 2)
            self.assertTrue(
                all(
                    "Check the alternate key" in request
                    and "flag{a}" in request
                    and "flag{b}" in request
                    for request in second_round_requests
                )
            )

    async def test_protocol_error_does_not_block_valid_agent(self) -> None:
        async def execute(config, request, challenge_dir, work_dir):
            output = (
                "not structured"
                if config.name == "codex"
                else update_line(
                    nodes=[
                        {"ref": "f1", "type": "fact", "content": "valid"}
                    ]
                )
            )
            return worker_result(config.name, work_dir, output)

        with tempfile.TemporaryDirectory() as directory:
            result = await run_orchestration(
                providers=(provider("codex"), provider("claude")),
                goal="Goal",
                origin="Origin",
                hints=(),
                challenge_dir=None,
                runs_root=Path(directory),
                max_rounds=1,
                execute=execute,
                hint_reader=lambda _: "",
                interactive=False,
            )
            database = Path(result.run_dir) / "blackboard.db"
            connection = sqlite3.connect(database)
            try:
                count = connection.execute(
                    "SELECT COUNT(*) FROM nodes WHERE type = 'fact'"
                ).fetchone()[0]
            finally:
                connection.close()

            self.assertEqual(count, 1)

    async def test_one_available_agent_can_reach_consensus(self) -> None:
        async def execute(config, request, challenge_dir, work_dir):
            if config.name == "missing":
                return worker_result(
                    config.name,
                    work_dir,
                    "",
                    status="unavailable",
                )
            return worker_result(
                config.name,
                work_dir,
                update_line(answer="flag{solo}"),
            )

        with tempfile.TemporaryDirectory() as directory:
            result = await run_orchestration(
                providers=(provider("missing"), provider("codex")),
                goal="Goal",
                origin="Origin",
                hints=(),
                challenge_dir=None,
                runs_root=Path(directory),
                max_rounds=2,
                execute=execute,
                hint_reader=lambda _: "",
                interactive=False,
            )

            self.assertEqual(result.status, "achieved")
            self.assertEqual(result.final_answer, "flag{solo}")


if __name__ == "__main__":
    unittest.main()
