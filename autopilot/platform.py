from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Mapping
from urllib.parse import urljoin

import httpx

from autopilot.models import AutoTask, PromptPlatformConfig, RuntimePlatformSecret
from autopilot.platform_models import (
    EndpointConfig,
    OutcomeCondition,
    PlatformResult,
    StructuredPlatformConfig,
    TEMPLATE_PATTERN,
)
from autopilot.solver_result import SubmissionPlan


RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})


def _extract(value: object, path: str | None) -> object:
    if path and "|" in path:
        errors: list[Exception] = []
        for candidate in path.split("|"):
            try:
                return _extract(value, candidate.strip())
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                errors.append(exc)
        raise ValueError(f"response mapping paths not found: {path}") from errors[-1]
    current = value
    if not path:
        return current
    for part in path.split("."):
        if isinstance(current, Mapping):
            current = current[part]
        elif isinstance(current, list) and part.isdigit():
            current = current[int(part)]
        else:
            raise ValueError(f"response mapping path not found: {path}")
    return current


def _condition_matches(condition: OutcomeCondition | None, status: int, payload: object) -> bool:
    if condition is None:
        return False
    if condition.status_codes and status in condition.status_codes:
        return True
    if condition.path is None:
        return False
    try:
        return _extract(payload, condition.path) == condition.equals
    except (KeyError, IndexError, ValueError):
        return False


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    header = response.headers.get("Retry-After", "").strip()
    if header:
        try:
            return max(0.1, min(float(header), 300.0))
        except ValueError:
            pass
    return float(min(2**attempt, 30))


def _render(value: object, context: Mapping[str, object]) -> object:
    if isinstance(value, dict):
        return {str(key): _render(item, context) for key, item in value.items()}
    if isinstance(value, list):
        return [_render(item, context) for item in value]
    if not isinstance(value, str):
        return value
    matches = TEMPLATE_PATTERN.findall(value)
    if len(matches) == 1 and TEMPLATE_PATTERN.fullmatch(value):
        name = matches[0]
        if name not in context:
            raise ValueError(f"unknown endpoint template variable: {name}")
        return context[name]
    result = value
    for name in matches:
        if name not in context:
            raise ValueError(f"unknown endpoint template variable: {name}")
        result = TEMPLATE_PATTERN.sub(
            lambda match: str(context[match.group(1)]),
            result,
        )
    return result


@dataclass(frozen=True, slots=True)
class PlatformRequestPlan:
    phase: str
    method: str
    url: str
    query: Mapping[str, object]
    body: object | None
    form: Mapping[str, str] | None
    fingerprint: str


