import json
import unittest

from worker.redaction import redact_text


class RedactionTests(unittest.TestCase):
    def test_business_identifiers_are_not_treated_as_secret_assignments(self) -> None:
        value = "fact-correct-password fact-token-analysis fact-secret-discovery"

        self.assertEqual(redact_text(value), value)

    def test_labeled_secrets_require_assignment_and_preserve_json_syntax(self) -> None:
        payload = {
            "ref": "fact-correct-password",
            "content": "password=nss826312",
            "token": "secret-token",
        }
        line = "BLACKBOARD_RESULT: " + json.dumps(payload) + "\n"

        redacted = redact_text(line)
        decoded = json.loads(redacted.split(": ", 1)[1])

        self.assertEqual(decoded["ref"], "fact-correct-password")
        self.assertEqual(decoded["content"], "password=[redacted]")
        self.assertEqual(decoded["token"], "[redacted]")
        self.assertEqual(redact_text('token="secret-value"'), 'token="[redacted]"')
        self.assertEqual(redact_text("password for login"), "password for login")

    def test_invalid_protocol_json_is_not_repaired_by_redaction(self) -> None:
        invalid = 'BLACKBOARD_RESULT: {"password":"unterminated}\n'

        self.assertEqual(redact_text(invalid), invalid)


if __name__ == "__main__":
    unittest.main()
