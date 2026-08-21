"""Offline parser LLM orchestration."""

from __future__ import annotations

import asyncio
from typing import Mapping

from automation.llm import LLMClient, LLMClientError
from automation.manifest import Manifest
from automation.skills import PARSER_SKILL, SkillLoader


class ParserError(RuntimeError):
    """Raised when the parser cannot produce a valid Manifest document."""


class OfflineManifestParser:
    """Parse pasted platform documentation without platform tools or network access."""

    def __init__(
        self,
        client: LLMClient,
        *,
        skill_loader: SkillLoader | None = None,
    ) -> None:
        self.client = client
        self.skill_loader = skill_loader or SkillLoader()

    async def parse(
        self,
        source_text: str,
        *,
        cancel_event: asyncio.Event | None = None,
    ) -> Manifest:
        source = source_text.strip()
        if not source:
            raise ParserError("parser input must be non-empty")
        skill = self.skill_loader.load(PARSER_SKILL)
        messages: tuple[Mapping[str, object], ...] = (
            {"role": "system", "content": skill},
            {"role": "user", "content": source},
        )
        try:
            payload = await self.client.complete_json(
                messages,
                cancel_event=cancel_event,
            )
            manifest_payload = payload.get("manifest", payload)
            return Manifest.model_validate(manifest_payload)
        except (LLMClientError, ValueError) as exc:
            raise ParserError("parser LLM returned an invalid manifest") from exc
