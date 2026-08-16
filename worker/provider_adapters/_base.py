from __future__ import annotations

import re
from dataclasses import dataclass


_AUTH_PATTERN = re.compile(
    r"(?i)(?:\b(?:401|403)\s+(?:unauthorized|forbidden)\b|"
    r"\bauthentication\s+(?:failed|required|error)\b|"
    r"\b(?:api|access)\s+key\s+(?:invalid|missing|expired)\b|"
    r"\b(?:login|sign[ -]?in)\s+(?:required|failed)\b|"
    r"认证失败|请(?:先)?登录|未登录)"
)
_DIAGNOSTIC_PREFIX = re.compile(
    r"(?i)^(?:error|fatal|exception|provider(?:\s+error)?)\b"
)


@dataclass(frozen=True, slots=True)
class ProviderPolicy:
    resume_mode: str = "none"
    network_mode: str = "host_unrestricted"
    isolation: str = "unsafe_local"


class ProviderAdapter:
    """Shared provider behavior used by the supervisor."""

    name = "generic"
    policy = ProviderPolicy()

    def is_auth_failure(self, detail: str) -> bool:
        for line in detail.splitlines():
            candidate = line.strip()
            if not candidate or not _DIAGNOSTIC_PREFIX.search(candidate):
                continue
            if _AUTH_PATTERN.search(candidate):
                return True
        return False
