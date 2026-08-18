import json
import sys
import unittest

from worker.capabilities import probe_provider_capabilities, static_capability_snapshot
from worker.models import CapabilityProbeConfig, ProviderCapabilities, ProviderConfig


def _provider(name: str, capabilities: ProviderCapabilities, probe=None) -> ProviderConfig:
    return ProviderConfig(
        name=name,
        enabled=True,
        priority=1,
        command=(name,),
        timeout_seconds=10,
        capabilities=capabilities,
        capability_probe=probe,
    )


class ProviderCapabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_mismatch_is_not_auto_ready(self) -> None:
        capabilities = ProviderCapabilities(
            result_protocol_versions=(1, 2),
            skill_mode="required",
            available_skills=("blackboard-result-protocol",),
            validates_result_locally=True,
            supports_result_fact=True,
            schema_version="2",
            fixture_digest="sha256:canonical",
        )
        runtime = {
            "provider": "dedicated",
            "provider_version": "1.0",
            "request_protocol_versions": [1],
            "result_protocol_versions": [1, 2],
            "skill_mode": "required",
            "available_skills": ["blackboard-result-protocol"],
            "validates_result_locally": True,
            "supports_result_fact": True,
            "schema_version": "2",
            "fixture_digest": "sha256:stale",
        }
        probe = CapabilityProbeConfig(
            command=(sys.executable, "-c", f"print({json.dumps(json.dumps(runtime))})"),
            timeout_seconds=5,
        )
        result = await probe_provider_capabilities(_provider("dedicated", capabilities, probe))
        self.assertFalse(result["auto_ready"])
        self.assertEqual(result["status"], "protocol-mismatch")

    async def test_legacy_and_v2_providers_are_classified_independently(self) -> None:
        ready = ProviderCapabilities(
            result_protocol_versions=(1, 2),
            skill_mode="required",
            available_skills=("blackboard-result-protocol",),
            supports_result_fact=True,
        )
        missing_skill = ProviderCapabilities(
            result_protocol_versions=(1, 2),
            skill_mode="required",
            supports_result_fact=True,
        )
        statuses = {
            provider.name: static_capability_snapshot(provider)["status"]
            for provider in (
                _provider("legacy", ProviderCapabilities()),
                _provider("ready", ready),
                _provider("missing", missing_skill),
            )
        }
        self.assertEqual(
            statuses,
            {"legacy": "legacy-only", "ready": "auto-ready", "missing": "skill-missing"},
        )

