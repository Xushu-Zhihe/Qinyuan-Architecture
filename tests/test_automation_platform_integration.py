"""Opt-in integration coverage for a declared, non-production platform."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Mapping
import unittest

from automation.dispatcher import ManifestDispatcher
from automation.http import ManifestOperationExecutor
from automation.manifest import Manifest, Operation
from automation.secrets import RuntimePlatformSecret


class ConfiguredSequenceLLM:
    """Drive the dispatcher deterministically while HTTP uses the real network."""

    def __init__(
        self,
        operation_id: str,
        final_decision: Mapping[str, object],
        variables: Mapping[str, object] | None = None,
    ) -> None:
        self.operation_id = operation_id
        self.final_decision = dict(final_decision)
        self.variables = dict(variables or {})
        self._returned_tool_call = False

    async def complete(
        self,
        messages: object,
        *,
        tools: object = (),
        cancel_event: object | None = None,
    ) -> dict[str, object]:
        del messages, tools, cancel_event
        if self._returned_tool_call:
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(
                                self.final_decision,
                                ensure_ascii=False,
                            ),
                        }
                    }
                ]
            }
        self._returned_tool_call = True
        arguments: dict[str, object] = {"operation_id": self.operation_id}
        if self.variables:
            arguments["variables"] = self.variables
        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": f"integration-{self.operation_id}",
                                "function": {
                                    "name": "execute_operation",
                                    "arguments": json.dumps(arguments),
                                },
                            }
                        ],
                    }
                }
            ]
        }


class RealPlatformIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """Run only when a human explicitly points at an isolated platform."""

    def setUp(self) -> None:
        if os.environ.get("AUTOMATION_PLATFORM_E2E") != "1":
            self.skipTest("set AUTOMATION_PLATFORM_E2E=1 to enable network E2E")
        if os.environ.get("AUTOMATION_PLATFORM_ISOLATED") != "1":
            self.fail(
                "AUTOMATION_PLATFORM_ISOLATED=1 is required; refusing an "
                "unmarked platform"
            )

    async def test_configured_isolated_platform_fetch_and_submit(self) -> None:
        manifest = self._load_manifest()
        fetch_operation_id = self._required("AUTOMATION_FETCH_OPERATION_ID")
        submit_operation_id = self._required("AUTOMATION_SUBMIT_OPERATION_ID")
        token = self._required("AUTOMATION_PLATFORM_TOKEN")
        fetch_decision = self._json_object("AUTOMATION_FETCH_DECISION_JSON")
        workgroup_result = self._json_object("AUTOMATION_WORKGROUP_RESULT_JSON")
        submit_variables = self._optional_json_object(
            "AUTOMATION_SUBMIT_VARIABLES_JSON"
        )

        manifest.validate_for_run()
        fetch_operation = self._find_operation(manifest, fetch_operation_id)
        if (
            fetch_operation.kind not in {"fetch", "current", "next"}
            or fetch_operation.side_effect
        ):
            raise AssertionError(
                "fetch operation must be a declared non-side-effect operation"
            )
        submit_operation = self._find_operation(manifest, submit_operation_id)
        if submit_operation.kind != "submit":
            raise AssertionError("submit operation ID must reference kind=submit")
        secret = RuntimePlatformSecret(token)
        executor = ManifestOperationExecutor(
            manifest,
            secret,
            timeout_seconds=self._timeout_seconds(),
        )
        try:
            fetch_result = await ManifestDispatcher(
                ConfiguredSequenceLLM(fetch_operation_id, fetch_decision),
                manifest,
                executor,
            ).run()
            self.assertIsNotNone(fetch_result.decision)
            self.assertEqual(fetch_result.decision.status, "question")
            self.assertTrue(fetch_result.operation_results)
            self.assertIsNot(
                fetch_result.operation_results[-1].success,
                False,
                "isolated platform fetch explicitly reported failure",
            )

            submit_result = await ManifestDispatcher(
                ConfiguredSequenceLLM(
                    submit_operation_id,
                    {
                        "status": "submitted",
                        "message": "isolated platform accepted the answer",
                        "metadata": {},
                    },
                    variables=submit_variables,
                ),
                manifest,
                executor,
            ).run(
                workgroup_result=workgroup_result,
                platform_state=fetch_result.decision.metadata,
            )
            self.assertIsNotNone(submit_result.decision)
            self.assertEqual(submit_result.decision.status, "submitted")
            self.assertTrue(submit_result.operation_results)
            self.assertTrue(submit_result.operation_results[-1].success)
        finally:
            await executor.close()
            secret.clear()

    @staticmethod
    def _required(name: str) -> str:
        value = os.environ.get(name, "").strip()
        if not value:
            raise AssertionError(f"missing required environment variable: {name}")
        return value

    @classmethod
    def _load_manifest(cls) -> Manifest:
        path = Path(cls._required("AUTOMATION_PLATFORM_MANIFEST")).expanduser()
        if not path.is_absolute():
            path = (Path.cwd() / path).resolve()
        if not path.is_file():
            raise AssertionError(f"manifest file does not exist: {path}")
        return Manifest.from_json(path.read_text(encoding="utf-8"))

    @staticmethod
    def _find_operation(manifest: Manifest, operation_id: str) -> Operation:
        for operation in manifest.operations:
            if operation.id == operation_id:
                return operation
        raise AssertionError(
            f"operation ID is not declared by the manifest: {operation_id}"
        )

    @classmethod
    def _json_object(cls, name: str) -> dict[str, object]:
        value = cls._required(name)
        try:
            payload = json.loads(value)
        except json.JSONDecodeError as exc:
            raise AssertionError(f"{name} must contain valid JSON") from exc
        if not isinstance(payload, dict):
            raise AssertionError(f"{name} must contain a JSON object")
        return {str(key): child for key, child in payload.items()}

    @classmethod
    def _optional_json_object(cls, name: str) -> dict[str, object]:
        value = os.environ.get(name, "").strip()
        if not value:
            return {}
        try:
            payload = json.loads(value)
        except json.JSONDecodeError as exc:
            raise AssertionError(f"{name} must contain valid JSON") from exc
        if not isinstance(payload, dict):
            raise AssertionError(f"{name} must contain a JSON object")
        return {str(key): child for key, child in payload.items()}

    @staticmethod
    def _timeout_seconds() -> float:
        raw = os.environ.get("AUTOMATION_PLATFORM_TIMEOUT_SECONDS", "60")
        try:
            value = float(raw)
        except ValueError as exc:
            raise AssertionError(
                "AUTOMATION_PLATFORM_TIMEOUT_SECONDS must be a number"
            ) from exc
        if value <= 0:
            raise AssertionError(
                "AUTOMATION_PLATFORM_TIMEOUT_SECONDS must be positive"
            )
        return value
