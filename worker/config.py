import json
import re
from pathlib import Path
from string import Formatter

from worker.models import (
    AuditConfig,
    CapabilityProbeConfig,
    ContainerBackendConfig,
    CoordinatorConfig,
    ExecutionConfig,
    HealthCheckConfig,
    ProviderConfig,
    ProviderCapabilities,
)
from worker.output_adapters import STREAM_FORMATS


ALLOWED_PLACEHOLDERS = {"challenge_dir", "work_dir", "origin_host"}


class ConfigError(ValueError):
    pass


def _validate_placeholders(provider_name: str, command: list[str]) -> None:
    formatter = Formatter()
    for argument in command:
        try:
            parsed = tuple(formatter.parse(argument))
        except ValueError as exc:
            raise ConfigError(
                f"provider {provider_name!r} has an invalid command placeholder"
            ) from exc
        for _, field_name, _, _ in parsed:
            if field_name and field_name not in ALLOWED_PLACEHOLDERS:
                raise ConfigError(
                    f"provider {provider_name!r} uses unsupported placeholder "
                    f"{field_name!r}"
                )


def _positive_number(value: object, field: str) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or value <= 0
    ):
        raise ConfigError(f"{field} must be positive")
    return float(value)


def _positive_integer(value: object, field: str) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value <= 0
    ):
        raise ConfigError(f"{field} must be a positive integer")
    return value


def _parse_health_check(
    raw: object,
    provider_name: str,
) -> HealthCheckConfig | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigError(
            f"provider {provider_name!r}.health_check must be an object"
        )
    command = raw.get("command")
    if (
        not isinstance(command, list)
        or not command
        or any(not isinstance(argument, str) or not argument for argument in command)
    ):
        raise ConfigError(
            f"provider {provider_name!r}.health_check.command must be a "
            "non-empty list of strings"
        )
    formatter = Formatter()
    for argument in command:
        try:
            parsed = tuple(formatter.parse(argument))
        except ValueError as exc:
            raise ConfigError(
                f"provider {provider_name!r}.health_check.command is invalid"
            ) from exc
        if any(field_name for _, field_name, _, _ in parsed):
            raise ConfigError(
                f"provider {provider_name!r}.health_check.command cannot use "
                "placeholders"
            )

    prefix = f"provider {provider_name!r}.health_check"
    timeout = _positive_number(raw.get("timeout_seconds"), f"{prefix}.timeout_seconds")
    cooldown = _positive_number(
        raw.get("cooldown_seconds"),
        f"{prefix}.cooldown_seconds",
    )
    max_cooldown = _positive_number(
        raw.get("max_cooldown_seconds", max(cooldown, 300.0)),
        f"{prefix}.max_cooldown_seconds",
    )
    if max_cooldown < cooldown:
        raise ConfigError(
            f"{prefix}.max_cooldown_seconds must be at least cooldown_seconds"
        )
    failure_threshold = raw.get("failure_threshold", 3)
    if (
        not isinstance(failure_threshold, int)
        or isinstance(failure_threshold, bool)
        or failure_threshold <= 0
    ):
        raise ConfigError(f"{prefix}.failure_threshold must be a positive integer")
    return HealthCheckConfig(
        command=tuple(command),
        timeout_seconds=timeout,
        cooldown_seconds=cooldown,
        max_cooldown_seconds=max_cooldown,
        failure_threshold=failure_threshold,
    )


def _parse_capability_probe(raw: object, provider_name: str) -> CapabilityProbeConfig | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigError(f"provider {provider_name!r}.capability_probe must be an object")
    command = raw.get("command")
    if not isinstance(command, list) or not command or any(not isinstance(item, str) or not item for item in command):
        raise ConfigError(f"provider {provider_name!r}.capability_probe.command must be a non-empty list of strings")
    _validate_placeholders(provider_name, command)
    return CapabilityProbeConfig(
        command=tuple(command),
        timeout_seconds=_positive_number(raw.get("timeout_seconds"), f"provider {provider_name!r}.capability_probe.timeout_seconds"),
    )


def _protocol_versions(value: object, field: str) -> tuple[int, ...]:
    if not isinstance(value, list) or not value or any(not isinstance(item, int) or isinstance(item, bool) or item < 1 for item in value):
        raise ConfigError(f"{field} must be a non-empty list of positive integers")
    return tuple(dict.fromkeys(value))


