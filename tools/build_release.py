from __future__ import annotations

import argparse
import hashlib
import json
import re
import stat
import tomllib
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, Sequence


ROOT_FILES = (
    ".gitignore",
    ".node-version",
    ".python-version",
    "CHANGELOG.md",
    "LICENSE",
    "LICENSE.txt",
    "QinyuanEnvironmentChecker.exe",
    "README.md",
    "VERSION",
    "main.py",
    "pyproject.toml",
    "requirements.lock",
    "requirements.txt",
    "web_main.py",
)
ROOT_DIRECTORIES = (
    ".agents",
    ".github",
    "experiments",
    "frontend",
    "orchestrator",
    "tests",
    "tools",
    "web",
    "worker",
)
FORBIDDEN_PARTS = {
    ".git",
    ".pytest_cache",
    ".superpowers",
    ".venv",
    ".worktrees",
    "__pycache__",
    "dist",
    "node_modules",
    "playwright-report",
    "test-results",
    "web-data",
}
FORBIDDEN_SUFFIXES = {
    ".db",
    ".db-shm",
    ".db-wal",
    ".log",
    ".pyc",
    ".pyo",
    ".sqlite",
    ".sqlite3",
    ".zip",
}
SENSITIVE_FILENAMES = {
    ".env",
    "credentials",
    "credentials.json",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_rsa",
    "secrets.json",
}
SECRET_PATTERNS = (
    ("OpenAI-style API key", re.compile(rb"sk-(?:proj-)?[A-Za-z0-9_-]{20,}")),
    ("Anthropic API key", re.compile(rb"sk-ant-[A-Za-z0-9_-]{20,}")),
    ("AWS access key", re.compile(rb"AKIA[0-9A-Z]{16}")),
    (
        "private key",
        re.compile(
            rb"(?m)^-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----\s*$"
        ),
    ),
)
MANIFEST_NAME = "RELEASE-MANIFEST.json"
MAX_SECRET_SCAN_BYTES = 8 * 1024 * 1024


class ReleasePolicyError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ReleaseFile:
    source: Path
    archive_path: PurePosixPath
    sha256: str
    size: int


def _is_forbidden_path(relative: Path) -> bool:
    lowered = tuple(part.casefold() for part in relative.parts)
    if any(part in FORBIDDEN_PARTS for part in lowered):
        return True
    if "experiments" in lowered and "results" in lowered:
        return True
    if "worker" in lowered and "runs" in lowered:
        return True
    if any(part in {"challenge", "challenges"} for part in lowered):
        return True
    name = relative.name.casefold()
    if name in SENSITIVE_FILENAMES or name.startswith(".env."):
        return True
    return any(name.endswith(suffix) for suffix in FORBIDDEN_SUFFIXES)


def _iter_directory(root: Path, directory: str) -> Iterable[Path]:
    base = root / directory
    if not base.is_dir():
        return ()
    return (path for path in base.rglob("*") if path.is_file())


def candidate_paths(root: Path) -> list[Path]:
    root = root.resolve()
    candidates: list[Path] = []
    for name in ROOT_FILES:
        path = root / name
        if path.is_file():
            candidates.append(path)
    for directory in ROOT_DIRECTORIES:
        candidates.extend(_iter_directory(root, directory))
    result: list[Path] = []
    for path in sorted(set(candidates), key=lambda item: item.as_posix().casefold()):
        relative = path.relative_to(root)
        if path.is_symlink() or _is_forbidden_path(relative):
            continue
        result.append(path)
    return result


def _scan_secret(path: Path, relative: Path) -> None:
    if path.stat().st_size > MAX_SECRET_SCAN_BYTES:
        return
    content = path.read_bytes()
    for label, pattern in SECRET_PATTERNS:
        if pattern.search(content):
            raise ReleasePolicyError(
                f"refusing to package possible {label}: {relative.as_posix()}"
            )


def collect_release_files(root: Path) -> list[ReleaseFile]:
    root = root.resolve()
    files: list[ReleaseFile] = []
    for path in candidate_paths(root):
        relative = path.relative_to(root)
        if _is_forbidden_path(relative):
            raise ReleasePolicyError(
                f"forbidden release path: {relative.as_posix()}"
            )
        _scan_secret(path, relative)
        content = path.read_bytes()
        files.append(
            ReleaseFile(
                source=path,
                archive_path=PurePosixPath(relative.as_posix()),
                sha256=hashlib.sha256(content).hexdigest(),
                size=len(content),
            )
        )
    if not files:
        raise ReleasePolicyError("no release files were found")
    return files


