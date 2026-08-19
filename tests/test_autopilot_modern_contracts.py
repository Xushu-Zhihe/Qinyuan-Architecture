from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import httpx

from autopilot.direct_controller import DirectControllerError, DirectLLMController
from autopilot.models import AutoTask, ControllerConfig, PromptPlatformConfig, RuntimeControllerSecret, RuntimePlatformSecret
from autopilot.platform import PromptPlatformAdapter, StructuredPlatformAdapter
from autopilot.platform_models import (
    AuthConfig,
    EndpointConfig,
    FieldMapping,
    OutcomeCondition,
    OutcomeConfig,
    StructuredPlatformConfig,
)


def _endpoint(method: str, path: str, **kwargs) -> EndpointConfig:
    return EndpointConfig(method=method, path=path, **kwargs)


def _platform_config(**kwargs) -> StructuredPlatformConfig:
    values = dict(
        base_url="https://platform.example/api",
        auth=AuthConfig(),
        fetch_task=_endpoint("GET", "/tasks", timeout_seconds=2),
        submit_result=_endpoint("POST", "/tasks/{{external_id}}/submit", body={"value": "{{submission.value}}"}),
        skip_task=_endpoint("POST", "/tasks/{{external_id}}/skip", body={"reason": "{{reason}}"}),
        mapping=FieldMapping(task_list_path="items", cursor_path="next", hints_path="hints"),
        outcomes=OutcomeConfig(
            fetch_finished=OutcomeCondition(path="finished", equals=True),
            fetch_empty=OutcomeCondition(path="empty", equals=True),
            submit_accepted=OutcomeCondition(path="accepted", equals=True),
            submit_rejected=OutcomeCondition(path="accepted", equals=False),
            skip_succeeded=OutcomeCondition(path="skipped", equals=True),
            ),
    )
    values.update(kwargs)
    return StructuredPlatformConfig(**values)


