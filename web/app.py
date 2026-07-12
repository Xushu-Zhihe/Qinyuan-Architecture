from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Response, status

from orchestrator.layout import LayoutConflict
from orchestrator.models import IntentProposal
from orchestrator.projects import ProjectService, project_record_dict
from web.schemas import EdgeCreate, LayoutUpdate, NodeCreate, ProjectCreate
from worker.models import ProviderConfig


DEFAULT_DATA_ROOT = Path(__file__).resolve().parents[1] / "web-data"


def create_app(
    data_root: Path | None = None,
    provider_configs: tuple[ProviderConfig, ...] = (),
) -> FastAPI:
    service = ProjectService(
        (data_root or DEFAULT_DATA_ROOT).resolve(),
        provider_configs,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        service.close()

    app = FastAPI(title="Blackboard", lifespan=lifespan)
    app.state.projects = service

    def handle(project_id: str):
        try:
            return service.get_handle(project_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="project not found") from exc

    @app.get("/api/projects")
    def list_projects():
        return service.list_projects()

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
        return project_record_dict(handle(project_id).record)

    @app.get("/api/projects/{project_id}/graph")
    def get_graph(project_id: str):
        project = handle(project_id)
        return {**project.store.snapshot(), "layout": project.layout.load()}

    @app.post(
        "/api/projects/{project_id}/nodes",
        status_code=status.HTTP_201_CREATED,
    )
    def create_node(project_id: str, payload: NodeCreate):
        project = handle(project_id)
        try:
            node_id = project.store.add_human_node(payload.type, payload.content)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return next(
            node
            for node in project.store.snapshot()["nodes"]
            if node["id"] == node_id
        )

    @app.delete(
        "/api/projects/{project_id}/nodes/{node_id}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    def archive_node(project_id: str, node_id: str):
        try:
            handle(project_id).store.archive_node(node_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="node not found") from exc
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post(
        "/api/projects/{project_id}/edges",
        status_code=status.HTTP_201_CREATED,
    )
    def create_edge(project_id: str, payload: EdgeCreate):
        project = handle(project_id)
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
        return next(
            edge
            for edge in project.store.snapshot()["edges"]
            if edge["id"] == edge_id
        )

    @app.put("/api/projects/{project_id}/layout")
    def update_layout(project_id: str, payload: LayoutUpdate):
        project = handle(project_id)
        try:
            return project.layout.save(
                payload.revision,
                {
                    "nodes": payload.nodes,
                    "edges": payload.edges,
                    "viewport": payload.viewport,
                },
            )
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

    @app.get("/api/projects/{project_id}/executions/{execution_id}/logs")
    def get_execution_logs(project_id: str, execution_id: str):
        project = handle(project_id)
        try:
            execution = project.store.execution(execution_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="execution not found") from exc
        return {
            "stdout": Path(str(execution["stdout_path"])).read_text(
                encoding="utf-8", errors="replace"
            ) if Path(str(execution["stdout_path"])).is_file() else "",
            "stderr": Path(str(execution["stderr_path"])).read_text(
                encoding="utf-8", errors="replace"
            ) if Path(str(execution["stderr_path"])).is_file() else "",
        }

    return app
