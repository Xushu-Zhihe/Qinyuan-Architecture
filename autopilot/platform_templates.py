from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from threading import RLock
from typing import Mapping
from uuid import uuid4


class PlatformTemplateStore:
    """Persist reusable structured-platform configuration without credentials."""

    def __init__(self, data_root: Path) -> None:
        self._path = data_root.resolve() / "autopilot-platform-templates.json"
        self._lock = RLock()

    def list(self) -> list[dict[str, object]]:
        with self._lock:
            return self._read()

    def create(self, name: str, platform: Mapping[str, object]) -> dict[str, object]:
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("template name must be non-empty")
        if len(clean_name) > 120:
            raise ValueError("template name must be at most 120 characters")
        now = _timestamp()
        entry = {
            "id": f"template-{uuid4().hex}",
            "name": clean_name,
            "created_at": now,
            "updated_at": now,
            "platform": _without_token(platform),
        }
        with self._lock:
            templates = self._read()
            if any(str(item.get("name", "")).casefold() == clean_name.casefold() for item in templates):
                raise ValueError(f"template name already exists: {clean_name}")
            templates.append(entry)
            self._write(templates)
        return entry

    def delete(self, template_id: str) -> bool:
        with self._lock:
            templates = self._read()
            remaining = [item for item in templates if item.get("id") != template_id]
            if len(remaining) == len(templates):
                return False
            self._write(remaining)
            return True

    def _read(self) -> list[dict[str, object]]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return []
        if not isinstance(raw, list):
            return []
        templates: list[dict[str, object]] = []
        for item in raw:
            if not isinstance(item, Mapping):
                continue
            entry = dict(item)
            platform = entry.get("platform")
            if isinstance(platform, Mapping):
                entry["platform"] = _without_token(platform)
            templates.append(entry)
        return templates

    def _write(self, templates: list[dict[str, object]]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(templates, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self._path)


def _without_token(platform: Mapping[str, object]) -> dict[str, object]:
    result = json.loads(json.dumps(platform, ensure_ascii=False))
    if isinstance(result, dict):
        auth = result.get("auth")
        if isinstance(auth, dict):
            auth["token"] = ""
    return result


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()
