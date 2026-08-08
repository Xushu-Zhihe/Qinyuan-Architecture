import json
import re
import tempfile
import unittest
from pathlib import Path

from orchestrator.store import BlackboardStore
from orchestrator.supervisor import ProjectSupervisor
from sandbox.bridge import SandboxBridgeServer
from sandbox.service import SandboxService
from tests.test_qinyuan_provider import FakeBackend, provider
from tests.test_sandbox_contracts import FakeRuntime
from worker.models import WorkerResult
from worker.qinyuan_provider import QinyuanMainProvider


class BlackboardBackend(FakeBackend):
    def __init__(self, origin_id: str, goal_id: str) -> None:
        super().__init__()
        self.origin_id = origin_id
        self.goal_id = goal_id

    async def execute(self, provider_config, request, challenge_dir, work_dir, **kwargs) -> WorkerResult:
        context = kwargs.get("execution_context")
        if context.execution_kind == "planning":
            output = "BLACKBOARD_INTENT: " + json.dumps({
                "action": "plan",
                "source_node_id": self.origin_id,
                "target_node_id": self.goal_id,
                "content": "Verify fake evidence",
            })
        else:
            match = re.search(r'"id": "(intent-[^"]+)"', request)
            assert match is not None
            output = "BLACKBOARD_RESULT: " + json.dumps({
                "intent_id": match.group(1),
                "facts": [{"ref": "fact-result", "content": "verified by fake main"}],
                "edges": [{"type": "supports", "from": "fact-result", "to": self.goal_id}],
                "goal_achieved": False,
                "answer_key": None,
                "final_answer": None,
            })
        result = await super().execute(provider_config, request, challenge_dir, work_dir, **kwargs)
        Path(result.stdout_path).write_text(output + "\n", encoding="utf-8")
        return WorkerResult(**(result.to_dict() | {"output": output}))


class QinyuanAcceptanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_supervisor_writes_execution_and_fact_without_docker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = BlackboardStore.create(root / "blackboard.db", "project-a", "goal", "origin", ())
            snapshot = store.snapshot()
            origin_id = next(node["id"] for node in snapshot["nodes"] if node["type"] == "origin")
            goal_id = next(node["id"] for node in snapshot["nodes"] if node["type"] == "goal")
            runtime = FakeRuntime()
            service = SandboxService(runtime)
            bridge = SandboxBridgeServer(service)
            await bridge.start()
            wrapper = QinyuanMainProvider(backend=BlackboardBackend(origin_id, goal_id), bridge=bridge)
            supervisor = ProjectSupervisor(
                store, (provider(),), None, root,
                execute=wrapper.execute,
                answer_quorum_provider_count=1,
            )
            try:
                status = await supervisor.run_agent_once("qinyuan-main")
                self.assertEqual(status, "completed")
                self.assertEqual(runtime.create_calls, 0)
                executions = store.executions()
                self.assertEqual(len(executions), 1)
                self.assertEqual(executions[0]["provider_isolation"], "host_process")
                self.assertTrue(any(node["content"] == "verified by fake main" for node in store.snapshot()["nodes"]))
            finally:
                await bridge.close()
                store.close()


if __name__ == "__main__":
    unittest.main()
