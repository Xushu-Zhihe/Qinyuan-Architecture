"""Tool Sandbox boundary owned by qinyuan-bat.

The package deliberately contains no model, MCP, or provider imports.  It
accepts only the versioned typed bridge contract and delegates execution to a
replaceable runtime (Docker in production, a fake runtime in tests).
"""

from sandbox.contracts import (
    PROTOCOL_VERSION,
    BridgeProtocolError,
    CommandRequest,
    CommandResult,
    RunContext,
)
from sandbox.service import SandboxService

__all__ = [
    "PROTOCOL_VERSION",
    "BridgeProtocolError",
    "CommandRequest",
    "CommandResult",
    "RunContext",
    "SandboxService",
]