class ModernContractTests(unittest.IsolatedAsyncioTestCase):
    def test_structured_config_rejects_absolute_paths_and_duplicate_auth(self) -> None:
        with self.assertRaises(ValueError):
            _endpoint("GET", "https://other.example/tasks")
        with self.assertRaises(ValueError):
            _platform_config(
                auth=AuthConfig(type="bearer"),
                fetch_task=_endpoint("GET", "/tasks", headers={"Authorization": "Bearer fixed"}),
            )

    async def test_structured_fetch_array_cursor_submit_skip_and_health(self) -> None:
        calls: list[tuple[str, str, object]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append((request.method, request.url.path, request.content))
            if request.url.path.endswith("/tasks"):
                return httpx.Response(
                    200,
                    json={
                        "items": [
                            {"id": "one", "name": "One", "goal": "Goal 1", "origin": "Origin", "hints": ["h"]},
                            {"id": "two", "name": "Two", "goal": "Goal 2", "origin": "Origin"},
                        ],
                        "next": "cursor-2",
                    },
                    request=request,
                )
            if request.url.path.endswith("/current"):
                return httpx.Response(
                    200,
                    json={
                        "items": [
                            {
                                "id": "current",
                                "name": "Current",
                                "goal": "Current goal",
                                "origin": "Current origin",
                                "hints": [],
                            }
                        ]
                    },
                    request=request,
                )
            if request.url.path.endswith("/submit"):
                return httpx.Response(200, json={"accepted": True}, request=request)
            if request.url.path.endswith("/skip"):
                return httpx.Response(200, json={"skipped": True}, request=request)
            if request.url.path.endswith("/health"):
                return httpx.Response(204, request=request)
            return httpx.Response(404, request=request)

        config = _platform_config(
            current_task=_endpoint("GET", "/current"),
            health_check=_endpoint("GET", "/health"),
        )
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        adapter = StructuredPlatformAdapter(config, secret=RuntimePlatformSecret(token="platform-token"), client=client)
        try:
            current = await adapter.inspect_current_task()
            first = await adapter.fetch_task()
            second = await adapter.fetch_task()
            self.assertEqual(current.status, "task")
            self.assertEqual(current.task.external_id, "current")
            self.assertEqual(first.status, "task")
            self.assertEqual(second.status, "task")
            self.assertEqual(first.task.external_id, "one")
            self.assertEqual(second.task.external_id, "two")
            self.assertEqual(adapter._cursor, "cursor-2")
            plan = type("Plan", (), {
                "content_type": "text", "text": "answer", "json_value": None,
                "form_fields": (), "named_values": {},
            })()
            goal_result = type("GoalResult", (), {
                "result_ref": "result:v2:one",
                "fact": type("Fact", (), {"id": "fact-1", "content": "answer"})(),
            })()
            submitted = await adapter.submit_result(first.task, plan, goal_result)
            self.assertEqual(submitted.status, "accepted")
            repeated = await adapter.submit_result(first.task, plan, goal_result)
            self.assertEqual(repeated.status, "fatal_error")
            skipped = await adapter.skip_task(second.task, "not needed")
            self.assertEqual(skipped.status, "skipped")
            health = await adapter.health_check()
            self.assertEqual(health.status, "healthy")
            self.assertTrue(any(path.endswith("/current") for _, path, _ in calls))
            self.assertTrue(any(path.endswith("/health") for _, path, _ in calls))
        finally:
            await adapter.close()
            await client.aclose()

    async def test_task_info_when_incomplete_and_skip_policy(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/tasks"):
                return httpx.Response(200, json={"items": [{"id": "one"}], "next": None}, request=request)
            if request.url.path.endswith("/info"):
                return httpx.Response(400, json={"error": "invalid"}, request=request)
            if request.url.path.endswith("/skip"):
                return httpx.Response(200, json={"skipped": True}, request=request)
            return httpx.Response(404, request=request)

        config = _platform_config(
            task_info=_endpoint("GET", "/tasks/{{external_id}}/info"),
            task_info_mode="when_incomplete",
            task_info_failure_action="skip_task",
        )
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        adapter = StructuredPlatformAdapter(config, client=client)
        try:
            result = await adapter.fetch_task()
            self.assertEqual(result.status, "skip_required")
            self.assertEqual(result.task.external_id, "one")
        finally:
            await adapter.close()
            await client.aclose()

    async def test_task_info_uses_the_configured_task_item_path(self) -> None:
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            if request.url.path.endswith("/next/"):
                return httpx.Response(
                    200,
                    json={
                        "code": 200,
                        "data": {
                            "attempt": {
                                "id": 123,
                                "problem": {
                                    "type_label": "Web",
                                    "content": "fetch content",
                                    "hint": None,
                                },
                            }
                        },
                    },
                    request=request,
                )
            if request.url.path.endswith("/attempt/123/"):
                return httpx.Response(
                    200,
                    json={
                        "code": 200,
                        "data": {
                            "attempt": {
                                "id": 123,
                                "problem": {
                                    "type_label": "Web",
                                    "content": "detail content",
                                    "hint": None,
                                },
                            }
                        },
                    },
                    request=request,
                )
            return httpx.Response(404, request=request)

        config = _platform_config(
            fetch_task=_endpoint("POST", "/skill/agent/arena/next/"),
            task_info=_endpoint(
                "GET",
                "/skill/agent/arena/attempt/{{external_id}}/",
            ),
            mapping=FieldMapping(
                task_list_path="data.attempt",
                external_id_path="id",
                name_path="problem.type_label",
                goal_path="problem.content",
                origin_path="problem.content",
                hints_path="problem.hint",
            ),
        )
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        adapter = StructuredPlatformAdapter(config, client=client)
        try:
            result = await adapter.fetch_task()
            self.assertEqual(result.status, "task")
            self.assertEqual(result.task.external_id, "123")
            self.assertEqual(result.task.name, "Web")
            self.assertEqual(result.task.goal, "detail content")
            self.assertIn("/api/skill/agent/arena/attempt/123/", calls)
        finally:
            await adapter.close()
            await client.aclose()

    async def test_task_info_empty_detail_field_uses_fetch_value(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            content = "fetch content" if request.url.path.endswith("/next/") else ""
            return httpx.Response(
                200,
                json={
                    "data": {
                        "attempt": {
                            "id": 123,
                            "problem": {
                                "type_label": "Web",
                                "content": content,
                                "hint": None,
                            },
                        }
                    }
                },
                request=request,
            )

        config = _platform_config(
            fetch_task=_endpoint("POST", "/skill/agent/arena/next/"),
            task_info=_endpoint(
                "GET",
                "/skill/agent/arena/attempt/{{external_id}}/",
            ),
            mapping=FieldMapping(
                task_list_path="data.attempt",
                external_id_path="id",
                name_path="problem.type_label",
                goal_path="problem.content",
                origin_path="problem.content",
                hints_path="problem.hint",
            ),
        )
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        adapter = StructuredPlatformAdapter(config, client=client)
        try:
            result = await adapter.fetch_task()
            self.assertEqual(result.status, "task")
            self.assertEqual(result.task.goal, "fetch content")
            self.assertEqual(result.task.origin, "fetch content")
        finally:
            await adapter.close()
            await client.aclose()

    async def test_nssctf_attachment_only_task_is_downloaded_for_solver(self) -> None:
        calls: list[tuple[str, str | None]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append((request.url.path, request.headers.get("Authorization")))
            if request.url.path.endswith("/next/") or request.url.path.endswith(
                "/attempt/123/"
            ):
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "attempt": {
                                "id": 123,
                                "problem": {
                                    "type_label": "Reverse",
                                    "content": "",
                                    "hint": None,
                                    "annex": {
                                        "name": "challenge.zip",
                                        "size": 7,
                                        "url": ["/media/challenge.zip"],
                                    },
                                },
                            }
                        }
                    },
                    request=request,
                )
            if request.url.path == "/media/challenge.zip":
                return httpx.Response(200, content=b"archive", request=request)
            return httpx.Response(404, request=request)

        config = _platform_config(
            fetch_task=_endpoint("POST", "/skill/agent/arena/next/"),
            task_info=_endpoint("GET", "/skill/agent/arena/attempt/{{external_id}}/"),
            mapping=FieldMapping(
                task_list_path="data.attempt",
                external_id_path="id",
                name_path="problem.type_label",
                goal_path="problem.content",
                origin_path="problem.content",
                hints_path="problem.hint",
                attachments_path="problem.annex",
            ),
        )
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        adapter = StructuredPlatformAdapter(
            config,
            secret=RuntimePlatformSecret(token="platform-token"),
            client=client,
        )
        with tempfile.TemporaryDirectory() as directory:
            runtime_root = Path(directory) / "session"
            adapter.set_runtime_root(runtime_root)
            try:
                result = await adapter.fetch_task()
                self.assertEqual(result.status, "task")
                self.assertIn("supplied challenge attachment", result.task.goal)
                paths = result.task.raw_metadata["attachment_paths"]
                promoted = adapter.promote_attachments(result.task.external_id, paths)
                challenge_root = adapter.challenge_root(result.task.external_id)
                self.assertEqual(len(promoted), 1)
                self.assertEqual(promoted[0].read_bytes(), b"archive")
                self.assertEqual(promoted[0].parent, challenge_root / "attachments")
                media_calls = [
                    auth for path, auth in calls if path.endswith("challenge.zip")
                ]
                self.assertEqual(media_calls, ["Bearer platform-token"])
            finally:
                await adapter.close()
                await client.aclose()

    async def test_nssctf_origin_uses_container_url_when_present(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "attempt": {
                            "id": 5001,
                            "problem": {
                                "type_label": "Web",
                                "content": "密码是多少呀",
                                "container": {
                                    "url": ["http://target.example:18080"]
                                },
                            },
                        }
                    }
                },
                request=request,
            )

        config = _platform_config(
            fetch_task=_endpoint("POST", "/skill/agent/arena/next/"),
            mapping=FieldMapping(
                task_list_path="data.attempt",
                external_id_path="id",
                name_path="problem.type_label",
                goal_path="problem.content",
                origin_path="problem.content",
                origin_endpoint_path="problem.container.url.0|problem.container.url",
                goal_format="flag_objective",
                hints_path=None,
            ),
        )
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        adapter = StructuredPlatformAdapter(config, client=client)
        try:
            result = await adapter.fetch_task()
            self.assertEqual(result.status, "task")
            self.assertEqual(
                result.task.goal.splitlines(),
                [
                    "\u9898\u76ee\uff1aWeb",
                    "\u9898\u9762\uff1a\u5bc6\u7801\u662f\u591a\u5c11\u5440",
                    "\u76ee\u6807\uff1a\u83b7\u53d6\u8be5\u9898\u76ee\u7684 flag "
                    "\u5e76\u4f5c\u4e3a\u6700\u7ec8\u7b54\u6848\u63d0\u4ea4\u3002",
                ],
            )
            self.assertEqual(result.task.origin, "http://target.example:18080")
        finally:
            await adapter.close()
            await client.aclose()

    async def test_nssctf_uses_question_as_origin_without_endpoint(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "attempt": {
                            "id": 5002,
                            "problem": {
                                "type_label": "Web",
                                "content": "题面内容",
                            },
                        }
                    }
                },
                request=request,
            )

        config = _platform_config(
            fetch_task=_endpoint("POST", "/skill/agent/arena/next/"),
            mapping=FieldMapping(
                task_list_path="data.attempt",
                external_id_path="id",
                name_path="problem.type_label",
                goal_path="problem.content",
                origin_path="problem.content",
                origin_endpoint_path="problem.container.url.0|problem.container.url",
                goal_format="flag_objective",
                hints_path=None,
            ),
        )
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        adapter = StructuredPlatformAdapter(config, client=client)
        try:
            result = await adapter.fetch_task()
            self.assertEqual(result.status, "task")
            self.assertEqual(result.task.origin, "题面内容")
        finally:
            await adapter.close()
            await client.aclose()

    async def test_nssctf_pwn_keeps_endpoint_and_attachment_context(self) -> None:
        config = _platform_config(
            mapping=FieldMapping(
                task_list_path="data.attempt",
                external_id_path="id",
                name_path="problem.type_label",
                goal_path="problem.content",
                origin_path="problem.content",
                origin_endpoint_path="problem.container.url.0|problem.container.url",
                goal_format="flag_objective",
                hints_path=None,
                attachments_path="problem.annex",
            ),
        )
        adapter = StructuredPlatformAdapter(config)
        try:
            task = adapter._map_task(
                {
                    "id": 5003,
                    "problem": {
                        "type_label": "Pwn",
                        "content": "分析服务并获取 flag",
                        "container": {"url": ["nc://target.example:31337"]},
                        "annex": {
                            "name": "pwn.bin",
                            "url": ["/media/pwn.bin"],
                        },
                    },
                }
            )
            self.assertEqual(task.origin, "nc://target.example:31337")
            self.assertEqual(
                task.raw_metadata["_attachments"],
                [{"url": "/media/pwn.bin", "name": "pwn.bin"}],
            )
            self.assertIn("flag", task.goal)
        finally:
            await adapter.close()

    async def test_attachment_retry_does_not_fetch_another_arena_task(self) -> None:
        next_calls = 0
        attachment_calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal next_calls, attachment_calls
            if request.url.path.endswith("/next/"):
                next_calls += 1
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "attempt": {
                                "id": 123,
                                "problem": {
                                    "type_label": "Pwn",
                                    "content": "",
                                    "annex": {"url": ["/media/pwn"]},
                                },
                            }
                        }
                    },
                    request=request,
                )
            attachment_calls += 1
            if attachment_calls == 1:
                return httpx.Response(503, request=request)
            return httpx.Response(200, content=b"binary", request=request)

        config = _platform_config(
            fetch_task=_endpoint("POST", "/skill/agent/arena/next/"),
            mapping=FieldMapping(
                task_list_path="data.attempt",
                external_id_path="id",
                name_path="problem.type_label",
                goal_path="problem.content",
                origin_path="problem.content",
                hints_path=None,
                attachments_path="problem.annex",
            ),
        )
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        adapter = StructuredPlatformAdapter(config, client=client)
        with tempfile.TemporaryDirectory() as directory:
            adapter.set_runtime_root(Path(directory) / "session")
            try:
                first = await adapter.fetch_task()
                second = await adapter.fetch_task()
                self.assertEqual(first.status, "retryable_error")
                self.assertEqual(second.status, "task")
                self.assertEqual(next_calls, 1)
                self.assertEqual(attachment_calls, 2)
            finally:
                await adapter.close()
                await client.aclose()

    async def test_cross_origin_attachment_never_receives_platform_credentials(self) -> None:
        received_headers: dict[str, str] = {}
        received_query: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal received_headers, received_query
            if request.url.host == "platform.example":
                return httpx.Response(
                    200,
                    json={
                        "items": [
                            {
                                "id": "one",
                                "name": "Reverse",
                                "goal": "",
                                "origin": "",
                                "attachments": [
                                    "https://download.example/challenge.bin"
                                ],
                            }
                        ]
                    },
                    request=request,
                )
            received_headers = dict(request.headers)
            received_query = dict(request.url.params)
            return httpx.Response(200, content=b"attachment", request=request)

        config = _platform_config(
            auth=AuthConfig(type="query", parameter_name="token"),
            mapping=FieldMapping(
                task_list_path="items",
                attachments_path="attachments",
                hints_path=None,
            ),
        )
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        adapter = StructuredPlatformAdapter(
            config,
            secret=RuntimePlatformSecret(token="platform-token"),
            client=client,
        )
        with tempfile.TemporaryDirectory() as directory:
            adapter.set_runtime_root(Path(directory) / "session")
            try:
                result = await adapter.fetch_task()
                self.assertEqual(result.status, "task")
                self.assertNotIn("authorization", received_headers)
                self.assertNotIn("token", received_query)
            finally:
                await adapter.close()
                await client.aclose()

    async def test_task_info_mapping_error_reports_fields_when_item_path_is_missing(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/next/"):
                return httpx.Response(
                    200,
                    json={"data": {"attempt": {"id": 123}}},
                    request=request,
                )
            return httpx.Response(
                200,
                json={"data": {"description": "detail shape"}},
                request=request,
            )

        config = _platform_config(
            fetch_task=_endpoint("POST", "/skill/agent/arena/next/"),
            task_info=_endpoint("GET", "/skill/agent/arena/attempt/{{external_id}}/"),
            mapping=FieldMapping(
                task_list_path="data.attempt",
                external_id_path="id",
                name_path="name",
                goal_path="description",
                origin_path="description",
            ),
        )
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        adapter = StructuredPlatformAdapter(config, client=client)
        try:
            result = await adapter.fetch_task()
            self.assertEqual(result.status, "fatal_error")
            self.assertIn("non-empty fields: data.description", result.message)
        finally:
            await adapter.close()
            await client.aclose()

    async def test_structured_duplicate_task_id_and_attachment_shape_fail_visibly(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"items": [{"id": "one", "name": "One", "goal": "Goal", "origin": "Origin"}], "next": None},
                request=request,
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        adapter = StructuredPlatformAdapter(_platform_config(), client=client)
        try:
            first = await adapter.fetch_task()
            second = await adapter.fetch_task()
            self.assertEqual(first.status, "task")
            self.assertEqual(second.status, "fatal_error")
            attachment_config = _platform_config(
                mapping=FieldMapping(task_list_path="items", attachments_path="attachments")
            )
            attachment_client = httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(
                        200,
                        json={"items": [{"id": "one", "name": "One", "goal": "Goal", "origin": "Origin", "attachments": [3]}], "next": None},
                        request=request,
                    )
                )
            )
            attachment_adapter = StructuredPlatformAdapter(attachment_config, client=attachment_client)
            try:
                result = await attachment_adapter.fetch_task()
                self.assertEqual(result.status, "fatal_error")
            finally:
                await attachment_adapter.close()
                await attachment_client.aclose()
        finally:
            await adapter.close()
            await client.aclose()

    async def test_controller_tool_contract_and_status_retry_policy(self) -> None:
        responses = [
            {"choices": [{"message": {"tool_calls": [{"id": "p", "function": {"name": "submit_platform_result", "arguments": '{"frozen_plan_fingerprint":"fp"}'}}]}}]},
            {"choices": [{"message": {"content": [{"type": "text", "text": '{"protocol_version":2,"action":"accepted"}'}]}}]},
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=responses.pop(0), request=request)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        controller = DirectLLMController(
            ControllerConfig("https://controller.example/v1", "model"),
            RuntimeControllerSecret("controller-key"),
            client=client,
        )
        try:
            result = await controller.prompt_phase("submit", "submit", lambda _n, _a: _result("accepted"))
            self.assertEqual(result.status, "accepted")
        finally:
            await controller.close()
            await client.aclose()

        bad_responses = [{"choices": [{"message": {"content": '{"protocol_version":2,"action":"accepted"}'}}]}]
        bad_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=bad_responses.pop(0), request=request)))
        bad_controller = DirectLLMController(ControllerConfig("https://controller.example/v1", "model"), RuntimeControllerSecret("key"), client=bad_client)
        try:
            with self.assertRaises(DirectControllerError):
                await bad_controller.prompt_phase("submit", "submit", lambda _n, _a: _result("accepted"))
        finally:
            await bad_controller.close()
            await bad_client.aclose()

        attempts = 0

        def status_handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            return httpx.Response(401, json={"error": "unauthorized"}, request=request)

        status_client = httpx.AsyncClient(transport=httpx.MockTransport(status_handler))
        status_controller = DirectLLMController(ControllerConfig("https://controller.example/v1", "model"), RuntimeControllerSecret("key"), client=status_client)
        try:
            with self.assertRaisesRegex(DirectControllerError, r"controller transport failed \(controller-[^)]+\): HTTP 401"):
                await status_controller.preflight()
            self.assertEqual(attempts, 1)
        finally:
            await status_controller.close()
            await status_client.aclose()

    async def test_controller_enforces_phase_actions_and_tool_call_ids(self) -> None:
        responses = [{"choices": [{"message": {"content": '{"protocol_version":2,"action":"accepted"}'}}]}]
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=responses.pop(0), request=request)
            )
        )
        controller = DirectLLMController(
            ControllerConfig("https://controller.example/v1", "model"),
            RuntimeControllerSecret("key"),
            client=client,
        )
        try:
            with self.assertRaises(DirectControllerError):
                await controller.prompt_phase("fetch", "fetch", lambda _n, _a: _result("ok"))
        finally:
            await controller.close()
            await client.aclose()

    async def test_prompt_attachment_staging_returns_relative_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(
                        200,
                        content=b"attachment",
                        headers={"Content-Type": "text/plain"},
                        request=request,
                    )
                )
            )
            adapter = PromptPlatformAdapter(PromptPlatformConfig("platform"), client=client)
            runtime_root = Path(directory) / "session"
            adapter.set_runtime_root(runtime_root)
            try:
                result = await adapter.download_attachment(
                    {"url_or_path": "https://platform.example/file", "destination_relative_path": "one/input.txt"}
                )
                self.assertEqual(result["status"], "downloaded")
                self.assertEqual(result["relative_path"], "one/input.txt")
                promoted = adapter.promote_attachments("task-1", ("one/input.txt",))
                self.assertEqual(len(promoted), 1)
                self.assertTrue(promoted[0].is_file())
            finally:
                await adapter.close()
                await client.aclose()

        responses = [{"choices": [{"message": {"tool_calls": [{"function": {"name": "platform_http_request", "arguments": "{}"}}]}}]}]
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=responses.pop(0), request=request)
            )
        )
        controller = DirectLLMController(
            ControllerConfig("https://controller.example/v1", "model"),
            RuntimeControllerSecret("key"),
            client=client,
        )
        try:
            with self.assertRaises(DirectControllerError):
                await controller.prompt_phase("fetch", "fetch", lambda _n, _a: _result("ok"))
        finally:
            await controller.close()
            await client.aclose()


async def _result(status: str) -> dict[str, str]:
    return {"status": status}


if __name__ == "__main__":
    unittest.main()
