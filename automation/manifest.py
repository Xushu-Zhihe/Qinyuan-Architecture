"""Manifest v1 models and deterministic validation/rendering helpers."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Literal, Mapping
from urllib.parse import parse_qsl, urlsplit
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ManifestValidationError(ValueError):
    """Raised when a manifest cannot be used for an automation run."""


class ManifestSource(BaseModel):
    """A non-secret reference to the source section used during parsing."""

    model_config = ConfigDict(extra="forbid")

    reference: str = Field(min_length=1, max_length=500)
    confidence: Literal["high", "medium", "low", "unknown"] = "unknown"
    note: str | None = Field(default=None, max_length=1000)


class SuccessCondition(BaseModel):
    """A platform-declared condition used to recognize an operation result."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal[
        "status_code",
        "json_path_equals",
        "json_path_exists",
        "text_contains",
        "text_regex",
    ]
    status_codes: tuple[int, ...] = ()
    path: str | None = Field(default=None, max_length=500)
    expected: Any = None
    text: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def validate_shape(self) -> "SuccessCondition":
        if self.kind == "status_code" and not self.status_codes:
            raise ValueError("status_code condition requires status_codes")
        if self.kind in {"json_path_equals", "json_path_exists"} and not self.path:
            raise ValueError(f"{self.kind} condition requires path")
        if self.kind in {"text_contains", "text_regex"} and not self.text:
            raise ValueError(f"{self.kind} condition requires text")
        return self


class AnswerField(BaseModel):
    """Maps a workgroup result path to one submitted field."""

    model_config = ConfigDict(extra="forbid")

    target: str = Field(min_length=1, max_length=200)
    source_path: str = Field(min_length=1, max_length=500)
    required: bool = True


class AnswerMapping(BaseModel):
    """Describes the payload shape expected by a submission operation."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["raw", "json", "form"] = "raw"
    fields: tuple[AnswerField, ...] = ()

    @model_validator(mode="after")
    def validate_fields(self) -> "AnswerMapping":
        targets = [field.target for field in self.fields]
        if len(set(targets)) != len(targets):
            raise ValueError("answer mapping targets must be unique")
        if self.mode in {"json", "form"} and not self.fields:
            raise ValueError(f"{self.mode} answer mapping requires fields")
        return self


class Operation(BaseModel):
    """One platform operation declared by the parser or added by a human."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9_.:-]+$")
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    kind: Literal[
        "fetch",
        "current",
        "next",
        "attachment",
        "submit",
        "skip",
        "abandon",
        "other",
    ] = "other"
    method: str = Field(min_length=1, max_length=20)
    url: str = Field(min_length=1, max_length=4000)
    headers: dict[str, str] = Field(default_factory=dict)
    query: dict[str, Any] = Field(default_factory=dict)
    body: Any = None
    body_mode: Literal["json", "form", "text", "none"] = "json"
    response_type: Literal["json", "text", "bytes"] = "json"
    depends_on: tuple[str, ...] = ()
    side_effect: bool = False
    success_condition: SuccessCondition | None = None
    answer_mapping: AnswerMapping | None = None
    source: ManifestSource | None = None

    @field_validator("method")
    @classmethod
    def normalize_method(cls, value: str) -> str:
        method = value.strip().upper()
        if not re.fullmatch(r"[A-Z]+", method):
            raise ValueError("operation method must contain only ASCII letters")
        return method

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        url = value.strip()
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("operation url must be an absolute HTTP URL")
        _reject_url_secrets(url, "operation.url")
        return url