def _parse_capabilities(raw: object, provider_name: str) -> ProviderCapabilities:
    if raw is None:
        return ProviderCapabilities()
    if not isinstance(raw, dict):
        raise ConfigError(f"provider {provider_name!r}.capabilities must be an object")
    prefix = f"provider {provider_name!r}.capabilities"
    skill_mode = raw.get("skill_mode", "inline")
    if skill_mode not in {"none", "inline", "required", "tool"}:
        raise ConfigError(f"{prefix}.skill_mode is invalid")
    skills = raw.get("available_skills", [])
    if not isinstance(skills, list) or any(not isinstance(item, str) or not item.strip() for item in skills):
        raise ConfigError(f"{prefix}.available_skills must be a list of non-empty strings")
    validates = raw.get("validates_result_locally", False)
    supports = raw.get("supports_result_fact", False)
    if not isinstance(validates, bool) or not isinstance(supports, bool):
        raise ConfigError(f"{prefix} boolean capability fields must be booleans")
    schema_version = raw.get("schema_version")
    fixture_digest = raw.get("fixture_digest")
    for value, field in ((schema_version, "schema_version"), (fixture_digest, "fixture_digest")):
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ConfigError(f"{prefix}.{field} must be a non-empty string")
    return ProviderCapabilities(
        request_protocol_versions=_protocol_versions(raw.get("request_protocol_versions", [1]), f"{prefix}.request_protocol_versions"),
        result_protocol_versions=_protocol_versions(raw.get("result_protocol_versions", [1]), f"{prefix}.result_protocol_versions"),
        skill_mode=skill_mode,
        available_skills=tuple(item.strip() for item in skills),
        validates_result_locally=validates,
        supports_result_fact=supports,
        schema_version=schema_version,
        fixture_digest=fixture_digest,
    )


def _parse_provider(raw: object, index: int) -> ProviderConfig:
    if not isinstance(raw, dict):
        raise ConfigError(f"providers[{index}] must be an object")

    name = raw.get("name")
    enabled = raw.get("enabled")
    priority = raw.get("priority")
    command = raw.get("command")
    protocol_arguments = raw.get("protocol_arguments", [])
    timeout = raw.get("timeout_seconds")

    if not isinstance(name, str) or not name.strip():
        raise ConfigError(f"providers[{index}].name must be a non-empty string")
    name = name.strip()
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", name) is None:
        raise ConfigError(
            f"provider {name!r}.name must be a safe directory name"
        )
    if not isinstance(enabled, bool):
        raise ConfigError(f"provider {name!r}.enabled must be a boolean")
    if not isinstance(priority, int) or isinstance(priority, bool):
        raise ConfigError(f"provider {name!r}.priority must be an integer")
    if (
        not isinstance(command, list)
        or not command
        or any(not isinstance(argument, str) or not argument for argument in command)
    ):
        raise ConfigError(
            f"provider {name!r}.command must be a non-empty list of strings"
        )
    if not isinstance(protocol_arguments, list) or any(
        not isinstance(argument, str) or not argument
        for argument in protocol_arguments
    ):
        raise ConfigError(
            f"provider {name!r}.protocol_arguments must be a list of strings"
        )
    timeout = _positive_number(timeout, f"provider {name!r}.timeout_seconds")
    planning_timeout = raw.get(
        "planning_timeout_seconds",
        min(timeout, 120.0),
    )
    stream_format = raw.get("stream_format", "text")
    if stream_format not in STREAM_FORMATS:
        raise ConfigError(
            f"provider {name!r}.stream_format must be one of "
            f"{sorted(STREAM_FORMATS)}"
        )
    streaming_supported = raw.get("streaming_supported", True)
    if not isinstance(streaming_supported, bool):
        raise ConfigError(f"provider {name!r}.streaming_supported must be a boolean")
    if (
        not isinstance(planning_timeout, (int, float))
        or isinstance(planning_timeout, bool)
        or planning_timeout <= 0
    ):
        raise ConfigError(
            f"provider {name!r}.planning_timeout_seconds must be positive"
        )

    _validate_placeholders(name, command)
    _validate_placeholders(name, protocol_arguments)
    container_image = raw.get("container_image")
    if container_image is not None and (
        not isinstance(container_image, str) or not container_image.strip()
    ):
        raise ConfigError(
            f"provider {name!r}.container_image must be a non-empty string"
        )
    container_command = raw.get("container_command")
    if container_command is not None:
        if (
            not isinstance(container_command, list)
            or not container_command
            or any(
                not isinstance(argument, str) or not argument
                for argument in container_command
            )
        ):
            raise ConfigError(
                f"provider {name!r}.container_command must be a "
                "non-empty list of strings"
            )
        _validate_placeholders(name, container_command)
    health_check = _parse_health_check(raw.get("health_check"), name)
    max_running = _positive_integer(
        raw.get("max_running", 4),
        f"provider {name!r}.max_running",
    )
    return ProviderConfig(
        name=name,
        enabled=enabled,
        priority=priority,
        command=tuple(command),
        timeout_seconds=timeout,
        planning_timeout_seconds=float(planning_timeout),
        health_check=health_check,
        max_running=max_running,
        container_image=(
            container_image.strip()
            if isinstance(container_image, str)
            else None
        ),
        container_command=(
            tuple(container_command)
            if isinstance(container_command, list)
            else None
        ),
        stream_format=str(stream_format),
        streaming_supported=streaming_supported,
        capabilities=_parse_capabilities(raw.get("capabilities"), name),
        capability_probe=_parse_capability_probe(raw.get("capability_probe"), name),
        protocol_arguments=tuple(protocol_arguments),
    )


