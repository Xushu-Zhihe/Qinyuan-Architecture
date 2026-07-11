import json
import re
from pathlib import Path
from string import Formatter

from worker.models import ProviderConfig


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
    if (
        not isinstance(timeout, (int, float))
        or isinstance(timeout, bool)
        or timeout <= 0
    ):
        raise ConfigError(f"provider {name!r}.timeout_seconds must be positive")

    _validate_placeholders(name, command)
    return ProviderConfig(
        name=name,
        enabled=enabled,
        priority=priority,
        command=tuple(command),
        timeout_seconds=float(timeout),
    )


def load_provider_configs(path: Path) -> tuple[ProviderConfig, ...]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"cannot read provider config: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid JSON in provider config: {path}") from exc

    if not isinstance(payload, dict) or not isinstance(payload.get("providers"), list):
        raise ConfigError("provider config must contain a providers list")

    providers = tuple(
        _parse_provider(raw, index)
        for index, raw in enumerate(payload["providers"])
    )
    names = [provider.name.casefold() for provider in providers]
    if len(names) != len(set(names)):
        raise ConfigError("duplicate provider name in provider config")
    return providers
