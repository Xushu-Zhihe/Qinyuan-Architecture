import asyncio
import dataclasses
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from worker import executor
from worker.executor import execute_provider
from worker.models import LogChunk, ProviderConfig


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

    async def test_redacts_and_limits_persisted_and_streamed_logs(self) -> None:
        provider = ProviderConfig(
            name="bounded",
            enabled=True,
            priority=10,
            command=(
                sys.executable,
                "-c",
                "print('token=sk-super-secret ' + 'x' * 200)",
            ),
            timeout_seconds=5,
        )
        chunks: list[LogChunk] = []

        async def on_chunk(chunk: LogChunk) -> None:
            chunks.append(chunk)

        with tempfile.TemporaryDirectory() as directory:
            result = await execute_provider(
                provider,
                "task",
                None,
                Path(directory) / "bounded",
                on_chunk=on_chunk,
                max_log_bytes=64,
            )
            persisted = Path(result.stdout_path).read_bytes()

        streamed = "".join(chunk.text for chunk in chunks)
        self.assertLessEqual(len(persisted), 64)
        self.assertNotIn(b"sk-super-secret", persisted)
        self.assertNotIn("sk-super-secret", streamed)
        self.assertIn("[redacted]", result.output)

    async def test_keeps_protocol_output_separate_from_redacted_audit_logs(self) -> None:
        payload = {
            "protocol_version": 2,
            "intent_id": "intent-1",
            "facts": [
                {
                    "ref": "fact-correct-password",
                    "content": "The correct password=nss826312",
                },
                {
                    "ref": "fact-token-analysis",
                    "content": "Token label was inspected",
                },
                {
                    "ref": "fact-secret-discovery",
                    "content": "Secret label was inspected",
                },
            ],
            "edges": [],
            "goal_achieved": False,
            "goal_result_ref": None,
        }
        protocol = "BLACKBOARD_RESULT: " + json.dumps(payload)
        provider = ProviderConfig(
            name="protocol",
            enabled=True,
            priority=10,
            command=(sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read())"),
            timeout_seconds=5,
        )
        chunks: list[LogChunk] = []

        with tempfile.TemporaryDirectory() as directory:
            result = await execute_provider(
                provider,
                protocol,
                None,
                Path(directory) / "protocol",
                on_chunk=chunks.append,
            )
            persisted = Path(result.stdout_path).read_text(encoding="utf-8")

        self.assertEqual(result.protocol_text, protocol)
        self.assertIn("fact-correct-password", result.output)
        self.assertIn("fact-token-analysis", result.output)
        self.assertIn("fact-secret-discovery", result.output)
        self.assertNotIn("nss826312", result.output)
        self.assertNotIn("nss826312", persisted)
        self.assertNotIn("nss826312", "".join(chunk.text for chunk in chunks))
        self.assertNotIn("protocol_output", result.to_dict())
        self.assertNotIn("nss826312", repr(result))
        self.assertIn("nss826312", dataclasses.asdict(result)["protocol_output"])


if __name__ == "__main__":
    unittest.main()
