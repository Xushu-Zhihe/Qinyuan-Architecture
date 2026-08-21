"""Skill-driven dispatcher loop over declared Manifest operations."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Sequence

from automation.answers import AnswerMappingError, build_answer_variables
from automation.commands import DispatcherCommand, DispatcherEvent
from automation.decision import DispatcherDecision
from automation.http import (
    ManifestOperationExecutor,
    OperationExecutionError,
    OperationResult,
)
from automation.llm import LLMClient, LLMClientError
from automation.manifest import Manifest
from automation.secrets import SecretRedactor
from automation.skills import DISPATCHER_SKILL, SkillLoader


class DispatcherError(RuntimeError):
    """Raised when the dispatcher cannot complete a planning turn."""


EventSink = Callable[[DispatcherEvent], Awaitable[None] | None]


@dataclass(slots=True)
class DispatcherRunResult:
    """Runtime-only dispatcher result; callers must persist only redacted events."""

    final_message: str
    decision: DispatcherDecision | None = None
    commands: list[DispatcherCommand] = field(default_factory=list)
    operation_results: list[OperationResult] = field(default_factory=list)
    events: list[DispatcherEvent] = field(default_factory=list)


class ManifestDispatcher:
    """Let a dispatcher LLM plan declared operations without platform-specific code."""

    def __init__(
        self,
        client: LLMClient,
        manifest: Manifest,
        executor: ManifestOperationExecutor,
        *,
        skill_loader: SkillLoader | None = None,
        event_sink: EventSink | None = None,
        download_root: Path | None = None,
    ) -> None:
        self.client = client
        self.manifest = manifest
        self.executor = executor
        self.skill_loader = skill_loader or SkillLoader()
        self.event_sink = event_sink
        self.download_root = download_root
        self.redactor = executor.redactor
        self._workgroup_result: object | None = None

    async def run(
        self,
        *,
        workgroup_result: object | None = None,
        platform_state: object | None = None,
        human_instruction: str | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> DispatcherRunResult:
        self._workgroup_result = workgroup_result
        skill = self.skill_loader.load(DISPATCHER_SKILL)
        context = {
            "manifest": self.manifest.model_dump(mode="json"),
            "workgroup_result": self.redactor.value(workgroup_result),
            "platform_state": self.redactor.value(platform_state),
            "human_instruction": self.redactor.text(human_instruction or ""),
        }
        messages: list[dict[str, object]] = [
            {"role": "system", "content": skill},
            {
                "role": "user",
                "content": json.dumps(context, ensure_ascii=False),
            },
        ]
        tools = _dispatcher_tools()
        commands: list[DispatcherCommand] = []
        operation_results: list[OperationResult] = []
        events: list[DispatcherEvent] = []
        while True:
            try:
                response = await self.client.complete(
                    messages,
                    tools=tools,
                    cancel_event=cancel_event,
                )
            except (LLMClientError, asyncio.CancelledError):
                raise
            except Exception as exc:
                raise DispatcherError("dispatcher LLM request failed") from exc
            message = _first_message(response)
            messages.append(message)
            tool_calls = message.get("tool_calls") or []
            if not tool_calls:
                final_message = _message_text(message.get("content"))
                if not final_message:
                    raise DispatcherError("dispatcher returned no final message")
                decision = _parse_decision(final_message)
                _validate_final_decision(
                    decision,
                    operation_results,
                    self.manifest,
                )
                return DispatcherRunResult(
                    final_message=final_message,
                    decision=decision,
                    commands=commands,
                    operation_results=operation_results,
                    events=events,
                )
            if not isinstance(tool_calls, list):
                raise DispatcherError("dispatcher tool_calls must be a list")
            for call in tool_calls:
                tool_id, name, arguments = _tool_call(call)
                if name == "execute_operation":
                    result = await self._execute_operation(
                        arguments,
                        commands,
                        operation_results,
                        events,
                    )
                elif name == "emit_control_command":
                    result = await self._emit_control_command(
                        arguments,
                        commands,
                        operation_results,
                        events,
                        human_instruction,
                    )
                else:
                    raise DispatcherError(
                        f"dispatcher requested unavailable tool: {name}"
                    )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_id,
                        "content": json.dumps(result, ensure_ascii=False),
                    }
                )

    async def _execute_operation(
        self,
        arguments: Mapping[str, object],
        commands: list[DispatcherCommand],
        operation_results: list[OperationResult],
        events: list[DispatcherEvent],
    ) -> dict[str, object]:
        operation_id = str(arguments.get("operation_id", "")).strip()
        if not operation_id:
            raise DispatcherError("execute_operation requires operation_id")
        variables = arguments.get("variables", {})
        if not isinstance(variables, Mapping):
            raise DispatcherError("execute_operation variables must be an object")
        operation = next(
            (item for item in self.manifest.operations if item.id == operation_id),
            None,
        )
        if operation is None:
            raise DispatcherError(
                f"operation is not declared in manifest: {operation_id}"
            )
        if operation.kind == "submit" and self._workgroup_result is None:
            raise DispatcherError(
                "submission requires a current workgroup result"
            )
        resolved_variables = dict(variables)
        if operation.kind == "submit":
            try:
                resolved_variables = build_answer_variables(
                    operation.answer_mapping,
                    self._workgroup_result,
                    resolved_variables,
                )
            except AnswerMappingError as exc:
                await self._emit_event(
                    DispatcherEvent(
                        event_type="operation",
                        operation_id=operation_id,
                        status="failed",
                        detail={"message": self.redactor.text(str(exc))},
                    ),
                    events,
                )
                raise DispatcherError(str(exc)) from exc
        download_to = self._download_path(
            operation_id,
            arguments.get("download_filename"),
        )
        command = DispatcherCommand(
            source="llm",
            command_type="operation",
            operation_id=operation_id,
            variables=resolved_variables,
            reason=str(arguments.get("reason", "")),
        )
        command.validate_against(self.manifest)
        commands.append(command)
        await self._emit_event(
            DispatcherEvent(
                event_type="command",
                command_id=command.command_id,
                operation_id=operation_id,
                status="planned",
                detail={"source": "llm", "reason": command.reason},
            ),
            events,
        )
        try:
            result = await self.executor.execute(
                operation_id,
                variables=resolved_variables,
                download_to=download_to,
            )
        except (OperationExecutionError, OSError) as exc:
            await self._emit_event(
                DispatcherEvent(
                    event_type="operation",
                    command_id=command.command_id,
                    operation_id=operation_id,
                    status="failed",
                    detail={"message": self.redactor.text(str(exc))},
                ),
                events,
            )
            await self._emit_event(
                DispatcherEvent(
                    event_type="command",
                    command_id=command.command_id,
                    operation_id=operation_id,
                    status="failed",
                    detail={"command_type": "operation"},
                ),
                events,
            )
            return {
                "status": "failed",
                "operation_id": operation_id,
                "message": str(exc),
            }
        operation_results.append(result)
        await self._emit_event(
            DispatcherEvent(
                event_type="operation",
                command_id=command.command_id,
                operation_id=operation_id,
                status=(
                    "succeeded"
                    if result.success is True
                    else "failed"
                    if result.success is False
                    else "unknown"
                ),
                detail=result.summary(self.redactor),
            ),
            events,
        )
        await self._emit_event(
            DispatcherEvent(
                event_type="command",
                command_id=command.command_id,
                operation_id=operation_id,
                status="succeeded",
                detail={
                    "command_type": "operation",
                    "platform_success": result.success,
                },
            ),
            events,
        )
        return {
            "status": "completed",
            "operation_id": operation_id,
            "result": self._result_for_llm(result),
        }

    async def _emit_control_command(
        self,
        arguments: Mapping[str, object],
        commands: list[DispatcherCommand],
        operation_results: list[OperationResult],
        events: list[DispatcherEvent],
        human_instruction: str | None,
    ) -> dict[str, object]:
        command_type = str(arguments.get("command_type", "")).strip()
        if command_type not in {
            "skip_current",
            "abandon_current",
            "stop_after_current",
            "stop_run",
            "retry_last",
            "refresh_state",
        }:
            raise DispatcherError(f"unsupported control command: {command_type}")
        operation_id_value = arguments.get("operation_id")
        operation_id = (
            str(operation_id_value).strip()
            if operation_id_value is not None
            else None
        )
        if operation_id is not None:
            operation = next(
                (item for item in self.manifest.operations if item.id == operation_id),
                None,
            )
            if operation is None:
                raise DispatcherError(
                    f"operation is not declared in manifest: {operation_id}"
                )
            expected_kinds = {
                "skip_current": {"skip", "next"},
                "abandon_current": {"abandon"},
            }
            if (
                command_type in expected_kinds
                and operation.kind not in expected_kinds[command_type]
            ):
                raise DispatcherError(
                    f"{command_type} cannot execute operation kind {operation.kind}"
                )
        command = DispatcherCommand(
            source="human" if human_instruction else "llm",
            command_type=command_type,
            operation_id=operation_id,
            reason=str(arguments.get("reason", "")),
            original_instruction=human_instruction,
        )
        command.validate_against(self.manifest)
        commands.append(command)
        await self._emit_event(
            DispatcherEvent(
                event_type="command",
                command_id=command.command_id,
                status="planned",
                detail={
                    "command_type": command.command_type,
                    "reason": command.reason,
                    "original_instruction": self.redactor.text(
                        human_instruction or ""
                    ),
                },
            ),
            events,
        )
        operation_result: dict[str, object] | None = None
        if operation_id is not None:
            operation_result = await self._execute_operation(
                arguments,
                commands,
                operation_results,
                events,
            )
        operation_completed = (
            operation_result is None
            or operation_result.get("status") == "completed"
        )
        await self._emit_event(
            DispatcherEvent(
                event_type="command",
                command_id=command.command_id,
                operation_id=operation_id,
                status="succeeded" if operation_completed else "failed",
                detail={
                    "command_type": command.command_type,
                    "operation_status": (
                        operation_result.get("status")
                        if operation_result
                        else None
                    ),
                },
            ),
            events,
        )
        return {
            "status": "command_recorded",
            "command_id": command.command_id,
            "command_type": command.command_type,
            "operation": operation_result,
        }

    async def _emit_event(
        self,
        event: DispatcherEvent,
        events: list[DispatcherEvent],
    ) -> None:
        events.append(event)
        if self.event_sink is not None:
            result = self.event_sink(event)
            if result is not None:
                await result

    def _download_path(self, operation_id: str, filename: object) -> Path | None:
        if self.download_root is None or filename is None:
            return None
        safe_name = Path(str(filename)).name
        if not safe_name or safe_name in {".", ".."}:
            raise DispatcherError("download_filename must contain a file name")
        return self.download_root / operation_id / safe_name

    def _result_for_llm(self, result: OperationResult) -> dict[str, object]:
        payload = result.payload
        if isinstance(payload, (bytes, bytearray)):
            payload_for_llm: object = {
                "type": "bytes",
                "length": len(payload),
                "downloaded_path": str(result.downloaded_path)
                if result.downloaded_path
                else None,
            }
        else:
            payload_for_llm = self.redactor.value(payload)
        return {
            "operation_id": result.operation_id,
            "status_code": result.status_code,
            "success": result.success,
            "message": self.redactor.text(result.message),
            "payload": payload_for_llm,
        }


def _dispatcher_tools() -> tuple[dict[str, object], ...]:
    return (
        {
            "type": "function",
            "function": {
                "name": "execute_operation",
                "description": "Execute one operation declared in the active Manifest.",
                "parameters": {
                    "type": "object",
                    "required": ["operation_id"],
                    "properties": {
                        "operation_id": {"type": "string"},
                        "variables": {"type": "object"},
                        "download_filename": {"type": ["string", "null"]},
                        "reason": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "emit_control_command",
                "description": (
                    "Record an explicit human or dispatcher control command."
                ),
                "parameters": {
                    "type": "object",
                    "required": ["command_type"],
                    "properties": {
                        "command_type": {
                            "type": "string",
                            "enum": [
                                "skip_current",
                                "abandon_current",
                                "stop_after_current",
                                "stop_run",
                                "retry_last",
                                "refresh_state",
                            ],
                        },
                        "operation_id": {"type": ["string", "null"]},
                        "variables": {"type": "object"},
                        "reason": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
            },
        },
    )


def _first_message(response: Mapping[str, object]) -> dict[str, object]:
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        raise DispatcherError("dispatcher response has no choices")
    choice = choices[0]
    if not isinstance(choice, Mapping):
        raise DispatcherError("dispatcher response choice is invalid")
    message = choice.get("message")
    if not isinstance(message, Mapping):
        raise DispatcherError("dispatcher response message is invalid")
    return dict(message)


def _tool_call(call: object) -> tuple[str, str, Mapping[str, object]]:
    if not isinstance(call, Mapping):
        raise DispatcherError("dispatcher tool call is invalid")
    call_id = str(call.get("id", "")).strip()
    function = call.get("function")
    if not call_id or not isinstance(function, Mapping):
        raise DispatcherError("dispatcher tool call is missing function or ID")
    name = str(function.get("name", "")).strip()
    raw_arguments = function.get("arguments", "{}")
    try:
        arguments = json.loads(raw_arguments)
    except (TypeError, ValueError) as exc:
        raise DispatcherError("dispatcher tool arguments are not valid JSON") from exc
    if not isinstance(arguments, Mapping):
        raise DispatcherError("dispatcher tool arguments must be an object")
    return call_id, name, arguments


def _message_text(content: object) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [
            item["text"]
            for item in content
            if isinstance(item, Mapping) and isinstance(item.get("text"), str)
        ]
        return "\n".join(parts).strip()
    return ""


def _parse_decision(content: str) -> DispatcherDecision | None:
    candidate = content.strip()
    if candidate.startswith("```") and candidate.endswith("```"):
        candidate = candidate.split("\n", 1)[1].rsplit("\n", 1)[0].strip()
    try:
        payload = json.loads(candidate)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, Mapping):
        return None
    try:
        return DispatcherDecision.model_validate(payload)
    except ValueError:
        return None


def _validate_final_decision(
    decision: DispatcherDecision | None,
    operation_results: Sequence[OperationResult],
    manifest: Manifest,
) -> None:
    """Prevent an LLM from claiming an unconfirmed platform submission."""

    if decision is None or decision.status != "submitted":
        return
    operation_kinds = {
        operation.id: operation.kind for operation in manifest.operations
    }
    if any(
        operation_kinds.get(result.operation_id) == "submit"
        and result.success is True
        for result in operation_results
    ):
        return
    raise DispatcherError(
        "dispatcher cannot report submitted before an explicit platform "
        "success condition matches"
    )
