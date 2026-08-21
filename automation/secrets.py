"""Runtime platform secret and redaction helpers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


class RuntimePlatformSecret:
    """Mutable platform token that is never serialized as part of a run."""

    __slots__ = ("token",)

    def __init__(self, token: str = "") -> None:
        self.token = token

    def __repr__(self) -> str:
        return "RuntimePlatformSecret(token='[redacted]')"

    __str__ = __repr__

    def clear(self) -> None:
        self.token = ""


class SecretRedactor:
    """Remove known runtime secrets from event and error payloads."""

    def __init__(self, *secrets: str) -> None:
        self._secrets = tuple(secret for secret in secrets if secret)

    def text(self, value: str) -> str:
        redacted = value
        for secret in self._secrets:
            redacted = redacted.replace(secret, "[redacted]")
        return redacted

    def value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, Mapping):
            return {str(key): self.value(child) for key, child in value.items()}
        if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
            return [self.value(child) for child in value]
        return value

