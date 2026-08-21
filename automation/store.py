"""Local JSON/Markdown storage for platform manifests."""

from __future__ import annotations

from pathlib import Path
import re

from automation.manifest import Manifest


class ManifestStoreError(RuntimeError):
    """Raised when a manifest artifact cannot be read or written."""


class ManifestStore:
    """Persist JSON as authority and regenerate the paired Markdown document."""

    _ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, manifest: Manifest) -> Manifest:
        try:
            manifest.write_files(
                self._path(manifest.manifest_id, ".json"),
                self._path(manifest.manifest_id, ".md"),
            )
        except OSError as exc:
            raise ManifestStoreError("cannot write manifest artifacts") from exc
        return manifest

    def load(self, manifest_id: str) -> Manifest:
        path = self._path(manifest_id, ".json")
        if not path.is_file():
            raise KeyError(manifest_id)
        try:
            return Manifest.from_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ManifestStoreError(f"cannot read manifest: {manifest_id}") from exc

    def list(self) -> tuple[Manifest, ...]:
        manifests: list[Manifest] = []
        for path in sorted(self.root.glob("*.json")):
            manifests.append(self.load(path.stem))
        return tuple(manifests)

    def _path(self, manifest_id: str, suffix: str) -> Path:
        if not self._ID_PATTERN.fullmatch(manifest_id):
            raise ManifestStoreError("invalid manifest ID")
        return self.root / f"{manifest_id}{suffix}"

