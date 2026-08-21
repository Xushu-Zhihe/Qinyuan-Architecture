"""Adapter from automation questions to the existing Blackboard owner."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orchestrator.models import GoalResult
from orchestrator.projects import ProjectHandle, ProjectService


class WorkgroupError(RuntimeError):
    """Raised when a question cannot be solved by the Blackboard workgroup."""


@dataclass(frozen=True, slots=True)
class WorkgroupQuestion:
    external_id: str
    name: str
    goal: str
    origin: str
    hints: tuple[str, ...] = ()
    attachments_dir: Path | None = None


@dataclass(frozen=True, slots=True)
class WorkgroupSolution:
    project_id: str
    goal_result: GoalResult

    def dispatcher_context(self) -> dict[str, Any]:
        result = self.goal_result
        return {
            "project_id": result.project_id,
            "goal_version": result.goal_version,
            "goal_node_id": result.goal_node_id,
            "result_ref": result.result_ref,
            "fact": {
                "id": result.fact.id,
                "content": result.fact.content,
                "sources": list(result.fact.sources),
                "produced_by_intent_id": result.fact.produced_by_intent_id,
                "produced_by_execution_id": result.fact.produced_by_execution_id,
            },
            "supporting_facts": [
                {"id": fact.id, "content": fact.content}
                for fact in result.supporting_facts
            ],
            "supporting_providers": list(result.supporting_providers),
            "completion_threshold": result.completion_threshold,
        }


class BlackboardWorkgroup:
    """Create one persistent Blackboard project per question and await its result."""

    def __init__(
        self,
        service: ProjectService,
        *,
        solver_providers: tuple[str, ...],
        agent_limit: int,
        timeout_seconds: float = 3600.0,
    ) -> None:
        if not solver_providers:
            raise ValueError("solver_providers must be non-empty")
        if not 1 <= agent_limit <= len(solver_providers):
            raise ValueError("agent_limit must be between 1 and solver provider count")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.service = service
        self.solver_providers = solver_providers
        self.agent_limit = agent_limit
        self.timeout_seconds = timeout_seconds

    async def solve(self, question: WorkgroupQuestion) -> WorkgroupSolution:
        if not question.external_id.strip():
            raise WorkgroupError("question external_id must be non-empty")
        handle: ProjectHandle = self.service.create_project(
            name=f"automation-{question.external_id}",
            goal=question.goal,
            origin=question.origin,
            hints=question.hints,
            mode="run",
            provider_names=self.solver_providers,
            agent_limit=self.agent_limit,
            challenge_path=question.attachments_dir,
        )
        supervisor = await self.service.start_project(handle.id)
        try:
            await supervisor.wait_until_idle(timeout=self.timeout_seconds)
        except TimeoutError as exc:
            await self.service.stop_project(handle.id)
            raise WorkgroupError(
                f"workgroup timed out for question {question.external_id}"
            ) from exc
        except Exception as exc:
            raise WorkgroupError(
                f"workgroup failed for question {question.external_id}"
            ) from exc
        goal_result = self.service.get_goal_result(handle.id)
        if goal_result is None:
            raise WorkgroupError(
                "workgroup produced no unambiguous GoalResult for question "
                f"{question.external_id}"
            )
        return WorkgroupSolution(project_id=handle.id, goal_result=goal_result)
