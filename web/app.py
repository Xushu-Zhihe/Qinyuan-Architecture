import asyncio
from contextlib import asynccontextmanager
import sqlite3
from pathlib import Path

from fastapi import (
    FastAPI,
    HTTPException,
    Response,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from autopilot.manager import AutoManager, AutoManagerError
from autopilot.models import AutoStartConfig
from orchestrator.audit import export_audit_bundle, read_log_page
from orchestrator.layout import LayoutConflict
from orchestrator.models import IntentProposal
from orchestrator.projects import (
    ProjectConflictError,
    ProjectService,
    project_record_dict,
)
from web.schemas import (
    EdgeCreate,
    EdgeUpdate,
    LayoutUpdate,
    NodeCreate,
    ProjectCreate,
    AutoStart as AutoStartConfigPayload,
)
from worker.models import AuditConfig, CoordinatorConfig, ProviderConfig


DEFAULT_DATA_ROOT = Path(__file__).resolve().parents[1] / "web-data"
DEFAULT_FRONTEND_DIST = Path(__file__).resolve().parents[1] / "frontend" / "dist"


def create_app(
    data_root: Path | None = None,
    provider_configs: tuple[ProviderConfig, ...] = (),
    execute=None,
    health_probe=None,
    coordinator_config: CoordinatorConfig | None = None,
    audit_config: AuditConfig | None = None,
    frontend_dist: Path | None = None,
    runtime=None,
) -> FastAPI:
    service = ProjectService(
        (data_root or DEFAULT_DATA_ROOT).resolve(),
        provider_configs,
        execute=execute,
        health_probe=health_probe,
        coordinator_config=coordinator_config,
        audit_config=audit_config,
    )
    autopilot = AutoManager(
        service=service,
        provider_configs=provider_configs,
        execute=execute,
        audit_config=audit_config,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if runtime is not None:
            await runtime.start()
        yield
        await autopilot.aclose()
        await service.aclose()
        if runtime is not None:
            await runtime.close()

    app = FastAPI(title="Blackboard", lifespan=lifespan)
    app.state.projects = service
    app.state.autopilot = autopilot
    app.state.provider_runtime = runtime
    dist_root = (frontend_dist or DEFAULT_FRONTEND_DIST).resolve()
    index_file = dist_root / "index.html"
    assets_root = dist_root / "assets"
    if index_file.is_file() and assets_root.is_dir():
        app.mount("/assets", StaticFiles(directory=assets_root), name="assets")

    def handle(project_id: str):
        try:
            return service.get_handle(project_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="project not found") from exc
        except ProjectConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    async def publish_since(project, sequence: int) -> None:
        for event in project.store.events_after(sequence):
            await project.hub.publish(event)

    def log_page_limit(value: int | None) -> int:
        limit = value or service.audit_config.default_log_page_bytes
        if limit <= 0 or limit > service.audit_config.max_log_page_bytes:
            raise HTTPException(
                status_code=400,
                detail=(
                    "log limit must be between 1 and "
                    f"{service.audit_config.max_log_page_bytes} bytes"
                ),
            )
        return limit

    def paged_logs(
        project,
        record: dict[str, object],
        *,
        stdout_offset: int,
        stderr_offset: int,
        all_offset: int,
        limit: int | None,
    ) -> dict[str, object]:
        if stdout_offset < 0 or stderr_offset < 0 or all_offset < 0:
            raise HTTPException(
                status_code=400,
                detail="log offsets must be non-negative",
            )
        page_limit = log_page_limit(limit)
        retained = bool(record.get("logs_retained", True))
        stdout = read_log_page(
            project.record.path,
            record["stdout_path"],
            offset=stdout_offset,
            limit=page_limit,
            retained=retained,
        )
        stderr = read_log_page(
            project.record.path,
            record["stderr_path"],
            offset=stderr_offset,
            limit=page_limit,
            retained=retained,
        )
        combined = read_log_page(
            project.record.path,
            Path(str(record["stdout_path"])).with_name("combined.txt"),
            offset=all_offset,
            limit=page_limit,
            retained=retained,
        )
        return {
            "stdout": stdout.pop("text"),
            "stderr": stderr.pop("text"),
            "all": combined.pop("text"),
            "pagination": {
                "limit_bytes": page_limit,
                "stdout": stdout,
                "stderr": stderr,
                "all": combined,
            },
        }

    @app.get("/api/projects")
    def list_projects():
        return service.list_projects()

    @app.get("/api/runtime")
    async def get_runtime():
        return await service.runtime_snapshot()

    @app.get("/api/autopilot")
    async def get_autopilot():
        return await autopilot.snapshot()

    @app.post("/api/autopilot/start", status_code=status.HTTP_202_ACCEPTED)
    async def start_autopilot(payload: AutoStartConfigPayload):
        try:
            config = AutoStartConfig(
                platform_prompt=payload.platform_prompt,
                controller_provider=payload.controller_provider,
                solver_providers=tuple(payload.solver_providers),
                agent_limit=payload.agent_limit,
                max_tasks=payload.max_tasks,
                task_timeout_seconds=payload.task_timeout_minutes * 60,
                controller_timeout_seconds=payload.controller_timeout_seconds,
            )
            return await autopilot.start(config)
        except AutoManagerError as exc:
            code = 409 if "already running" in str(exc) else 400
            raise HTTPException(status_code=code, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/autopilot/stop")
    async def stop_autopilot():
        return await autopilot.stop()

    @app.delete(
        "/api/projects/{project_id}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    async def delete_project(project_id: str):
        try:
            await service.delete_project(project_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="project not found") from exc
        except ProjectConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post("/api/projects", status_code=status.HTTP_201_CREATED)
    def create_project(payload: ProjectCreate):
        challenge = Path(payload.challenge_path).resolve() if payload.challenge_path else None
        if challenge is not None and not challenge.is_dir():
            raise HTTPException(status_code=400, detail="challenge path is not a directory")
        try:
            project = service.create_project(
                name=payload.name,
                goal=payload.goal,
                origin=payload.origin,
                hints=tuple(hint for hint in payload.hints if hint.strip()),
                mode=payload.mode,
                provider_names=tuple(payload.providers),
                agent_limit=payload.agent_limit,
                challenge_path=challenge,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return project_record_dict(project.record)

    @app.get("/api/projects/{project_id}")
    def get_project(project_id: str):
        project = handle(project_id)
        snapshot = project.store.snapshot()
        return {
            **project_record_dict(project.record),
            "goal_version": snapshot["project"]["goal_version"],
            "final_answer": snapshot["project"]["final_answer"],
            "provider_health": list(service.provider_health(project)),
            "recovery": project.store.recovery_summary(),
        }

    @app.get("/api/projects/{project_id}/providers")
    def get_provider_health(project_id: str):
        return list(service.provider_health(handle(project_id)))

    @app.get("/api/projects/{project_id}/graph")
    def get_graph(project_id: str, include_archived: bool = False):
        project = handle(project_id)
        snapshot = project.store.snapshot()
        snapshot["provider_health"] = list(service.provider_health(project))
        if not include_archived:
            active_nodes = {
                str(node["id"])
                for node in snapshot["nodes"]
                if node["state"] != "archived"
            }
            snapshot["nodes"] = [
                node for node in snapshot["nodes"]
                if str(node["id"]) in active_nodes
            ]
            snapshot["edges"] = [
                edge for edge in snapshot["edges"]
                if str(edge["from_node"]) in active_nodes
                and str(edge["to_node"]) in active_nodes
            ]
        graph_project = dict(snapshot["project"])
        snapshot["project"] = {
            **project_record_dict(project.record),
            "status": graph_project["status"],
            "final_answer": graph_project.get("final_answer"),
            "goal_version": graph_project["goal_version"],
        }
        return {
            **snapshot,
            "layout": project.layout.load(),
            "recovery": project.store.recovery_summary(),
        }

    @app.post(
        "/api/projects/{project_id}/nodes",
        status_code=status.HTTP_201_CREATED,
    )
    async def create_node(project_id: str, payload: NodeCreate):
        project = handle(project_id)
        sequence = int(project.store.snapshot()["sequence"])
        try:
            node_id = project.store.add_human_node(payload.type, payload.content)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        result = next(
            node
            for node in project.store.snapshot()["nodes"]
            if node["id"] == node_id
        )
        await publish_since(project, sequence)
        return result

    @app.delete(
        "/api/projects/{project_id}/nodes/{node_id}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    async def archive_node(project_id: str, node_id: str):
        project = handle(project_id)
        sequence = int(project.store.snapshot()["sequence"])
        try:
            project.store.archive_node(node_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="node not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        await publish_since(project, sequence)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post(
        "/api/projects/{project_id}/nodes/{node_id}/restore",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    async def restore_node(project_id: str, node_id: str):
        project = handle(project_id)
        sequence = int(project.store.snapshot()["sequence"])
        try:
            project.store.restore_node(node_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="node not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        await publish_since(project, sequence)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post(
        "/api/projects/{project_id}/edges",
        status_code=status.HTTP_201_CREATED,
    )
    async def create_edge(project_id: str, payload: EdgeCreate):
        project = handle(project_id)
        sequence = int(project.store.snapshot()["sequence"])
        try:
            if payload.type == "intent":
                if payload.content is None or not payload.content.strip():
                    raise ValueError("intent content must be non-empty")
                record = project.store.create_or_merge_intent(
                    "human",
                    IntentProposal(
                        payload.from_node_id,
                        payload.to_node_id,
                        payload.content,
                    ),
                )
                edge_id = record.id
            else:
                edge_id = project.store.add_relation(
                    payload.type,
                    payload.from_node_id,
                    payload.to_node_id,
                )
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        result = next(
            edge
            for edge in project.store.snapshot()["edges"]
            if edge["id"] == edge_id
        )
        await publish_since(project, sequence)
        return result

    @app.patch("/api/projects/{project_id}/edges/{edge_id}")
    async def reconnect_edge(
        project_id: str,
        edge_id: str,
        payload: EdgeUpdate,
    ):
        project = handle(project_id)
        sequence = int(project.store.snapshot()["sequence"])
        try:
            project.store.reconnect_edge(
                edge_id,
                payload.from_node_id,
                payload.to_node_id,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="edge not found") from exc
        except (ValueError, sqlite3.IntegrityError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        result = next(
            edge
            for edge in project.store.snapshot()["edges"]
            if edge["id"] == edge_id
        )
        await publish_since(project, sequence)
        return result

    @app.delete(
        "/api/projects/{project_id}/edges/{edge_id}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    async def delete_edge(project_id: str, edge_id: str):
        project = handle(project_id)
        sequence = int(project.store.snapshot()["sequence"])
        try:
            project.store.delete_edge(edge_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="edge not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        await publish_since(project, sequence)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.put("/api/projects/{project_id}/layout")
    async def update_layout(project_id: str, payload: LayoutUpdate):
        project = handle(project_id)
        try:
            result = project.layout.save(
                payload.revision,
                {
                    "nodes": payload.nodes,
                    "edges": payload.edges,
                    "viewport": payload.viewport,
                },
            )
            event = project.store.append_event(
                "graph.layout_updated",
                "human",
                {"revision": result["revision"]},
            )
            await project.hub.publish(event)
            return result
        except LayoutConflict as exc:
            raise HTTPException(
                status_code=409,
                detail={"current_revision": exc.current_revision},
            ) from exc

    @app.get("/api/projects/{project_id}/executions/{execution_id}")
    def get_execution(project_id: str, execution_id: str):
        project = handle(project_id)
        try:
            return project.store.execution(execution_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="execution not found") from exc

    @app.get("/api/projects/{project_id}/executions")
    def get_executions(project_id: str):
        return list(handle(project_id).store.executions())

    @app.post(
        "/api/projects/{project_id}/executions/{execution_id}/retry",
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def retry_execution(project_id: str, execution_id: str):
        try:
            return await service.retry_execution(project_id, execution_id)
        except KeyError as exc:
            raise HTTPException(
                status_code=404,
                detail="execution not found",
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/projects/{project_id}/executions/{execution_id}/logs")
    def get_execution_logs(
        project_id: str,
        execution_id: str,
        stdout_offset: int = 0,
        stderr_offset: int = 0,
        all_offset: int = 0,
        limit: int | None = None,
    ):
        project = handle(project_id)
        try:
            execution = project.store.execution(execution_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="execution not found") from exc
        return paged_logs(
            project,
            execution,
            stdout_offset=stdout_offset,
            stderr_offset=stderr_offset,
            all_offset=all_offset,
            limit=limit,
        )

    @app.get("/api/projects/{project_id}/planning-attempts")
    def get_planning_attempts(project_id: str):
        return list(handle(project_id).store.planning_attempts())

    @app.get(
        "/api/projects/{project_id}/planning-attempts/{planning_attempt_id}"
    )
    def get_planning_attempt(project_id: str, planning_attempt_id: str):
        project = handle(project_id)
        try:
            return project.store.planning_attempt(planning_attempt_id)
        except KeyError as exc:
            raise HTTPException(
                status_code=404,
                detail="planning attempt not found",
            ) from exc

    @app.get(
        "/api/projects/{project_id}/planning-attempts/"
        "{planning_attempt_id}/logs"
    )
    def get_planning_attempt_logs(
        project_id: str,
        planning_attempt_id: str,
        stdout_offset: int = 0,
        stderr_offset: int = 0,
        all_offset: int = 0,
        limit: int | None = None,
    ):
        project = handle(project_id)
        try:
            attempt = project.store.planning_attempt(planning_attempt_id)
        except KeyError as exc:
            raise HTTPException(
                status_code=404,
                detail="planning attempt not found",
            ) from exc
        return paged_logs(
            project,
            attempt,
            stdout_offset=stdout_offset,
            stderr_offset=stderr_offset,
            all_offset=all_offset,
            limit=limit,
        )

    @app.get("/api/projects/{project_id}/audit/export")
    def export_project_audit(project_id: str):
        project = handle(project_id)
        path = export_audit_bundle(
            project.store,
            project.record.path,
            service.audit_config.max_retained_exports,
        )
        return FileResponse(
            path,
            media_type="application/zip",
            filename=path.name,
        )

    @app.post(
        "/api/projects/{project_id}/reopen",
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def reopen_project(project_id: str):
        try:
            return await service.reopen_project(project_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="project not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post(
        "/api/projects/{project_id}/start",
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def start_project(project_id: str):
        try:
            supervisor = await service.start_project(project_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="project not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "status": handle(project_id).record.status,
            "agents": supervisor.agent_statuses(),
            "providers": list(supervisor.provider_health().values()),
        }

    @app.post("/api/projects/{project_id}/stop")
    async def stop_project(project_id: str):
        try:
            record = await service.stop_project(project_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="project not found") from exc
        except ProjectConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return project_record_dict(record)

    @app.websocket("/ws/projects/{project_id}")
    async def project_socket(
        websocket: WebSocket,
        project_id: str,
        after: int = 0,
    ):
        try:
            project = service.get_handle(project_id)
        except KeyError:
            await websocket.close(code=4404)
            return
        await websocket.accept()
        try:
            async with project.hub.subscribe() as queue:
                replay = project.store.events_after(after)
                queued: dict[int, dict[str, object]] = {}
                while True:
                    try:
                        event = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    queued[int(event["sequence"])] = event

                last_sequence = after
                for persisted in replay:
                    sequence = int(persisted["sequence"])
                    event = queued.pop(sequence, persisted)
                    await websocket.send_json(
                        {"project_id": project_id, **event}
                    )
                    last_sequence = sequence
                project.hub.record_backfill(len(replay))
                for sequence in sorted(queued):
                    if sequence <= last_sequence:
                        continue
                    await websocket.send_json(
                        {"project_id": project_id, **queued[sequence]}
                    )
                    last_sequence = sequence

                while True:
                    event = await queue.get()
                    sequence = int(event["sequence"])
                    if sequence <= last_sequence:
                        continue
                    if sequence > last_sequence + 1:
                        recovered = 0
                        for persisted in project.store.events_after(last_sequence):
                            persisted_sequence = int(persisted["sequence"])
                            if persisted_sequence > sequence:
                                break
                            selected = (
                                event
                                if persisted_sequence == sequence
                                else persisted
                            )
                            if persisted_sequence != sequence:
                                recovered += 1
                            await websocket.send_json(
                                {"project_id": project_id, **selected}
                            )
                            last_sequence = persisted_sequence
                        project.hub.record_backfill(recovered)
                        continue
                    await websocket.send_json(
                        {"project_id": project_id, **event}
                    )
                    last_sequence = sequence
        except WebSocketDisconnect:
            return

    if index_file.is_file():
        @app.get("/{path:path}", include_in_schema=False)
        def frontend_route(path: str):
            if path == "api" or path.startswith("api/"):
                raise HTTPException(status_code=404, detail="not found")
            return FileResponse(index_file)

    return app