def read_version(root: Path) -> str:
    root = root.resolve()
    version_path = root / "VERSION"
    pyproject_path = root / "pyproject.toml"
    package_path = root / "frontend" / "package.json"
    try:
        version = version_path.read_text(encoding="utf-8").strip()
        pyproject = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
        package = json.loads(package_path.read_text(encoding="utf-8"))
        metadata_versions = {
            "VERSION": version,
            "pyproject.toml": str(pyproject["project"]["version"]),
            "frontend/package.json": str(package["version"]),
        }
    except (OSError, KeyError, TypeError, json.JSONDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ReleasePolicyError(f"cannot read release versions: {exc}") from exc
    if not version or len(set(metadata_versions.values())) != 1:
        details = ", ".join(
            f"{name}={value!r}" for name, value in metadata_versions.items()
        )
        raise ReleasePolicyError(f"release versions do not match: {details}")
    if not re.fullmatch(r"(?:0|[1-9]\d*)(?:\.(?:0|[1-9]\d*)){2}", version):
        raise ReleasePolicyError(f"VERSION is not a stable SemVer value: {version!r}")
    return version


def _manifest_bytes(version: str, files: Sequence[ReleaseFile]) -> bytes:
    manifest = {
        "format": 1,
        "version": version,
        "files": [
            {
                "path": item.archive_path.as_posix(),
                "sha256": item.sha256,
                "size": item.size,
            }
            for item in files
        ],
    }
    return (
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _zip_info(name: str, executable: bool = False) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    mode = 0o755 if executable else 0o644
    info.external_attr = (stat.S_IFREG | mode) << 16
    info.create_system = 3
    return info


def build_release(root: Path, output: Path | None = None) -> Path:
    root = root.resolve()
    version = read_version(root)
    files = collect_release_files(root)
    output = (
        output.resolve()
        if output is not None
        else root / "dist" / f"qinyuan-blackboard-{version}.zip"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    temporary.unlink(missing_ok=True)
    try:
        with zipfile.ZipFile(temporary, "w", allowZip64=True) as archive:
            for item in files:
                executable = item.source.suffix.casefold() in {".ps1", ".sh"}
                archive.writestr(
                    _zip_info(item.archive_path.as_posix(), executable),
                    item.source.read_bytes(),
                )
            archive.writestr(
                _zip_info(MANIFEST_NAME),
                _manifest_bytes(version, files),
            )
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    verify_archive(output)
    return output


def verify_archive(path: Path) -> None:
    try:
        with zipfile.ZipFile(path, "r") as archive:
            names = archive.namelist()
            if len(names) != len(set(names)):
                raise ReleasePolicyError("release archive contains duplicate paths")
            if MANIFEST_NAME not in names:
                raise ReleasePolicyError("release archive has no manifest")
            manifest = json.loads(archive.read(MANIFEST_NAME))
            entries = {item["path"]: item for item in manifest["files"]}
            payload_names = set(names) - {MANIFEST_NAME}
            if set(entries) != payload_names:
                raise ReleasePolicyError("release manifest does not match archive")
            for name in payload_names:
                relative = Path(*PurePosixPath(name).parts)
                if _is_forbidden_path(relative):
                    raise ReleasePolicyError(f"forbidden archive path: {name}")
                content = archive.read(name)
                if hashlib.sha256(content).hexdigest() != entries[name]["sha256"]:
                    raise ReleasePolicyError(f"release hash mismatch: {name}")
    except (OSError, KeyError, TypeError, ValueError, zipfile.BadZipFile) as exc:
        if isinstance(exc, ReleasePolicyError):
            raise
        raise ReleasePolicyError(f"cannot verify release archive: {exc}") from exc


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build and verify a source release without user data or credentials."
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="repository root",
    )
    parser.add_argument("--output", type=Path, help="output ZIP path")
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="validate candidate files without creating an archive",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        version = read_version(args.root)
        files = collect_release_files(args.root)
        if args.check_only:
            print(f"release {version}: {len(files)} files passed policy checks")
            return 0
        output = build_release(args.root, args.output)
        print(output)
        return 0
    except ReleasePolicyError as exc:
        print(f"release error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
