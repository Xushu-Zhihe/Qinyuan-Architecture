import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

from orchestrator.audit import export_audit_bundle, prune_audit_logs
from orchestrator.store import BlackboardStore


class AuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = BlackboardStore.create(
            self.root / "blackboard.db",
            "project-1",
            "Recover the flag",
            "Challenge statement",
            (),
        )
        self.addCleanup(self.store.close)

    def planning_attempt(self, name: str, content: str) -> tuple[str, Path]:
        work_dir = self.root / "executions" / name
        work_dir.mkdir(parents=True)
        stdout = work_dir / "stdout.txt"
        stderr = work_dir / "stderr.txt"
        stdout.write_text(content, encoding="utf-8")
        stderr.write_text("", encoding="utf-8")
        attempt_id = self.store.start_planning_attempt(
            "codex",
            int(self.store.snapshot()["sequence"]),
            str(stdout),
            str(stderr),
            str(work_dir),
        )
        self.store.mark_planning_started(attempt_id, 0, ())
        self.store.finish_planning_attempt(
            attempt_id,
            "idle",
            "nothing else to do",
        )
        return attempt_id, stdout

    def test_prunes_only_oldest_finished_log_sets(self) -> None:
        first_id, first_path = self.planning_attempt("first", "first")
        second_id, second_path = self.planning_attempt("second", "second")

        pruned = prune_audit_logs(self.store, self.root, 1)

        self.assertEqual(pruned, (first_id,))
        self.assertFalse(first_path.exists())
        self.assertFalse(self.store.planning_attempt(first_id)["logs_retained"])
        self.assertTrue(second_path.exists())
        self.assertTrue(self.store.planning_attempt(second_id)["logs_retained"])

    def test_export_redacts_again_and_limits_retained_archives(self) -> None:
        self.planning_attempt(
            "secret",
            "Authorization: Bearer sk-export-secret-value\n",
        )
        self.store.add_human_node(
            "fact",
            "token=sk-fact-secret-value",
        )

        first = export_audit_bundle(self.store, self.root, 2)
        export_audit_bundle(self.store, self.root, 2)
        latest = export_audit_bundle(self.store, self.root, 2)

        archives = list((self.root / "audit-exports").glob("audit-*.zip"))
        self.assertEqual(len(archives), 2)
        self.assertFalse(first.exists())
        self.assertTrue(latest.exists())
        with ZipFile(BytesIO(latest.read_bytes())) as archive:
            payload = b"\n".join(
                archive.read(name) for name in archive.namelist()
            )
            names = archive.namelist()
        self.assertIn("snapshot.json", names)
        self.assertIn("logs/index.json", names)
        self.assertNotIn(b"sk-export-secret-value", payload)
        self.assertNotIn(b"sk-fact-secret-value", payload)
        self.assertIn(b"[redacted]", payload)


if __name__ == "__main__":
    unittest.main()
