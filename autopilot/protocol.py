import json
from dataclasses import dataclass
from pathlib import Path

from autopilot.models import AutoControllerResult, AutoTask


PREFIX = "AUTOPILOT_RESULT:"
TASK_FIELDS = {"action", "external_id", "name", "goal", "origin", "hints"}
MESSAGE_FIELDS = {"action", "message"}


@dataclass(frozen=True, slots=True)
class ControllerParseResult:
    status: str
    result: AutoControllerResult | None
    error: str | None


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value.strip()


def _require_fields(payload: dict[str, object], expected: set[str]) -> None:
    extra = set(payload) - expected
    missing = expected - set(payload)
    if extra:
        raise ValueError(f"unexpected fields: {sorted(extra)}")
    if missing:
        raise ValueError(f"missing fields: {sorted(missing)}")


def render_controller_prompt(
    platform_prompt: str,
    action: str,
    *,
    completed_ids: tuple[str, ...] = (),
    staging_relative_path: str = "challenge",
    task: AutoTask | None = None,
    answer: str | None = None,
) -> str:
    completed = json.dumps(list(completed_ids), ensure_ascii=False)
    header = (
        "You are the API controller for an automated challenge queue.\n"
        "The user-supplied platform prompt below contains the API contract and "
        "credential. Follow it to call the platform using your available tools. "
        "Never repeat, log, or include the credential in your result.\n"
        "You are not the solving agent. Do not solve a challenge yourself.\n"
        "Finish with exactly one AUTOPILOT_RESULT: line containing strict JSON.\n\n"
        "<platform-prompt>\n"
        + platform_prompt
        + "\n</platform-prompt>\n\n"
    )
    if action == "next":
        return (
            header
            + "Fetch the next unsolved challenge. Do not return an ID already "
            "listed in completed_ids. If the queue is exhausted, return action "
            "finished. If the platform is temporarily empty, return action wait. "
            "When attachments are needed, download or extract them only under "
            f"the relative directory {staging_relative_path!r}.\n"
            "Return task fields action, external_id, name, goal, origin, hints; "
            "or message fields action, message. Keep task text free of credentials.\n"
            f"completed_ids={completed}\n"
            f"AUTOPILOT_RESULT: {{\"action\":\"task\",\"external_id\":\"id\","
            "\"name\":\"name\",\"goal\":\"goal\",\"origin\":\"origin\","
            "\"hints\":[]}}\n"
        )
    if action == "submit":
        if task is None or answer is None:
            raise ValueError("submit requires task and answer")
        return (
            header
            + "Submit the supplied answer for this exact challenge through the "
            "platform API. Do not request another challenge in this call. Return "
            "action accepted when the platform confirms correctness, or rejected "
            "when it does not. Use message fields only and never include tokens.\n"
            f"external_id={task.external_id}\n"
            f"answer={answer}\n"
            "AUTOPILOT_RESULT: {\"action\":\"accepted\",\"message\":\"ok\"}\n"
        )
    raise ValueError(f"unsupported controller action: {action}")


def parse_controller_result(output: str) -> ControllerParseResult:
    candidates = [
        line[len(PREFIX) :].strip()
        for line in output.splitlines()
        if line.startswith(PREFIX)
    ]
    if not candidates:
        return ControllerParseResult("protocol_error", None, "missing AUTOPILOT_RESULT")
    try:
        payload = json.loads(candidates[-1])
        if not isinstance(payload, dict):
            raise ValueError("result must be an object")
        action = _text(payload.get("action"), "action")
        if action == "task":
            _require_fields(payload, TASK_FIELDS)
            raw_hints = payload["hints"]
            if not isinstance(raw_hints, list):
                raise ValueError("hints must be a list")
            hints = tuple(_text(item, "hint") for item in raw_hints)
            return ControllerParseResult(
                "valid",
                AutoControllerResult(
                    action=action,
                    external_id=_text(payload["external_id"], "external_id"),
                    name=_text(payload["name"], "name"),
                    goal=_text(payload["goal"], "goal"),
                    origin=_text(payload["origin"], "origin"),
                    hints=hints,
                ),
                None,
            )
        if action not in {"wait", "finished", "accepted", "rejected"}:
            raise ValueError("unsupported action")
        _require_fields(payload, MESSAGE_FIELDS)
        return ControllerParseResult(
            "valid",
            AutoControllerResult(action=action, message=_text(payload["message"], "message")),
            None,
        )
    except (json.JSONDecodeError, ValueError) as exc:
        return ControllerParseResult("protocol_error", None, str(exc))
