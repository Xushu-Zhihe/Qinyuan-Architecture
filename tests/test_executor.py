import asyncio
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from worker import executor
from worker.executor import execute_provider
from worker.models import ProviderConfig


class ExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        asyncio.get_running_loop().slow_callback_duration = 1.0

    @patch("worker.executor.os.name", "nt")
    async def test_resolves_windows_command_shims(self) -> None:
        with patch.object(
            shutil,
            "which",
            return_value=r"C:\tools\codex.CMD",
        ) as which:
            command = executor._resolve_executable(("codex", "--version"))

        self.assertEqual(command, (r"C:\tools\codex.CMD", "--version"))
        which.assert_called_once_with("codex")

    async def test_passes_rendered_request_unchanged_and_uses_provider_cwd(self) -> None:
        code = (
            "import os,sys;"
            "data=sys.stdin.read();"
            "print(os.getcwd());"
            "print(data)"
        )
        provider = ProviderConfig(
            name="fake",
            enabled=True,
            priority=10,
            command=(sys.executable, "-c", code),
            timeout_seconds=5,
        )
        request = "GRAPH SNAPSHOT\nBLACKBOARD_UPDATE contract | keep $text"

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            work_dir = root / "fake"
            challenge_dir = root / "challenge"
            challenge_dir.mkdir()

            result = await execute_provider(
                provider, request, challenge_dir, work_dir
            )

            self.assertEqual(result.status, "success")
            normalized_output = result.output.replace("\r\n", "\n")
            self.assertIn(str(work_dir.resolve()), normalized_output)
            self.assertIn(request, normalized_output)
            self.assertNotIn("Task:\n", normalized_output)
            self.assertTrue(Path(result.stdout_path).is_file())
            self.assertTrue(Path(result.stderr_path).is_file())

    async def test_records_non_zero_exit(self) -> None:
        provider = ProviderConfig(
            name="failed",
            enabled=True,
            priority=10,
            command=(
                sys.executable,
                "-c",
                "import sys;print('bad', file=sys.stderr);sys.exit(7)",
            ),
            timeout_seconds=5,
        )
        with tempfile.TemporaryDirectory() as directory:
            work_dir = Path(directory) / "failed"
            result = await execute_provider(provider, "task", None, work_dir)
            self.assertEqual(result.status, "failed")
            self.assertEqual(result.exit_code, 7)
            self.assertIn("bad", Path(result.stderr_path).read_text(encoding="utf-8"))

    async def test_records_missing_executable(self) -> None:
        provider = ProviderConfig(
            name="missing",
            enabled=True,
            priority=10,
            command=("definitely-not-a-real-ctf-cli",),
            timeout_seconds=5,
        )
        with tempfile.TemporaryDirectory() as directory:
            result = await execute_provider(
                provider, "task", None, Path(directory) / "missing"
            )
            self.assertEqual(result.status, "unavailable")
            self.assertIsNone(result.exit_code)

    async def test_kills_timed_out_provider(self) -> None:
        provider = ProviderConfig(
            name="slow",
            enabled=True,
            priority=10,
            command=(sys.executable, "-c", "import time;time.sleep(5)"),
            timeout_seconds=0.05,
        )
        with tempfile.TemporaryDirectory() as directory:
            result = await execute_provider(
                provider, "task", None, Path(directory) / "slow"
            )
            self.assertEqual(result.status, "timeout")
            self.assertIsNone(result.exit_code)


if __name__ == "__main__":
    unittest.main()
