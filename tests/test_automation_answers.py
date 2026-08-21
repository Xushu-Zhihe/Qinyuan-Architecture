import json
import unittest

from automation.answers import AnswerMappingError, build_answer_variables
from automation.manifest import AnswerField, AnswerMapping


class AnswerMappingTests(unittest.TestCase):
    def test_form_mapping_builds_multiple_named_variables(self) -> None:
        mapping = AnswerMapping(
            mode="form",
            fields=(
                AnswerField(target="flag", source_path="fact.content"),
                AnswerField(target="language", source_path="metadata.language"),
            ),
        )

        variables = build_answer_variables(
            mapping,
            {
                "fact": {"content": "FLAG{form}"},
                "metadata": {"language": "python"},
            },
        )

        self.assertEqual(variables["flag"], "FLAG{form}")
        self.assertEqual(variables["language"], "python")
        self.assertEqual(
            variables["answers"], {"flag": "FLAG{form}", "language": "python"}
        )
        self.assertEqual(variables["answer"], variables["answers"])

    def test_required_mapping_field_missing_is_rejected(self) -> None:
        mapping = AnswerMapping(
            mode="json",
            fields=(AnswerField(target="flag", source_path="fact.content"),),
        )

        with self.assertRaisesRegex(AnswerMappingError, "flag"):
            build_answer_variables(mapping, {"fact": {"other": "value"}})

    def test_optional_mapping_field_can_be_absent(self) -> None:
        mapping = AnswerMapping(
            mode="form",
            fields=(
                AnswerField(
                    target="comment", source_path="fact.comment", required=False
                ),
            ),
        )

        variables = build_answer_variables(mapping, {"fact": {"content": "x"}})

        self.assertEqual(variables["answers"], {})
        self.assertEqual(variables["answer"], {})

    def test_json_fact_content_can_supply_nested_answer_fields(self) -> None:
        mapping = AnswerMapping(
            mode="json",
            fields=(
                AnswerField(
                    target="flag",
                    source_path="fact.content.answer",
                ),
                AnswerField(
                    target="language",
                    source_path="fact.content.metadata.language",
                ),
            ),
        )

        variables = build_answer_variables(
            mapping,
            {
                "fact": {
                    "content": json.dumps(
                        {
                            "answer": "FLAG{json-content}",
                            "metadata": {"language": "python"},
                        }
                    )
                }
            },
        )

        self.assertEqual(
            variables["answers"],
            {"flag": "FLAG{json-content}", "language": "python"},
        )
