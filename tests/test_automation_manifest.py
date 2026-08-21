import unittest
from pathlib import Path

from automation.manifest import (
    AnswerField,
    AnswerMapping,
    Manifest,
    ManifestValidationError,
    Operation,
    PlatformInfo,
    SuccessCondition,
)


def manifest_with_submit(*, condition: SuccessCondition | None) -> Manifest:
    return Manifest(
        platform=PlatformInfo(
            name="Example Platform",
            base_url="https://platform.example/api",
        ),
        operations=(
            Operation(
                id="fetch-question",
                name="Fetch question",
                kind="fetch",
                method="GET",
                url="https://platform.example/api/questions/current",
                headers={"Authorization": "Bearer {{platform_token}}"},
            ),
            Operation(
                id="submit-answer",
                name="Submit answer",
                kind="submit",
                method="POST",
                url="https://platform.example/api/questions/current/submit",
                body={"answer": "{{answer}}"},
                success_condition=condition,
                answer_mapping=AnswerMapping(
                    mode="json",
                    fields=(AnswerField(target="answer", source_path="answer"),),
                ),
            ),
        ),
    )


class ManifestTests(unittest.TestCase):
    def test_documented_example_is_a_valid_runnable_manifest(self) -> None:
        path = (
            Path(__file__).resolve().parents[1]
            / "automation"
            / "examples"
            / "manifest.example.json"
        )

        manifest = Manifest.from_json(path.read_text(encoding="utf-8"))
        markdown_path = path.with_suffix(".md")

        manifest.validate_for_run()
        self.assertEqual(
            manifest.to_markdown(),
            markdown_path.read_text(encoding="utf-8"),
        )

    def test_json_markdown_and_hash_are_available(self) -> None:
        manifest = manifest_with_submit(
            condition=SuccessCondition(kind="status_code", status_codes=(200,))
        )

        restored = Manifest.from_json(manifest.to_json())

        self.assertEqual(restored.content_hash(), manifest.content_hash())
        self.assertIn("submit-answer", restored.to_markdown())
        restored.validate_for_run()

    def test_submission_without_explicit_success_condition_is_not_runnable(
        self,
    ) -> None:
        manifest = manifest_with_submit(condition=None)

        with self.assertRaises(ManifestValidationError):
            manifest.validate_for_run()

    def test_raw_secret_like_manifest_value_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            Manifest(
                platform=PlatformInfo(name="Example"),
                operations=(
                    Operation(
                        id="bad",
                        name="Bad",
                        method="POST",
                        url="https://platform.example/api",
                        body={"api_key": "raw-key"},
                    ),
                ),
            )

    def test_raw_authorization_header_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            Manifest(
                platform=PlatformInfo(name="Example"),
                operations=(
                    Operation(
                        id="bad-authorization",
                        name="Bad authorization",
                        method="GET",
                        url="https://platform.example/api",
                        headers={"Authorization": "Bearer raw-token"},
                    ),
                ),
            )

    def test_secret_like_headers_queries_and_url_credentials_are_rejected(
        self,
    ) -> None:
        cases = (
            ({"X-API-Key": "raw-key"}, {}, "https://platform.example/api"),
            ({"X-Auth-Token": "raw-token"}, {}, "https://platform.example/api"),
            ({}, {"access_token": "raw-token"}, "https://platform.example/api"),
            ({}, {}, "https://user:password@platform.example/api"),
            ({}, {}, "https://platform.example/api?token=raw-token"),
        )
        for index, (headers, query, url) in enumerate(cases):
            with self.subTest(index=index):
                with self.assertRaises(ValueError):
                    Manifest(
                        platform=PlatformInfo(name="Example"),
                        operations=(
                            Operation(
                                id=f"bad-secret-{index}",
                                name="Bad secret",
                                method="GET",
                                url=url,
                                headers=headers,
                                query=query,
                            ),
                        ),
                    )

        with self.assertRaises(ValueError):
            PlatformInfo(
                name="Example",
                base_url="https://platform.example/api?access_token=raw-token",
            )
