import hashlib
import re
import unicodedata
from datetime import datetime, timezone
from uuid import uuid4


FLAG_EXTRACTOR_VERSION = "flag-scalar-v1"
MAX_RESULT_VALUE_BYTES = 1024


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex}"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_text(content: str) -> str:
    normalized = unicodedata.normalize("NFKC", content)
    return " ".join(normalized.strip().split())


def content_fingerprint(content: str) -> str:
    return hashlib.sha256(normalize_text(content).encode("utf-8")).hexdigest()


def raw_fingerprint(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def canonical_result_value(content: str) -> str:
    """Canonicalize a scalar result without changing its answer semantics."""
    value = content.strip()
    if not value:
        raise ValueError("result value must be non-empty")
    if "\x00" in value or "\r" in value or "\n" in value:
        raise ValueError("result value must be one line without control separators")
    if any(ord(char) < 32 for char in value):
        raise ValueError("result value contains a control character")
    if len(value.encode("utf-8")) > MAX_RESULT_VALUE_BYTES:
        raise ValueError("result value is too long")
    return value


def extract_flag_value(
    content: str,
    *,
    pattern: str | None = None,
) -> str:
    """Extract exactly one deterministic Flag/scalar value from a result Fact."""
    if pattern:
        try:
            compiled = re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"invalid flag pattern: {exc}") from exc
        matches = tuple(dict.fromkeys(match.group(0) for match in compiled.finditer(content)))
        if not matches:
            raise ValueError("result Fact contains no value matching the flag pattern")
        if len(matches) > 1:
            raise ValueError("result Fact contains multiple values matching the flag pattern")
        return canonical_result_value(matches[0])
    return canonical_result_value(content)


def result_fingerprint(value: str, extractor_version: str) -> str:
    material = (
        "goal-result:v1\0"
        + extractor_version.strip()
        + "\0"
        + canonical_result_value(value)
    )
    return raw_fingerprint(material)
