import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import AsyncMock, patch

import main
from orchestrator.models import OrchestrationResult


class MainTests(unittest.TestCase):
    @patch("main.load_provider_configs", return_value=())
    @patch("main.run_orchestration", new_callable=AsyncMock)
    def test_passes_iterations_and_prints_generation(
        self,
        run_orchestration: AsyncMock,
        load_provider_configs,
    ) -> None:
        run_orchestration.return_value = OrchestrationResult(
            project_id="project-1",
            run_dir=str(Path("worker/runs/project-1").resolve()),
            status="achieved",
            highest_generation=3,
            final_answer="flag{ok}",
            candidates=("flag{ok}",),
        )
        output = io.StringIO()

        with redirect_stdout(output):
            exit_code = main.run_cli([
                "--goal", "Recover the flag",
                "--origin", "Challenge statement",
                "--max-iterations", "7",
            ])

        self.assertEqual(exit_code, 0)
        self.assertIn("Generation: 3", output.getvalue())
        self.assertEqual(
            run_orchestration.await_args.kwargs["max_iterations"],
            7,
        )

    def test_rejects_empty_goal(self) -> None:
        errors = io.StringIO()
        with redirect_stderr(errors):
            exit_code = main.run_cli(["--goal", " ", "--origin", "Origin"])
        self.assertEqual(exit_code, 1)
        self.assertIn("goal must not be empty", errors.getvalue())

    def test_rejects_non_positive_iterations(self) -> None:
        with self.assertRaises(SystemExit):
            main.run_cli([
                "--goal", "Goal",
                "--origin", "Origin",
                "--max-iterations", "0",
            ])


if __name__ == "__main__":
    unittest.main()
