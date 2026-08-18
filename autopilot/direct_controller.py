from __future__ import annotations

import asyncio
import json
from typing import Awaitable, Callable, Mapping

import httpx

from autopilot.controller_tools import PHASE_TOOLS
from autopilot.models import AutoTask, ControllerConfig, RuntimeControllerSecret
from autopilot.platform_models import PlatformResult
from autopilot.solver_result import SubmissionPlan


class DirectControllerError(RuntimeError):
    pass


SYSTEM_PROMPT = """You are a platform orchestration controller, not a solver.
Handle exactly one task and only the requested phase, preserving its lease and
never fetching multiple tasks. Use only the tools made available in this call.
Never infer platform acceptance: report accepted only after the submission tool
returns an explicit business-success status, and never submit an external ID
that is already accepted. A rejected result is not retryable; technical
retries use retryable_error. Keep tokens, keys, cookies, and Authorization
values out of actions, task fields, messages, and errors. Attachments may only
be written to the backend-provided staging directory. Distinguish temporary
empty queues, finished queues, business rejection, and transport failure.
Return one strict JSON object without Markdown."""

_ACTION_FIELDS = {
    "task": {"protocol_version", "action", "task", "message"},
    "wait": {"protocol_version", "action", "message", "retry_after_seconds"},
    "finished": {"protocol_version", "action", "message"},
    "accepted": {"protocol_version", "action", "message"},
    "rejected": {"protocol_version", "action", "message"},
    "retryable_error": {"protocol_version", "action", "message", "retry_after_seconds"},
    "fatal_error": {"protocol_version", "action", "message"},
    "skipped": {"protocol_version", "action", "message"},
    "skip_failed": {"protocol_version", "action", "message"},
}
_OUTCOME_TOOL_STATUS = {
    "accepted": ("accepted",),
    "rejected": ("rejected",),
    "skipped": ("skipped",),
}


