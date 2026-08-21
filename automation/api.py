"""FastAPI router for the new parser and Manifest workflow."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from automation.llm import LLMClient, LLMConfig, RuntimeLLMSecret
from automation.manager import AutomationManager, AutomationManagerError
from automation.manifest import Manifest, ManifestValidationError
from automation.parser import OfflineManifestParser, ParserError
from automation.store import ManifestStore, ManifestStoreError


class LLMConfigPayload(BaseModel):
    """Independent parser or dispatcher LLM connection settings."""

    model_config = ConfigDict(extra="forbid")

    base_url: str = Field(min_length=1, max_length=2000)
    api_key: SecretStr = Field(min_length=1)
    model: str = Field(min_length=1, max_length=200)
    timeout_seconds: float = Field(default=300, gt=0, le=3600)
    max_transport_retries: int = Field(default=2, ge=0, le=10)
    temperature: float | None = Field(default=None, ge=0, le=2)


class ParseManifestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_text: str = Field(min_length=1, max_length=2_000_000)
    parser_llm: LLMConfigPayload


class StartAutomationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    manifest_id: str = Field(min_length=1, max_length=200)
    dispatcher_llm: LLMConfigPayload
    platform_token: SecretStr = Field(min_length=1)
    solver_providers: list[str] = Field(min_length=1)
    agent_limit: int = Field(default=1, ge=1, le=16)
    max_tasks: int = Field(default=10, ge=1, le=1000)
    task_timeout_seconds: float = Field(default=3600, gt=0, le=86400)
    poll_interval_seconds: float = Field(default=5, gt=0, le=3600)


class AutomationInstructionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instruction: str = Field(min_length=1, max_length=4000)


def create_automation_router(manager: AutomationManager) -> APIRouter:
    router = APIRouter(prefix="/api/automation", tags=["automation"])
    store = manager.manifests

    @router.post("/manifests/parse", status_code=status.HTTP_201_CREATED)
    async def parse_manifest(payload: ParseManifestRequest) -> dict[str, Any]:
        config = LLMConfig(
            base_url=payload.parser_llm.base_url,
            model=payload.parser_llm.model,
            timeout_seconds=payload.parser_llm.timeout_seconds,
            max_transport_retries=payload.parser_llm.max_transport_retries,
            temperature=payload.parser_llm.temperature,
        )
        secret = RuntimeLLMSecret(payload.parser_llm.api_key.get_secret_value())
        client = LLMClient(config, secret)
        try:
            manifest = await OfflineManifestParser(client).parse(payload.source_text)
            store.save(manifest)
        except (ParserError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ManifestStoreError as exc:
            raise HTTPException(status_code=500, detail="cannot save manifest") from exc
        finally:
            secret.clear()
            await client.close()
        return _manifest_response(manifest)

    @router.get("/manifests/{manifest_id}")
    def get_manifest(manifest_id: str) -> dict[str, Any]:
        try:
            return _manifest_response(store.load(manifest_id))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="manifest not found") from exc
        except ManifestStoreError as exc:
            raise HTTPException(status_code=500, detail="cannot read manifest") from exc

    @router.put("/manifests/{manifest_id}")
    def update_manifest(manifest_id: str, manifest: Manifest) -> dict[str, Any]:
        if manifest.manifest_id != manifest_id:
            raise HTTPException(
                status_code=409,
                detail="manifest ID does not match path",
            )
        try:
            return _manifest_response(store.save(manifest.touched()))
        except ManifestStoreError as exc:
            raise HTTPException(status_code=500, detail="cannot save manifest") from exc

    @router.get("/manifests")
    def list_manifests() -> list[dict[str, Any]]:
        try:
            return [_manifest_response(manifest) for manifest in store.list()]
        except ManifestStoreError as exc:
            raise HTTPException(
                status_code=500,
                detail="cannot list manifests",
            ) from exc

    @router.post("/manifests/{manifest_id}/validate")
    def validate_manifest(manifest_id: str) -> dict[str, Any]:
        try:
            manifest = store.load(manifest_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="manifest not found") from exc
        except ManifestStoreError as exc:
            raise HTTPException(status_code=500, detail="cannot read manifest") from exc
        try:
            manifest.validate_for_run()
        except ManifestValidationError as exc:
            return {"valid": False, "manifest_id": manifest_id, "errors": [str(exc)]}
        return {"valid": True, "manifest_id": manifest_id, "errors": []}

    @router.post("/runs", status_code=status.HTTP_202_ACCEPTED)
    async def start_run(payload: StartAutomationRequest) -> dict[str, object]:
        config = LLMConfig(
            base_url=payload.dispatcher_llm.base_url,
            model=payload.dispatcher_llm.model,
            timeout_seconds=payload.dispatcher_llm.timeout_seconds,
            max_transport_retries=payload.dispatcher_llm.max_transport_retries,
            temperature=payload.dispatcher_llm.temperature,
        )
        try:
            return await manager.start(
                manifest_id=payload.manifest_id,
                dispatcher_config=config,
                dispatcher_api_key=payload.dispatcher_llm.api_key.get_secret_value(),
                platform_token=payload.platform_token.get_secret_value(),
                solver_providers=tuple(payload.solver_providers),
                agent_limit=payload.agent_limit,
                max_tasks=payload.max_tasks,
                task_timeout_seconds=payload.task_timeout_seconds,
                poll_interval_seconds=payload.poll_interval_seconds,
            )
        except AutomationManagerError as exc:
            code = 409 if "already running" in str(exc) else 400
            raise HTTPException(status_code=code, detail=str(exc)) from exc

    @router.get("/runs/current")
    def get_current_run() -> dict[str, object]:
        try:
            return manager.snapshot()
        except AutomationManagerError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/runs/{run_id}")
    def get_run(run_id: str) -> dict[str, object]:
        try:
            snapshot = manager.snapshot()
        except AutomationManagerError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if snapshot["run_id"] != run_id:
            raise HTTPException(status_code=404, detail="run not found")
        return snapshot

    @router.post("/runs/{run_id}/stop")
    async def stop_run(run_id: str) -> dict[str, object]:
        _ensure_run_id(manager, run_id)
        try:
            return await manager.stop()
        except AutomationManagerError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/runs/{run_id}/commands", status_code=status.HTTP_202_ACCEPTED)
    async def submit_instruction(
        run_id: str,
        payload: AutomationInstructionRequest,
    ) -> dict[str, object]:
        _ensure_run_id(manager, run_id)
        try:
            return await manager.submit_instruction(payload.instruction)
        except AutomationManagerError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.get("/runs/{run_id}/events")
    def get_run_events(run_id: str) -> list[dict[str, object]]:
        _ensure_run_id(manager, run_id)
        try:
            return list(manager.events())
        except AutomationManagerError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    return router


def _manifest_response(manifest: Manifest) -> dict[str, Any]:
    return {
        "manifest_id": manifest.manifest_id,
        "manifest_hash": manifest.content_hash(),
        "manifest": manifest.model_dump(mode="json"),
        "json": manifest.to_json(),
        "markdown": manifest.to_markdown(),
    }


def _ensure_run_id(manager: AutomationManager, run_id: str) -> None:
    try:
        current = manager.snapshot()
    except AutomationManagerError as exc:
        raise HTTPException(status_code=404, detail="run not found") from exc
    if current["run_id"] != run_id:
        raise HTTPException(status_code=404, detail="run not found")
