"""Generic Manifest operation executor."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any, Mapping

import httpx

from automation.manifest import Manifest, Operation, SuccessCondition
from automation.secrets import RuntimePlatformSecret, SecretRedactor


class OperationExecutionError(RuntimeError):
    """Raised when a declared operation cannot be executed."""


@dataclass(frozen=True, slots=True)
class OperationResult:
    operation_id: str
    method: str
    url: str
    status_code: int | None
    response_headers: Mapping[str, str]
    payload: Any
    success: bool | None
    message: str
    downloaded_path: Path | None = None

    def summary(self, redactor: SecretRedactor) -> dict[str, object]:
        return {
            "operation_id": self.operation_id,
            "method": self.method,
            "url": redactor.text(self.url),
            "status_code": self.status_code,
            "response_headers": redactor.value(dict(self.response_headers)),
            "success": self.success,
            "message": redactor.text(self.message),
            "downloaded_path": (
                str(self.downloaded_path) if self.downloaded_path else None
            ),
        }


class ManifestOperationExecutor:
    """Execute arbitrary declared operations through one reusable HTTP boundary."""

    def __init__(
        self,
        manifest: Manifest,
        secret: RuntimePlatformSecret,
        *,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 300.0,
        redactor: SecretRedactor | None = None,
    ) -> None:
        self.manifest = manifest
        self.secret = secret
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(timeout=timeout_seconds)
        self.redactor = redactor or SecretRedactor(secret.token)

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def execute(
        self,
        operation_id: str,
        *,
        variables: Mapping[str, object] | None = None,
        download_to: Path | None = None,
    ) -> OperationResult:
        operation = self._operation(operation_id)
        context = dict(variables or {})
        context["platform_token"] = self.secret.token
        url = _resolve_string(operation.url, context)
        headers = {
            key: _resolve_string(value, context)
            for key, value in operation.headers.items()
        }
        query = _resolve_value(operation.query, context)
        body = _resolve_value(operation.body, context)
        request_kwargs: dict[str, object] = {
            "method": operation.method,
            "url": url,
            "headers": headers,
            "params": query,
        }
        if operation.body_mode == "json":
            request_kwargs["json"] = body
        elif operation.body_mode == "form":
            request_kwargs["data"] = body
        elif operation.body_mode == "text":
            request_kwargs["content"] = "" if body is None else str(body)
        elif operation.body_mode != "none":
            raise OperationExecutionError(
                f"unsupported body mode for operation {operation_id}: "
                f"{operation.body_mode}"
            )
        try:
            response = await self.client.request(**request_kwargs)
        except httpx.HTTPError as exc:
            raise OperationExecutionError(
                f"operation {operation_id} transport failed: {type(exc).__name__}"
            ) from exc
        payload = _response_payload(response, operation.response_type)
        if download_to is not None:
            download_to.parent.mkdir(parents=True, exist_ok=True)
            download_to.write_bytes(response.content)
        success = evaluate_success(
            operation.success_condition,
            response.status_code,
            payload,
        )
        message = (
            "platform success condition matched"
            if success is True
            else "platform success condition did not match"
            if success is False
            else "platform success condition is not declared"
        )
        return OperationResult(
            operation_id=operation.id,
            method=operation.method,
            url=url,
            status_code=response.status_code,
            response_headers={
                key: value
                for key, value in response.headers.items()
                if key.lower() not in {"authorization", "set-cookie"}
            },
            payload=payload,
            success=success,
            message=message,
            downloaded_path=download_to,
        )

    def _operation(self, operation_id: str) -> Operation:
        for operation in self.manifest.operations:
            if operation.id == operation_id:
                return operation
        raise OperationExecutionError(
            f"operation is not declared in manifest: {operation_id}"
        )


def evaluate_success(
    condition: SuccessCondition | None,
    status_code: int,
    payload: object,
) -> bool | None:
    """Evaluate only the explicit platform condition, never transport status alone."""

    if condition is None:
        return None
    if condition.kind == "status_code":
        return status_code in condition.status_codes
    if condition.kind == "json_path_equals":
        found, value = _json_path(payload, condition.path or "")
        return found and value == condition.expected
    if condition.kind == "json_path_exists":
        found, _ = _json_path(payload, condition.path or "")
        return found
    text = (
        payload
        if isinstance(payload, str)
        else json.dumps(payload, ensure_ascii=False)
    )
    if condition.kind == "text_contains":
        return bool(condition.text) and condition.text in text
    if condition.kind == "text_regex":
        return bool(condition.text) and re.search(condition.text, text) is not None
    return False


def _response_payload(response: httpx.Response, response_type: str) -> object:
    if response_type == "bytes":
        return response.content
    if response_type == "text":
        return response.text
    try:
        return response.json()
    except ValueError:
        return response.text


def _resolve_value(value: object, context: Mapping[str, object]) -> object:
    if isinstance(value, str):
        exact = _TEMPLATE.fullmatch(value.strip())
        if exact is not None:
            path = exact.group(1)
            found, resolved = _lookup(context, path)
            if not found:
                raise OperationExecutionError(
                    f"template variable is missing: {path}"
                )
            return resolved
        return _resolve_string(value, context)
    if isinstance(value, Mapping):
        return {
            str(key): _resolve_value(child, context)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [_resolve_value(child, context) for child in value]
    if isinstance(value, tuple):
        return [_resolve_value(child, context) for child in value]
    return value


_TEMPLATE = re.compile(r"\{\{\s*([A-Za-z0-9_.:-]+)\s*\}\}")


def _resolve_string(value: str, context: Mapping[str, object]) -> str:
    def replacement(match: re.Match[str]) -> str:
        path = match.group(1)
        found, resolved = _lookup(context, path)
        if not found:
            raise OperationExecutionError(f"template variable is missing: {path}")
        return str(resolved)

    return _TEMPLATE.sub(replacement, value)


def _lookup(context: Mapping[str, object], path: str) -> tuple[bool, object]:
    current: object = context
    for part in path.split("."):
        if isinstance(current, Mapping) and part in current:
            current = current[part]
        else:
            return False, None
    return True, current


def _json_path(payload: object, path: str) -> tuple[bool, object]:
    current = payload
    normalized = path.strip().removeprefix("$").lstrip(".")
    if not normalized:
        return True, current
    for part in normalized.split("."):
        if isinstance(current, Mapping) and part in current:
            current = current[part]
            continue
        if isinstance(current, list) and part.isdigit():
            index = int(part)
            if index < len(current):
                current = current[index]
                continue
        return False, None
    return True, current
