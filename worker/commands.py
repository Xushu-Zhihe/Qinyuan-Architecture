from pathlib import Path

from worker.models import ProviderConfig


def expand_command(
    provider: ProviderConfig,
    challenge_path: str,
    work_path: str,
    *,
    container: bool = False,
    origin_host: str = "",
    agent_owner_id: str = "",
    agent_work_dir: str = "",
) -> tuple[str, ...]:
    template = (
        provider.container_command
        if container and provider.container_command is not None
        else provider.command
    )
    replacements = {
        "{challenge_dir}": challenge_path,
        "{work_dir}": work_path,
        "{origin_host}": origin_host,
        "{agent_owner_id}": agent_owner_id,
        "{agent_work_dir}": agent_work_dir,
    }
    expanded: list[str] = []
    configured_arguments = list(template)
    insertion_index = (
        len(configured_arguments) - 1
        if configured_arguments and configured_arguments[-1] == "-"
        else len(configured_arguments)
    )
    configured_arguments[insertion_index:insertion_index] = provider.protocol_arguments
    for configured in configured_arguments:
        argument = configured
        for placeholder, value in replacements.items():
            argument = argument.replace(placeholder, value)
        expanded.append(argument)
    return tuple(expanded)


def local_command(
    provider: ProviderConfig,
    challenge_dir: Path | None,
    work_dir: Path,
    *,
    origin_host: str | None = None,
    agent_owner_id: str | None = None,
    agent_work_dir: Path | None = None,
) -> tuple[str, ...]:
    return expand_command(
        provider,
        str(challenge_dir.resolve()) if challenge_dir else "",
        str(work_dir.resolve()),
        origin_host=origin_host or "",
        agent_owner_id=agent_owner_id or "",
        agent_work_dir=(
            str(agent_work_dir.resolve()) if agent_work_dir is not None else ""
        ),
    )


def container_command(
    provider: ProviderConfig,
    challenge_mount_path: str,
    work_mount_path: str,
    *,
    has_challenge: bool,
    origin_host: str | None = None,
    agent_owner_id: str | None = None,
    agent_work_dir: str | None = None,
) -> tuple[str, ...]:
    return expand_command(
        provider,
        challenge_mount_path if has_challenge else "",
        work_mount_path,
        container=True,
        origin_host=origin_host or "",
        agent_owner_id=agent_owner_id or "",
        agent_work_dir=agent_work_dir or "",
    )


def lifecycle_release_command(
    provider: ProviderConfig,
    agent_owner_id: str,
    agent_work_dir: Path,
) -> tuple[str, ...]:
    lifecycle = provider.agent_lifecycle
    if lifecycle is None:
        raise ValueError(f"provider {provider.name!r} has no Agent lifecycle")
    replacements = {
        "{agent_owner_id}": agent_owner_id,
        "{agent_work_dir}": str(agent_work_dir.resolve()),
    }
    expanded: list[str] = []
    for configured in lifecycle.release_command:
        argument = configured
        for placeholder, value in replacements.items():
            argument = argument.replace(placeholder, value)
        expanded.append(argument)
    return tuple(expanded)
