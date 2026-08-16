from ._base import ProviderAdapter, ProviderPolicy


class QSubagentAdapter(ProviderAdapter):
    name = "q-subagent"
    policy = ProviderPolicy(
        resume_mode="batch_auto_once",
        network_mode="mcp_authorized_only",
        isolation="mcp_host_allowed",
    )