class PlatformInfo(BaseModel):
    """Human-readable platform metadata without credentials."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    base_url: str | None = None

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlsplit(value.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("platform base_url must be an absolute HTTP URL")
        normalized = value.strip().rstrip("/")
        _reject_url_secrets(normalized, "platform.base_url")
        return normalized


class Manifest(BaseModel):
    """Machine-readable platform contract generated from human documentation."""

    model_config = ConfigDict(extra="forbid")

    manifest_version: Literal[1] = 1
    manifest_id: str = Field(default_factory=lambda: uuid4().hex)
    platform: PlatformInfo
    operations: tuple[Operation, ...] = Field(min_length=1)
    sources: tuple[ManifestSource, ...] = ()
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def validate_operations(self) -> "Manifest":
        operation_ids = [operation.id for operation in self.operations]
        if len(set(operation_ids)) != len(operation_ids):
            raise ValueError("manifest operation IDs must be unique")
        known_ids = set(operation_ids)
        for operation in self.operations:
            missing = sorted(set(operation.depends_on) - known_ids)
            if missing:
                raise ValueError(
                    f"operation {operation.id} depends on unknown operations: {missing}"
                )
        _reject_persisted_secrets(self.model_dump(mode="python"))
        return self

    def validate_for_run(self) -> None:
        """Reject a parsed manifest that is not safe to execute."""

        missing_conditions = [
            operation.id
            for operation in self.operations
            if operation.kind == "submit" and operation.success_condition is None
        ]
        if missing_conditions:
            raise ManifestValidationError(
                "submission operations require explicit success conditions: "
                + ", ".join(missing_conditions)
            )

    def content_hash(self) -> str:
        """Return a stable hash for audit and optimistic update checks."""

        payload = self.model_dump(mode="json", exclude={"created_at", "updated_at"})
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def touched(self) -> "Manifest":
        """Return a copy with a fresh modification timestamp."""

        return self.model_copy(update={"updated_at": datetime.now(timezone.utc)})

    def to_json(self) -> str:
        """Serialize the manifest without credentials."""

        return self.model_dump_json(indent=2, exclude_none=True)

    def to_markdown(self) -> str:
        """Render a human-readable view from the JSON authority."""

        lines = [
            f"# Platform Manifest: {self.platform.name}",
            "",
            f"- Manifest ID: `{self.manifest_id}`",
            f"- Version: `{self.manifest_version}`",
            f"- Content hash: `{self.content_hash()}`",
            f"- Base URL: `{self.platform.base_url or 'operation URLs are absolute'}`",
            "",
            "## Operations",
            "",
        ]
        for operation in self.operations:
            lines.extend(
                [
                    f"### `{operation.id}` — {operation.name}",
                    "",
                    f"- Kind: `{operation.kind}`",
                    f"- Request: `{operation.method} {operation.url}`",
                    f"- Side effect: `{operation.side_effect}`",
                    f"- Body mode: `{operation.body_mode}`",
                    f"- Response type: `{operation.response_type}`",
                ]
            )
            if operation.description:
                lines.append(f"- Description: {operation.description}")
            if operation.depends_on:
                lines.append(
                    "- Depends on: "
                    + ", ".join(f"`{item}`" for item in operation.depends_on)
                )
            if operation.success_condition is not None:
                lines.append(
                    "- Success condition: "
                    + json.dumps(
                        operation.success_condition.model_dump(mode="json"),
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                )
            if operation.answer_mapping is not None:
                lines.append(
                    f"- Answer mapping: `{operation.answer_mapping.mode}` "
                    f"({len(operation.answer_mapping.fields)} field(s))"
                )
            lines.append("")
        if self.sources:
            lines.extend(["## Sources", ""])
            for source in self.sources:
                lines.append(f"- `{source.reference}` ({source.confidence})")
        return "\n".join(lines).rstrip() + "\n"

    @classmethod
    def from_json(cls, payload: str) -> "Manifest":
        """Parse a JSON document into a validated manifest."""

        try:
            return cls.model_validate_json(payload)
        except ValueError as exc:
            raise ManifestValidationError("invalid manifest JSON") from exc

    def write_files(self, json_path: Path, markdown_path: Path) -> None:
        """Write the paired JSON and Markdown artifacts using UTF-8."""

        json_path.parent.mkdir(parents=True, exist_ok=True)
        markdown_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(self.to_json() + "\n", encoding="utf-8")
        markdown_path.write_text(self.to_markdown(), encoding="utf-8")


_SENSITIVE_KEYS = frozenset(
    {
        "authorization",
        "api_key",
        "apikey",
        "access_token",
        "client_secret",
        "cookie",
        "password",
        "platform_token",
        "proxy_authorization",
        "refresh_token",
        "secret_key",
        "token",
    }
)
_TEMPLATE_PATTERN = re.compile(r"\{\{[^{}]+\}\}")
_AUTHORIZATION_TEMPLATE_PATTERN = re.compile(
    r"[A-Za-z][A-Za-z0-9_-]*\s+\{\{[^{}]+\}\}"
)


def _reject_persisted_secrets(value: object, path: str = "manifest") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_text = str(key).lower().replace("-", "_")
            child_path = f"{path}.{key}"
            if _is_sensitive_key(key_text) and child not in (None, ""):
                if not _is_allowed_secret_template(key_text, child):
                    raise ValueError(
                        f"secret-like value is not allowed at {child_path}"
                    )
            _reject_persisted_secrets(child, child_path)
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _reject_persisted_secrets(child, f"{path}[{index}]")
    elif isinstance(value, str) and path.endswith(".url"):
        _reject_url_secrets(value, path)


def _is_sensitive_key(key: str) -> bool:
    return key in _SENSITIVE_KEYS or key.endswith(
        ("_api_key", "_authorization", "_secret", "_token")
    )


def _is_allowed_secret_template(key: str, value: object) -> bool:
    if not isinstance(value, str):
        return False
    candidate = value.strip()
    if _TEMPLATE_PATTERN.fullmatch(candidate):
        return True
    return (
        key == "authorization" or key.endswith("_authorization")
    ) and bool(_AUTHORIZATION_TEMPLATE_PATTERN.fullmatch(candidate))


def _reject_url_secrets(value: str, path: str) -> None:
    parsed = urlsplit(value)
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"secret-like URL credentials are not allowed at {path}")
    for key, query_value in parse_qsl(parsed.query, keep_blank_values=True):
        key_text = key.lower().replace("-", "_")
        if _is_sensitive_key(key_text) and query_value not in (None, ""):
            if not _is_allowed_secret_template(key_text, query_value):
                raise ValueError(
                    f"secret-like URL query value is not allowed at {path}"
                )
