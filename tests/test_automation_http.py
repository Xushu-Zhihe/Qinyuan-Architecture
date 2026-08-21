import json
from pathlib import Path
import tempfile
import unittest

import httpx

from automation.commands import DispatcherCommand
from automation.http import ManifestOperationExecutor, OperationExecutionError
from automation.manifest import Manifest, Operation, PlatformInfo, SuccessCondition
from automation.secrets import RuntimePlatformSecret


class AutomationHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_declared_method_headers_body_and_success_condition_are_used(
        self,
    ) -> None:
        captured: list[httpx.Request] = []

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
                    url="https://platform.example/submit/{{question_id}}",
                    headers={"Authorization": "Bearer {{platform_token}}"},
                    body={"answer": "{{answer}}"},
                    success_condition=SuccessCondition(
                        kind="json_path_equals", path="accepted", expected=True
                    ),
                ),
            ),
        )
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        secret = RuntimePlatformSecret("platform-secret")
        executor = ManifestOperationExecutor(manifest, secret, client=client)

        result = await executor.execute(
            "submit",
            variables={"question_id": "q-1", "answer": "FLAG{ok}"},
        )

        self.assertEqual(result.success, True)
        self.assertEqual(captured[0].method, "POST")
        self.assertEqual(str(captured[0].url), "https://platform.example/submit/q-1")
        self.assertEqual(captured[0].headers["Authorization"], "Bearer platform-secret")
        self.assertEqual(json.loads(captured[0].content), {"answer": "FLAG{ok}"})
        self.assertNotIn("platform-secret", str(result.summary(executor.redactor)))
        await client.aclose()

    async def test_undeclared_operation_is_rejected(self) -> None:
        manifest = Manifest(
            platform=PlatformInfo(name="Example"),
            operations=(
                Operation(
                    id="fetch",
                    name="Fetch",
                    method="GET",
                    url="https://platform.example/fetch",
                ),
            ),
        )
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200))
        )
        executor = ManifestOperationExecutor(
            manifest,
            RuntimePlatformSecret(),
            client=client,
        )

        with self.assertRaises(OperationExecutionError):
            await executor.execute("guess")
        await client.aclose()

    async def test_attachment_can_be_downloaded_without_upload_support(self) -> None:
        manifest = Manifest(
            platform=PlatformInfo(name="Example"),
            operations=(
                Operation(
                    id="attachment",
                    name="Download",
                    kind="attachment",
                    method="GET",
                    url="https://platform.example/file",
                    response_type="bytes",
                    body_mode="none",
                ),
            ),
        )
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, content=b"data")
            )
        )
        executor = ManifestOperationExecutor(
            manifest,
            RuntimePlatformSecret(),
            client=client,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "attachment.bin"
            result = await executor.execute("attachment", download_to=path)
            self.assertEqual(path.read_bytes(), b"data")
            self.assertIsNone(result.success)
        await client.aclose()

    async def test_exact_template_preserves_json_object_types(self) -> None:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json={"ok": True})

        manifest = Manifest(
            platform=PlatformInfo(name="Example"),
            operations=(
                Operation(
                    id="submit",
                    name="Submit",
                    method="POST",
                    url="https://platform.example/submit",
                    body={"answer": "{{answer}}"},
                    success_condition=SuccessCondition(
                        kind="json_path_equals", path="ok", expected=True
                    ),
                ),
            ),
        )
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        )
        executor = ManifestOperationExecutor(
            manifest, RuntimePlatformSecret(), client=client
        )

        await executor.execute("submit", variables={"answer": {"flag": "x"}})

        self.assertEqual(json.loads(captured[0].content), {"answer": {"flag": "x"}})
        await client.aclose()


class DispatcherCommandTests(unittest.TestCase):
    def test_operation_command_must_belong_to_manifest(self) -> None:
        manifest = Manifest(
            platform=PlatformInfo(name="Example"),
            operations=(
                Operation(
                    id="fetch",
                    name="Fetch",
                    method="GET",
                    url="https://platform.example/fetch",
                ),
            ),
        )
        command = DispatcherCommand(
            source="human",
            command_type="operation",
            operation_id="unknown",
        )

        with self.assertRaises(ValueError):
            command.validate_against(manifest)
