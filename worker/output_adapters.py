from __future__ import annotations

import json
from abc import ABC, abstractmethod


STREAM_FORMATS = frozenset({"text", "claude-stream-json"})


class OutputAdapter(ABC):
    @abstractmethod
    def feed(self, text: str) -> tuple[str, ...]:
        raise NotImplementedError

    @abstractmethod
    def finish(self) -> tuple[str, ...]:
        raise NotImplementedError


class TextOutputAdapter(OutputAdapter):
    def feed(self, text: str) -> tuple[str, ...]:
        return (text,) if text else ()

    def finish(self) -> tuple[str, ...]:
        return ()


class ClaudeStreamJsonAdapter(OutputAdapter):
    def __init__(self) -> None:
        self._pending = ""
        self._saw_partial_text = False
        self._emitted_assistant_text = False

    @staticmethod
    def _message_text(record: dict[str, object]) -> str:
        message = record.get("message")
        if not isinstance(message, dict):
            return ""
        content = message.get("content")
        if not isinstance(content, list):
            return ""
        parts: list[str] = []
        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type") == "text"
                and isinstance(block.get("text"), str)
            ):
                parts.append(str(block["text"]))
        return "".join(parts)

    def _decode_line(self, line: str) -> tuple[str, ...]:
        if not line.strip():
            return ()
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            return (f"[claude stream parse error] {line}\n",)
        if not isinstance(record, dict):
            return ()

        record_type = record.get("type")
        if record_type == "stream_event":
            event = record.get("event")
            if not isinstance(event, dict):
                return ()
            if event.get("type") == "content_block_delta":
                delta = event.get("delta")
                if (
                    isinstance(delta, dict)
                    and delta.get("type") == "text_delta"
                    and isinstance(delta.get("text"), str)
                ):
                    self._saw_partial_text = True
                    return (str(delta["text"]),)
            if event.get("type") == "content_block_start":
                block = event.get("content_block")
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    name = block.get("name")
                    if isinstance(name, str) and name:
                        return (f"\n[tool] {name}\n",)
            return ()

        if record_type == "assistant" and not self._saw_partial_text:
            text = self._message_text(record)
            if text:
                self._emitted_assistant_text = True
                return (text,)
            return ()

        if record_type == "result":
            result = record.get("result")
            if (
                not self._saw_partial_text
                and not self._emitted_assistant_text
                and isinstance(result, str)
                and result
            ):
                return (result,)
            if record.get("is_error"):
                error = record.get("error") or record.get("subtype") or "unknown error"
                return (f"\n[claude error] {error}\n",)
        return ()

    def feed(self, text: str) -> tuple[str, ...]:
        self._pending += text
        lines = self._pending.splitlines(keepends=True)
        if lines and not lines[-1].endswith(("\n", "\r")):
            self._pending = lines.pop()
        else:
            self._pending = ""
        output: list[str] = []
        for line in lines:
            output.extend(self._decode_line(line.rstrip("\r\n")))
        return tuple(output)

    def finish(self) -> tuple[str, ...]:
        if not self._pending:
            return ()
        pending = self._pending
        self._pending = ""
        return self._decode_line(pending)


def build_output_adapter(stream_format: str) -> OutputAdapter:
    if stream_format == "claude-stream-json":
        return ClaudeStreamJsonAdapter()
    return TextOutputAdapter()
