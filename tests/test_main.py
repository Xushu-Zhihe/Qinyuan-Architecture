import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import main
from orchestrator.models import OrchestrationResult


class MainTests(unittest.TestCase):
    @patch("main.load_provider_configs", return_value=())
    @patch("main.run_orchestration", new_callable=AsyncMock)
    def test_passes_explicit_blackboard_inputs_and_prints_answer(
        self,
        run_orchestration: AsyncMock,
        load_provider_configs: Mock,
    ) -> None:
        run_orchestration.return_value = OrchestrationResult(
            run_id="run-1",
            run_dir=str(Path("worker/runs/run-1").resolve()),
            status="achieved",
            rounds_completed=2,
            final_answer="flag{ok}",
            candidates=("flag{ok}",),
        )
        output = io.StringIO()

        with redirect_stdout(output):
            exit_code = main.run_cli(
                [
                    "--goal",
                    "Recover the flag",
                    "--origin",
                    "Challenge statement",
                    "--hint",
                    "Check config",
                    "--hint",
                    "Inspect strings",
                    "--max-rounds",
                    "7",
                ]
            )

        self.assertEqual(exit_code, 0)
        self.assertIn("flag{ok}", output.getvalue())
        kwargs = run_orchestration.await_args.kwargs
        self.assertEqual(kwargs["goal"], "Recover the flag")
        self.assertEqual(kwargs["origin"], "Challenge statement")
        self.assertEqual(
            kwargs["hints"],
            ("Check config", "Inspect strings"),
        )
        self.assertEqual(kwargs["max_rounds"], 7)

    def test_rejects_empty_goal(self) -> None:
        errors = io.StringIO()
        with redirect_stderr(errors):
            exit_code = main.run_cli(
                ["--goal", " ", "--origin", "Origin"]
            )
        self.assertEqual(exit_code, 1)
        self.assertIn("goal must not be empty", errors.getvalue())

    def test_rejects_empty_origin(self) -> None:
        errors = io.StringIO()
        with redirect_stderr(errors):
            exit_code = main.run_cli(
                ["--goal", "Goal", "--origin", " "]
            )
        self.assertEqual(exit_code, 1)
        self.assertIn("origin must not be empty", errors.getvalue())

    def test_rejects_missing_challenge_directory(self) -> None:
        errors = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing"
            with redirect_stderr(errors):
                exit_code = main.run_cli(
                    [
                        "--goal",
                        "Goal",
                        "--origin",
                        "Origin",
                        "--challenge",
                        str(missing),
                    ]
                )

        self.assertEqual(exit_code, 1)
        self.assertIn("challenge directory does not exist", errors.getvalue())

    def test_rejects_non_positive_max_rounds(self) -> None:
        with self.assertRaises(SystemExit):
            main.run_cli(
                [
                    "--goal",
                    "Goal",
                    "--origin",
                    "Origin",
                    "--max-rounds",
                    "0",
                ]
            )

    @patch("main.load_provider_configs", return_value=())
    @patch("main.run_orchestration", new_callable=AsyncMock)
    def test_prints_unresolved_candidates_and_returns_one(
        self,
        run_orchestration: AsyncMock,
        load_provider_configs: Mock,
    ) -> None:
        run_orchestration.return_value = OrchestrationResult(
            run_id="run-1",
            run_dir="run-dir",
            status="unresolved",
            rounds_completed=5,
            final_answer=None,
            candidates=("flag{a}", "flag{b}"),
        )
        output = io.StringIO()

        with redirect_stdout(output):
            exit_code = main.run_cli(
                ["--goal", "Goal", "--origin", "Origin"]
            )

        self.assertEqual(exit_code, 1)
        self.assertIn("flag{a}", output.getvalue())
        self.assertIn("flag{b}", output.getvalue())


if __name__ == "__main__":
    unittest.main()
