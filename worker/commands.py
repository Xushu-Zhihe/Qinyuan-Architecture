from pathlib import Path

from worker.models import ProviderConfig


def expand_command(
    provider: ProviderConfig,
    challenge_path: str,
    work_path: str,
    *,
    container: bool = False,
    origin_host: str = "",
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
    }
    expanded: list[str] = []
    for configured in template:
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
) -> tuple[str, ...]:
    return expand_command(
        provider,
        str(challenge_dir.resolve()) if challenge_dir else "",
        str(work_dir.resolve()),
        origin_host=origin_host or "",
    )


def container_command(
    provider: ProviderConfig,
    challenge_mount_path: str,
    work_mount_path: str,
    *,
    has_challenge: bool,
    origin_host: str | None = None,
) -> tuple[str, ...]:
    return expand_command(
        provider,
        challenge_mount_path if has_challenge else "",
        work_mount_path,
        container=True,
        origin_host=origin_host or "",
    )
