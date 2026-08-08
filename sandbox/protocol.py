from __future__ import annotations

import json

from sandbox.contracts import BridgeProtocolError, CommandRequest, CommandResult, PROTOCOL_VERSION


MAX_REQUEST_BYTES = 128 * 1024


def decode_command_body(body: bytes | str) -> CommandRequest:
    if isinstance(body, str):
        body = body.encode("utf-8")
    if len(body) > MAX_REQUEST_BYTES:
        raise BridgeProtocolError("request body is too large")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BridgeProtocolError("request body must be UTF-8 JSON") from exc
    return CommandRequest.from_dict(payload)


def encode_result(result: CommandResult) -> bytes:
    return json.dumps(result.to_dict(), ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def error_payload(category: str, detail: str) -> dict[str, object]:
    if category not in {"auth", "expired", "protocol", "policy", "sandbox_unavailable", "tool_timeout", "cancelled", "internal"}:
        category = "internal"
    return {"protocol_version": PROTOCOL_VERSION, "error": category, "detail": detail[:300]}
