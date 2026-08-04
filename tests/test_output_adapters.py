import json
import asyncio
import tempfile
import unittest
from pathlib import Path

from worker.executor import _read_stream
from worker.output_adapters import ClaudeStreamJsonAdapter, TextOutputAdapter


class OutputAdapterTests(unittest.TestCase):
    def test_text_adapter_preserves_plain_output(self) -> None:
        adapter = TextOutputAdapter()
        self.assertEqual(adapter.feed("plain output"), ("plain output",))
        self.assertEqual(adapter.finish(), ())

    def test_claude_adapter_streams_text_and_tool_summary(self) -> None:
        adapter = ClaudeStreamJsonAdapter()
        records = [
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "BLACKBOARD_"},
                },
            },
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_start",
                    "content_block": {"type": "tool_use", "name": "Read"},
                },
            },
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "RESULT: {}"},
                },
            },
        ]
        raw = "\n".join(json.dumps(record) for record in records) + "\n"

        output = adapter.feed(raw[:37]) + adapter.feed(raw[37:]) + adapter.finish()

        self.assertEqual("".join(output), "BLACKBOARD_\n[tool] Read\nRESULT: {}")

    def test_claude_adapter_rebuilds_final_assistant_message_without_partials(self) -> None:
        adapter = ClaudeStreamJsonAdapter()
        record = {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "BLACKBOARD_RESULT: "},
                    {"type": "text", "text": "{\"goal_achieved\":false}"},
                ]
            },
        }

        self.assertEqual(
            adapter.feed(json.dumps(record) + "\n"),
            ('BLACKBOARD_RESULT: {"goal_achieved":false}',),
        )

    def test_claude_adapter_surfaces_parse_and_result_errors(self) -> None:
        adapter = ClaudeStreamJsonAdapter()
        malformed = adapter.feed("not-json\n")
        result_error = adapter.feed(
            json.dumps(
                {
                    "type": "result",
                    "is_error": True,
                    "subtype": "rate_limit",
                }
            )
            + "\n"
        )

        self.assertIn("parse error", malformed[0])
        self.assertIn("rate_limit", result_error[0])


class StreamAuditTests(unittest.IsolatedAsyncioTestCase):
    async def test_claude_stream_keeps_raw_jsonl_separate_from_visible_output(self) -> None:
        record = {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "visible answer"}]},
        }
        raw = json.dumps(record) + "\n"
        reader = asyncio.StreamReader()
        reader.feed_data(raw.encode("utf-8"))
        reader.feed_eof()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            visible_path = root / "stdout.txt"
            raw_path = root / "stdout.raw.jsonl"

            output = await _read_stream(
                reader,
                "stdout",
                visible_path,
                None,
                1024 * 1024,
                ClaudeStreamJsonAdapter(),
                raw_path=raw_path,
            )

            self.assertEqual(output, "visible answer")
            self.assertEqual(visible_path.read_text(encoding="utf-8"), "visible answer")
            self.assertEqual(raw_path.read_text(encoding="utf-8"), raw)


if __name__ == "__main__":
    unittest.main()
