import json
import tempfile
import unittest
from pathlib import Path

from worker.config import (
    ConfigError,
    load_audit_config,
    load_coordinator_config,
    load_provider_configs,
)


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
            self.assertIsNone(providers[0].health_check)

    def test_loads_optional_health_check(self) -> None:
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
                            "health_check": {
                                "command": ["codex", "--version"],
                                "timeout_seconds": 5,
                                "cooldown_seconds": 2,
                                "max_cooldown_seconds": 20,
                                "failure_threshold": 4,
                            },
                        }
                    ]
                },
            )

            provider = load_provider_configs(path)[0]

            self.assertIsNotNone(provider.health_check)
            assert provider.health_check is not None
            self.assertEqual(provider.health_check.command, ("codex", "--version"))
            self.assertEqual(provider.health_check.failure_threshold, 4)

    def test_loads_coordinator_and_provider_limits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_config(
                directory,
                {
                    "coordinator": {
                        "max_global_running": 8,
                        "max_project_running": 3,
                        "max_global_planning": 2,
                        "max_project_planning": 1,
                        "aging_seconds": 5,
                    },
                    "providers": [
                        {
                            "name": "codex",
                            "enabled": True,
                            "priority": 100,
                            "max_running": 2,
                            "command": ["codex"],
                            "timeout_seconds": 30,
                        }
                    ],
                },
            )

            coordinator = load_coordinator_config(path)
            provider = load_provider_configs(path)[0]

            self.assertEqual(coordinator.max_global_running, 8)
            self.assertEqual(coordinator.max_project_running, 3)
            self.assertEqual(coordinator.max_global_planning, 2)
            self.assertEqual(coordinator.max_project_planning, 1)
            self.assertEqual(coordinator.aging_seconds, 5)
            self.assertEqual(provider.max_running, 2)

    def test_loads_audit_limits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_config(
                directory,
                {
                    "audit": {
                        "max_log_bytes": 1024,
                        "max_retained_log_sets": 10,
                        "max_retained_exports": 2,
                        "default_log_page_bytes": 128,
                        "max_log_page_bytes": 512,
                    },
                    "providers": [],
                },
            )

            audit = load_audit_config(path)

            self.assertEqual(audit.max_log_bytes, 1024)
            self.assertEqual(audit.max_retained_log_sets, 10)
            self.assertEqual(audit.max_retained_exports, 2)
            self.assertEqual(audit.default_log_page_bytes, 128)
            self.assertEqual(audit.max_log_page_bytes, 512)

    def test_rejects_audit_default_page_above_maximum(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_config(
                directory,
                {
                    "audit": {
                        "default_log_page_bytes": 513,
                        "max_log_page_bytes": 512,
                    },
                    "providers": [],
                },
            )

            with self.assertRaisesRegex(ConfigError, "cannot exceed"):
                load_audit_config(path)

    def test_rejects_invalid_coordinator_limit_relationships(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_config(
                directory,
                {
                    "coordinator": {
                        "max_global_running": 2,
                        "max_project_running": 3,
                    },
                    "providers": [],
                },
            )

            with self.assertRaisesRegex(ConfigError, "cannot exceed"):
                load_coordinator_config(path)

    def test_smaller_global_limit_bounds_omitted_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_config(
                directory,
                {
                    "coordinator": {"max_global_running": 2},
                    "providers": [],
                },
            )

            coordinator = load_coordinator_config(path)

            self.assertEqual(coordinator.max_global_running, 2)
            self.assertEqual(coordinator.max_project_running, 2)
            self.assertEqual(coordinator.max_global_planning, 2)
            self.assertEqual(coordinator.max_project_planning, 2)

    def test_rejects_non_positive_provider_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_config(
                directory,
                {
                    "providers": [
                        {
                            "name": "codex",
                            "enabled": True,
                            "priority": 100,
                            "max_running": 0,
                            "command": ["codex"],
                            "timeout_seconds": 30,
                        }
                    ]
                },
            )

            with self.assertRaisesRegex(ConfigError, "max_running"):
                load_provider_configs(path)

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
        self.assertTrue(all(provider.health_check for provider in providers.values()))

    def test_rejects_invalid_health_check_cooldown_range(self) -> None:
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
                            "health_check": {
                                "command": ["codex", "--version"],
                                "timeout_seconds": 5,
                                "cooldown_seconds": 10,
                                "max_cooldown_seconds": 5,
                            },
                        }
                    ]
                },
            )

            with self.assertRaisesRegex(ConfigError, "at least cooldown_seconds"):
                load_provider_configs(path)

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
