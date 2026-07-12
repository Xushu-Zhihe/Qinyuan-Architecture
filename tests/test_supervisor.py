import json
import asyncio
import tempfile
import unittest
from pathlib import Path

from orchestrator.events import ProjectEventHub
from orchestrator.store import BlackboardStore
from orchestrator.supervisor import ProjectSupervisor
from worker.models import LogChunk, ProviderConfig, WorkerResult


def provider(name: str) -> ProviderConfig:
    return ProviderConfig(name, True, 10, ("unused",), 5)


class SupervisorTests(unittest.IsolatedAsyncioTestCase):
    def make_store(self) -> tuple[BlackboardStore, Path]:
        temporary = tempfile.TemporaryDirectory()
        self.addAsyncCleanup(__import__("asyncio").to_thread, temporary.cleanup)
        root = Path(temporary.name)
        store = BlackboardStore.create(
            root / "blackboard.db",
            "project-1",
            "Recover the flag",
            "Challenge statement",
            (),
        )
        self.addAsyncCleanup(__import__("asyncio").to_thread, store.close)
        return store, root

    @staticmethod
    def result(
        provider_name: str,
        work_dir: Path,
        output: str,
        status: str = "success",
    ) -> WorkerResult:
        work_dir.mkdir(parents=True, exist_ok=True)
        stdout = work_dir / "stdout.txt"
        stderr = work_dir / "stderr.txt"
        stdout.write_text(output, encoding="utf-8")
        stderr.write_text("", encoding="utf-8")
        return WorkerResult(
            provider=provider_name,
            priority=10,
            status=status,
            exit_code=0 if status == "success" else None,
            duration_seconds=0.01,
            output=output,
            stdout_path=str(stdout),
            stderr_path=str(stderr),
            work_dir=str(work_dir),
        )

    async def test_agent_plans_before_execution_and_streams_log(self) -> None:
        store, root = self.make_store()
        calls: list[str] = []
        hub = ProjectEventHub()
        events: list[dict[str, object]] = []

        async def execute(config, request, challenge_dir, work_dir, **kwargs):
            if "BLACKBOARD_INTENT:" in request:
                calls.append("planning")
                origin = store.node_id("origin")
                goal = store.node_id("goal")
                output = "BLACKBOARD_INTENT: " + json.dumps(
                    {
                        "action": "plan",
                        "source_node_id": origin,
                        "target_node_id": goal,
                        "content": "Inspect the challenge",
                    }
                )
                return self.result(config.name, work_dir, output)
            calls.append("execution")
            callback = kwargs["on_chunk"]
            await callback(LogChunk("stdout", "working\n"))
            intent_id = store.snapshot()["edges"][-1]["id"]
            output = "BLACKBOARD_RESULT: " + json.dumps(
                {
                    "intent_id": intent_id,
                    "facts": [
                        {"ref": "f1", "content": "Useful fact"}
                    ],
                    "edges": [],
                    "goal_achieved": False,
                    "final_answer": None,
                }
            )
            return self.result(config.name, work_dir, output)

        async with hub.subscribe() as queue:
            supervisor = ProjectSupervisor(
                store=store,
                providers=(provider("codex"),),
                challenge_dir=None,
                project_dir=root,
                hub=hub,
                execute=execute,
                max_iterations=1,
            )
            status = await supervisor.run_agent_once("codex")
            while not queue.empty():
                events.append(queue.get_nowait())

        self.assertEqual(status, "completed")
        self.assertEqual(calls, ["planning", "execution"])
        self.assertTrue(
            any(event["type"] == "intent.log_chunk" for event in events)
        )
        self.assertEqual(
            [edge["type"] for edge in store.snapshot()["edges"]],
            ["intent"],
        )

    async def test_one_agent_failure_does_not_stop_another(self) -> None:
        store, root = self.make_store()

        async def execute(config, request, challenge_dir, work_dir, **kwargs):
            if config.name == "broken":
                return self.result(
                    config.name,
                    work_dir,
                    "",
                    status="unavailable",
                )
            if "BLACKBOARD_INTENT:" in request:
                return self.result(
                    config.name,
                    work_dir,
                    "BLACKBOARD_INTENT: "
                    + json.dumps(
                        {
                            "action": "idle",
                            "reason": "No unresolved work",
                        }
                    ),
                )
            raise AssertionError("idle agent must not execute")

        supervisor = ProjectSupervisor(
            store=store,
            providers=(provider("broken"), provider("working")),
            challenge_dir=None,
            project_dir=root,
            execute=execute,
            max_iterations=1,
        )

        await supervisor.start()
        await supervisor.wait_until_idle(timeout=1)

        self.assertEqual(supervisor.agent_statuses()["broken"], "failed")
        self.assertEqual(supervisor.agent_statuses()["working"], "idle")

    async def test_matching_answers_reach_consensus(self) -> None:
        store, root = self.make_store()

        async def execute(config, request, challenge_dir, work_dir, **kwargs):
            if "BLACKBOARD_INTENT:" in request:
                origin = store.node_id("origin")
                goal = store.node_id("goal")
                return self.result(
                    config.name,
                    work_dir,
                    "BLACKBOARD_INTENT: "
                    + json.dumps(
                        {
                            "action": "plan",
                            "source_node_id": origin,
                            "target_node_id": goal,
                            "content": "Solve independently",
                        }
                    ),
                )
            intent_id = next(
                edge["id"]
                for edge in store.snapshot()["edges"]
                if edge["type"] == "intent"
            )
            return self.result(
                config.name,
                work_dir,
                "BLACKBOARD_RESULT: "
                + json.dumps(
                    {
                        "intent_id": intent_id,
                        "facts": [],
                        "edges": [],
                        "goal_achieved": True,
                        "final_answer": "flag{same}",
                    }
                ),
            )

        supervisor = ProjectSupervisor(
            store=store,
            providers=(provider("codex"), provider("claude")),
            challenge_dir=None,
            project_dir=root,
            execute=execute,
            max_iterations=1,
        )

        await supervisor.start()
        await supervisor.wait_until_idle(timeout=1)

        self.assertEqual(store.snapshot()["project"]["status"], "achieved")
        self.assertEqual(
            store.snapshot()["project"]["final_answer"],
            "flag{same}",
        )

    async def test_stop_cancels_active_execution_and_marks_project_stopped(self) -> None:
        store, root = self.make_store()
        execution_started = asyncio.Event()

        async def execute(config, request, challenge_dir, work_dir, **kwargs):
            if "BLACKBOARD_INTENT:" in request:
                return self.result(
                    config.name,
                    work_dir,
                    "BLACKBOARD_INTENT: "
                    + json.dumps(
                        {
                            "action": "plan",
                            "source_node_id": store.node_id("origin"),
                            "target_node_id": store.node_id("goal"),
                            "content": "Long running work",
                        }
                    ),
                )
            execution_started.set()
            await kwargs["cancel_event"].wait()
            return self.result(
                config.name,
                work_dir,
                "",
                status="cancelled",
            )

        supervisor = ProjectSupervisor(
            store=store,
            providers=(provider("codex"),),
            challenge_dir=None,
            project_dir=root,
            execute=execute,
        )
        await supervisor.start()
        await asyncio.wait_for(execution_started.wait(), timeout=1)

        await supervisor.stop()

        self.assertEqual(store.snapshot()["project"]["status"], "stopped")
        intent = next(
            edge
            for edge in store.snapshot()["edges"]
            if edge["type"] == "intent"
        )
        self.assertEqual(intent["executions"][0]["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
