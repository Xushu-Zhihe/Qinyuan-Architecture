import json
import tempfile
import unittest
from pathlib import Path

from worker.config import ConfigError, load_provider_configs


DEFAULT_PROVIDERS = Path(__file__).resolve().parents[1] / "worker" / "providers.json"


class ProviderConfigTests(unittest.TestCase):
    def write_config(self, directory: str, payload: object) -> Path:
        path = Path(directory) / "providers.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_loads_valid_providers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_config(
                directory,
                {
                    "providers": [
                        {
                            "name": "codex",
                            "enabled": True,
                            "priority": 100,
                            "command": ["codex", "-C", "{work_dir}", "-"],
                            "timeout_seconds": 30,
                        }
                    ]
                },
            )

            providers = load_provider_configs(path)

            self.assertEqual(len(providers), 1)
            self.assertEqual(providers[0].name, "codex")
            self.assertEqual(providers[0].command[-1], "-")
            self.assertEqual(providers[0].planning_timeout_seconds, 30)

    def test_default_agents_run_with_full_vm_permissions(self) -> None:
        providers = {
            provider.name: provider
            for provider in load_provider_configs(DEFAULT_PROVIDERS)
        }

        codex = providers["codex"]
        claude = providers["claude"]
        self.assertIn(
            "--dangerously-bypass-approvals-and-sandbox",
            codex.command,
        )
        self.assertNotIn("workspace-write", codex.command)
        self.assertFalse(
            any("approval_policy" in argument for argument in codex.command)
        )
        self.assertIn("--dangerously-skip-permissions", claude.command)
        self.assertNotIn("dontAsk", claude.command)
        self.assertEqual(codex.planning_timeout_seconds, 120)
        self.assertEqual(claude.planning_timeout_seconds, 120)

    def test_rejects_duplicate_names(self) -> None:
        provider = {
            "name": "codex",
            "enabled": True,
            "priority": 100,
            "command": ["codex"],
            "timeout_seconds": 30,
        }
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_config(directory, {"providers": [provider, provider]})
            with self.assertRaisesRegex(ConfigError, "duplicate provider name"):
                load_provider_configs(path)

    def test_rejects_case_insensitive_duplicate_names(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_config(
                directory,
                {
                    "providers": [
                        {
                            "name": "Codex",
                            "enabled": True,
                            "priority": 100,
                            "command": ["codex"],
                            "timeout_seconds": 30,
                        },
                        {
                            "name": "codex",
                            "enabled": True,
                            "priority": 90,
                            "command": ["codex"],
                            "timeout_seconds": 30,
                        },
                    ]
                },
            )
            with self.assertRaisesRegex(ConfigError, "duplicate provider name"):
                load_provider_configs(path)

    def test_rejects_provider_name_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_config(
                directory,
                {
                    "providers": [
                        {
                            "name": "../escape",
                            "enabled": True,
                            "priority": 100,
                            "command": ["codex"],
                            "timeout_seconds": 30,
                        }
                    ]
                },
            )
            with self.assertRaisesRegex(ConfigError, "safe directory name"):
                load_provider_configs(path)

    def test_rejects_non_positive_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_config(
                directory,
                {
                    "providers": [
                        {
                            "name": "codex",
                            "enabled": True,
                            "priority": 100,
                            "command": ["codex"],
                            "timeout_seconds": 0,
                        }
                    ]
                },
            )
            with self.assertRaisesRegex(ConfigError, "timeout_seconds"):
                load_provider_configs(path)

    def test_rejects_non_positive_planning_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_config(
                directory,
                {
                    "providers": [
                        {
                            "name": "codex",
                            "enabled": True,
                            "priority": 100,
                            "command": ["codex"],
                            "timeout_seconds": 30,
                            "planning_timeout_seconds": 0,
                        }
                    ]
                },
            )
            with self.assertRaisesRegex(
                ConfigError,
                "planning_timeout_seconds",
            ):
                load_provider_configs(path)

    def test_rejects_unknown_command_placeholder(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_config(
                directory,
                {
                    "providers": [
                        {
                            "name": "codex",
                            "enabled": True,
                            "priority": 100,
                            "command": ["codex", "{prompt}"],
                            "timeout_seconds": 30,
                        }
                    ]
                },
            )
            with self.assertRaisesRegex(ConfigError, "unsupported placeholder"):
                load_provider_configs(path)

    def test_smoke_provider_fixture_loads(self) -> None:
        path = Path(__file__).parent / "fixtures" / "smoke_providers.json"

        providers = load_provider_configs(path)

        self.assertEqual(
            [provider.name for provider in providers],
            ["fake-a", "fake-b"],
        )


if __name__ == "__main__":
    unittest.main()
