from __future__ import annotations

import asyncio
import json
from dataclasses import asdict

from worker.models import ProviderConfig


PROBE_FIELDS = {
    "provider", "provider_version", "request_protocol_versions",
    "result_protocol_versions", "skill_mode", "available_skills",
    "validates_result_locally", "supports_result_fact", "schema_version",
    "fixture_digest",
}


def static_capability_snapshot(provider: ProviderConfig) -> dict[str, object]:
    payload = asdict(provider.capabilities)
    payload["auto_ready"] = (
        2 in provider.capabilities.result_protocol_versions
        and provider.capabilities.supports_result_fact
        and (
            provider.capabilities.skill_mode != "required"
            or "blackboard-result-protocol" in provider.capabilities.available_skills
        )
    )
    payload["status"] = "auto-ready" if payload["auto_ready"] else "legacy-only"
    return payload


async def probe_provider_capabilities(provider: ProviderConfig) -> dict[str, object]:
    static = static_capability_snapshot(provider)
    probe = provider.capability_probe
    if probe is None:
        return {"provider": provider.name, **static, "source": "static"}
    try:
        process = await asyncio.create_subprocess_exec(
            *probe.command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=probe.timeout_seconds,
        )
    except (OSError, asyncio.TimeoutError) as exc:
        return {"provider": provider.name, **static, "auto_ready": False, "status": "probe-failed", "error": str(exc)}
    if process.returncode != 0:
        return {"provider": provider.name, **static, "auto_ready": False, "status": "probe-failed", "error": stderr.decode("utf-8", "replace")[:500]}
    try:
        runtime = json.loads(stdout.decode("utf-8", "strict"))
        if not isinstance(runtime, dict):
            raise ValueError("capability probe must return an object")
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        return {"provider": provider.name, **static, "auto_ready": False, "status": "protocol-mismatch", "error": str(exc)}
    compared = {
        "request_protocol_versions": list(provider.capabilities.request_protocol_versions),
        "result_protocol_versions": list(provider.capabilities.result_protocol_versions),
        "skill_mode": provider.capabilities.skill_mode,
        "available_skills": list(provider.capabilities.available_skills),
        "validates_result_locally": provider.capabilities.validates_result_locally,
        "supports_result_fact": provider.capabilities.supports_result_fact,
        "schema_version": provider.capabilities.schema_version,
        "fixture_digest": provider.capabilities.fixture_digest,
    }
    mismatches = [key for key, expected in compared.items() if runtime.get(key) != expected]
    ready = not mismatches and runtime.get("provider") == provider.name
    return {
        "provider": provider.name,
        **static,
        "auto_ready": ready,
        "status": "auto-ready" if ready else "protocol-mismatch",
        "source": "probe",
        "provider_version": runtime.get("provider_version", "unknown"),
        "mismatches": mismatches,
    }
