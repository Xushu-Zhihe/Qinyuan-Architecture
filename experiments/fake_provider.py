import asyncio
import json
from pathlib import Path

from experiments.config import ExperimentTask
from orchestrator.health import ProbeResult
from worker.models import LogChunk, ProviderConfig, WorkerResult


def _section(text: str, start: str, end: str | None = None) -> str:
    value = text.split(start, 1)[1]
    return value.split(end, 1)[0] if end else value


class DeterministicFakeExecutor:
    def __init__(self, task: ExperimentTask) -> None:
        self.task = task
        self.execution_calls: dict[str, int] = {}
        self.successful_providers: set[str] = set()

    async def __call__(
        self,
        provider: ProviderConfig,
        request: str,
        challenge_dir: Path | None,
        work_dir: Path,
        on_chunk=None,
        cancel_event: asyncio.Event | None = None,
        max_log_bytes: int = 8 * 1024 * 1024,
    ) -> WorkerResult:
        del challenge_dir, max_log_bytes
        work_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = work_dir / "stdout.txt"
        stderr_path = work_dir / "stderr.txt"
        is_planning = "Assigned intent:\n" not in request
        if is_planning:
            delay = self.task.fake.planning_delay_ms.get(provider.name, 0)
            if delay:
                await asyncio.sleep(delay / 1000)
            output = self._planning_output(provider.name, request)
            status = "success"
            stderr = ""
        else:
            await asyncio.sleep(0)
            count = self.execution_calls.get(provider.name, 0)
            self.execution_calls[provider.name] = count + 1
            if count < self.task.fake.execution_failures_each:
                output = ""
                status = "failed"
                stderr = "deterministic temporary provider failure\n"
            else:
                output = self._execution_output(provider.name, request)
                status = "success"
                stderr = ""

        if cancel_event is not None and cancel_event.is_set():
            output = ""
            status = "cancelled"
            stderr = "deterministic execution cancelled\n"
        stdout_path.write_text(output + ("\n" if output else ""), encoding="utf-8")
        stderr_path.write_text(stderr, encoding="utf-8")
        if on_chunk is not None:
            if output:
                result = on_chunk(LogChunk("stdout", output + "\n"))
                if asyncio.iscoroutine(result):
                    await result
            if stderr:
                result = on_chunk(LogChunk("stderr", stderr))
                if asyncio.iscoroutine(result):
                    await result

        input_tokens = max(1, len(request) // 4)
        output_tokens = max(1, len(output) // 4)
        return WorkerResult(
            provider=provider.name,
            priority=provider.priority,
            status=status,
            exit_code=0 if status == "success" else 1,
            duration_seconds=(
                self.task.fake.planning_delay_ms.get(provider.name, 0) / 1000
                if is_planning
                else 0.001
            ),
            output=output,
            stdout_path=str(stdout_path.resolve()),
            stderr_path=str(stderr_path.resolve()),
            work_dir=str(work_dir.resolve()),
            cancel_reason=(
                "experiment cancelled" if status == "cancelled" else None
            ),
            model="deterministic-fake-v1",
            cli_version="deterministic-fake-v1",
            token_usage={
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
            },
        )

    def _planning_output(self, provider_name: str, request: str) -> str:
        snapshot = json.loads(_section(request, "Graph snapshot:\n"))
        evidence = f"{self.task.id}: verified evidence"
        if provider_name in self.successful_providers:
            return "BLACKBOARD_INTENT: " + json.dumps(
                {"action": "idle", "reason": "provider verification completed"}
            )
        source = next(
            (
                node
                for node in snapshot["nodes"]
                if node["type"] == "fact" and node["state"] == "active"
            ),
            next(node for node in snapshot["nodes"] if node["type"] == "origin"),
        )
        target = next(node for node in snapshot["nodes"] if node["type"] == "goal")
        return "BLACKBOARD_INTENT: " + json.dumps(
            {
                "action": "plan",
                "source_node_id": source["id"],
                "target_node_id": target["id"],
                "content": f"Verify deterministic task {self.task.id}",
            }
        )

    def _execution_output(self, provider_name: str, request: str) -> str:
        intent = json.loads(
            _section(
                request,
                "Assigned intent:\n",
                "\n\nGraph snapshot:\n",
            )
        )
        is_correction = "This is a protocol correction attempt." in request
        if self.task.fake.requires_protocol_correction and not is_correction:
            return "BLACKBOARD_RESULT: " + json.dumps(
                {
                    "intent_id": intent["id"],
                    "facts": ["invalid string fact"],
                    "edges": [],
                    "goal_achieved": True,
                    "final_answer": self.task.expected_answer,
                }
            )
        self.successful_providers.add(provider_name)
        return "BLACKBOARD_RESULT: " + json.dumps(
            {
                "intent_id": intent["id"],
                "facts": [
                    {
                        "ref": "evidence",
                        "content": f"{self.task.id}: verified evidence",
                    }
                ],
                "edges": [],
                "goal_achieved": True,
                "final_answer": self.task.expected_answer,
            }
        )


async def fake_health_probe(provider: ProviderConfig) -> ProbeResult:
    return ProbeResult("success", cli_version="deterministic-fake-v1")
