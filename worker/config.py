import json
import re
from pathlib import Path
from string import Formatter

from worker.models import CoordinatorConfig, HealthCheckConfig, ProviderConfig


ALLOWED_PLACEHOLDERS = {"challenge_dir", "work_dir"}


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


def _parse_provider(raw: object, index: int) -> ProviderConfig:
    if not isinstance(raw, dict):
        raise ConfigError(f"providers[{index}] must be an object")

    name = raw.get("name")
    enabled = raw.get("enabled")
    priority = raw.get("priority")
    command = raw.get("command")
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
    timeout = _positive_number(timeout, f"provider {name!r}.timeout_seconds")
    planning_timeout = raw.get(
        "planning_timeout_seconds",
        min(timeout, 120.0),
    )
    if (
        not isinstance(planning_timeout, (int, float))
        or isinstance(planning_timeout, bool)
        or planning_timeout <= 0
    ):
        raise ConfigError(
            f"provider {name!r}.planning_timeout_seconds must be positive"
        )

    _validate_placeholders(name, command)
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
