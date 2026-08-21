"""Small isolated Chat Completions client shared by parser and dispatcher."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import re
from typing import Mapping, Sequence
from urllib.parse import urlsplit
from uuid import uuid4

import httpx


class LLMClientError(RuntimeError):
    """Raised for transport or response-contract failures from an LLM."""


@dataclass(frozen=True, slots=True, repr=False)
class LLMConfig:
    base_url: str
    model: str
    timeout_seconds: float = 300.0
    max_transport_retries: int = 2
    temperature: float | None = None

    def __post_init__(self) -> None:
        base_url = self.base_url.strip().rstrip("/")
        if base_url.endswith("/chat/completions"):
            base_url = base_url[: -len("/chat/completions")]
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("LLM base_url must be an absolute HTTP URL")
        model = self.model.strip()
        if (
            not model
            or len(model) > 200
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}", model) is None
        ):
            raise ValueError(
                "LLM model must be 1-200 characters using safe model characters"
            )
        if not 0 < self.timeout_seconds <= 3600:
            raise ValueError("LLM timeout_seconds must be between 0 and 3600")
        if not 0 <= self.max_transport_retries <= 10:
            raise ValueError("LLM max_transport_retries must be between 0 and 10")
        if self.temperature is not None and not 0 <= self.temperature <= 2:
            raise ValueError("LLM temperature must be between 0 and 2")
        object.__setattr__(self, "base_url", base_url)
        object.__setattr__(self, "model", model)

    @property
    def chat_completions_url(self) -> str:
        return f"{self.base_url}/chat/completions"

    def __repr__(self) -> str:
        return (
            "LLMConfig("
            f"base_url={self.base_url!r}, model={self.model!r}, "
            f"timeout_seconds={self.timeout_seconds!r})"
        )


class RuntimeLLMSecret:
    """Mutable runtime-only API key that intentionally redacts itself."""

    __slots__ = ("api_key",)

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    def __repr__(self) -> str:
        return "RuntimeLLMSecret(api_key='[redacted]')"

    __str__ = __repr__

    def clear(self) -> None:
        self.api_key = ""


class LLMClient:
    """Execute one LLM request at a time without persisting prompts or tokens."""

    def __init__(
        self,
        config: LLMConfig,
        secret: RuntimeLLMSecret,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config
        self.secret = secret
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(timeout=config.timeout_seconds)

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def complete(
        self,
        messages: Sequence[Mapping[str, object]],
        *,
        tools: Sequence[Mapping[str, object]] = (),
        cancel_event: asyncio.Event | None = None,
    ) -> dict[str, object]:
        request_id = f"automation-llm-{uuid4().hex}"
        payload: dict[str, object] = {
            "model": self.config.model,
            "messages": [dict(message) for message in messages],
        }
        if tools:
            payload["tools"] = [dict(tool) for tool in tools]
            payload["tool_choice"] = "auto"
        if self.config.temperature is not None:
            payload["temperature"] = self.config.temperature
        headers = {
            "Authorization": f"Bearer {self.secret.api_key}",
            "Content-Type": "application/json",
            "X-Request-ID": request_id,
        }
        last_error: Exception | None = None
        for attempt in range(self.config.max_transport_retries + 1):
            try:
                request = asyncio.create_task(
                    self.client.post(
                        self.config.chat_completions_url,
                        headers=headers,
                        json=payload,
                    )
                )
                response = await _wait_for_request(request, cancel_event)
                response.raise_for_status()
                data = response.json()
                if not isinstance(data, dict):
                    raise LLMClientError("LLM response must be an object")
                return data
            except asyncio.CancelledError:
                raise
            except (httpx.HTTPError, ValueError, LLMClientError) as exc:
                last_error = exc
                if (
                    not _is_retryable(exc)
                    or attempt >= self.config.max_transport_retries
                ):
                    break
                await asyncio.sleep(0.25 * (2**attempt))
        if isinstance(last_error, httpx.HTTPStatusError):
            raise LLMClientError(
                f"LLM request failed ({request_id}): "
                f"HTTP {last_error.response.status_code}"
            ) from last_error
        raise LLMClientError(
            f"LLM request failed ({request_id}): "
            f"{type(last_error).__name__ if last_error else 'unknown error'}"
        ) from last_error

    async def complete_json(
        self,
        messages: Sequence[Mapping[str, object]],
        *,
        cancel_event: asyncio.Event | None = None,
    ) -> dict[str, object]:
        data = await self.complete(messages, cancel_event=cancel_event)
        content = _message_content(data)
        try:
            parsed = json.loads(content)
        except (TypeError, ValueError) as exc:
            raise LLMClientError("LLM response content must be valid JSON") from exc
        if not isinstance(parsed, dict):
            raise LLMClientError("LLM JSON response must be an object")
        return parsed


async def _wait_for_request(
    request: asyncio.Task[httpx.Response],
    cancel_event: asyncio.Event | None,
) -> httpx.Response:
    if cancel_event is None:
        return await request
    cancel_task = asyncio.create_task(cancel_event.wait())
    done, _ = await asyncio.wait(
        {request, cancel_task}, return_when=asyncio.FIRST_COMPLETED
    )
    if cancel_task in done and cancel_event.is_set():
        request.cancel()
        await asyncio.gather(request, return_exceptions=True)
        raise asyncio.CancelledError
    cancel_task.cancel()
    await asyncio.gather(cancel_task, return_exceptions=True)
    return await request


def _is_retryable(error: Exception) -> bool:
    if isinstance(error, httpx.HTTPStatusError):
        return error.response.status_code == 429 or error.response.status_code >= 500
    return isinstance(error, (httpx.TransportError, httpx.TimeoutException, ValueError))


def _message_content(response: Mapping[str, object]) -> str:
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LLMClientError("LLM response has no choices")
    first = choices[0]
    if not isinstance(first, Mapping):
        raise LLMClientError("LLM response choice is invalid")
    message = first.get("message")
    if not isinstance(message, Mapping):
        raise LLMClientError("LLM response message is invalid")
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, Mapping) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts).strip()
    raise LLMClientError("LLM response content is missing")
