import json
import tempfile
import unittest
from pathlib import Path

from web_main import create_server_app


class WebMainTests(unittest.TestCase):
    def test_loads_provider_configuration_for_web_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "providers.json"
            config.write_text(
                json.dumps(
                    {
                        "providers": [
                            {
                                "name": "fake",
                                "enabled": True,
                                "priority": 1,
                                "command": ["fake-cli"],
                                "timeout_seconds": 5,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            app = create_server_app(
                data_root=root / "data",
                config_path=config,
            )
            try:
                self.assertEqual(
                    tuple(app.state.projects.provider_configs),
                    ("fake",),
                )
            finally:
                app.state.projects.close()


if __name__ == "__main__":
    unittest.main()
