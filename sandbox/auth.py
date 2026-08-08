from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from sandbox.contracts import BridgeProtocolError, TOOL_PROFILES


class AuthError(PermissionError):
    category = "auth"


class ExpiredToken(AuthError):
    category = "expired"


@dataclass(slots=True)
class TokenLease:
    agent_run_id: str
    tool_profile: str
    deadline: datetime
    token_digest: str
    active: bool = True
    calls: dict[str, object] = field(default_factory=dict)
    command_lock: object = field(default_factory=lambda: __import__("asyncio").Lock(), repr=False)

    def check(self, token: str, now: datetime | None = None) -> None:
        if not self.active:
            raise AuthError("token is revoked")
        if not hmac.compare_digest(self.token_digest, digest_token(token)):
            raise AuthError("invalid token")
        current = now or datetime.now(timezone.utc)
        deadline = self.deadline
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        if current >= deadline:
            self.active = False
            raise ExpiredToken("token expired")
        if self.tool_profile not in TOOL_PROFILES:
            raise AuthError("token profile is invalid")

    def revoke(self) -> None:
        self.active = False


def digest_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class TokenRegistry:
    def __init__(self) -> None:
        self._leases: dict[str, TokenLease] = {}

    def issue(
        self,
        agent_run_id: str,
        tool_profile: str,
        deadline: datetime,
        *,
        token_file: Path | None = None,
    ) -> tuple[TokenLease, str]:
        if tool_profile not in TOOL_PROFILES:
            raise ValueError("unsupported tool profile")
        raw = secrets.token_urlsafe(32)
        lease = TokenLease(agent_run_id, tool_profile, deadline, digest_token(raw))
        self._leases[agent_run_id] = lease
        if token_file is not None:
            token_file.parent.mkdir(parents=True, exist_ok=True)
            token_file.write_text(raw + "\n", encoding="utf-8")
            try:
                import os
                os.chmod(token_file, 0o600)
            except OSError:
                pass
        return lease, raw

    def authenticate(self, token: str) -> TokenLease:
        if not isinstance(token, str) or not token:
            raise AuthError("missing bearer token")
        digest = digest_token(token)
        for lease in tuple(self._leases.values()):
            if hmac.compare_digest(lease.token_digest, digest):
                lease.check(token)
                return lease
        raise AuthError("invalid token")

    def revoke(self, agent_run_id: str) -> bool:
        lease = self._leases.get(agent_run_id)
        if lease is None:
            return False
        lease.revoke()
        return True

    def revoke_all(self) -> None:
        for lease in self._leases.values():
            lease.revoke()

    def remove(self, agent_run_id: str) -> None:
        self._leases.pop(agent_run_id, None)

    def __len__(self) -> int:
        return len(self._leases)
