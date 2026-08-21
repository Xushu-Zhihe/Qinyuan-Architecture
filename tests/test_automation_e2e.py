import json
from pathlib import Path
import unittest

import httpx

from automation.dispatcher import ManifestDispatcher
from automation.http import ManifestOperationExecutor
from automation.manifest import Manifest
from automation.secrets import RuntimePlatformSecret


class SequenceLLM:
    def __init__(self, responses: list[dict[str, object]]) -> None:
        self.responses = responses

    async def complete(self, messages, *, tools=(), cancel_event=None):
        return self.responses.pop(0)


class IsolatedCompetitionE2ETests(unittest.IsolatedAsyncioTestCase):
    async def test_fetch_solve_and_submit_against_isolated_platform(self) -> None:
        manifest = Manifest.from_json(
            (
                Path(__file__).resolve().parents[1]
                / "automation"
                / "examples"
                / "manifest.example.json"
            ).read_text(encoding="utf-8")
        )
        requests: list[httpx.Request] = []

        def platform(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            path = request.url.path
            if path.endswith("/questions/next"):
                return httpx.Response(
                    200,
                    json={
                        "id": "q-1",
                        "name": "Isolated question",
                        "goal": "Recover the answer",
                        "origin": "fixture",
                    },
                )
            if path.endswith("/submit"):
                payload = json.loads(request.content)
                return httpx.Response(
                    200,
                    json={"accepted": payload.get("answer") == "FLAG{e2e}"},
                )
            return httpx.Response(404, json={"accepted": False})

        client = httpx.AsyncClient(transport=httpx.MockTransport(platform))
        executor = ManifestOperationExecutor(
            manifest,
            RuntimePlatformSecret("fixture-token"),
            client=client,
        )
        fetch_llm = SequenceLLM(
            [
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "tool_calls": [
                                    {
                                        "id": "fetch-call",
                                        "function": {
                                            "name": "execute_operation",
                                            "arguments": json.dumps(
                                                {"operation_id": "fetch-question"}
                                            ),
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                },
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": json.dumps(
                                    {
                                        "status": "question",
                                        "message": "question fetched",
                                        "question": {
                                            "external_id": "q-1",
                                            "name": "Isolated question",
                                            "goal": "Recover the answer",
                                            "origin": "fixture",
                                            "hints": [],
                                        },
                                        "metadata": {"question_id": "q-1"},
                                    }
                                ),
                            }
                        }
                    ]
                },
            ]
        )
        fetch_result = await ManifestDispatcher(
            fetch_llm, manifest, executor
        ).run()

        self.assertEqual(fetch_result.decision.status, "question")
        submit_llm = SequenceLLM(
            [
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "tool_calls": [
                                    {
                                        "id": "submit-call",
                                        "function": {
                                            "name": "execute_operation",
                                            "arguments": json.dumps(
                                                {
                                                    "operation_id": "submit-answer",
                                                    "variables": {"question_id": "q-1"},
                                                }
                                            ),
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                },
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": json.dumps(
                                    {
                                        "status": "submitted",
                                        "message": "accepted by fixture",
                                        "metadata": {},
                                    }
                                ),
                            }
                        }
                    ]
                },
            ]
        )
        submit_result = await ManifestDispatcher(
            submit_llm, manifest, executor
        ).run(
            workgroup_result={"fact": {"content": "FLAG{e2e}"}},
            platform_state=fetch_result.decision.metadata,
        )

        self.assertEqual(submit_result.decision.status, "submitted")
        self.assertTrue(submit_result.operation_results[-1].success)
        self.assertEqual(json.loads(requests[-1].content), {"answer": "FLAG{e2e}"})
        serialized_events = json.dumps(
            [event.model_dump(mode="json") for event in submit_result.events]
        )
        self.assertNotIn("fixture-token", serialized_events)
        await client.aclose()
