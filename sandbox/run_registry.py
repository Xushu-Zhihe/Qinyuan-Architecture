from __future__ import annotations

from datetime import datetime
from pathlib import Path

from sandbox.auth import TokenLease, TokenRegistry


class IssuedToken:
    __slots__ = ("agent_run_id", "tool_profile", "deadline", "token")

    def __init__(self, lease: TokenLease, token: str) -> None:
        self.agent_run_id = lease.agent_run_id
        self.tool_profile = lease.tool_profile
        self.deadline = lease.deadline
        self.token = token

    def __repr__(self) -> str:
        return f"IssuedToken(agent_run_id={self.agent_run_id!r}, tool_profile={self.tool_profile!r}, token=[redacted])"


class RunRegistry:
    """Small ownership registry kept separate from the Blackboard store."""

    def __init__(self) -> None:
        self.tokens = TokenRegistry()

    def register(
        self,
        agent_run_id: str,
        profile: str,
        deadline: datetime,
        *,
        token_file: Path | None = None,
    ) -> IssuedToken:
        lease, token = self.tokens.issue(agent_run_id, profile, deadline, token_file=token_file)
        return IssuedToken(lease, token)

    def revoke(self, agent_run_id: str) -> bool:
        return self.tokens.revoke(agent_run_id)

    def revoke_all(self) -> None:
        self.tokens.revoke_all()
