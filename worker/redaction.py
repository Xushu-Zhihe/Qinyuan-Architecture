import json
import re


_SECRET_PATTERN = re.compile(
    r"(?i)\b(api[ _-]?key|access[ _-]?token|refresh[ _-]?token|token|"
    r"authorization|bearer|secret|password)\b(\s*[:=]\s*)"
    r"(?P<bearer>bearer\s+)?"
    r"(?:(?P<quote>[\"'])(?P<quoted>[^\"'\r\n]*)(?P=quote)|"
    r"(?P<bare>[^\s,;\"']+))"
)
_PROTOCOL_LINE_PATTERN = re.compile(
    r"^(?P<prefix>\s*BLACKBOARD_(?:RESULT|INTENT):\s*)"
    r"(?P<body>\{.*\})(?P<newline>\r?\n?)$"
)
_SECRET_KEY_PATTERN = re.compile(
    r"(?i)^(?:api[ _-]?key|access[ _-]?token|refresh[ _-]?token|token|"
    r"authorization|bearer|secret|password)$"
)
_SECRET_VALUE_PATTERN = re.compile(
    r"(?i)\b(?:sk-(?:ant-)?[a-z0-9_-]{8,}|ghp_[a-z0-9]{12,}|"
    r"github_pat_[a-z0-9_]{12,})\b"
)


def _redact_plain_text(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        quote = match.group("quote") or ""
        bearer = match.group("bearer") or ""
        return f"{match.group(1)}{match.group(2)}{bearer}{quote}[redacted]{quote}"

    labeled = _SECRET_PATTERN.sub(replace, value.replace("\x00", ""))
    return _SECRET_VALUE_PATTERN.sub("[redacted]", labeled)


def _redact_protocol_line(value: str) -> str | None:
    match = _PROTOCOL_LINE_PATTERN.match(value)
    if match is None:
        return None
    try:
        payload = json.loads(match.group("body"))
    except json.JSONDecodeError:
        return None

    def redact_value(item: object) -> object:
        if isinstance(item, str):
            return _redact_plain_text(item)
        if isinstance(item, dict):
            return {
                key: (
                    "[redacted]"
                    if _SECRET_KEY_PATTERN.fullmatch(str(key))
                    else redact_value(child)
                )
                for key, child in item.items()
            }
        if isinstance(item, list):
            return [redact_value(child) for child in item]
        return item

    redacted = json.dumps(
        redact_value(payload),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"{match.group('prefix')}{redacted}{match.group('newline')}"


def redact_text(value: str) -> str:
    value = value.replace("\x00", "")
    lines = value.splitlines(keepends=True)
    return "".join(
        redacted if (redacted := _redact_protocol_line(line)) is not None
        else _redact_plain_text(line)
        for line in lines
    )


def redact_error(value: str, limit: int = 400) -> str:
    normalized = " ".join(redact_text(value).split())
    if len(normalized) > limit:
        return normalized[: limit - 3] + "..."
    return normalized
