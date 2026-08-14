import json
import asyncio
import tempfile
import time
import unittest
from pathlib import Path

from orchestrator.coordinator import ExecutionCoordinator
from orchestrator.events import ProjectEventHub
from orchestrator.health import ProbeResult
from orchestrator.store import BlackboardStore
from orchestrator.supervisor import ProjectSupervisor
from worker.models import (
    CoordinatorConfig,
    HealthCheckConfig,
    LogChunk,
    ProviderConfig,
    WorkerResult,
)


def provider(name: str) -> ProviderConfig:
    return ProviderConfig(name, True, 10, ("unused",), 5)


def health_provider(
    name: str,
    *,
    cooldown: float = 0.01,
    threshold: int = 3,
) -> ProviderConfig:
    return ProviderConfig(
        name,
        True,
        10,
        ("unused",),
        5,
        health_check=HealthCheckConfig(
            ("unused", "--version"),
            1,
            cooldown,
            max(cooldown, 0.05),
            threshold,
        ),
    )


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
        backend_metadata: dict[str, object] | None = None,
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
            backend_metadata=backend_metadata or {},
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
        planning = store.planning_attempts()
        executions = store.executions()
        self.assertEqual(len(planning), 1)
        self.assertEqual(planning[0]["status"], "valid")
        self.assertEqual(planning[0]["intent_edge_id"], executions[0]["intent_edge_id"])
        self.assertEqual(executions[0]["status"], "completed")
        self.assertEqual(executions[0]["retry_count"], 0)
        self.assertEqual(executions[0]["duration_seconds"], 0.01)
        self.assertEqual(
            (Path(str(executions[0]["stdout_path"])).with_name("combined.txt"))
            .read_text(encoding="utf-8"),
            "[stdout]\nworking\n",
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

    async def test_timeout_recovers_verified_facts_with_summary_pass(self) -> None:
        store, root = self.make_store()
        calls: list[str] = []

        async def execute(config, request, challenge_dir, work_dir, **kwargs):
            if "BLACKBOARD_INTENT:" in request:
                calls.append("planning")
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
            if "immediate timeout-recovery pass" in request:
                calls.append("timeout-summary")
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
                            "facts": [
                                {
                                    "ref": "recovered-fact",
                                    "content": "The captured output verified the key.",
                                }
                            ],
                            "edges": [],
                            "goal_achieved": False,
                            "answer_key": None,
                            "final_answer": None,
                        }
                    ),
                )
            calls.append("execution")
            return self.result(
                config.name,
                work_dir,
                "The key was verified before the provider timed out.",
                status="timeout",
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

        self.assertEqual(status, "completed")
        self.assertEqual(calls, ["planning", "execution", "timeout-summary"])
        facts = [
            node["content"]
            for node in store.snapshot()["nodes"]
            if node["type"] == "fact"
        ]
        self.assertEqual(facts, ["The captured output verified the key."])
        execution = store.executions()[0]
        self.assertEqual(execution["status"], "completed")
        self.assertEqual(
            execution["backend_metadata"]["timeout_recovery"]["mode"],
            "summary_process",
        )

    async def test_timeout_with_existing_result_does_not_start_second_pass(self) -> None:
        store, root = self.make_store()
        calls = 0

        async def execute(config, request, challenge_dir, work_dir, **kwargs):
            nonlocal calls
            calls += 1
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
                        "facts": [
                            {"ref": "partial", "content": "Captured fact"}
                        ],
                        "edges": [],
                        "goal_achieved": False,
                        "answer_key": None,
                        "final_answer": None,
                    }
                ),
                status="timeout",
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

        self.assertEqual(status, "completed")
        self.assertEqual(calls, 2)
        self.assertEqual(
            store.executions()[0]["backend_metadata"]["timeout_recovery"]["mode"],
            "captured_result",
        )

    async def test_empty_timeout_summary_preserves_timeout_failure(self) -> None:
        store, root = self.make_store()

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
                            "content": "Inspect the challenge",
                        }
                    ),
                )
            if "immediate timeout-recovery pass" not in request:
                return self.result(
                    config.name,
                    work_dir,
                    "No verified result was produced.",
                    status="timeout",
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
                        "answer_key": None,
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

        status = await supervisor.run_agent_once("codex")

        self.assertEqual(status, "health_failure")
        self.assertEqual(store.executions()[0]["status"], "timeout")
        self.assertFalse(
            any(node["type"] == "fact" for node in store.snapshot()["nodes"])
        )

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

    async def test_missing_provider_is_skipped_while_healthy_provider_runs(
        self,
    ) -> None:
        store, root = self.make_store()
        execution_calls: list[str] = []

        async def health_probe(config):
            if config.name == "missing":
                return ProbeResult("missing", "not found")
            return ProbeResult("success", cli_version="healthy-cli 1.0")

        async def execute(config, request, challenge_dir, work_dir, **kwargs):
            execution_calls.append(config.name)
            return self.result(
                config.name,
                work_dir,
                "BLACKBOARD_INTENT: "
                + json.dumps({"action": "idle", "reason": "No work"}),
            )

        supervisor = ProjectSupervisor(
            store=store,
            providers=(health_provider("missing"), health_provider("healthy")),
            challenge_dir=None,
            project_dir=root,
            execute=execute,
            health_probe=health_probe,
            max_iterations=1,
        )

        await supervisor.start()
        await supervisor.wait_until_idle(timeout=1)

        self.assertEqual(execution_calls, ["healthy"])
        self.assertEqual(
            store.provider_health_for("missing")["status"],
            "missing",
        )
        self.assertEqual(
            store.provider_health_for("healthy")["status"],
            "healthy",
        )
        self.assertEqual(
            store.provider_health_for("healthy")["cli_version"],
            "healthy-cli 1.0",
        )
        self.assertEqual(store.snapshot()["project"]["status"], "degraded")

    async def test_auth_failure_stops_without_retry_and_redacts_state(self) -> None:
        store, root = self.make_store()
        calls = 0
        results: list[WorkerResult] = []

        async def health_probe(config):
            return ProbeResult("success")

        async def execute(config, request, challenge_dir, work_dir, **kwargs):
            nonlocal calls
            calls += 1
            result = self.result(
                config.name,
                work_dir,
                "403 Forbidden token=sk-secret-value",
                status="failed",
            )
            results.append(result)
            return result

        supervisor = ProjectSupervisor(
            store=store,
            providers=(health_provider("codex"),),
            challenge_dir=None,
            project_dir=root,
            execute=execute,
            health_probe=health_probe,
            max_iterations=5,
        )

        await supervisor.start()
        await supervisor.wait_until_idle(timeout=1)

        health = store.provider_health_for("codex")
        self.assertEqual(calls, 1)
        self.assertEqual(health["status"], "auth_failed")
        self.assertNotIn("sk-secret-value", str(health["last_error"]))
        self.assertEqual(Path(results[0].stdout_path).read_text(encoding="utf-8"), "")
        self.assertNotIn(
            "sk-secret-value",
            Path(results[0].stderr_path).read_text(encoding="utf-8"),
        )
        self.assertEqual(store.snapshot()["project"]["status"], "degraded")

    async def test_temporary_failure_cools_down_then_recovers(self) -> None:
        store, root = self.make_store()
        planning_times: list[float] = []
        probes = 0

        async def health_probe(config):
            nonlocal probes
            probes += 1
            return ProbeResult("success")

        async def execute(config, request, challenge_dir, work_dir, **kwargs):
            planning_times.append(time.monotonic())
            if len(planning_times) == 1:
                return self.result(
                    config.name,
                    work_dir,
                    "503 Service Unavailable",
                    status="failed",
                )
            return self.result(
                config.name,
                work_dir,
                "BLACKBOARD_INTENT: "
                + json.dumps({"action": "idle", "reason": "Recovered"}),
            )

        supervisor = ProjectSupervisor(
            store=store,
            providers=(health_provider("codex", cooldown=0.02),),
            challenge_dir=None,
            project_dir=root,
            execute=execute,
            health_probe=health_probe,
            max_iterations=2,
        )

        await supervisor.start()
        await supervisor.wait_until_idle(timeout=1)

        self.assertEqual(len(planning_times), 2)
        self.assertGreaterEqual(planning_times[1] - planning_times[0], 0.015)
        self.assertEqual(probes, 2)
        health = store.provider_health_for("codex")
        self.assertEqual(health["status"], "healthy")
        self.assertEqual(health["consecutive_failures"], 0)
        self.assertEqual(store.snapshot()["project"]["status"], "idle")

    async def test_repeated_temporary_failure_reaches_circuit_threshold(
        self,
    ) -> None:
        store, root = self.make_store()
        calls = 0

        async def health_probe(config):
            return ProbeResult("success")

        async def execute(config, request, challenge_dir, work_dir, **kwargs):
            nonlocal calls
            calls += 1
            return self.result(
                config.name,
                work_dir,
                "503 Service Unavailable",
                status="failed",
            )

        supervisor = ProjectSupervisor(
            store=store,
            providers=(health_provider("codex", threshold=2),),
            challenge_dir=None,
            project_dir=root,
            execute=execute,
            health_probe=health_probe,
            max_iterations=2,
        )

        await supervisor.start()
        await supervisor.wait_until_idle(timeout=1)

        health = store.provider_health_for("codex")
        self.assertEqual(calls, 2)
        self.assertEqual(health["status"], "cooldown")
        self.assertEqual(health["consecutive_failures"], 2)
        self.assertEqual(store.snapshot()["project"]["status"], "degraded")

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
                        "answer_key": "flag{same}",
                        "final_answer": (
                            "flag{same}"
                            if config.name == "codex"
                            else "Verified answer: flag{same}"
                        ),
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
            "Verified answer: flag{same}",
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
                answer_quorum_provider_count=1,
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
            [item["retry_count"] for item in intent["executions"]],
            [0, 1],
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
        health = store.provider_health_for("codex")
        self.assertEqual(health["status"], "unknown")
        self.assertEqual(health["consecutive_failures"], 0)

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

    async def test_stop_removes_planning_waiter_without_calling_provider(self) -> None:
        store, root = self.make_store()
        coordinator = ExecutionCoordinator(
            CoordinatorConfig(
                max_global_running=1,
                max_project_running=1,
                max_global_planning=1,
                max_project_planning=1,
                aging_seconds=1,
            )
        )
        blocker_started = asyncio.Event()
        blocker_release = asyncio.Event()
        execute_called = False

        async def hold_capacity() -> None:
            async with coordinator.permit(
                project_id="other-project",
                provider="other-provider",
                kind="execution",
                priority=1,
                provider_limit=1,
            ):
                blocker_started.set()
                await blocker_release.wait()

        async def execute(config, request, challenge_dir, work_dir, **kwargs):
            nonlocal execute_called
            execute_called = True
            raise AssertionError("cancelled planning waiter called provider")

        blocker = asyncio.create_task(hold_capacity())
        await blocker_started.wait()
        supervisor = ProjectSupervisor(
            store=store,
            providers=(provider("codex"),),
            challenge_dir=None,
            project_dir=root,
            execute=execute,
            coordinator=coordinator,
        )
        await supervisor.start()
        for _ in range(100):
            if (await coordinator.snapshot())["pending"]["total"] == 1:
                break
            await asyncio.sleep(0)
        else:
            self.fail("planning call did not enter the coordinator queue")

        await asyncio.wait_for(supervisor.stop(), timeout=1)
        snapshot = await coordinator.snapshot()
        self.assertFalse(execute_called)
        self.assertEqual(store.snapshot()["project"]["status"], "stopped")
        self.assertEqual(snapshot["pending"]["total"], 0)
        self.assertEqual(snapshot["running"]["global"], 1)
        planning = store.planning_attempts()
        self.assertEqual(len(planning), 1)
        self.assertEqual(planning[0]["status"], "cancelled")
        self.assertEqual(
            planning[0]["decision_reason"],
            "scheduler permit wait cancelled",
        )
        self.assertIn("global_limit", planning[0]["wait_reasons"])

        blocker_release.set()
        await blocker

    async def test_procedural_completion_is_not_accepted(self) -> None:
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
                            "content": "Probe JS assets and source maps for routes",
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
                        "facts": [
                            {
                                "ref": "plan-fact",
                                "content": "Focus on the next client-side probe",
                            }
                        ],
                        "edges": [],
                        "goal_achieved": True,
                        "answer_key": "probe js assets and source maps",
                        "final_answer": "Probe JS assets and source maps, then extract routes.",
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

        self.assertEqual(status, "failed")
        self.assertEqual(execution_calls, 2)
        intent = next(
            edge for edge in store.snapshot()["edges"] if edge["type"] == "intent"
        )
        self.assertEqual(
            [execution["status"] for execution in intent["executions"]],
            ["protocol_error", "protocol_error"],
        )
        self.assertNotEqual(store.snapshot()["project"]["status"], "achieved")

    async def test_tool_call_protocol_failure_retries_same_intent(self) -> None:
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
            if execution_calls == 1:
                return self.result(
                    config.name,
                    work_dir,
                    "BLACKBOARD_RESULT: {\"status\":\"failed\"}",
                    status="failed",
                    backend_metadata={
                        "tool_protocol_error": (
                            "No tool call found for function call output with call_id call_1"
                        )
                    },
                )
            return self.result(
                config.name,
                work_dir,
                "BLACKBOARD_RESULT: "
                + json.dumps(
                    {
                        "intent_id": intent_id,
                        "facts": [
                            {"ref": "verified", "content": "The key was verified"}
                        ],
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

        status = await supervisor.run_agent_once("codex")

        self.assertEqual(status, "completed")
        self.assertEqual(execution_calls, 2)
        intent = next(
            edge for edge in store.snapshot()["edges"] if edge["type"] == "intent"
        )
        self.assertEqual(
            [execution["status"] for execution in intent["executions"]],
            ["protocol_error", "completed"],
        )


if __name__ == "__main__":
    unittest.main()
