import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from web.app import create_app


class FakeParserLLM:
    def __init__(self, *_args, **_kwargs) -> None:
        self.closed = False

    async def complete_json(self, messages, *, cancel_event=None):
        self.messages = messages
        return {
            "platform": {"name": "Example"},
            "operations": [
                {
                    "id": "fetch",
                    "name": "Fetch question",
                    "kind": "fetch",
                    "method": "GET",
                    "url": "https://platform.example/question",
                }
            ],
        }

    async def close(self) -> None:
        self.closed = True


class AutomationApiTests(unittest.TestCase):
    def test_parse_persists_json_and_markdown_without_api_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch(
            "automation.api.LLMClient", FakeParserLLM
        ):
            root = Path(directory)
            with TestClient(create_app(root)) as client:
                response = client.post(
                    "/api/automation/manifests/parse",
                    json={
                        "source_text": "GET https://platform.example/question",
                        "parser_llm": {
                            "base_url": "https://llm.example/v1",
                            "api_key": "parser-secret",
                            "model": "parser-model",
                        },
                    },
                )

                self.assertEqual(response.status_code, 201, response.text)
                self.assertNotIn("parser-secret", response.text)
                manifest_id = response.json()["manifest_id"]
                json_path = root / "automation" / "manifests" / f"{manifest_id}.json"
                markdown_path = root / "automation" / "manifests" / f"{manifest_id}.md"
                self.assertTrue(json_path.is_file())
                self.assertTrue(markdown_path.is_file())
                self.assertEqual(
                    client.get(f"/api/automation/manifests/{manifest_id}").status_code,
                    200,
                )
                validation = client.post(
                    f"/api/automation/manifests/{manifest_id}/validate"
                )
                self.assertTrue(validation.json()["valid"])
                self.assertNotIn("parser-secret", json_path.read_text(encoding="utf-8"))

    def test_manifest_update_rejects_path_id_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with TestClient(create_app(root)) as client:
                payload = {
                    "manifest_version": 1,
                    "manifest_id": "manifest-a",
                    "platform": {"name": "Example"},
                    "operations": [
                        {
                            "id": "fetch",
                            "name": "Fetch",
                            "method": "GET",
                            "url": "https://platform.example/question",
                        }
                    ],
                }
                response = client.put(
                    "/api/automation/manifests/manifest-b", json=payload
                )
                self.assertEqual(response.status_code, 409, response.text)

