from __future__ import annotations

from functools import lru_cache

from ._base import ProviderAdapter, ProviderPolicy
from .claude import ClaudeAdapter
from .codex import CodexAdapter
from .kimi import KimiAdapter
from .q_subagent import QSubagentAdapter
from .qinyuan_agent import QinyuanAgentAdapter


_ADAPTERS = {
    adapter.name: adapter
    for adapter in (
        CodexAdapter(),
        ClaudeAdapter(),
        KimiAdapter(),
        QinyuanAgentAdapter(),
        QSubagentAdapter(),
    )
}


@lru_cache(maxsize=None)
def get_provider_adapter(name: str) -> ProviderAdapter:
    return _ADAPTERS.get(name, ProviderAdapter())


__all__ = [
    "ClaudeAdapter",
    "CodexAdapter",
    "KimiAdapter",
    "ProviderAdapter",
    "ProviderPolicy",
    "QSubagentAdapter",
    "QinyuanAgentAdapter",
    "get_provider_adapter",
]
