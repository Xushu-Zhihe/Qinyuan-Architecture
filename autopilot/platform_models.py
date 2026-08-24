from __future__ import annotations

from dataclasses import dataclass, field
import math
import re
from typing import Mapping
from urllib.parse import urlsplit


HTTP_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})
TEMPLATE_PATTERN = re.compile(r"{{\s*([A-Za-z0-9_.-]+)\s*}}")
TEMPLATE_NAMES = frozenset(
    {
        "external_id",
        "reason",
        "lease_id",
        "cursor",
        "goal_result.id",
        "goal_result.content",
        "goal_result.result_ref",
        "submission.value",
        "submission.text",
        "submission.json",
    }
)


def _validate_templates(value: object) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            _validate_templates(str(key))
            _validate_templates(item)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _validate_templates(item)
        return
    if not isinstance(value, str):
        return
    for name in TEMPLATE_PATTERN.findall(value):
        if (
            name not in TEMPLATE_NAMES
            and not name.startswith("task.")
            and not name.startswith("submission.named.")
        ):
            raise ValueError(f"unknown endpoint template variable: {name}")
    stripped = TEMPLATE_PATTERN.sub("", value)
    if "{{" in stripped or "}}" in stripped:
        raise ValueError("invalid endpoint template syntax")


@dataclass(frozen=True, slots=True, repr=False)
class AuthConfig:
    type: str = "bearer"
    header_name: str = "Authorization"
    prefix: str = "Bearer"
    parameter_name: str = "token"

    def __post_init__(self) -> None:
        if self.type not in {"bearer", "header", "query", "cookie"}:
            raise ValueError("unsupported platform auth type")
        if not self.header_name.strip() or not self.parameter_name.strip():
            raise ValueError("platform auth names must be non-empty")

    def __repr__(self) -> str:
        return (
            f"AuthConfig(type={self.type!r}, header_name={self.header_name!r}, "
            f"prefix={self.prefix!r}, parameter_name={self.parameter_name!r})"
        )


@dataclass(frozen=True, slots=True)
class EndpointConfig:
    method: str
    path: str
    query: Mapping[str, object] = field(default_factory=dict)
    headers: Mapping[str, str] = field(default_factory=dict)
    body: object | None = None
    form: Mapping[str, str] | None = None
    timeout_seconds: float = 30.0
    max_retries: int = 0
    retry_status_codes: tuple[int, ...] = (429, 500, 502, 503, 504)

    def __post_init__(self) -> None:
        method = self.method.upper()
        if method not in HTTP_METHODS:
            raise ValueError(f"unsupported HTTP method: {self.method}")
        if not self.path.strip():
            raise ValueError("endpoint path must be non-empty")
        parsed_path = urlsplit(self.path.strip())
        if parsed_path.scheme or parsed_path.netloc or self.path.strip().startswith("//"):
            raise ValueError("endpoint path must be relative to platform base_url")
        if self.timeout_seconds <= 0 or self.timeout_seconds > 300:
            raise ValueError("endpoint timeout_seconds must be between 0 and 300")
        if self.max_retries < 0 or self.max_retries > 10:
            raise ValueError("endpoint max_retries must be between 0 and 10")
        if any(code < 100 or code > 599 for code in self.retry_status_codes):
            raise ValueError("endpoint retry status codes must be valid HTTP statuses")
        _validate_templates(self.path)
        _validate_templates(self.query)
        _validate_templates(self.headers)
        _validate_templates(self.body)
        _validate_templates(self.form)
        object.__setattr__(self, "method", method)


