from __future__ import annotations

import unittest

import httpx

from autopilot.direct_controller import DirectControllerError, DirectLLMController
from autopilot.models import AutoTask, ControllerConfig, RuntimeControllerSecret, RuntimePlatformSecret
from autopilot.platform import StructuredPlatformAdapter
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
            if request.url.path.endswith("/submit"):
                return httpx.Response(200, json={"accepted": True}, request=request)
            if request.url.path.endswith("/skip"):
                return httpx.Response(200, json={"skipped": True}, request=request)
            if request.url.path.endswith("/health"):
                return httpx.Response(204, request=request)
            return httpx.Response(404, request=request)

        config = _platform_config(
            health_check=_endpoint("GET", "/health"),
        )
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        adapter = StructuredPlatformAdapter(config, secret=RuntimePlatformSecret(token="platform-token"), client=client)
        try:
            first = await adapter.fetch_task()
            second = await adapter.fetch_task()
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
            with self.assertRaises(DirectControllerError):
                await status_controller.preflight()
            self.assertEqual(attempts, 1)
        finally:
            await status_controller.close()
            await status_client.aclose()


async def _result(status: str) -> dict[str, str]:
    return {"status": status}


if __name__ == "__main__":
    unittest.main()
