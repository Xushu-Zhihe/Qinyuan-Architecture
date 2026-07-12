import tempfile
import unittest
from pathlib import Path

from orchestrator.projects import ProjectPathError, ProjectRegistry


class ProjectRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.registry = ProjectRegistry(self.root)
        self.addCleanup(self.registry.close)

    def test_project_directory_is_generated_under_root(self) -> None:
        project = self.registry.create(
            name="Demo",
            mode="manual",
            challenge_path=None,
            providers=(),
            agent_limit=1,
        )

        self.assertEqual(project.path.parent, self.root / "projects")
        self.assertTrue(project.path.name.startswith("project-"))
        self.assertTrue(project.path.is_dir())

    def test_execution_path_cannot_cross_project_boundary(self) -> None:
        project = self.registry.create(
            "Alpha", "manual", None, (), 1
        )

        with self.assertRaises(ProjectPathError):
            self.registry.resolve_project_path(
                project.id,
                "../other/blackboard.db",
            )

    def test_list_returns_persisted_metadata(self) -> None:
        created = self.registry.create(
            "Alpha",
            "run",
            self.root / "challenge",
            ("codex", "claude"),
            2,
        )
        self.registry.update_status(created.id, "running")

        reopened = ProjectRegistry(self.root)
        self.addCleanup(reopened.close)
        projects = reopened.list()

        self.assertEqual(len(projects), 1)
        self.assertEqual(projects[0].name, "Alpha")
        self.assertEqual(projects[0].providers, ("codex", "claude"))
        self.assertEqual(projects[0].status, "running")


if __name__ == "__main__":
    unittest.main()
