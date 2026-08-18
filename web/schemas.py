from __future__ import annotations

from typing import Annotated, Literal, Union
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from autopilot.models import (
    CONTROLLER_EXTRA_BODY_FIELDS,
    CONTROLLER_RESERVED_BODY_FIELDS,
)


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    goal: str = Field(min_length=1)
    origin: str = Field(min_length=1)
    hints: list[str] = Field(default_factory=list)
    mode: Literal["manual", "run"]
    providers: list[str] = Field(default_factory=list)
    agent_limit: int = Field(ge=1, le=16)
    challenge_path: str | None = None


class ControllerAdvanced(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timeout_seconds: float = Field(default=300, gt=0, le=1800)
    max_rounds: int = Field(default=20, ge=1, le=50)
    max_transport_retries: int = Field(default=2, ge=0, le=10)
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_output_tokens: int | None = Field(default=None, ge=1, le=200000)
    reasoning_effort: str | None = Field(default=None, min_length=1, max_length=40)
    extra_headers: dict[str, str] = Field(default_factory=dict)
    extra_body: dict[str, object] = Field(default_factory=dict)

    @model_validator(mode="after")
    def allowed_extra_body(self):
        unknown = sorted(set(self.extra_body) - CONTROLLER_EXTRA_BODY_FIELDS)
        if unknown:
            raise ValueError(f"unsupported controller extra_body fields: {unknown}")
        reserved = sorted(set(self.extra_body) & CONTROLLER_RESERVED_BODY_FIELDS)
        if reserved:
            raise ValueError(
                f"controller extra_body cannot override request fields: {reserved}"
            )
        return self


class ControllerPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_key: SecretStr = Field(min_length=1)
    base_url: str = Field(min_length=1, max_length=500)
    model: str = Field(
        min_length=1,
        max_length=200,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$",
    )
    advanced: ControllerAdvanced = Field(default_factory=ControllerAdvanced)

    @model_validator(mode="after")
    def absolute_base_url(self):
        parsed = urlsplit(self.base_url.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("controller.base_url must be an absolute HTTP URL")
        return self


class PromptCompletenessPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    controller: ControllerPayload
    prompt: str = Field(min_length=1, max_length=200000)


class AuthPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["bearer", "header", "query", "cookie"] = "bearer"
    token: SecretStr = Field(default=SecretStr(""))
    header_name: str = Field(default="Authorization", min_length=1, max_length=120)
    prefix: str = Field(default="Bearer", max_length=80)
    parameter_name: str = Field(default="token", min_length=1, max_length=120)


class EndpointPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
    path: str = Field(min_length=1, max_length=1000)
    query: dict[str, object] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    body: object | None = None
    form: dict[str, str] | None = None
    timeout_seconds: float = Field(default=30, gt=0, le=300)
    max_retries: int = Field(default=0, ge=0, le=10)
    retry_status_codes: tuple[int, ...] = (429, 500, 502, 503, 504)

    @model_validator(mode="after")
    def relative_path(self):
        if "://" in self.path:
            raise ValueError("endpoint path must be relative to platform base_url")
        return self

    @model_validator(mode="after")
    def retry_codes(self):
        if any(code < 100 or code > 599 for code in self.retry_status_codes):
            raise ValueError("retry_status_codes must contain valid HTTP statuses")
        return self


class FieldMappingPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_list_path: str | None = None
    external_id_path: str = "id"
    name_path: str = "name"
    goal_path: str = "goal"
    origin_path: str = "origin"
    hints_path: str | None = "hints"
    hints_item_path: str | None = None
    attachments_path: str | None = None
    cursor_path: str | None = None
    lease_id_path: str | None = None


class ConditionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str | None = None
    equals: object = True
    status_codes: tuple[int, ...] = ()


class FetchOutcomePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    finished: ConditionPayload | None = None
    empty: ConditionPayload | None = None


class SubmitOutcomePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    accepted: ConditionPayload | None = None
    rejected: ConditionPayload | None = None
    retryable: ConditionPayload | None = None


class SkipOutcomePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    succeeded: ConditionPayload | None = None


class OutcomesPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fetch: FetchOutcomePayload = Field(default_factory=FetchOutcomePayload)
    submit: SubmitOutcomePayload = Field(default_factory=SubmitOutcomePayload)
    skip: SkipOutcomePayload = Field(default_factory=SkipOutcomePayload)


class StructuredPlatformPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str = Field(min_length=1, max_length=500)
    auth: AuthPayload = Field(default_factory=AuthPayload)
    default_headers: dict[str, str] = Field(default_factory=dict)
    fetch_task: EndpointPayload
    current_task: EndpointPayload | None = None
    task_info: EndpointPayload | None = None
    submit_result: EndpointPayload
    skip_task: EndpointPayload | None = None
    mapping: FieldMappingPayload = Field(default_factory=FieldMappingPayload)
    outcomes: OutcomesPayload = Field(default_factory=OutcomesPayload)
    health_check: EndpointPayload | None = None
    task_info_mode: Literal["always", "when_incomplete"] = "always"
    task_info_failure_action: Literal[
        "fail_task", "use_fetch_payload", "skip_task"
    ] = "fail_task"
    initial_cursor: str | None = None


class AutoCommon(BaseModel):
    model_config = ConfigDict(extra="forbid")

    controller: ControllerPayload
    solver_providers: list[str] = Field(min_length=1)
    agent_limit: int = Field(default=2, ge=1, le=16)
    max_tasks: int = Field(default=10, ge=1, le=1000)
    task_timeout_minutes: float = Field(default=60, gt=0, le=1440)
    poll_interval_seconds: float = Field(default=1, gt=0, le=3600)
    rejected_action: Literal["replan", "skip", "fail"] = "fail"
    solver_failure_action: Literal["skip", "fail"] = "fail"
    stop_active_task_action: Literal["leave", "skip"] = "leave"
    max_replans_after_rejection: int = Field(default=1, ge=0, le=10)
    max_transport_retries: int = Field(default=2, ge=0, le=10)

    @model_validator(mode="after")
    def unique_providers(self):
        providers = [provider.strip() for provider in self.solver_providers]
        if any(not provider for provider in providers):
            raise ValueError("solver_providers must contain non-empty names")
        self.solver_providers = providers
        if len(set(providers)) != len(providers):
            raise ValueError("solver_providers must be unique")
        if self.agent_limit > len(providers):
            raise ValueError("agent_limit cannot exceed solver provider count")
        return self


class PromptAutoStart(AutoCommon):
    mode: Literal["prompt"]
    platform: dict[str, str] = Field(min_length=1)

    @model_validator(mode="after")
    def prompt_payload(self):
        if not str(self.platform.get("prompt", "")).strip():
            raise ValueError("platform.prompt must be non-empty")
        if set(self.platform) != {"prompt"}:
            raise ValueError("prompt platform accepts only prompt")
        return self


class StructuredAutoStart(AutoCommon):
    mode: Literal["structured"]
    platform: StructuredPlatformPayload

    @model_validator(mode="after")
    def skip_capability(self):
        if (
            (
                self.rejected_action == "skip"
                or self.solver_failure_action == "skip"
                or self.stop_active_task_action == "skip"
            )
            and self.platform.skip_task is None
        ):
            raise ValueError(
                "skip policies require a structured platform skip_task endpoint"
            )
        return self


ModernAutoStart = Annotated[
    Union[PromptAutoStart, StructuredAutoStart], Field(discriminator="mode")
]


class LegacyAutoStart(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform_prompt: str = Field(min_length=1)
    controller_provider: str = Field(min_length=1)
    solver_providers: list[str] = Field(min_length=1)
    agent_limit: int = Field(default=2, ge=1, le=16)
    max_tasks: int = Field(default=10, ge=1, le=1000)
    task_timeout_minutes: float = Field(default=60, gt=0, le=1440)
    controller_timeout_seconds: float = Field(default=300, gt=0, le=1800)


AutoStart = Union[ModernAutoStart, LegacyAutoStart]


class PromptPlatformValidation(BaseModel):
    mode: Literal["prompt"]
    platform: dict[str, str]


class StructuredPlatformValidation(BaseModel):
    mode: Literal["structured"]
    platform: StructuredPlatformPayload
    sample_response: object | None = None


PlatformValidation = Annotated[
    Union[PromptPlatformValidation, StructuredPlatformValidation],
    Field(discriminator="mode"),
]


class NodeCreate(BaseModel):
    type: Literal["fact", "hint"]
    content: str = Field(min_length=1)


class EdgeCreate(BaseModel):
    type: str = Field(min_length=1)
    from_node_id: str = Field(min_length=1)
    to_node_id: str = Field(min_length=1)
    content: str | None = None


class EdgeUpdate(BaseModel):
    from_node_id: str = Field(min_length=1)
    to_node_id: str = Field(min_length=1)


class LayoutUpdate(BaseModel):
    revision: int = Field(ge=0)
    nodes: dict[str, object] = Field(default_factory=dict)
    edges: dict[str, object] = Field(default_factory=dict)
    viewport: dict[str, object] = Field(default_factory=dict)
