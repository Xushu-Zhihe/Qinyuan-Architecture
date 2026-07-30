import json
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from orchestrator.coordinator import ExecutionCoordinator
from orchestrator.events import ProjectEventHub
from orchestrator.health import HealthProbe
from orchestrator.layout import LayoutStore
from orchestrator.store import BlackboardStore
from orchestrator.supervisor import ProjectSupervisor
from worker.models import (
    AuditConfig,
    CoordinatorConfig,
    ProviderConfig,
    WorkerResult,
)


REGISTRY_SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 5000;

CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    mode TEXT NOT NULL CHECK(mode IN ('manual', 'run')),
    status TEXT NOT NULL,
    project_path TEXT NOT NULL UNIQUE,
    challenge_path TEXT,
    providers_json TEXT NOT NULL,
    agent_limit INTEGER NOT NULL CHECK(agent_limit > 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_opened_at TEXT NOT NULL
);
"""


class ProjectPathError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ProjectRecord:
    id: str
    name: str
    mode: str
    status: str
    path: Path
    challenge_path: Path | None
    providers: tuple[str, ...]
    agent_limit: int
    created_at: str
    updated_at: str
    last_opened_at: str


@dataclass(slots=True)
class ProjectHandle:
    record: ProjectRecord
    store: BlackboardStore
    layout: LayoutStore
    hub: ProjectEventHub

    @property
    def id(self) -> str:
        return self.record.id


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ProjectRegistry:
    def __init__(self, data_root: Path) -> None:
        self.data_root = data_root.resolve()
        self.projects_root = self.data_root / "projects"
        self.projects_root.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(
            self.data_root / "projects.db",
            check_same_thread=False,
        )
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(REGISTRY_SCHEMA)

    def close(self) -> None:
        self.connection.close()

    @staticmethod
    def _record(row: sqlite3.Row) -> ProjectRecord:
        challenge = row["challenge_path"]
        return ProjectRecord(
            id=str(row["id"]),
            name=str(row["name"]),
            mode=str(row["mode"]),
            status=str(row["status"]),
            path=Path(str(row["project_path"])),
            challenge_path=Path(str(challenge)) if challenge else None,
            providers=tuple(json.loads(str(row["providers_json"]))),
            agent_limit=int(row["agent_limit"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            last_opened_at=str(row["last_opened_at"]),
        )

    def create(
        self,
        name: str,
        mode: str,
        challenge_path: Path | None,
        providers: tuple[str, ...],
        agent_limit: int,
    ) -> ProjectRecord:
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("project name must be non-empty")
        if mode not in {"manual", "run"}:
            raise ValueError("project mode must be manual or run")
        if agent_limit <= 0:
            raise ValueError("agent_limit must be positive")
        project_id = f"project-{uuid4().hex}"
        project_path = (self.projects_root / project_id).resolve()
        project_path.mkdir(parents=True, exist_ok=False)
        now = _now()
        challenge = challenge_path.resolve() if challenge_path else None
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO projects(
                    id, name, mode, status, project_path,
                    challenge_path, providers_json, agent_limit,
                    created_at, updated_at, last_opened_at
                ) VALUES (?, ?, ?, 'idle', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    project_id,
                    clean_name,
                    mode,
                    str(project_path),
                    str(challenge) if challenge else None,
                    json.dumps(list(providers)),
                    agent_limit,
                    now,
                    now,
                    now,
                ),
            )
        return self.get(project_id)

    def get(self, project_id: str) -> ProjectRecord:
        row = self.connection.execute(
            "SELECT * FROM projects WHERE id = ?",
            (project_id,),
        ).fetchone()
        if row is None:
            raise KeyError(project_id)
        return self._record(row)

    def list(self) -> tuple[ProjectRecord, ...]:
        return tuple(
            self._record(row)
            for row in self.connection.execute(
                """
                SELECT * FROM projects
                ORDER BY updated_at DESC, id
                """
            ).fetchall()
        )

    def update_status(self, project_id: str, status: str) -> ProjectRecord:
        now = _now()
        with self.connection:
            cursor = self.connection.execute(
                """
                UPDATE projects SET status = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, now, project_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(project_id)
        return self.get(project_id)

    def resolve_project_path(
        self,
        project_id: str,
        relative_path: str | Path,
    ) -> Path:
        root = self.get(project_id).path.resolve()
        target = (root / relative_path).resolve()
        if target != root and root not in target.parents:
            raise ProjectPathError("path escapes project directory")
        return target


