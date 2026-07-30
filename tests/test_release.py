import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from tools.build_release import (
    MANIFEST_NAME,
    ReleasePolicyError,
    build_release,
    collect_release_files,
    verify_archive,
)


class ReleaseBuilderTests(unittest.TestCase):
    def make_root(self, directory: str) -> Path:
        root = Path(directory)
        (root / "frontend").mkdir(parents=True)
        (root / "worker").mkdir()
        (root / "VERSION").write_text("0.2.0\n", encoding="utf-8")
        (root / "pyproject.toml").write_text(
            '[project]\nname="example"\nversion="0.2.0"\n',
            encoding="utf-8",
        )
        (root / "frontend" / "package.json").write_text(
            json.dumps({"name": "example", "version": "0.2.0"}),
            encoding="utf-8",
        )
        (root / "worker" / "module.py").write_text("SAFE = True\n", encoding="utf-8")
        return root

    def test_policy_excludes_runtime_user_and_generated_data(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_root(directory)
            unsafe = (
                root / "worker" / "runs" / "run-1" / "blackboard.db",
                root / "web-data" / "registry.sqlite3",
                root / "experiments" / "results" / "raw.json",
                root / "frontend" / "dist" / "index.html",
                root / "frontend" / "test-results" / "trace.zip",
                root / "challenge" / "input.txt",
                root / ".env",
            )
            for path in unsafe:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("do not package", encoding="utf-8")

            names = {
                item.archive_path.as_posix()
                for item in collect_release_files(root)
            }

            self.assertIn("worker/module.py", names)
            self.assertFalse(any("runs" in name for name in names))
            self.assertFalse(any("results" in name for name in names))
            self.assertFalse(any(name.endswith((".db", ".sqlite3", ".zip")) for name in names))
            self.assertNotIn(".env", names)

    def test_policy_rejects_high_confidence_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_root(directory)
            credential = "sk-" + ("x" * 30)
            (root / "worker" / "config.py").write_text(
                "TOKEN = " + repr(credential) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ReleasePolicyError, "API key"):
                collect_release_files(root)

    def test_build_is_manifested_and_verifiable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_root(directory)
            output = root / "artifacts" / "release.zip"

            built = build_release(root, output)
            verify_archive(built)

            with zipfile.ZipFile(built) as archive:
                names = archive.namelist()
                manifest = json.loads(archive.read(MANIFEST_NAME))
            self.assertIn(MANIFEST_NAME, names)
            self.assertIn("worker/module.py", names)
            self.assertEqual(manifest["version"], "0.2.0")
            self.assertEqual(
                {item["path"] for item in manifest["files"]},
                set(names) - {MANIFEST_NAME},
            )

    def test_version_mismatch_blocks_build(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_root(directory)
            (root / "frontend" / "package.json").write_text(
                json.dumps({"name": "example", "version": "0.1.0"}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ReleasePolicyError, "do not match"):
                build_release(root, root / "release.zip")


if __name__ == "__main__":
    unittest.main()
