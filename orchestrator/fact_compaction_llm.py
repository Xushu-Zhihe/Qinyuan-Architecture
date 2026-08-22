import asyncio
import json
import time
from pathlib import Path

import httpx

from worker.models import FactCompactionLLMConfig, WorkerResult


def _message_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "".join(parts)
    return ""


async def execute_fact_compaction_llm(
    config: FactCompactionLLMConfig,
    request: str,
    work_dir: Path,
    *,
    cancel_event: asyncio.Event | None = None,
) -> WorkerResult:
    started = time.monotonic()
    work_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = work_dir / "stdout.txt"
    stderr_path = work_dir / "stderr.txt"
    if cancel_event is not None and cancel_event.is_set():
        raise asyncio.CancelledError
    payload = {
        "model": config.model,
        "messages": [
            {
                "role": "user",
                "content": request,
            }
        ],
        "temperature": 0,
    }
    headers = {
        "Authorization": f"Bearer {config.api_key}",
        "Content-Type": "application/json",
    }
    output = ""
    error = ""
    status = "failed"
    exit_code: int | None = None
    try:
        async with httpx.AsyncClient(timeout=config.timeout_seconds) as client:
            response = await client.post(
                config.chat_completions_url,
                headers=headers,
                json=payload,
            )
        if response.status_code >= 400:
            error = f"LLM HTTP {response.status_code}"
            exit_code = response.status_code
        else:
            body = response.json()
            if not isinstance(body, dict):
                raise ValueError("response body is not an object")
            choices = body.get("choices")
            if not isinstance(choices, list) or not choices:
                raise ValueError("response choices are missing")
            first_choice = choices[0]
            if not isinstance(first_choice, dict):
                raise ValueError("response choice is invalid")
            message = first_choice.get("message")
            if not isinstance(message, dict):
                raise ValueError("response message is missing")
            output = _message_text(message.get("content"))
            if not output.strip():
                error = "LLM response contained no message content"
            else:
                status = "success"
                exit_code = 0
    except asyncio.CancelledError:
        raise
    except (httpx.HTTPError, json.JSONDecodeError, IndexError, KeyError, TypeError):
        error = "LLM request failed"
    stdout_path.write_text(output, encoding="utf-8")
    stderr_path.write_text(error, encoding="utf-8")
    return WorkerResult(
        provider="summary-llm",
        priority=-100,
        status=status,
        exit_code=exit_code,
        duration_seconds=time.monotonic() - started,
        output=output,
        stdout_path=str(stdout_path),
        stderr_path=str(stderr_path),
        work_dir=str(work_dir),
        model=config.model,
        token_usage="unknown",
        execution_backend="direct-http",
        isolation="none",
    )
