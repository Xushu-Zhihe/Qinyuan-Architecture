import os
from pathlib import Path

import uvicorn

from web.app import DEFAULT_DATA_ROOT, create_app
from worker.config import (
    load_audit_config,
    load_coordinator_config,
    load_provider_configs,
)


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
    return create_app(
        data_root=resolved_data_root,
        provider_configs=load_provider_configs(resolved_config),
        coordinator_config=load_coordinator_config(resolved_config),
        audit_config=load_audit_config(resolved_config),
    )


if __name__ == "__main__":
    uvicorn.run(
        "web_main:create_server_app",
        factory=True,
        host="127.0.0.1",
        port=8000,
        reload=False,
    )
