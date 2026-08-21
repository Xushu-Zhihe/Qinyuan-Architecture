"""Project-local Skill loading for isolated parser and dispatcher LLMs."""

from __future__ import annotations

from pathlib import Path
from typing import Final


class SkillLoadError(ValueError):
    """Raised when a required automation Skill cannot be loaded."""


PARSER_SKILL: Final[str] = "automation-parser"
DISPATCHER_SKILL: Final[str] = "automation-dispatcher"
_ALLOWED_SKILLS = frozenset({PARSER_SKILL, DISPATCHER_SKILL})


class SkillLoader:
    """Load only the two Skills owned by the new automation runtime."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = (
            root or Path(__file__).resolve().parents[1] / ".agents" / "skills"
        ).resolve()

    def load(self, name: str) -> str:
        if name not in _ALLOWED_SKILLS:
            raise SkillLoadError(f"unsupported automation skill: {name}")
        skill_path = self.root / name / "SKILL.md"
        if not skill_path.is_file():
            raise SkillLoadError(f"automation skill is missing: {skill_path}")
        try:
            content = skill_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise SkillLoadError(f"cannot read automation skill: {skill_path}") from exc
        if not content:
            raise SkillLoadError(f"automation skill is empty: {skill_path}")
        return content