class DirectLLMController:
    def __init__(self, config: ControllerConfig, secret: RuntimeControllerSecret, *, client: httpx.AsyncClient | None = None) -> None:
        self.config = config
        self.secret = secret
        self.client = client or httpx.AsyncClient(timeout=config.timeout_seconds)
        self._owns_client = client is None

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def preflight(self) -> dict[str, object]:
        readiness_tool = {
            "type": "function",
            "function": {
                "name": "controller_readiness",
                "description": "Confirm tool-call support.",
                "parameters": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {},
                },
            },
        }
        payload = await self._completion(
            [
                {"role": "system", "content": "Call controller_readiness exactly once."},
                {"role": "user", "content": "Check tool-call support."},
            ],
            (readiness_tool,),
        )
        try:
            message = payload["choices"][0]["message"]
            calls = message["tool_calls"]
            if not isinstance(calls, list) or not calls:
                raise TypeError
            if calls[0].get("function", {}).get("name") != "controller_readiness":
                raise TypeError
        except (KeyError, IndexError, TypeError):
            raise DirectControllerError("controller API does not support required tools/tool_calls")
        return {"status": "valid", "model": payload.get("model", self.config.model)}

    async def validate_prompt(self, prompt: str) -> dict[str, object]:
        """Assess a platform contract without exposing any platform tools."""
        if not isinstance(prompt, str) or not prompt.strip():
            raise DirectControllerError("platform prompt is required")
        response = await self._completion(
            [
                {
                    "role": "system",
                    "content": (
                        "Assess the supplied platform protocol description offline. "
                        "Do not call tools or access the network. Return strict JSON "
                        "with status (valid|warning|invalid), missing (string array), "
                        "and warnings (string array). Check fetch, submit, outcome, "
                        "and skip/release instructions."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            (),
        )
        try:
            message = response["choices"][0]["message"]
            result = json.loads(self._message_text(message.get("content")))
            if not isinstance(result, dict):
                raise ValueError("result must be an object")
            if result.get("status") not in {"valid", "warning", "invalid"}:
                raise ValueError("invalid status")
            for field in ("missing", "warnings"):
                if not isinstance(result.get(field), list) or not all(isinstance(item, str) for item in result[field]):
                    raise ValueError(f"{field} must be a string array")
            if set(result) - {"status", "missing", "warnings"}:
                raise ValueError("unknown completeness fields")
            return result
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise DirectControllerError(f"invalid prompt completeness response: {exc}") from exc

    async def prompt_phase(self, phase: str, instruction: str, handler: Callable[[str, Mapping[str, object]], Awaitable[object]]) -> PlatformResult:
        content, tool_results = await self._tool_loop(phase, instruction, handler)
        try:
            payload = json.loads(content)
            if not isinstance(payload, dict) or payload.get("protocol_version") != 2:
                raise ValueError("controller action must use protocol_version 2")
            action = payload.get("action")
            if not isinstance(action, str) or action not in _ACTION_FIELDS:
                raise ValueError("unsupported controller action")
            unknown = set(payload) - _ACTION_FIELDS[action]
            if unknown:
                raise ValueError(f"unknown action fields: {sorted(unknown)}")
            if action == "task":
                raw = payload.get("task")
                if not isinstance(raw, dict):
                    raise ValueError("task action requires task")
                task_unknown = set(raw) - {"external_id", "name", "goal", "origin", "hints", "attachment_paths"}
                if task_unknown:
                    raise ValueError(f"unknown task fields: {sorted(task_unknown)}")
                hints = raw.get("hints", [])
                attachments = raw.get("attachment_paths", [])
                if not isinstance(hints, list) or not all(isinstance(item, str) for item in hints):
                    raise ValueError("task hints must be a string array")
                if not isinstance(attachments, list) or not all(isinstance(item, str) for item in attachments):
                    raise ValueError("task attachment_paths must be a string array")
                task = AutoTask(
                    external_id=str(raw.get("external_id", "")).strip(),
                    name=str(raw.get("name", "")).strip(),
                    goal=str(raw.get("goal", "")).strip(),
                    origin=str(raw.get("origin", "")).strip(),
                    hints=tuple(hints),
                    raw_metadata={"attachment_paths": tuple(attachments)},
                )
                if not task.external_id or not task.name or not task.goal or not task.origin:
                    raise ValueError("task fields must be non-empty")
                return PlatformResult("task", task=task, message=payload.get("message"))
            if phase == "submit" and action in {"accepted", "rejected", "retryable_error", "fatal_error"} and "submit_platform_result" not in tool_results:
                raise ValueError("submission outcome requires submit_platform_result tool call")
            if action in {"skipped", "skip_failed"} and "skip_platform_task" not in tool_results:
                raise ValueError("skip outcome requires skip_platform_task tool call")
            if action in _OUTCOME_TOOL_STATUS:
                statuses = [item.get("status") for item in tool_results.get("submit_platform_result", [])]
                if action == "skipped":
                    statuses = [item.get("status") for item in tool_results.get("skip_platform_task", [])]
                if not any(status in _OUTCOME_TOOL_STATUS[action] for status in statuses if isinstance(status, str)):
                    raise ValueError(f"{action} requires explicit successful tool result")
            if action == "rejected" and "retry_after_seconds" in payload:
                raise ValueError("rejected action cannot be retryable")
            delay = payload.get("retry_after_seconds")
            if delay is not None:
                if isinstance(delay, bool):
                    raise ValueError("retry_after_seconds must be numeric")
                delay = float(delay)
                if not 0.1 <= delay <= 300:
                    raise ValueError("retry_after_seconds must be between 0.1 and 300")
            return PlatformResult(action, message=payload.get("message"), retry_after_seconds=delay)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise DirectControllerError(f"invalid controller action: {exc}") from exc

    async def plan_submission(self, task: AutoTask, goal_result, *, rejection: str | None = None) -> SubmissionPlan:
        instruction = {
            "phase": "plan_submission",
            "task": {"external_id": task.external_id, "name": task.name},
            "goal_result": {
                "result_ref": goal_result.result_ref,
                "fact": {"id": goal_result.fact.id, "content": goal_result.fact.content},
                "supporting_facts": [{"id": fact.id, "content": fact.content} for fact in goal_result.supporting_facts],
            },
            "previous_rejection": rejection,
            "required_output": {
                "result_ref": "string", "source_fact_ids": ["string"],
                "content_type": "text|json|form", "text": None,
                "json_value": None, "named_values": {}, "form_fields": [],
            },
        }
        content, _called_tools = await self._tool_loop("plan", json.dumps(instruction, ensure_ascii=False), self._no_tools)
        try:
            plan = SubmissionPlan.from_dict(json.loads(content))
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise DirectControllerError(f"invalid submission plan: {exc}") from exc
        if plan.result_ref != goal_result.result_ref:
            raise DirectControllerError("submission plan references a stale Goal result")
        allowed = {goal_result.fact.id, *(fact.id for fact in goal_result.supporting_facts)}
        if not set(plan.source_fact_ids).issubset(allowed):
            raise DirectControllerError("submission plan references unavailable Facts")
        return plan

    @staticmethod
    async def _no_tools(_name: str, _arguments: Mapping[str, object]) -> object:
        raise DirectControllerError("tools are unavailable in this phase")

    async def _tool_loop(self, phase: str, instruction: str, handler) -> tuple[str, dict[str, list[dict[str, object]]]]:
        if phase not in PHASE_TOOLS:
            raise DirectControllerError("unknown controller phase")
        messages: list[dict[str, object]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": instruction},
        ]
        tools = PHASE_TOOLS[phase]
        allowed_tools = {
            item.get("function", {}).get("name")
            for item in tools
            if isinstance(item, dict)
        }
        tool_results: dict[str, list[dict[str, object]]] = {}
        for _ in range(self.config.max_rounds):
            response = await self._completion(messages, tools)
            choices = response.get("choices")
            if not isinstance(choices, list) or not choices:
                raise DirectControllerError("controller response has no choices")
            message = choices[0].get("message")
            if not isinstance(message, dict):
                raise DirectControllerError("controller response message is invalid")
            tool_calls = message.get("tool_calls") or []
            messages.append(message)
            if not tool_calls:
                content = self._message_text(message.get("content"))
                if not content:
                    raise DirectControllerError("controller returned no action")
                return content, tool_results
            if not tools:
                raise DirectControllerError("controller API attempted a tool call in a tool-free phase")
            for call in tool_calls:
                function = call.get("function", {})
                name = str(function.get("name", ""))
                if name not in allowed_tools:
                    raise DirectControllerError(f"controller requested unavailable tool: {name}")
                try:
                    arguments = json.loads(function.get("arguments", "{}"))
                    result = await handler(name, arguments)
                except Exception as exc:
                    result = {"status": "tool_error", "message": str(exc)}
                if not isinstance(result, Mapping):
                    result = {"status": "tool_error", "message": "tool returned non-object"}
                tool_results.setdefault(name, []).append(dict(result))
                messages.append({"role": "tool", "tool_call_id": call.get("id"), "content": json.dumps(result, ensure_ascii=False)})
        raise DirectControllerError("controller exceeded max_rounds")

    @staticmethod
    def _message_text(content: object) -> str:
        if isinstance(content, str):
            return content.strip()
        if not isinstance(content, list):
            return ""
        parts: list[str] = []
        for item in content:
            if not isinstance(item, Mapping):
                continue
            text = item.get("text", item.get("content"))
            if isinstance(text, str):
                parts.append(text)
        return "\n".join(parts).strip()

    async def _completion(self, messages, tools) -> dict[str, object]:
        headers = {"Authorization": f"Bearer {self.secret.api_key}", "Content-Type": "application/json", **dict(self.config.extra_headers)}
        payload: dict[str, object] = {"model": self.config.model, "messages": messages, **self.config.extra_body}
        if tools:
            payload["tools"] = list(tools)
            payload["tool_choice"] = "auto"
        if self.config.temperature is not None:
            payload["temperature"] = self.config.temperature
        if self.config.max_output_tokens is not None:
            payload["max_tokens"] = self.config.max_output_tokens
        if self.config.reasoning_effort is not None:
            payload["reasoning_effort"] = self.config.reasoning_effort
        last_error: Exception | None = None
        data = None
        for attempt in range(3):
            try:
                response = await self.client.post(self.config.chat_completions_url, headers=headers, json=payload)
                response.raise_for_status()
                data = response.json()
                break
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
                retryable = not isinstance(exc, httpx.HTTPStatusError) or (
                    exc.response.status_code == 429 or exc.response.status_code >= 500
                )
                if attempt < 2 and retryable:
                    await asyncio.sleep(0.25 * (2**attempt))
                elif not retryable:
                    break
        if data is None:
            assert last_error is not None
            raise DirectControllerError(
                f"controller transport failed: {type(last_error).__name__}"
            ) from last_error
        if not isinstance(data, dict):
            raise DirectControllerError("controller response must be an object")
        return data
