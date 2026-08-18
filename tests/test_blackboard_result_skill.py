import json
import hashlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = (
    ROOT / ".agents" / "skills" / "blackboard-result-protocol"
)
VALIDATOR = SKILL_ROOT / "scripts" / "validate_result.py"


class BlackboardResultSkillTests(unittest.TestCase):
    def test_conformance_fixture_manifest_is_complete(self) -> None:
        fixture_root = SKILL_ROOT / "fixtures"
        required = {
            "valid-text.json",
            "valid-structured.json",
            "valid-evidence.json",
            "valid-partial.json",
            "invalid-result-ref.json",
            "invalid-node-type.json",
            "invalid-goal-version.json",
            "invalid-missing-address.json",
        }
        fixtures = {path.name: path for path in fixture_root.glob("*.json")}
        self.assertEqual(set(fixtures), required)
        encoded = b"".join(
            name.encode("utf-8")
            + b"\0"
            + fixtures[name].read_text(encoding="utf-8").strip().encode("utf-8")
            + b"\n"
            for name in sorted(fixtures)
        )
        manifest = json.loads((SKILL_ROOT / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["fixture_digest"], "sha256:" + hashlib.sha256(encoded).hexdigest())

    def run_validator(
        self,
        payload: object,
        existing_node_ids: tuple[str, ...] = ("goal-1",),
        goal_node_id: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as directory:
            payload_path = Path(directory) / "result.json"
            payload_path.write_text(
                json.dumps(payload),
                encoding="utf-8",
            )
            command = [
                sys.executable,
                str(VALIDATOR),
                "--intent-id",
                "intent-1",
            ]
            if goal_node_id is not None:
                command.extend(("--goal-node-id", goal_node_id))
            for node_id in existing_node_ids:
                command.extend(("--existing-node-id", node_id))
            command.extend(("--file", str(payload_path)))
            return subprocess.run(
                command,
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

    def test_skill_documents_exact_protocol_and_failures(self) -> None:
        skill_path = SKILL_ROOT / "SKILL.md"
        metadata_path = SKILL_ROOT / "agents" / "openai.yaml"
        self.assertTrue(skill_path.is_file())
        self.assertTrue(metadata_path.is_file())
        if not skill_path.is_file() or not metadata_path.is_file():
            return

        skill = skill_path.read_text(encoding="utf-8")
        metadata = metadata_path.read_text(encoding="utf-8")

        self.assertIn("BLACKBOARD_RESULT:", skill)
        self.assertIn('"ref"', skill)
        self.assertIn('"content"', skill)
        self.assertIn('"type"', skill)
        self.assertIn("fact proposal must be an object", skill)
        self.assertIn("key=value", skill)
        self.assertIn("$blackboard-result-protocol", metadata)

    def test_validator_accepts_parser_compatible_result(self) -> None:
        self.assertTrue(VALIDATOR.is_file())
        if not VALIDATOR.is_file():
            return
        result = self.run_validator(
            {
                "intent_id": "intent-1",
                "facts": [
                    {"ref": "flag-fact", "content": "flag = flag{same}"}
                ],
                "edges": [
                    {
                        "type": "supports",
                        "from": "flag-fact",
                        "to": "goal-1",
                    }
                ],
                "goal_achieved": True,
                "final_answer": "flag{same}",
            }
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "valid")

    def test_validator_rejects_string_fact(self) -> None:
        self.assertTrue(VALIDATOR.is_file())
        if not VALIDATOR.is_file():
            return
        result = self.run_validator(
            {
                "intent_id": "intent-1",
                "facts": ["flag = flag{same}"],
                "edges": [],
                "goal_achieved": True,
                "final_answer": "flag{same}",
            }
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("fact proposal must be an object", result.stderr)

    def test_validator_rejects_unknown_edge_reference(self) -> None:
        self.assertTrue(VALIDATOR.is_file())
        if not VALIDATOR.is_file():
            return
        result = self.run_validator(
            {
                "intent_id": "intent-1",
                "facts": [],
                "edges": [
                    {
                        "type": "supports",
                        "from": "missing",
                        "to": "goal-1",
                    }
                ],
                "goal_achieved": False,
                "final_answer": None,
            }
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("unknown node reference", result.stderr)

    def test_v2_requires_explicit_goal_context(self) -> None:
        payload = {
            "protocol_version": 2,
            "intent_id": "intent-1",
            "facts": [{"ref": "result", "content": "complete"}],
            "edges": [{"type": "addresses", "from": "result", "to": "goal-1"}],
            "goal_achieved": True,
            "goal_result_ref": "result",
        }
        missing_goal = self.run_validator(payload)
        self.assertEqual(missing_goal.returncode, 1)
        self.assertIn("--goal-node-id is required", missing_goal.stderr)
        valid = self.run_validator(payload, goal_node_id="goal-1")
        self.assertEqual(valid.returncode, 0, valid.stderr)


if __name__ == "__main__":
    unittest.main()
