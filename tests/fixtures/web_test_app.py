import os
import sys
from pathlib import Path

from web.app import create_app as create_web_app
from worker.models import ProviderConfig


ROOT = Path(__file__).resolve().parents[2]
FAKE_AGENT = Path(__file__).resolve().with_name("fake_agent.py")


def create_app():
    data_root = Path(
        os.environ.get(
            "BLACKBOARD_E2E_DATA_ROOT",
            ROOT / "frontend" / "test-results" / "web-data",
        )
    )
    providers = tuple(
        ProviderConfig(
            name=name,
            enabled=True,
            priority=priority,
            command=(sys.executable, "-u", str(FAKE_AGENT), name),
            timeout_seconds=10,
        )
        for priority, name in enumerate(("codex", "claude", "kimi"), start=1)
    )
    return create_web_app(
        data_root=data_root,
        provider_configs=providers,
        frontend_dist=ROOT / "frontend" / "dist",
    )
