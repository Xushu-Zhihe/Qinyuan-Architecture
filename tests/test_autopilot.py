import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from autopilot.manager import AutoManager
from autopilot.models import AutoStartConfig
from autopilot.protocol import parse_controller_result
from orchestrator.coordinator import ExecutionCoordinator
from worker.models import CoordinatorConfig, ProviderConfig, WorkerResult


class ProtocolTests(unittest.TestCase):
    def test_accepts_only_strict_task_line(self) -> None:
        parsed = parse_controller_result(
            'AUTOPILOT_RESULT: '
            + json.dumps(
                {
                    "action": "task",
                    "external_id": "q-1",
                    "name": "Question",
                    "goal": "Find the answer",
                    "origin": "Statement",
                    "hints": ["Read the file"],
                }
            )
        )
        self.assertEqual(parsed.status, "valid")
        self.assertEqual(parsed.result.external_id, "q-1")

    def test_rejects_extra_fields_and_missing_result(self) -> None:
        extra = parse_controller_result(
            'AUTOPILOT_RESULT: {"action":"finished","message":"done","token":"x"}'
        )
        missing = parse_controller_result("plain output")
        self.assertEqual(extra.status, "protocol_error")
        self.assertEqual(missing.status, "protocol_error")


class _FakeStore:
    def __init__(self, project_id: str) -> None:
        self.project_id = project_id
        self.project = {"status": "idle", "final_answer": None}

    def snapshot(self) -> dict[str, object]:
        return {"project": dict(self.project)}


class _FakeHandle:
    def __init__(self, project_id: str) -> None:
        self.id = project_id
        self.store = _FakeStore(project_id)


class _FakeRegistry:
    def __init__(self, root: Path) -> None:
        self.data_root = root


class _FakeService:
    def __init__(self, root: Path) -> None:
        self.registry = _FakeRegistry(root)
        self.coordinator = ExecutionCoordinator(
            CoordinatorConfig(
                max_global_running=2,
                max_project_running=1,
                max_global_planning=1,
                max_project_planning=1,
            )
        )
        self.handles: dict[str, _FakeHandle] = {}
        self.created: list[dict[str, object]] = []

    def create_project(self, **kwargs):
        project_id = f"project-{len(self.handles) + 1}"
        handle = _FakeHandle(project_id)
        self.handles[project_id] = handle
        self.created.append(kwargs)
        return handle

    async def start_project(self, project_id: str):
        self.handles[project_id].store.project = {
            "status": "achieved",
            "final_answer": "flag{ok}",
        }

    def get_handle(self, project_id: str):
        return self.handles[project_id]

    async def stop_project(self, project_id: str):
        self.handles[project_id].store.project["status"] = "stopped"


class ManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_completed_submission_advances_to_next_queue_item(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = _FakeService(root)
            provider = ProviderConfig("fake", True, 10, ("unused",), 10, max_running=1)
            calls: list[str] = []

            async def execute(config, request, challenge_dir, work_dir, **kwargs):
                calls.append(request)
                work_dir.mkdir(parents=True, exist_ok=True)
                if "Submit the supplied answer" in request:
                    output = 'AUTOPILOT_RESULT: {"action":"accepted","message":"ok"}'
                elif 'completed_ids=["q-1"]' in request:
                    output = 'AUTOPILOT_RESULT: {"action":"finished","message":"empty"}'
                else:
                    challenge = work_dir / "challenge"
                    challenge.mkdir()
                    (challenge / "input.txt").write_text("attachment", encoding="utf-8")
                    output = (
                        'AUTOPILOT_RESULT: '
                        + json.dumps(
                            {
                                "action": "task",
                                "external_id": "q-1",
                                "name": "First",
                                "goal": "Solve first",
                                "origin": "token=secret-token Prompt",
                                "hints": [],
                            }
                        )
                    )
                stdout = work_dir / "stdout.txt"
                stderr = work_dir / "stderr.txt"
                stdout.write_text(output, encoding="utf-8")
                stderr.write_text("", encoding="utf-8")
                return WorkerResult(
                    provider=config.name,
                    priority=config.priority,
                    status="success",
                    exit_code=0,
                    duration_seconds=0.01,
                    output=output,
                    stdout_path=str(stdout),
                    stderr_path=str(stderr),
                    work_dir=str(work_dir),
                )

            manager = AutoManager(
                service=service,
                provider_configs=(provider,),
                execute=execute,
            )
            config = AutoStartConfig(
                platform_prompt="API https://example.invalid token secret-token",
                controller_provider="fake",
                solver_providers=("fake",),
                agent_limit=1,
                max_tasks=3,
                poll_interval_seconds=0.001,
            )
            await manager.start(config)
            await asyncio.wait_for(manager._runner, timeout=2)
            snapshot = await manager.snapshot()
            session = snapshot["session"]
            self.assertEqual(session["status"], "finished")
            self.assertEqual(session["completed_tasks"], 1)
            self.assertNotIn("secret-token", json.dumps(snapshot))
            self.assertEqual(len(service.created), 1)
            attachment = service.created[0]["challenge_path"]
            self.assertTrue(Path(attachment).joinpath("input.txt").is_file())
            self.assertNotIn("secret-token", str(service.created[0]["origin"]))
            self.assertEqual(manager._session.config.platform_prompt, "")
            self.assertTrue(calls)
            await manager.aclose()


if __name__ == "__main__":
    unittest.main()
