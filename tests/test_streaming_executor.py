import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

from worker.executor import execute_provider
from worker.models import LogChunk, ProviderConfig


def provider_for(code: str, timeout: float = 5) -> ProviderConfig:
    return ProviderConfig(
        name="fake",
        enabled=True,
        priority=10,
        command=(sys.executable, "-u", "-c", code),
        timeout_seconds=timeout,
    )


class StreamingExecutorTests(unittest.IsolatedAsyncioTestCase):
    def make_work_dir(self) -> Path:
        temporary = tempfile.TemporaryDirectory()
        self.addAsyncCleanup(asyncio.to_thread, temporary.cleanup)
        return Path(temporary.name) / "work"

    async def test_streams_stdout_and_stderr_before_exit(self) -> None:
        chunks: list[LogChunk] = []
        first_chunk = asyncio.Event()

        def collect(chunk: LogChunk) -> None:
            chunks.append(chunk)
            first_chunk.set()

        task = asyncio.create_task(
            execute_provider(
                provider_for(
                    "import sys,time;"
                    "print('one',flush=True);"
                    "print('warn',file=sys.stderr,flush=True);"
                    "time.sleep(.2);"
                    "print('two',flush=True)"
                ),
                "request",
                None,
                self.make_work_dir(),
                on_chunk=collect,
            )
        )

        await asyncio.wait_for(first_chunk.wait(), timeout=1)
        self.assertFalse(task.done())
        result = await task

        self.assertEqual(result.status, "success")
        self.assertEqual(
            [chunk.text.strip() for chunk in chunks if chunk.stream == "stdout"],
            ["one", "two"],
        )
        self.assertEqual(
            [chunk.text.strip() for chunk in chunks if chunk.stream == "stderr"],
            ["warn"],
        )

    async def test_cancel_event_kills_process_and_preserves_partial_log(self) -> None:
        cancel = asyncio.Event()
        chunks: list[LogChunk] = []
        started = asyncio.Event()

        def collect(chunk: LogChunk) -> None:
            chunks.append(chunk)
            started.set()

        task = asyncio.create_task(
            execute_provider(
                provider_for(
                    "import time;print('started',flush=True);time.sleep(10)"
                ),
                "request",
                None,
                self.make_work_dir(),
                on_chunk=collect,
                cancel_event=cancel,
            )
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        cancel.set()

        result = await asyncio.wait_for(task, timeout=3)

        self.assertEqual(result.status, "cancelled")
        self.assertIn("started", Path(result.stdout_path).read_text(encoding="utf-8"))

    async def test_timeout_reports_timeout_and_keeps_logs(self) -> None:
        result = await execute_provider(
            provider_for(
                "import time;print('before timeout',flush=True);time.sleep(10)",
                timeout=0.1,
            ),
            "request" * 100_000,
            None,
            self.make_work_dir(),
        )

        self.assertEqual(result.status, "timeout")
        self.assertIn("before timeout", result.output)
        self.assertIn(
            "exceeded timeout",
            Path(result.stderr_path).read_text(encoding="utf-8"),
        )

    async def test_records_non_zero_exit(self) -> None:
        result = await execute_provider(
            provider_for("import sys;print('bad',file=sys.stderr);sys.exit(7)"),
            "request" * 100_000,
            None,
            self.make_work_dir(),
        )

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.exit_code, 7)


if __name__ == "__main__":
    unittest.main()