def _load_payload(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"cannot read provider config: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid JSON in provider config: {path}") from exc

    if not isinstance(payload, dict) or not isinstance(payload.get("providers"), list):
        raise ConfigError("provider config must contain a providers list")
    return payload


def load_coordinator_config(path: Path) -> CoordinatorConfig:
    payload = _load_payload(path)
    raw = payload.get("coordinator", {})
    if not isinstance(raw, dict):
        raise ConfigError("coordinator must be an object")
    defaults = CoordinatorConfig()
    max_global_running = _positive_integer(
        raw.get("max_global_running", defaults.max_global_running),
        "coordinator.max_global_running",
    )
    max_project_running = _positive_integer(
        raw.get(
            "max_project_running",
            min(defaults.max_project_running, max_global_running),
        ),
        "coordinator.max_project_running",
    )
    max_global_planning = _positive_integer(
        raw.get(
            "max_global_planning",
            min(defaults.max_global_planning, max_global_running),
        ),
        "coordinator.max_global_planning",
    )
    config = CoordinatorConfig(
        max_global_running=max_global_running,
        max_project_running=max_project_running,
        max_global_planning=max_global_planning,
        max_project_planning=_positive_integer(
            raw.get(
                "max_project_planning",
                min(defaults.max_project_planning, max_project_running),
            ),
            "coordinator.max_project_planning",
        ),
        aging_seconds=_positive_number(
            raw.get("aging_seconds", defaults.aging_seconds),
            "coordinator.aging_seconds",
        ),
    )
    if config.max_project_running > config.max_global_running:
        raise ConfigError(
            "coordinator.max_project_running cannot exceed max_global_running"
        )
    if config.max_global_planning > config.max_global_running:
        raise ConfigError(
            "coordinator.max_global_planning cannot exceed max_global_running"
        )
    if config.max_project_planning > config.max_project_running:
        raise ConfigError(
            "coordinator.max_project_planning cannot exceed max_project_running"
        )
    return config


def load_audit_config(path: Path) -> AuditConfig:
    payload = _load_payload(path)
    raw = payload.get("audit", {})
    if not isinstance(raw, dict):
        raise ConfigError("audit must be an object")
    defaults = AuditConfig()
    max_log_page_bytes = _positive_integer(
        raw.get("max_log_page_bytes", defaults.max_log_page_bytes),
        "audit.max_log_page_bytes",
    )
    default_log_page_bytes = _positive_integer(
        raw.get("default_log_page_bytes", defaults.default_log_page_bytes),
        "audit.default_log_page_bytes",
    )
    if default_log_page_bytes > max_log_page_bytes:
        raise ConfigError(
            "audit.default_log_page_bytes cannot exceed max_log_page_bytes"
        )
    return AuditConfig(
        max_log_bytes=_positive_integer(
            raw.get("max_log_bytes", defaults.max_log_bytes),
            "audit.max_log_bytes",
        ),
        max_retained_log_sets=_positive_integer(
            raw.get(
                "max_retained_log_sets",
                defaults.max_retained_log_sets,
            ),
            "audit.max_retained_log_sets",
        ),
        max_retained_exports=_positive_integer(
            raw.get("max_retained_exports", defaults.max_retained_exports),
            "audit.max_retained_exports",
        ),
        default_log_page_bytes=default_log_page_bytes,
        max_log_page_bytes=max_log_page_bytes,
    )


def load_provider_configs(path: Path) -> tuple[ProviderConfig, ...]:
    payload = _load_payload(path)

    providers = tuple(
        _parse_provider(raw, index)
        for index, raw in enumerate(payload["providers"])
    )
    names = [provider.name.casefold() for provider in providers]
    if len(names) != len(set(names)):
        raise ConfigError("duplicate provider name in provider config")
    return providers


