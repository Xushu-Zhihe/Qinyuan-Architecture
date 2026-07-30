import re


_SECRET_PATTERN = re.compile(
    r"(?i)\b(api[ _-]?key|access[ _-]?token|refresh[ _-]?token|token|"
    r"authorization|bearer|secret|password)\b\s*[:=]?\s*"
    r"(?:bearer\s+)?[^\s,;]+"
)
_SECRET_VALUE_PATTERN = re.compile(
    r"(?i)\b(?:sk-(?:ant-)?[a-z0-9_-]{8,}|ghp_[a-z0-9]{12,}|"
    r"github_pat_[a-z0-9_]{12,})\b"
)


def redact_text(value: str) -> str:
    labeled = _SECRET_PATTERN.sub(
        lambda match: f"{match.group(1)}=[redacted]",
        value.replace("\x00", ""),
    )
    return _SECRET_VALUE_PATTERN.sub("[redacted]", labeled)


def redact_error(value: str, limit: int = 400) -> str:
    normalized = " ".join(redact_text(value).split())
    if len(normalized) > limit:
        return normalized[: limit - 3] + "..."
    return normalized
