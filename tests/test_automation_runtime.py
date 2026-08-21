import asyncio
import json
from pathlib import Path
import tempfile
import unittest

import httpx

from automation.commands import DispatcherEvent
from automation.llm import LLMClient, LLMConfig, RuntimeLLMSecret
from automation.manager import (
    AutomationManager,
    _AutomationRun,
    _claim_question_attachments,
)
from automation.manifest import Manifest, Operation, PlatformInfo
from automation.parser import OfflineManifestParser
from automation.secrets import RuntimePlatformSecret
from automation.skills import DISPATCHER_SKILL, PARSER_SKILL, SkillLoader


class AutomationRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_attachment_staging_is_claimed_by_one_question(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pending = root / "attachments" / "_pending"
            (pending / "download").mkdir(parents=True)
            (pending / "download" / "file.bin").write_bytes(b"data")

            target = _claim_question_attachments(
                pending,
                root / "attachments",
                1,
                "question/one",
            )

            self.assertFalse(pending.exists())
            self.assertEqual((target / "download" / "file.bin").read_bytes(), b"data")
            self.assertNotIn("/", target.name)

    async def test_events_are_persisted_redacted_and_without_prompt_snapshots(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = AutomationManager.__new__(AutomationManager)
            run = _AutomationRun(
                run_id="automation-test",
                manifest=Manifest(
                    platform=PlatformInfo(name="Example"),
                    operations=(
                        Operation(
                            id="fetch",
                            name="Fetch",
                            method="GET",
                            url="https://platform.example/question",
                        ),
                    ),
                ),
                max_tasks=1,
                task_timeout_seconds=10,
                poll_interval_seconds=1,
                started_at="now",
                llm_secret=RuntimeLLMSecret("llm-secret"),
                platform_secret=RuntimePlatformSecret("platform-secret"),
                event_log_path=root / "events.jsonl",
            )

            await manager._append_event(
                run,
                DispatcherEvent(
                    event_type="operation",
                    operation_id="fetch",
                    status="succeeded",
                    detail={
                        "url": "https://platform.example/?token=platform-secret",
                        "prompt": "do not persist this prompt",
                    },
                ),
            )

            serialized = (root / "events.jsonl").read_text(encoding="utf-8")
            self.assertNotIn("platform-secret", serialized)
            self.assertIn("[redacted]", serialized)
            self.assertNotIn("do not persist this prompt", serialized)

    async def test_parser_loads_only_parser_skill_and_returns_manifest(self) -> None:
        captured: list[dict[str, object]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            captured.append(payload)
            manifest = {
                "platform": {"name": "Example"},
                "operations": [
                    {
                        "id": "fetch",
                        "name": "Fetch",
                        "kind": "fetch",
                        "method": "GET",
                        "url": "https://platform.example/question",
                    }
                ],
            }
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"content": json.dumps(manifest)}}
                    ]
                },
            )

        transport = httpx.MockTransport(handler)
        http_client = httpx.AsyncClient(transport=transport)
        secret = RuntimeLLMSecret("llm-secret")
        llm = LLMClient(
            LLMConfig("https://llm.example/v1", "parser-model"),
            secret,
            client=http_client,
        )
        parser = OfflineManifestParser(llm)

        manifest = await parser.parse("GET https://platform.example/question")

        self.assertEqual(manifest.platform.name, "Example")
        self.assertEqual(len(captured), 1)
        self.assertIn(PARSER_SKILL, captured[0]["messages"][0]["content"])
        self.assertNotIn(DISPATCHER_SKILL, captured[0]["messages"][0]["content"])
        self.assertEqual(
            captured[0]["messages"][1]["content"],
            "GET https://platform.example/question",
        )
        await http_client.aclose()

    async def test_llm_secret_is_redacted_and_cancelled_request_does_not_return(
        self,
    ) -> None:
        secret = RuntimeLLMSecret("do-not-print")
        self.assertNotIn("do-not-print", repr(secret))
        secret.clear()
        self.assertEqual(secret.api_key, "")

        cancel_event = asyncio.Event()
        cancel_event.set()

        async def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        llm = LLMClient(
            LLMConfig("https://llm.example/v1", "model"),
            RuntimeLLMSecret("key"),
            client=client,
        )
        with self.assertRaises(asyncio.CancelledError):
            await llm.complete(
                ({"role": "user", "content": "x"},),
                cancel_event=cancel_event,
            )
        await client.aclose()


class SkillLoaderTests(unittest.TestCase):
    def test_project_skills_are_present_and_isolated_by_name(self) -> None:
        loader = SkillLoader()

        parser_skill = loader.load(PARSER_SKILL)
        dispatcher_skill = loader.load(DISPATCHER_SKILL)

        self.assertIn("offline platform-contract parser", parser_skill)
        self.assertIn("platform dispatcher", dispatcher_skill)
        self.assertNotEqual(parser_skill, dispatcher_skill)