def _string_list(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ConfigError(f"{field} must be a list of non-empty strings")
    return tuple(item.strip() for item in value)


def load_execution_config(path: Path) -> ExecutionConfig:
    payload = _load_payload(path)
    raw = payload.get("execution", {"backend": "local"})
    if not isinstance(raw, dict):
        raise ConfigError("execution must be an object")
    backend = raw.get("backend", "local")
    if backend not in {"local", "container"}:
        raise ConfigError("execution.backend must be local or container")
    if backend == "local":
        return ExecutionConfig(backend="local")

    container_raw = raw.get("container")
    if not isinstance(container_raw, dict):
        raise ConfigError("execution.container must be an object")
    defaults = ContainerBackendConfig()
    runtime = _string_list(
        container_raw.get("runtime_command", list(defaults.runtime_command)),
        "execution.container.runtime_command",
    )
    default_image = container_raw.get("default_image")
    if default_image is not None and (
        not isinstance(default_image, str) or not default_image.strip()
    ):
        raise ConfigError(
            "execution.container.default_image must be a non-empty string"
        )
    network = container_raw.get("network", defaults.network)
    if not isinstance(network, str) or not network.strip():
        raise ConfigError("execution.container.network must be a non-empty string")
    capabilities = _string_list(
        container_raw.get("capabilities", []),
        "execution.container.capabilities",
    )
    if any(re.fullmatch(r"[A-Za-z0-9_]+", item) is None for item in capabilities):
        raise ConfigError("execution.container.capabilities contains an unsafe value")
    environment_raw = container_raw.get("environment", {})
    if not isinstance(environment_raw, dict) or any(
        not isinstance(key, str)
        or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) is None
        or not isinstance(value, str)
        for key, value in environment_raw.items()
    ):
        raise ConfigError(
            "execution.container.environment must map environment names to strings"
        )
    environment_allowlist = _string_list(
        container_raw.get("environment_allowlist", []),
        "execution.container.environment_allowlist",
    )
    if any(
        re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) is None
        for name in environment_allowlist
    ):
        raise ConfigError(
            "execution.container.environment_allowlist contains an unsafe name"
        )
    cleanup_policy = container_raw.get(
        "cleanup_policy",
        defaults.cleanup_policy,
    )
    if cleanup_policy not in {"keep", "stop", "remove"}:
        raise ConfigError(
            "execution.container.cleanup_policy must be keep, stop or remove"
        )
    challenge_mount = container_raw.get(
        "challenge_mount_path",
        defaults.challenge_mount_path,
    )
    work_mount = container_raw.get(
        "work_mount_path",
        defaults.work_mount_path,
    )
    for value, field in (
        (challenge_mount, "challenge_mount_path"),
        (work_mount, "work_mount_path"),
    ):
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(
                f"execution.container.{field} must be a non-empty string"
            )
    if str(challenge_mount).casefold() == str(work_mount).casefold():
        raise ConfigError(
            "execution.container challenge and work mount paths must differ"
        )
    read_only_root = container_raw.get(
        "read_only_root",
        defaults.read_only_root,
    )
    if not isinstance(read_only_root, bool):
        raise ConfigError("execution.container.read_only_root must be a boolean")
    memory_limit = container_raw.get("memory_limit")
    if memory_limit is not None and (
        not isinstance(memory_limit, str) or not memory_limit.strip()
    ):
        raise ConfigError(
            "execution.container.memory_limit must be a non-empty string"
        )
    cpu_limit = container_raw.get("cpu_limit")
    if cpu_limit is not None:
        cpu_limit = _positive_number(cpu_limit, "execution.container.cpu_limit")
    pids_limit = container_raw.get("pids_limit")
    if pids_limit is not None:
        pids_limit = _positive_integer(
            pids_limit,
            "execution.container.pids_limit",
        )
    stop_timeout = _positive_number(
        container_raw.get(
            "stop_timeout_seconds",
            defaults.stop_timeout_seconds,
        ),
        "execution.container.stop_timeout_seconds",
    )
    setup_timeout = _positive_number(
        container_raw.get(
            "setup_timeout_seconds",
            defaults.setup_timeout_seconds,
        ),
        "execution.container.setup_timeout_seconds",
    )
    return ExecutionConfig(
        backend="container",
        container=ContainerBackendConfig(
            runtime_command=runtime,
            default_image=(
                default_image.strip()
                if isinstance(default_image, str)
                else None
            ),
            network=network.strip(),
            capabilities=capabilities,
            environment=tuple(sorted(environment_raw.items())),
            environment_allowlist=environment_allowlist,
            cleanup_policy=str(cleanup_policy),
            challenge_mount_path=str(challenge_mount).strip(),
            work_mount_path=str(work_mount).strip(),
            read_only_root=read_only_root,
            memory_limit=(
                memory_limit.strip()
                if isinstance(memory_limit, str)
                else None
            ),
            cpu_limit=float(cpu_limit) if cpu_limit is not None else None,
            pids_limit=int(pids_limit) if pids_limit is not None else None,
            stop_timeout_seconds=stop_timeout,
            setup_timeout_seconds=setup_timeout,
        ),
    )
