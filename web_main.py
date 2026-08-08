import os
from pathlib import Path

import uvicorn

from web.app import DEFAULT_DATA_ROOT, create_app
from worker.backends import build_execution_backend
from worker.config import (
    load_audit_config,
    load_coordinator_config,
    load_execution_config,
    load_provider_configs,
    load_tool_sandbox_config,
    validate_provider_tool_config,
)
from worker.runtime import ProviderRuntime


ROOT = Path(__file__).resolve().parent
DEFAULT_PROVIDER_CONFIG = ROOT / "worker" / "providers.json"


def create_server_app(
    data_root: Path | None = None,
    config_path: Path | None = None,
):
    resolved_data_root = data_root or Path(
        os.environ.get("BLACKBOARD_DATA_ROOT", DEFAULT_DATA_ROOT)
    )
    resolved_config = config_path or Path(
        os.environ.get("BLACKBOARD_PROVIDER_CONFIG", DEFAULT_PROVIDER_CONFIG)
    )
    provider_configs = load_provider_configs(resolved_config)
    tool_config = load_tool_sandbox_config(resolved_config)
    validate_provider_tool_config(provider_configs, tool_config)
    backend = build_execution_backend(load_execution_config(resolved_config))
    runtime = ProviderRuntime(
        backend,
        tool_config,
        resolved_data_root / "artifacts",
    )
    return create_app(
        data_root=resolved_data_root,
        provider_configs=provider_configs,
        coordinator_config=load_coordinator_config(resolved_config),
        audit_config=load_audit_config(resolved_config),
        execute=runtime.execute,
        runtime=runtime,
    )


def main() -> None:
    uvicorn.run(
        "web_main:create_server_app",
        factory=True,
        host="127.0.0.1",
        port=8000,
        reload=False,
    )


if __name__ == "__main__":
    main()
