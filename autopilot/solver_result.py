from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from typing import Mapping


@dataclass(frozen=True, slots=True)
class SubmissionPlan:
    result_ref: str
    source_fact_ids: tuple[str, ...]
    content_type: str
    text: str | None = None
    json_value: object | None = None
    named_values: Mapping[str, object] = field(default_factory=dict)
    form_fields: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not self.result_ref.strip() or not self.source_fact_ids:
            raise ValueError("submission plan must reference a Goal result and source Facts")
        if any(not item.strip() for item in self.source_fact_ids):
            raise ValueError("source Fact ids must be non-empty")
        if len(set(self.source_fact_ids)) != len(self.source_fact_ids):
            raise ValueError("source Fact ids must be unique")
        if self.content_type not in {"text", "json", "form"}:
            raise ValueError("content_type must be text, json, or form")
        populated = sum((self.text is not None, self.json_value is not None, bool(self.form_fields)))
        if populated != 1:
            raise ValueError("submission plan must contain exactly one payload representation")
        if self.content_type == "text" and self.text is None:
            raise ValueError("text plan requires text")
        if self.content_type == "json" and self.json_value is None:
            raise ValueError("json plan requires json_value")
        if self.content_type == "form" and not self.form_fields:
            raise ValueError("form plan requires form_fields")
        if len({name for name, _ in self.form_fields}) != len(self.form_fields):
            raise ValueError("form field names must be unique")
        try:
            json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError) as exc:
            raise ValueError("submission plan must be JSON serializable") from exc

    @property
    def fingerprint(self) -> str:
        encoded = json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return "sha256:" + hashlib.sha256(encoded).hexdigest()

    def to_dict(self) -> dict[str, object]:
        return {
            "result_ref": self.result_ref,
            "source_fact_ids": list(self.source_fact_ids),
            "content_type": self.content_type,
            "text": self.text,
            "json_value": self.json_value,
            "named_values": dict(self.named_values),
            "form_fields": [list(item) for item in self.form_fields],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "SubmissionPlan":
        return cls(
            result_ref=str(value.get("result_ref", "")),
            source_fact_ids=tuple(str(item) for item in value.get("source_fact_ids", [])),
            content_type=str(value.get("content_type", "")),
            text=(str(value["text"]) if value.get("text") is not None else None),
            json_value=value.get("json_value"),
            named_values=(value.get("named_values") if isinstance(value.get("named_values"), dict) else {}),
            form_fields=tuple((str(item[0]), str(item[1])) for item in value.get("form_fields", []) if isinstance(item, (list, tuple)) and len(item) == 2),
        )