@dataclass(frozen=True, slots=True)
class FieldMapping:
    task_list_path: str | None = None
    external_id_path: str = "id"
    name_path: str = "name"
    goal_path: str = "goal"
    origin_path: str = "origin"
    origin_endpoint_path: str | None = None
    goal_format: str = "plain"
    flag_header: str | None = None
    flag_pattern: str | None = None
    flag_pattern_id: str | None = None
    hints_path: str | None = "hints"
    hints_item_path: str | None = None
    attachments_path: str | None = None
    cursor_path: str | None = None
    lease_id_path: str | None = None

    def __post_init__(self) -> None:
        if self.goal_format not in {"plain", "flag_objective"}:
            raise ValueError("unsupported goal format")
        if self.flag_header is not None:
            flag_header = self.flag_header.strip()
            if len(flag_header) > 200:
                raise ValueError("flag_header must be at most 200 characters")
            object.__setattr__(self, "flag_header", flag_header or None)
        if self.flag_pattern is not None:
            flag_pattern = self.flag_pattern.strip()
            if len(flag_pattern) > 500:
                raise ValueError("flag_pattern must be at most 500 characters")
            if not flag_pattern:
                flag_pattern = None
            else:
                try:
                    re.compile(flag_pattern)
                except re.error as exc:
                    raise ValueError(f"invalid flag_pattern: {exc}") from exc
            object.__setattr__(self, "flag_pattern", flag_pattern)
        if self.flag_pattern_id is not None:
            flag_pattern_id = self.flag_pattern_id.strip()
            if len(flag_pattern_id) > 100:
                raise ValueError("flag_pattern_id must be at most 100 characters")
            object.__setattr__(self, "flag_pattern_id", flag_pattern_id or None)
        if self.goal_format == "flag_objective" and self.flag_pattern and not self.flag_pattern_id:
            object.__setattr__(self, "flag_pattern_id", "configured-v1")


@dataclass(frozen=True, slots=True)
class OutcomeCondition:
    path: str | None = None
    equals: object = True
    status_codes: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class OutcomeConfig:
    fetch_finished: OutcomeCondition | None = None
    fetch_empty: OutcomeCondition | None = None
    submit_accepted: OutcomeCondition | None = None
    submit_rejected: OutcomeCondition | None = None
    submit_retryable_status_codes: tuple[int, ...] = (429, 500, 502, 503, 504)
    skip_succeeded: OutcomeCondition | None = None


@dataclass(frozen=True, slots=True)
class StructuredPlatformConfig:
    base_url: str
    fetch_task: EndpointConfig
    submit_result: EndpointConfig
    mapping: FieldMapping = field(default_factory=FieldMapping)
    outcomes: OutcomeConfig = field(default_factory=OutcomeConfig)
    auth: AuthConfig = field(default_factory=AuthConfig)
    default_headers: Mapping[str, str] = field(default_factory=dict)
    current_task: EndpointConfig | None = None
    task_info: EndpointConfig | None = None
    skip_task: EndpointConfig | None = None
    health_check: EndpointConfig | None = None
    task_info_mode: str = "always"
    task_info_failure_action: str = "fail_task"
    task_info_delay_seconds: float = 0
    initial_cursor: str | None = None

    def __post_init__(self) -> None:
        base = self.base_url.strip().rstrip("/")
        if not base:
            raise ValueError("platform base_url must be non-empty")
        parsed = urlsplit(base)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("platform base_url must be an absolute HTTP URL")
        if self.task_info_mode not in {"always", "when_incomplete"}:
            raise ValueError("invalid task_info_mode")
        if self.task_info_failure_action not in {
            "fail_task",
            "use_fetch_payload",
            "skip_task",
        }:
            raise ValueError("invalid task_info_failure_action")
        if (
            not math.isfinite(self.task_info_delay_seconds)
            or self.task_info_delay_seconds < 0
        ):
            raise ValueError("task_info_delay_seconds must be a non-negative finite number")
        auth_header = self.auth.header_name.casefold()
        endpoints = (
            self.fetch_task,
            self.submit_result,
            self.current_task,
            self.task_info,
            self.skip_task,
            self.health_check,
        )
        if self.auth.type in {"bearer", "header"}:
            if any(key.casefold() == auth_header for key in self.default_headers):
                raise ValueError("authorization header must not be configured twice")
            if any(
                endpoint is not None and any(key.casefold() == auth_header for key in endpoint.headers)
                for endpoint in endpoints
            ):
                raise ValueError("endpoint authorization header must not be configured twice")
        if self.auth.type == "query" and any(
            endpoint is not None and self.auth.parameter_name in endpoint.query
            for endpoint in endpoints
        ):
            raise ValueError("endpoint auth query parameter must not be configured twice")
        object.__setattr__(self, "base_url", base)


@dataclass(frozen=True, slots=True)
class PlatformResult:
    status: str
    task: object | None = None
    message: str | None = None
    retry_after_seconds: float | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)
