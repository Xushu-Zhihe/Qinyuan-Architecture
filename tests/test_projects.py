import sqlite3
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path

from orchestrator.projects import (
    ProjectConflictError,
    ProjectPathError,
    ProjectRegistry,
    ProjectService,
)
from orchestrator.store import BlackboardStore


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

    def test_delete_removes_registry_record(self) -> None:
        project = self.registry.create("Delete", "manual", None, (), 1)

        self.registry.delete(project.id)

        with self.assertRaises(KeyError):
            self.registry.get(project.id)

    def test_registry_migrates_v0_with_backup(self) -> None:
        root = self.root / "legacy"
        registry = ProjectRegistry(root)
        registry.create("Legacy", "manual", None, (), 1)
        registry.close()
        path = root / "projects.db"
        connection = sqlite3.connect(path)
        try:
            with connection:
                connection.execute("PRAGMA user_version = 0")
                connection.execute("DELETE FROM schema_migrations")
        finally:
            connection.close()

        reopened = ProjectRegistry(root)
        self.addCleanup(reopened.close)

        self.assertEqual(reopened.list()[0].name, "Legacy")
        self.assertEqual(
            reopened.connection.execute("PRAGMA user_version").fetchone()[0],
            1,
        )
        backups = tuple(root.glob("projects.db.pre-v0-to-v1-*.bak"))
        self.assertEqual(len(backups), 1)

    def test_latest_registry_schema_drift_is_rejected(self) -> None:
        root = self.root / "drift"
        registry = ProjectRegistry(root)
        registry.close()
        path = root / "projects.db"
        connection = sqlite3.connect(path)
        try:
            with connection:
                connection.execute("ALTER TABLE projects DROP COLUMN updated_at")
        finally:
            connection.close()

        with self.assertRaisesRegex(RuntimeError, "schema drift"):
            ProjectRegistry(root)


class ProjectServiceConcurrencyTests(unittest.TestCase):
    def test_listing_waits_until_project_blackboard_is_initialized(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        service = ProjectService(Path(temporary.name))
        self.addCleanup(service.close)

        original_create = BlackboardStore.create
        creation_entered = threading.Event()
        release_creation = threading.Event()

        def delayed_create(path, project_id, goal, origin, hints):
            creation_entered.set()
            if not release_creation.wait(5):
                raise AssertionError("timed out waiting to release project creation")
            return original_create(path, project_id, goal, origin, hints)

        BlackboardStore.create = delayed_create
        self.addCleanup(setattr, BlackboardStore, "create", original_create)

        create_errors: list[BaseException] = []
        list_errors: list[BaseException] = []
        listed: list[tuple[dict[str, object], ...]] = []
        listing_finished = threading.Event()

        def create_project() -> None:
            try:
                service.create_project(
                    name="Race",
                    goal="Goal",
                    origin="Origin",
                    hints=(),
                    mode="manual",
                    provider_names=(),
                    agent_limit=1,
                    challenge_path=None,
                )
            except BaseException as exc:  # pragma: no cover - assertion below
                create_errors.append(exc)

        def list_projects() -> None:
            try:
                listed.append(service.list_projects())
            except BaseException as exc:  # pragma: no cover - assertion below
                list_errors.append(exc)
            finally:
                listing_finished.set()

        creator = threading.Thread(target=create_project)
        creator.start()
        self.assertTrue(creation_entered.wait(5))

        listing = threading.Thread(target=list_projects)
        listing.start()
        time.sleep(0.05)
        self.assertFalse(listing_finished.is_set())

        release_creation.set()
        creator.join(5)
        listing.join(5)

        self.assertFalse(creator.is_alive())
        self.assertFalse(listing.is_alive())
        self.assertEqual(create_errors, [])
        self.assertEqual(list_errors, [])
        self.assertEqual([item["name"] for item in listed[0]], ["Race"])


class ProjectServiceDeletionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.service = ProjectService(self.root / "data")

    async def asyncTearDown(self) -> None:
        await self.service.aclose()
        self.temporary.cleanup()

    def create_project(self, challenge_path: Path | None = None):
        return self.service.create_project(
            name="Delete me",
            goal="Goal",
            origin="Origin",
            hints=(),
            mode="manual",
            provider_names=(),
            agent_limit=1,
            challenge_path=challenge_path,
        )

    async def test_delete_moves_only_project_directory_to_trash(self) -> None:
        challenge = self.root / "challenge"
        challenge.mkdir()
        challenge_file = challenge / "keep.txt"
        challenge_file.write_text("keep", encoding="utf-8")
        handle = self.create_project(challenge)
        project_id = handle.id
        project_path = handle.record.path

        trash_path = await self.service.delete_project(project_id)

        self.assertFalse(project_path.exists())
        self.assertTrue((trash_path / "blackboard.db").is_file())
        self.assertEqual(challenge_file.read_text(encoding="utf-8"), "keep")
        with self.assertRaises(KeyError):
            self.service.registry.get(project_id)

    async def test_registry_failure_rolls_directory_and_handle_back(self) -> None:
        handle = self.create_project()
        project_id = handle.id
        project_path = handle.record.path
        original_delete = self.service.registry.delete

        def fail_delete(_project_id: str) -> None:
            raise sqlite3.OperationalError("registry busy")

        self.service.registry.delete = fail_delete
        try:
            with self.assertRaisesRegex(ProjectConflictError, "registry busy"):
                await self.service.delete_project(project_id)
        finally:
            self.service.registry.delete = original_delete

        self.assertTrue(project_path.is_dir())
        self.assertEqual(self.service.get_handle(project_id).id, project_id)
        self.assertEqual(self.service.registry.get(project_id).id, project_id)

    async def test_active_records_prevent_delete_without_data_loss(self) -> None:
        handle = self.create_project()
        intent = handle.store.create_or_merge_intent(
            "human",
            __import__("orchestrator.models", fromlist=["IntentProposal"])
            .IntentProposal(
                handle.store.node_id("origin"),
                handle.store.node_id("goal"),
                "Work",
            ),
        )
        handle.store.start_execution(
            intent.id,
            "codex",
            "stdout",
            "stderr",
            "work",
            defer_start=True,
        )

        with self.assertRaisesRegex(ProjectConflictError, "active planning"):
            await self.service.delete_project(handle.id)

        self.assertTrue(handle.record.path.is_dir())
        self.assertEqual(self.service.registry.get(handle.id).id, handle.id)

    async def test_project_path_outside_managed_root_is_rejected(self) -> None:
        handle = self.create_project()
        outside = self.root / "outside"
        outside.mkdir()
        with self.service.registry.connection:
            self.service.registry.connection.execute(
                "UPDATE projects SET project_path = ? WHERE id = ?",
                (str(outside), handle.id),
            )
        handle.record = replace(handle.record, path=outside)

        with self.assertRaisesRegex(ProjectConflictError, "outside"):
            await self.service.delete_project(handle.id)

        self.assertTrue(outside.is_dir())
        self.assertEqual(self.service.registry.get(handle.id).id, handle.id)



if __name__ == "__main__":
    unittest.main()
