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

    async def test_planning_uses_dedicated_timeout(self) -> None:
        store, root = self.make_store()
        observed_timeouts: list[float] = []

        async def execute(config, request, challenge_dir, work_dir, **kwargs):
            observed_timeouts.append(config.timeout_seconds)
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
                            "content": "Inspect the challenge",
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
                        "goal_achieved": False,
                        "final_answer": None,
                    }
                ),
            )

        configured = ProviderConfig(
            "codex",
            True,
            10,
            ("unused",),
            timeout_seconds=900,
            planning_timeout_seconds=30,
        )
        supervisor = ProjectSupervisor(
            store=store,
            providers=(configured,),
            challenge_dir=None,
            project_dir=root,
            execute=execute,
            max_iterations=1,
        )

        status = await supervisor.run_agent_once("codex")

        self.assertEqual(status, "completed")
        self.assertEqual(observed_timeouts, [30, 900])

    async def test_stale_bootstrap_plan_replans_without_failure(self) -> None:
        store, root = self.make_store()
        planning_calls = 0
        execution_calls = 0

        async def execute(config, request, challenge_dir, work_dir, **kwargs):
            nonlocal planning_calls, execution_calls
            if "BLACKBOARD_INTENT:" in request:
                planning_calls += 1
                snapshot = json.loads(request.split("Graph snapshot:\n", 1)[1])
                goal = next(
                    node["id"]
                    for node in snapshot["nodes"]
                    if node["type"] == "goal"
                )
                if planning_calls == 1:
                    source = next(
                        node["id"]
                        for node in snapshot["nodes"]
                        if node["type"] == "origin"
                    )
                    store.add_human_node("fact", "Concurrent evidence")
                else:
                    source = next(
                        node["id"]
                        for node in snapshot["nodes"]
                        if node["type"] == "fact"
                    )
                return self.result(
                    config.name,
                    work_dir,
                    "BLACKBOARD_INTENT: "
                    + json.dumps(
                        {
                            "action": "plan",
                            "source_node_id": source,
                            "target_node_id": goal,
                            "content": "Follow current evidence",
                        }
                    ),
                )

            execution_calls += 1
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
                        "goal_achieved": False,
                        "final_answer": None,
                    }
                ),
            )

        supervisor = ProjectSupervisor(
            store=store,
            providers=(provider("codex"),),
            challenge_dir=None,
            project_dir=root,
            execute=execute,
            max_iterations=1,
        )

        await supervisor.start()
        await supervisor.wait_until_idle(timeout=1)

        failed_events = [
            event
            for event in store.events_after(0)
            if event["type"] == "agent.status_changed"
            and event["payload"]["status"] == "failed"
        ]
        self.assertEqual(planning_calls, 2)
        self.assertEqual(execution_calls, 1)
        self.assertEqual(failed_events, [])
        self.assertEqual(supervisor.agent_statuses()["codex"], "completed")

    async def test_idle_agent_replans_only_after_graph_change(self) -> None:
        store, root = self.make_store()
        planning_calls = 0
        first_call = asyncio.Event()
        second_call = asyncio.Event()

        async def execute(config, request, challenge_dir, work_dir, **kwargs):
            nonlocal planning_calls
            if "BLACKBOARD_INTENT:" not in request:
                raise AssertionError("idle planner must not execute an Intent")
            planning_calls += 1
            if planning_calls == 1:
                first_call.set()
            elif planning_calls == 2:
                second_call.set()
            return self.result(
                config.name,
                work_dir,
                "BLACKBOARD_INTENT: "
                + json.dumps(
                    {
                        "action": "idle",
                        "reason": "Wait for new graph evidence",
                    }
                ),
            )

        supervisor = ProjectSupervisor(
            store=store,
            providers=(provider("codex"),),
            challenge_dir=None,
            project_dir=root,
            execute=execute,
            idle_poll_interval=0.01,
        )
        await supervisor.start()
        await asyncio.wait_for(first_call.wait(), timeout=1)
        await asyncio.sleep(0.05)

        self.assertEqual(planning_calls, 1)
        store.add_human_node("fact", "Human supplied evidence")
        await asyncio.wait_for(second_call.wait(), timeout=1)
        await supervisor.stop()

        self.assertEqual(planning_calls, 2)

    async def test_stop_wakes_idle_agent_immediately(self) -> None:
        store, root = self.make_store()
        idle_returned = asyncio.Event()

        async def execute(config, request, challenge_dir, work_dir, **kwargs):
            idle_returned.set()
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

        supervisor = ProjectSupervisor(
            store=store,
            providers=(provider("codex"),),
            challenge_dir=None,
            project_dir=root,
            execute=execute,
            idle_poll_interval=10,
        )
        await supervisor.start()
        await asyncio.wait_for(idle_returned.wait(), timeout=1)
        await asyncio.sleep(0)

        await asyncio.wait_for(supervisor.stop(), timeout=0.2)

        self.assertEqual(store.snapshot()["project"]["status"], "stopped")

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

    async def test_protocol_error_retries_same_intent_and_reaches_goal(
        self,
    ) -> None:
        store, root = self.make_store()
        planning_calls = 0
        execution_requests: list[str] = []
        hub = ProjectEventHub()
        published_events: list[dict[str, object]] = []

        async def execute(config, request, challenge_dir, work_dir, **kwargs):
            nonlocal planning_calls
            if "BLACKBOARD_INTENT:" in request:
                planning_calls += 1
                return self.result(
                    config.name,
                    work_dir,
                    "BLACKBOARD_INTENT: "
                    + json.dumps(
                        {
                            "action": "plan",
                            "source_node_id": store.node_id("origin"),
                            "target_node_id": store.node_id("goal"),
                            "content": "Recover the flag",
                        }
                    ),
                )

            execution_requests.append(request)
            intent_id = next(
                edge["id"]
                for edge in store.snapshot()["edges"]
                if edge["type"] == "intent"
            )
            if len(execution_requests) == 1:
                payload = {
                    "intent_id": intent_id,
                    "facts": ["flag = flag{same}"],
                    "edges": [],
                    "goal_achieved": True,
                    "final_answer": "flag{same}",
                }
            else:
                payload = {
                    "intent_id": intent_id,
                    "facts": [
                        {
                            "ref": "flag-fact",
                            "content": "flag = flag{same}",
                        }
                    ],
                    "edges": [
                        {
                            "type": "supports",
                            "from": "flag-fact",
                            "to": store.node_id("goal"),
                        }
                    ],
                    "goal_achieved": True,
                    "final_answer": "flag{same}",
                }
            return self.result(
                config.name,
                work_dir,
                "BLACKBOARD_RESULT: " + json.dumps(payload),
            )

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
                published_events.append(queue.get_nowait())
        snapshot = store.snapshot()
        intent = next(
            edge for edge in snapshot["edges"] if edge["type"] == "intent"
        )

        self.assertEqual(status, "completed")
        self.assertEqual(planning_calls, 1)
        self.assertEqual(len(execution_requests), 2)
        self.assertIn(
            "fact proposal must be an object",
            execution_requests[1],
        )
        self.assertIn("same assigned Intent", execution_requests[1])
        self.assertEqual(
            [item["status"] for item in intent["executions"]],
            ["protocol_error", "completed"],
        )
        self.assertEqual(
            len({item["work_dir"] for item in intent["executions"]}),
            2,
        )
        self.assertEqual(snapshot["project"]["status"], "achieved")
        self.assertEqual(
            snapshot["project"]["final_answer"],
            "flag{same}",
        )
        self.assertEqual(
            [
                event["payload"]["status"]
                for event in published_events
                if event["type"] == "intent.execution_finished"
            ],
            ["protocol_error", "completed"],
        )

    async def test_protocol_correction_stops_after_second_error(self) -> None:
        store, root = self.make_store()
        execution_calls = 0

        async def execute(config, request, challenge_dir, work_dir, **kwargs):
            nonlocal execution_calls
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
                            "content": "Recover the flag",
                        }
                    ),
                )
            execution_calls += 1
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
                        "facts": ["invalid fact"],
                        "edges": [],
                        "goal_achieved": True,
                        "final_answer": "flag{same}",
                    }
                ),
            )

        supervisor = ProjectSupervisor(
            store=store,
            providers=(provider("codex"),),
            challenge_dir=None,
            project_dir=root,
            execute=execute,
            max_iterations=1,
        )

        status = await supervisor.run_agent_once("codex")
        intent = next(
            edge
            for edge in store.snapshot()["edges"]
            if edge["type"] == "intent"
        )

        self.assertEqual(status, "failed")
        self.assertEqual(execution_calls, 2)
        self.assertEqual(
            [item["status"] for item in intent["executions"]],
            ["protocol_error", "protocol_error"],
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
