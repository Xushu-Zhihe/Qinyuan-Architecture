import json
import unittest

import httpx

from automation.dispatcher import DispatcherError, ManifestDispatcher
from automation.http import ManifestOperationExecutor
from automation.manifest import (
    AnswerField,
    AnswerMapping,
    Manifest,
    Operation,
    PlatformInfo,
    SuccessCondition,
)
from automation.secrets import RuntimePlatformSecret


class SequenceLLM:
    def __init__(self, responses: list[dict[str, object]]) -> None:
        self.responses = responses
        self.messages: list[list[dict[str, object]]] = []

    async def complete(self, messages, *, tools=(), cancel_event=None):
        self.messages.append([dict(message) for message in messages])
        return self.responses.pop(0)


def dispatcher_manifest() -> Manifest:
    return Manifest(
        platform=PlatformInfo(name="Example"),
        operations=(
            Operation(
                id="fetch",
                name="Fetch",
                kind="fetch",
                method="GET",
                url="https://platform.example/question",
                success_condition=SuccessCondition(
                    kind="status_code", status_codes=(200,)
                ),
            ),
        ),
    )


class DispatcherTests(unittest.IsolatedAsyncioTestCase):
    async def test_dispatcher_executes_only_declared_operation_and_returns_final_message(
        self,
    ) -> None:
        llm = SequenceLLM(
            [
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "tool_calls": [
                                    {
                                        "id": "call-1",
                                        "function": {
                                            "name": "execute_operation",
                                            "arguments": json.dumps(
                                                {"operation_id": "fetch"}
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
                        {"message": {"role": "assistant", "content": "题目已获取"}}
                    ]
                },
            ]
        )
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json={"question": "x"})
            )
        )
        secret = RuntimePlatformSecret("platform-secret")
        executor = ManifestOperationExecutor(
            dispatcher_manifest(),
            secret,
            client=client,
        )
        dispatcher = ManifestDispatcher(llm, dispatcher_manifest(), executor)

        result = await dispatcher.run(
            workgroup_result={"answer": "FLAG{x}"},
            human_instruction="请处理 platform-secret",
        )

        self.assertEqual(result.final_message, "题目已获取")
        self.assertEqual(
            [item.operation_id for item in result.operation_results],
            ["fetch"],
        )
        self.assertTrue(any(event.event_type == "operation" for event in result.events))
        serialized_messages = json.dumps(llm.messages, ensure_ascii=False)
        self.assertNotIn("platform-secret", serialized_messages)
        await client.aclose()

    async def test_human_instruction_becomes_explicit_control_command(self) -> None:
        llm = SequenceLLM(
            [
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "tool_calls": [
                                    {
                                        "id": "call-1",
                                        "function": {
                                            "name": "emit_control_command",
                                            "arguments": json.dumps(
                                                {
                                                    "command_type": "abandon_current",
                                                    "reason": "人工要求放弃",
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
                        {"message": {"role": "assistant", "content": "已记录放弃命令"}}
                    ]
                },
            ]
        )
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200))
        )
        executor = ManifestOperationExecutor(
            dispatcher_manifest(),
            RuntimePlatformSecret(),
            client=client,
        )
        dispatcher = ManifestDispatcher(llm, dispatcher_manifest(), executor)

        result = await dispatcher.run(human_instruction="放弃当前题目")

        self.assertEqual(len(result.commands), 1)
        self.assertEqual(result.commands[0].command_type, "abandon_current")
        self.assertEqual(result.commands[0].source, "human")
        self.assertEqual(result.commands[0].original_instruction, "放弃当前题目")
        await client.aclose()

    async def test_submit_uses_declared_mapping_from_goal_result(self) -> None:
        captured: list[httpx.Request] = []
        llm = SequenceLLM(
            [
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "tool_calls": [
                                    {
                                        "id": "call-submit",
                                        "function": {
                                            "name": "execute_operation",
                                            "arguments": json.dumps(
                                                {"operation_id": "submit"}
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
                        {"message": {"role": "assistant", "content": "已提交"}}
                    ]
                },
            ]
        )

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json={"accepted": True})

        manifest = Manifest(
            platform=PlatformInfo(name="Example"),
            operations=(
                Operation(
                    id="submit",
                    name="Submit",
                    kind="submit",
                    method="POST",
                    url="https://platform.example/submit",
                    body="{{answer}}",
                    answer_mapping=AnswerMapping(
                        mode="json",
                        fields=(
                            AnswerField(
                                target="answer", source_path="fact.content"
                            ),
                        ),
                    ),
                    success_condition=SuccessCondition(
                        kind="json_path_equals", path="accepted", expected=True
                    ),
                ),
            ),
        )
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        executor = ManifestOperationExecutor(
            manifest, RuntimePlatformSecret(), client=client
        )
        dispatcher = ManifestDispatcher(llm, manifest, executor)

        result = await dispatcher.run(
            workgroup_result={"fact": {"content": "FLAG{mapped}"}}
        )

        self.assertEqual(json.loads(captured[0].content), {"answer": "FLAG{mapped}"})
        self.assertEqual(result.operation_results[0].success, True)
        self.assertEqual(
            [command.operation_id for command in result.commands], ["submit"]
        )
        await client.aclose()

    async def test_control_command_can_execute_declared_skip_operation(self) -> None:
        captured: list[httpx.Request] = []
        llm = SequenceLLM(
            [
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "tool_calls": [
                                    {
                                        "id": "call-skip",
                                        "function": {
                                            "name": "emit_control_command",
                                            "arguments": json.dumps(
                                                {
                                                    "command_type": "skip_current",
                                                    "operation_id": "skip",
                                                    "reason": "人工要求跳过",
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
                        {"message": {"role": "assistant", "content": "已跳过"}}
                    ]
                },
            ]
        )

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(204)

        manifest = Manifest(
            platform=PlatformInfo(name="Example"),
            operations=(
                Operation(
                    id="skip",
                    name="Skip",
                    kind="skip",
                    method="POST",
                    url="https://platform.example/skip",
                    body_mode="none",
                ),
            ),
        )
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        executor = ManifestOperationExecutor(
            manifest, RuntimePlatformSecret(), client=client
        )
        dispatcher = ManifestDispatcher(llm, manifest, executor)

        result = await dispatcher.run(human_instruction="跳过当前题目")

        self.assertEqual(str(captured[0].url), "https://platform.example/skip")
        self.assertEqual(result.commands[0].command_type, "skip_current")
        self.assertEqual(result.operation_results[0].operation_id, "skip")
        await client.aclose()

    async def test_submit_before_workgroup_result_is_rejected(self) -> None:
        llm = SequenceLLM(
            [
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "tool_calls": [
                                    {
                                        "id": "call-submit",
                                        "function": {
                                            "name": "execute_operation",
                                            "arguments": json.dumps(
                                                {"operation_id": "submit"}
                                            ),
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                }
            ]
        )
        manifest = Manifest(
            platform=PlatformInfo(name="Example"),
            operations=(
                Operation(
                    id="submit",
                    name="Submit",
                    kind="submit",
                    method="POST",
                    url="https://platform.example/submit",
                    body={"answer": "{{answer}}"},
                    success_condition=SuccessCondition(
                        kind="status_code", status_codes=(200,)
                    ),
                ),
            ),
        )
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200))
        )
        executor = ManifestOperationExecutor(
            manifest, RuntimePlatformSecret(), client=client
        )

        with self.assertRaisesRegex(DispatcherError, "workgroup result"):
            await ManifestDispatcher(llm, manifest, executor).run()
        await client.aclose()

    async def test_submitted_status_requires_matched_success_condition(self) -> None:
        llm = SequenceLLM(
            [
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "tool_calls": [
                                    {
                                        "id": "call-submit",
                                        "function": {
                                            "name": "execute_operation",
                                            "arguments": json.dumps(
                                                {"operation_id": "submit"}
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
                                        "message": "submitted",
                                        "metadata": {},
                                    }
                                ),
                            }
                        }
                    ]
                },
            ]
        )
        manifest = Manifest(
            platform=PlatformInfo(name="Example"),
            operations=(
                Operation(
                    id="submit",
                    name="Submit",
                    kind="submit",
                    method="POST",
                    url="https://platform.example/submit",
                    body={"answer": "{{answer}}"},
                    answer_mapping=AnswerMapping(
                        mode="json",
                        fields=(
                            AnswerField(
                                target="answer",
                                source_path="fact.content",
                            ),
                        ),
                    ),
                    success_condition=SuccessCondition(
                        kind="json_path_equals",
                        path="accepted",
                        expected=True,
                    ),
                ),
            ),
        )
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json={"accepted": False})
            )
        )
        executor = ManifestOperationExecutor(
            manifest,
            RuntimePlatformSecret(),
            client=client,
        )

        with self.assertRaisesRegex(DispatcherError, "success condition"):
            await ManifestDispatcher(llm, manifest, executor).run(
                workgroup_result={"fact": {"content": "FLAG{rejected}"}}
            )
        await client.aclose()