class ProjectService:
    def __init__(
        self,
        data_root: Path,
        provider_configs: tuple[ProviderConfig, ...] = (),
        execute: Callable[..., Awaitable[WorkerResult]] | None = None,
        health_probe: HealthProbe | None = None,
        coordinator_config: CoordinatorConfig | None = None,
        audit_config: AuditConfig | None = None,
    ) -> None:
        self.registry = ProjectRegistry(data_root)
        self.provider_configs = {
            provider.name: provider for provider in provider_configs
        }
        self.coordinator = ExecutionCoordinator(coordinator_config)
        self.audit_config = audit_config or AuditConfig()
        self.execute = execute
        self.health_probe = health_probe
        self._handles: dict[str, ProjectHandle] = {}
        self._supervisors: dict[str, ProjectSupervisor] = {}

    def _sync_project_status(self, handle: ProjectHandle) -> ProjectRecord:
        status = str(handle.store.snapshot()["project"]["status"])
        registry_record = self.registry.get(handle.id)
        if registry_record.status != status:
            registry_record = self.registry.update_status(handle.id, status)
        handle.record = registry_record
        return handle.record

    def _set_project_status(
        self,
        handle: ProjectHandle,
        status: str,
        final_answer: str | None = None,
    ) -> ProjectRecord:
        handle.store.set_project_status(status, final_answer)
        handle.record = self.registry.update_status(handle.id, status)
        return handle.record

    def _recover_project(self, handle: ProjectHandle) -> None:
        handle.store.mark_interrupted()
        self._sync_project_status(handle)
        if handle.store.active_execution_count() != 0:
            raise RuntimeError(
                f"project {handle.id} still has active execution records"
            )

    def close(self) -> None:
        for handle in self._handles.values():
            handle.store.close()
        self._handles.clear()
        self.registry.close()

    async def aclose(self) -> None:
        for supervisor in tuple(self._supervisors.values()):
            await supervisor.stop()
        self._supervisors.clear()
        self.close()

    def create_project(
        self,
        *,
        name: str,
        goal: str,
        origin: str,
        hints: tuple[str, ...],
        mode: str,
        provider_names: tuple[str, ...],
        agent_limit: int,
        challenge_path: Path | None,
    ) -> ProjectHandle:
        unknown = set(provider_names) - set(self.provider_configs)
        if self.provider_configs and unknown:
            raise ValueError(f"unknown providers: {sorted(unknown)}")
        record = self.registry.create(
            name,
            mode,
            challenge_path,
            provider_names,
            agent_limit,
        )
        store = BlackboardStore.create(
            record.path / "blackboard.db",
            record.id,
            goal,
            origin,
            hints,
        )
        handle = ProjectHandle(
            record,
            store,
            LayoutStore(record.path),
            ProjectEventHub(),
        )
        handle.store.ensure_provider_health(record.providers)
        self._handles[record.id] = handle
        return handle

    def get_handle(self, project_id: str) -> ProjectHandle:
        handle = self._handles.get(project_id)
        if handle is not None:
            self._sync_project_status(handle)
            return handle
        record = self.registry.get(project_id)
        handle = ProjectHandle(
            record,
            BlackboardStore.open(record.path / "blackboard.db", record.id),
            LayoutStore(record.path),
            ProjectEventHub(),
        )
        handle.store.ensure_provider_health(record.providers)
        self._handles[record.id] = handle
        self._recover_project(handle)
        return handle

    def list_projects(self) -> tuple[dict[str, object], ...]:
        result = []
        for record in self.registry.list():
            handle = self.get_handle(record.id)
            snapshot = handle.store.snapshot()
            result.append(
                {
                    **project_record_dict(handle.record),
                    "fact_count": sum(
                        node["type"] == "fact" for node in snapshot["nodes"]
                    ),
                    "intent_count": sum(
                        edge["type"] == "intent" for edge in snapshot["edges"]
                    ),
                    "highest_generation": snapshot["highest_generation"],
                }
            )
        return tuple(result)

    def _create_supervisor(self, handle: ProjectHandle) -> ProjectSupervisor:
        configs = tuple(
            self.provider_configs[name]
            for name in handle.record.providers
            if name in self.provider_configs
        )[: handle.record.agent_limit]
        if not configs:
            raise ValueError("project has no configured providers")
        kwargs = {}
        if self.execute is not None:
            kwargs["execute"] = self.execute
        if self.health_probe is not None:
            kwargs["health_probe"] = self.health_probe
        return ProjectSupervisor(
            store=handle.store,
            providers=configs,
            challenge_dir=handle.record.challenge_path,
            project_dir=handle.record.path,
            hub=handle.hub,
            coordinator=self.coordinator,
            audit_config=self.audit_config,
            project_status_writer=lambda status, final_answer: (
                self._set_project_status(handle, status, final_answer)
            ),
            **kwargs,
        )

    async def start_project(self, project_id: str) -> ProjectSupervisor:
        existing = self._supervisors.get(project_id)
        if existing is not None and any(
            not task.done() for task in existing.tasks.values()
        ):
            return existing
        handle = self.get_handle(project_id)
        if handle.store.active_execution_count() != 0:
            self._recover_project(handle)
        supervisor = self._create_supervisor(handle)
        self._supervisors[project_id] = supervisor
        await supervisor.start()
        return supervisor

    async def retry_execution(
        self,
        project_id: str,
        execution_id: str,
    ) -> dict[str, object]:
        handle = self.get_handle(project_id)
        try:
            handle.store.execution(execution_id)
        except KeyError:
            raise
        supervisor = self._supervisors.get(project_id)
        if supervisor is None:
            supervisor = self._create_supervisor(handle)
            self._supervisors[project_id] = supervisor
        return await supervisor.queue_retry(execution_id)

    async def reopen_project(self, project_id: str) -> dict[str, object]:
        handle = self.get_handle(project_id)
        supervisor = self._supervisors.get(project_id)
        if supervisor is not None and (
            any(not task.done() for task in supervisor.tasks.values())
            or any(not task.done() for task in supervisor.manual_tasks.values())
        ):
            raise ValueError("stop the project before reopening it")
        sequence = int(handle.store.snapshot()["sequence"])
        goal_version = handle.store.reopen_project()
        handle.record = self.registry.update_status(project_id, "idle")
        for event in handle.store.events_after(sequence):
            await handle.hub.publish(event)
        return {
            **project_record_dict(handle.record),
            "goal_version": goal_version,
            "final_answer": None,
        }

    async def runtime_snapshot(self) -> dict[str, object]:
        coordinator = await self.coordinator.snapshot()
        projects: list[dict[str, object]] = []
        active_tasks: list[dict[str, object]] = []
        for summary in self.list_projects():
            project_id = str(summary["id"])
            handle = self.get_handle(project_id)
            planning = [
                {
                    "kind": "planning",
                    "id": attempt["id"],
                    "project_id": project_id,
                    "project_name": summary["name"],
                    "provider": attempt["provider"],
                    "status": attempt["status"],
                    "queued_at": attempt["queued_at"],
                    "started_at": attempt["started_at"],
                }
                for attempt in handle.store.planning_attempts()
                if attempt["status"] in {"queued", "running"}
            ]
            executions = [
                {
                    "kind": "execution",
                    "id": execution["id"],
                    "project_id": project_id,
                    "project_name": summary["name"],
                    "provider": execution["provider"],
                    "status": execution["status"],
                    "queued_at": execution["queued_at"],
                    "started_at": execution["started_at"],
                    "intent_id": execution["intent_edge_id"],
                }
                for execution in handle.store.executions()
                if execution["status"] in {"planned", "running"}
            ]
            project_tasks = planning + executions
            active_tasks.extend(project_tasks)
            projects.append(
                {
                    **summary,
                    "provider_health": list(handle.store.provider_health()),
                    "active_task_count": len(project_tasks),
                    "recovery": handle.store.recovery_summary(),
                }
            )
        return {
            "limits": asdict(self.coordinator.config),
            "coordinator": coordinator,
            "projects": projects,
            "active_tasks": active_tasks,
        }

    async def stop_project(self, project_id: str) -> ProjectRecord:
        handle = self.get_handle(project_id)
        supervisor = self._supervisors.get(project_id)
        if supervisor is not None:
            await supervisor.stop()
        elif handle.store.snapshot()["project"]["status"] != "achieved":
            self._set_project_status(handle, "stopped")
        return self._sync_project_status(handle)


def project_record_dict(record: ProjectRecord) -> dict[str, object]:
    return {
        "id": record.id,
        "name": record.name,
        "mode": record.mode,
        "status": record.status,
        "challenge_path": (
            str(record.challenge_path) if record.challenge_path else None
        ),
        "providers": list(record.providers),
        "agent_limit": record.agent_limit,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "last_opened_at": record.last_opened_at,
    }