class StructuredPlatformAdapter:
    def __init__(
        self,
        config: StructuredPlatformConfig,
        *,
        secret: RuntimePlatformSecret | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config
        self.secret = secret or RuntimePlatformSecret()
        self.client = client or httpx.AsyncClient(timeout=30.0, follow_redirects=True)
        self._owns_client = client is None
        self._pending_items: list[object] = []
        self._cursor = config.initial_cursor
        self._terminal_tasks: dict[str, str] = {}
        self._seen_task_ids: set[str] = set()

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()
        self._pending_items.clear()
        self._seen_task_ids.clear()

    def _headers_and_query(self, endpoint: EndpointConfig, context: Mapping[str, object]) -> tuple[dict[str, str], dict[str, object]]:
        headers = dict(
            _render({**self.config.default_headers, **endpoint.headers}, context)
        )
        query = dict(_render(dict(endpoint.query), context))
        auth = self.config.auth
        token = self.secret.token
        if token:
            if auth.type == "bearer":
                headers[auth.header_name] = f"{auth.prefix} {token}".strip()
            elif auth.type == "header":
                headers[auth.header_name] = f"{auth.prefix} {token}".strip()
            elif auth.type == "query":
                query[auth.parameter_name] = token
            elif auth.type == "cookie":
                headers["Cookie"] = f"{auth.parameter_name}={token}"
        return headers, query

    async def _request(self, endpoint: EndpointConfig, context: Mapping[str, object]):
        path = str(_render(endpoint.path, context))
        headers, query = self._headers_and_query(endpoint, context)
        body = _render(endpoint.body, context)
        form = _render(dict(endpoint.form), context) if endpoint.form is not None else None
        response = None
        for attempt in range(endpoint.max_retries + 1):
            try:
                response = await self.client.request(
                    endpoint.method,
                    urljoin(self.config.base_url + "/", path.lstrip("/")),
                    headers=headers,
                    params=query,
                    json=body if form is None else None,
                    data=form,
                    timeout=endpoint.timeout_seconds,
                )
            except httpx.HTTPError as exc:
                if attempt >= endpoint.max_retries:
                    return None, None, PlatformResult(
                        "retryable_error",
                        message=f"platform transport {type(exc).__name__}",
                    )
                await asyncio.sleep(min(2**attempt, 30))
                continue
            if (
                response.status_code in endpoint.retry_status_codes
                and attempt < endpoint.max_retries
            ):
                await asyncio.sleep(_retry_delay(response, attempt))
                continue
            break
        assert response is not None
        try:
            payload = response.json()
        except ValueError:
            payload = {"text": response.text[:20000]}
        return response, payload, None

    async def fetch_task(self) -> PlatformResult:
        if self._pending_items:
            return await self._task_result(self._pending_items.pop(0))
        response, payload, error = await self._request(
            self.config.fetch_task,
            {"cursor": self._cursor or ""},
        )
        if error:
            return error
        assert response is not None
        if response.status_code in RETRYABLE_STATUS_CODES:
            return PlatformResult(
                "retryable_error",
                message=f"platform fetch HTTP {response.status_code}",
                retry_after_seconds=_retry_delay(response, 0),
            )
        outcomes = self.config.outcomes
        finished = _condition_matches(outcomes.fetch_finished, response.status_code, payload)
        empty = _condition_matches(outcomes.fetch_empty, response.status_code, payload)
        if finished and empty:
            return PlatformResult("fatal_error", message="fetch response matched conflicting outcomes")
        if finished:
            return PlatformResult("finished")
        if empty:
            return PlatformResult("wait")
        if response.is_error:
            return PlatformResult("fatal_error", message=f"HTTP {response.status_code}")
        try:
            if self.config.mapping.cursor_path:
                cursor = _extract(payload, self.config.mapping.cursor_path)
                self._cursor = None if cursor is None else str(cursor)
            item = _extract(payload, self.config.mapping.task_list_path)
            if isinstance(item, list):
                if not item:
                    return PlatformResult(
                        "wait", metadata={"cursor": self._cursor}
                    )
                self._pending_items.extend(item[1:])
                item = item[0]
            return await self._task_result(item)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            return PlatformResult("fatal_error", message=f"task mapping failed: {exc}")

    async def _task_result(self, item: object) -> PlatformResult:
        try:
            task = self._map_task(item)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            if self.config.task_info is None:
                return PlatformResult("fatal_error", message=f"task mapping failed: {exc}")
            task = None
        if task is not None:
            if task.external_id in self._seen_task_ids:
                return PlatformResult(
                    "fatal_error",
                    message=f"duplicate task external_id: {task.external_id}",
                )
            self._seen_task_ids.add(task.external_id)
        if self.config.task_info is None:
            return PlatformResult("task", task=task, metadata={"cursor": self._cursor})
        if task is not None and self.config.task_info_mode == "when_incomplete":
            return PlatformResult("task", task=task, metadata={"cursor": self._cursor})
        if task is None:
            context = {"cursor": self._cursor or "", "lease_id": ""}
            if isinstance(item, Mapping):
                for key, value in item.items():
                    context[f"task.{key}"] = value
                try:
                    context["external_id"] = str(
                        _extract(item, self.config.mapping.external_id_path)
                    )
                except (KeyError, IndexError, TypeError, ValueError):
                    return PlatformResult(
                        "fatal_error",
                        message="task info requires an external_id mapping",
                    )
        else:
            context = self._task_context(task)
        info_response, info_payload, info_error = await self._request(
            self.config.task_info,
            context,
        )
        if (
            info_response is not None
            and info_response.status_code in RETRYABLE_STATUS_CODES
        ):
            return PlatformResult(
                "retryable_error",
                message=f"task info HTTP {info_response.status_code}",
                retry_after_seconds=_retry_delay(info_response, 0),
            )
        if info_error or info_response is None or info_response.is_error:
            if self.config.task_info_failure_action == "use_fetch_payload" and task is not None:
                return PlatformResult(
                    "task",
                    task=task,
                    message="task info unavailable; using fetch payload",
                    metadata={"cursor": self._cursor},
                )
            if self.config.task_info_failure_action == "skip_task":
                if task is None:
                    try:
                        external_id = str(
                            _extract(item, self.config.mapping.external_id_path)
                        ).strip()
                    except (KeyError, IndexError, TypeError, ValueError):
                        external_id = ""
                    if external_id:
                        task = AutoTask(
                            external_id=external_id,
                            name=external_id,
                            goal="",
                            origin="",
                        )
                return PlatformResult(
                    "skip_required" if task is not None else "fatal_error",
                    task=task,
                    message="task info failed and task requires skip",
                    metadata={"reason_code": "task_info_skip_required"},
                )
            return info_error or PlatformResult(
                "fatal_error",
                message=f"task info HTTP {info_response.status_code}",
            )
        try:
            info_task = self._map_task(info_payload, fallback=task)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            return PlatformResult("fatal_error", message=f"task info mapping failed: {exc}")
        return PlatformResult(
            "task", task=info_task, metadata={"cursor": self._cursor}
        )

    async def health_check(self) -> PlatformResult:
        if self.config.health_check is None:
            return PlatformResult("fatal_error", message="health endpoint is not configured")
        response, payload, error = await self._request(self.config.health_check, {"cursor": self._cursor or ""})
        if error:
            return error
        assert response is not None
        if response.status_code in RETRYABLE_STATUS_CODES:
            return PlatformResult("retryable_error", message=f"health endpoint HTTP {response.status_code}")
        if response.is_error:
            return PlatformResult("fatal_error", message=f"health endpoint HTTP {response.status_code}")
        return PlatformResult("healthy", metadata={"http_status": response.status_code, "payload": payload})

    def _map_task(self, item: object, fallback: AutoTask | None = None) -> AutoTask:
        if not isinstance(item, Mapping):
            raise TypeError("mapped task must be an object")
        mapping = self.config.mapping

        def text(path: str, label: str, default: str | None = None) -> str:
            try:
                raw = _extract(item, path)
            except (KeyError, IndexError, TypeError, ValueError):
                if default is not None:
                    return default
                raise ValueError(f"{label} path not found: {path}")
            if not isinstance(raw, (str, int, float)) or isinstance(raw, bool):
                raise TypeError(f"{label} must be a string or number")
            value = str(raw).strip()
            if not value:
                raise ValueError(f"{label} is empty")
            return value

        hints = fallback.hints if fallback else ()
        if mapping.hints_path:
            try:
                raw_hints = _extract(item, mapping.hints_path)
            except (KeyError, IndexError, TypeError, ValueError):
                raw_hints = None
            if raw_hints is None:
                hints = fallback.hints if fallback else ()
            elif not isinstance(raw_hints, list):
                raise TypeError("hints must be an array")
            elif mapping.hints_item_path:
                hints = tuple(
                    str(_extract(hint, mapping.hints_item_path))
                    for hint in raw_hints
                )
            else:
                if any(not isinstance(hint, (str, int, float)) for hint in raw_hints):
                    raise TypeError("hints array items must be scalar")
                hints = tuple(str(hint) for hint in raw_hints)
        lease_id = fallback.lease_id if fallback else None
        if mapping.lease_id_path:
            try:
                raw_lease = _extract(item, mapping.lease_id_path)
                lease_id = None if raw_lease is None else str(raw_lease)
            except (KeyError, IndexError, TypeError, ValueError):
                if fallback is None:
                    raise ValueError(
                        f"lease_id path not found: {mapping.lease_id_path}"
                    )
        metadata = dict(item)
        if mapping.attachments_path:
            try:
                attachments = _extract(item, mapping.attachments_path)
            except (KeyError, IndexError, TypeError, ValueError):
                attachments = None
            if attachments is None:
                attachments = []
            if not isinstance(attachments, list):
                raise TypeError("attachments must be an array")
            for attachment in attachments:
                if isinstance(attachment, str):
                    if not attachment.strip():
                        raise ValueError("attachment path must be non-empty")
                elif isinstance(attachment, Mapping):
                    url = attachment.get("url")
                    name = attachment.get("name")
                    if not isinstance(url, str) or not url.strip():
                        raise TypeError("attachment object url must be a non-empty string")
                    if name is not None and (not isinstance(name, str) or not name.strip()):
                        raise TypeError("attachment object name must be a non-empty string")
                else:
                    raise TypeError("attachments must contain strings or {url, name} objects")
            metadata["_attachments"] = attachments
        return AutoTask(
            external_id=text(
                mapping.external_id_path,
                "external_id",
                fallback.external_id if fallback else None,
            ),
            name=text(mapping.name_path, "name", fallback.name if fallback else None),
            goal=text(mapping.goal_path, "goal", fallback.goal if fallback else None),
            origin=text(
                mapping.origin_path,
                "origin",
                fallback.origin if fallback else None,
            ),
            hints=hints,
            raw_metadata=metadata,
            lease_id=lease_id,
        )

    @staticmethod
    def _task_context(task: AutoTask) -> dict[str, object]:
        context: dict[str, object] = {
            "external_id": task.external_id,
            "lease_id": task.lease_id or "",
            "cursor": "",
        }
        for key, value in task.raw_metadata.items():
            context[f"task.{key}"] = value
        return context

    async def submit_result(self, task: AutoTask, plan: SubmissionPlan, goal_result) -> PlatformResult:
        terminal = self._terminal_tasks.get(task.external_id)
        if terminal is not None:
            return PlatformResult(
                "fatal_error",
                message=f"task is already terminal: {terminal}",
            )
        value = plan.text if plan.content_type == "text" else plan.json_value if plan.content_type == "json" else dict(plan.form_fields)
        context = {
            **self._task_context(task),
            "external_id": task.external_id,
            "lease_id": task.lease_id or "",
            "goal_result.id": goal_result.fact.id,
            "goal_result.content": goal_result.fact.content,
            "goal_result.result_ref": goal_result.result_ref,
            "submission.value": value,
            "submission.text": plan.text or "",
            "submission.json": plan.json_value,
        }
        context.update(
            {f"submission.named.{key}": item for key, item in plan.named_values.items()}
        )
        response, payload, error = await self._request(self.config.submit_result, context)
        if error:
            return error
        assert response is not None
        outcomes = self.config.outcomes
        if response.status_code in outcomes.submit_retryable_status_codes:
            return PlatformResult(
                "retryable_error",
                message=f"platform submit HTTP {response.status_code}",
                retry_after_seconds=_retry_delay(response, 0),
                metadata={"http_status": response.status_code},
            )
        accepted = _condition_matches(outcomes.submit_accepted, response.status_code, payload)
        rejected = _condition_matches(outcomes.submit_rejected, response.status_code, payload)
        if accepted and rejected:
            return PlatformResult("fatal_error", message="submit response matched conflicting outcomes")
        if accepted:
            self._terminal_tasks[task.external_id] = "accepted"
            return PlatformResult(
                "accepted", metadata={"http_status": response.status_code}
            )
        if rejected:
            return PlatformResult(
                "rejected", metadata={"http_status": response.status_code}
            )
        if response.is_error:
            return PlatformResult("fatal_error", message=f"HTTP {response.status_code}")
        return PlatformResult("fatal_error", message="submit response did not match an outcome")

    async def skip_task(self, task: AutoTask, reason: str) -> PlatformResult:
        terminal = self._terminal_tasks.get(task.external_id)
        if terminal == "accepted":
            return PlatformResult("skip_failed", message="accepted task cannot be skipped")
        if terminal == "skipped":
            return PlatformResult("skipped", message="task was already skipped")
        if self.config.skip_task is None:
            return PlatformResult("skip_failed", message="skip endpoint is not configured")
        response, payload, error = await self._request(self.config.skip_task, {"external_id": task.external_id, "lease_id": task.lease_id or "", "reason": reason})
        if error:
            return error
        assert response is not None
        if response.status_code in RETRYABLE_STATUS_CODES:
            return PlatformResult("retryable_error", message=f"HTTP {response.status_code}")
        if _condition_matches(self.config.outcomes.skip_succeeded, response.status_code, payload):
            self._terminal_tasks[task.external_id] = "skipped"
            return PlatformResult(
                "skipped", metadata={"http_status": response.status_code}
            )
        if response.is_error:
            return PlatformResult("skip_failed", message=f"HTTP {response.status_code}")
        return PlatformResult("skip_failed", message="skip response did not match an outcome")


class PromptPlatformAdapter:
    PHASE_TOOLS = {
        "fetch": frozenset({"platform_http_request", "download_attachment", "extract_task_fields"}),
        "submit": frozenset({"get_solver_result", "list_solver_facts", "preview_submission", "submit_platform_result"}),
        "skip": frozenset({"skip_platform_task"}),
    }

    def __init__(self, config: PromptPlatformConfig, *, client: httpx.AsyncClient | None = None) -> None:
        self.config = config
        self.client = client or httpx.AsyncClient(timeout=30.0, follow_redirects=True)
        self._owns_client = client is None
        self._frozen_requests: dict[str, dict[str, object]] = {}
        self._runtime_root: Path | None = None

    def set_runtime_root(self, runtime_root: Path) -> None:
        self._runtime_root = runtime_root.resolve()
        (self._runtime_root / "staging").mkdir(parents=True, exist_ok=True)

    def _relative_staging_path(self, value: object) -> PurePosixPath:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("destination_relative_path is required")
        relative = PurePosixPath(value.replace("\\", "/"))
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("attachment destination must stay in staging")
        return relative

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()
        self._frozen_requests.clear()
        if self._runtime_root is not None:
            shutil.rmtree(self._runtime_root / "staging", ignore_errors=True)

    async def download_attachment(self, arguments: Mapping[str, object]) -> dict[str, object]:
        if self._runtime_root is None:
            raise ValueError("prompt runtime root is not configured")
        relative = self._relative_staging_path(arguments.get("destination_relative_path"))
        target = self._runtime_root / "staging" / Path(*relative.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            response = await self.client.get(
                str(arguments.get("url_or_path", "")),
                headers=arguments.get("headers"),
                timeout=120,
            )
            response.raise_for_status()
            target.write_bytes(response.content)
        except (httpx.HTTPError, OSError, ValueError) as exc:
            return {"status": "retryable_error", "category": "attachment_error", "message": type(exc).__name__}
        return {
            "status": "downloaded",
            "relative_path": relative.as_posix(),
            "size_bytes": target.stat().st_size,
            "content_type": response.headers.get("Content-Type", "application/octet-stream"),
        }

    async def extract_task_fields(self, arguments: Mapping[str, object]) -> dict[str, object]:
        response = arguments.get("response")
        mapping = arguments.get("mapping")
        if not isinstance(response, Mapping) or not isinstance(mapping, Mapping):
            raise ValueError("response and mapping must be objects")
        def read(path: object, default: object = None) -> object:
            if path is None:
                return default
            return _extract(response, str(path))
        try:
            return {
                "status": "ok",
                "external_id": read(mapping.get("external_id_path", "id")),
                "name": read(mapping.get("name_path", "name")),
                "goal": read(mapping.get("goal_path", "goal")),
                "origin": read(mapping.get("origin_path", "origin")),
                "hints": read(mapping.get("hints_path", "hints"), []),
                "metadata": dict(response),
            }
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            return {"status": "fatal_error", "message": f"task field extraction failed: {exc}"}

    def promote_attachments(self, external_id: str, paths: object) -> tuple[Path, ...]:
        if self._runtime_root is None or not isinstance(paths, (list, tuple)):
            return ()
        destination = self._runtime_root / "tasks" / external_id
        promoted: list[Path] = []
        for item in paths:
            relative = self._relative_staging_path(item)
            source = self._runtime_root / "staging" / Path(*relative.parts)
            if not source.is_file():
                continue
            target = destination / Path(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(target))
            promoted.append(target)
        return tuple(promoted)

    async def platform_http_request(self, arguments: Mapping[str, object]) -> dict[str, object]:
        try:
            response = await self.client.request(
                str(arguments.get("method", "GET")),
                str(arguments["url_or_path"]),
                params=arguments.get("query"),
                headers=arguments.get("headers"),
                json=arguments.get("json_body"),
                data=arguments.get("form_body"),
                timeout=min(float(arguments.get("timeout_seconds", 30)), 120),
            )
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            return {
                "category": "network_error",
                "status": "retryable_error",
                "message": type(exc).__name__,
            }
        try:
            json_value = response.json()
        except ValueError:
            json_value = None
        category = "ok"
        if response.is_error:
            category = "http_error"
        elif json_value is None and response.text:
            category = "json_error"
        return {
            "category": category,
            "status": response.status_code,
            "retryable": response.status_code in RETRYABLE_STATUS_CODES,
            "body": response.text[:20000],
            "json": json_value,
            "truncated": len(response.text) > 20000,
        }

    def freeze_submission(self, request: Mapping[str, object]) -> str:
        encoded = json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        fingerprint = "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        self._frozen_requests.setdefault(fingerprint, dict(request))
        return fingerprint

    async def replay_submission(self, fingerprint: str) -> dict[str, object]:
        if fingerprint not in self._frozen_requests:
            raise ValueError("unknown frozen submission plan")
        return await self.platform_http_request(self._frozen_requests[fingerprint])
