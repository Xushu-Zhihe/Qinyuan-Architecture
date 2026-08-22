import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator.fact_compaction_llm import execute_fact_compaction_llm
from worker.models import FactCompactionLLMConfig


class _Response:
    def __init__(self, status_code: int, payload: object) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> object:
        return self._payload


class _Client:
    def __init__(self, response: _Response) -> None:
        self.response = response
        self.calls: list[tuple[str, dict[str, str], dict[str, object]]] = []

    async def __aenter__(self) -> "_Client":
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def post(self, url: str, *, headers: dict[str, str], json: dict[str, object]) -> _Response:
        self.calls.append((url, headers, json))
        return self.response


class FactCompactionLLMTests(unittest.IsolatedAsyncioTestCase):
    async def test_posts_openai_compatible_chat_request(self) -> None:
        client = _Client(
            _Response(
                200,
                {"choices": [{"message": {"content": "{\"action\":\"skip\"}"}}]},
            )
        )
        config = FactCompactionLLMConfig(
            base_url="https://llm.example/v1",
            api_key="secret-key",
            model="compact-model",
        )
        with tempfile.TemporaryDirectory() as directory:
            with patch(
                "orchestrator.fact_compaction_llm.httpx.AsyncClient",
                return_value=client,
            ):
                result = await execute_fact_compaction_llm(
                    config,
                    "summarize these facts",
                    Path(directory),
                )

        self.assertEqual(result.status, "success")
        self.assertEqual(result.output, '{"action":"skip"}')
        self.assertEqual(len(client.calls), 1)
        url, headers, payload = client.calls[0]
        self.assertEqual(url, "https://llm.example/v1/chat/completions")
        self.assertEqual(headers["Authorization"], "Bearer secret-key")
        self.assertEqual(payload["model"], "compact-model")

    async def test_http_error_does_not_echo_api_key(self) -> None:
        client = _Client(_Response(401, {"error": "invalid key"}))
        config = FactCompactionLLMConfig(
            base_url="https://llm.example/v1",
            api_key="secret-key",
            model="compact-model",
        )
        with tempfile.TemporaryDirectory() as directory:
            with patch(
                "orchestrator.fact_compaction_llm.httpx.AsyncClient",
                return_value=client,
            ):
                result = await execute_fact_compaction_llm(
                    config,
                    "summarize these facts",
                    Path(directory),
                )

        self.assertEqual(result.status, "failed")
        self.assertNotIn("secret-key", result.output)
        self.assertNotIn("secret-key", result.stderr_path)
        self.assertEqual(result.exit_code, 401)
