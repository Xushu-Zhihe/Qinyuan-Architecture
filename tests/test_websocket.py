import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from orchestrator.health import ProbeResult
from orchestrator.models import IntentProposal
from web.app import create_app
from worker.models import HealthCheckConfig, ProviderConfig, WorkerResult


class WebSocketTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)

    @staticmethod
    def project_payload(name: str, providers=None) -> dict[str, object]:
        return {
            "name": name,
            "goal": "Recover flag",
            "origin": "Challenge",
            "hints": [],
            "mode": "run" if providers else "manual",
            "providers": providers or [],
            "agent_limit": 2,
            "challenge_path": None,
        }

    def test_reconnect_replays_persisted_events_after_sequence(self) -> None:
        with TestClient(create_app(Path(self.temporary.name))) as client:
            project = client.post(
                "/api/projects",
                json=self.project_payload("Alpha"),
            ).json()
            graph = client.get(
                f"/api/projects/{project['id']}/graph"
            ).json()
            sequence = graph["sequence"]
            client.post(
                f"/api/projects/{project['id']}/nodes",
                json={"type": "fact", "content": "New fact"},
            )

            with client.websocket_connect(
                f"/ws/projects/{project['id']}?after={sequence}"
            ) as socket:
                event = socket.receive_json()

            self.assertEqual(event["project_id"], project["id"])
            self.assertEqual(event["type"], "graph.node_added")
            self.assertGreater(event["sequence"], sequence)

    def test_live_websocket_is_project_scoped(self) -> None:
        with TestClient(create_app(Path(self.temporary.name))) as client:
            alpha = client.post(
                "/api/projects",
                json=self.project_payload("Alpha"),
            ).json()
            beta = client.post(
                "/api/projects",
                json=self.project_payload("Beta"),
            ).json()
            sequence = client.get(
                f"/api/projects/{alpha['id']}/graph"
            ).json()["sequence"]

            with client.websocket_connect(
                f"/ws/projects/{alpha['id']}?after={sequence}"
            ) as socket:
                client.post(
                    f"/api/projects/{beta['id']}/nodes",
                    json={"type": "fact", "content": "Wrong project"},
                )
                client.post(
                    f"/api/projects/{alpha['id']}/nodes",
                    json={"type": "fact", "content": "Right project"},
                )
                event = socket.receive_json()

            self.assertEqual(event["project_id"], alpha["id"])
            self.assertEqual(event["payload"]["type"], "fact")

    def test_restart_recovery_events_are_replayed_after_sequence(self) -> None:
        data_root = Path(self.temporary.name)
        with TestClient(create_app(data_root)) as client:
            project = client.post(
                "/api/projects",
                json=self.project_payload("Recovery"),
            ).json()
            project_id = str(project["id"])
            service = client.app.state.projects
            handle = service.get_handle(project_id)
            fact_id = handle.store.add_human_node("fact", "Known evidence")
            intent = handle.store.create_or_merge_intent(
                "human",
                IntentProposal(
                    fact_id,
                    handle.store.node_id("goal"),
                    "Continue investigation",
                ),
            )
            execution = handle.store.start_execution(
                intent.id,
                "codex",
                "stdout.txt",
                "stderr.txt",
                "work",
            )
            handle.store.set_project_status("running")
            service.registry.update_status(project_id, "running")
            sequence = int(handle.store.snapshot()["sequence"])

        with TestClient(create_app(data_root)) as client:
            with client.websocket_connect(
                f"/ws/projects/{project_id}?after={sequence}"
            ) as socket:
                execution_event = socket.receive_json()
                project_event = socket.receive_json()

            self.assertEqual(execution_event["project_id"], project_id)
            self.assertEqual(
                execution_event["type"],
                "intent.execution_finished",
            )
            self.assertEqual(
                execution_event["payload"]["execution_id"],
                execution.id,
            )
            self.assertEqual(
                execution_event["payload"]["status"],
                "interrupted",
            )
            self.assertEqual(
                execution_event["payload"]["error_message"],
                "server restarted",
            )
            self.assertEqual(project_event["type"], "project.status_changed")
            self.assertEqual(project_event["payload"]["status"], "idle")

            detail = client.get(f"/api/projects/{project_id}").json()
            graph = client.get(f"/api/projects/{project_id}/graph").json()
            recovered = client.get(
                f"/api/projects/{project_id}/executions/{execution.id}"
            ).json()
            self.assertEqual(detail["status"], "idle")
            self.assertEqual(graph["project"]["status"], "idle")
            self.assertEqual(recovered["status"], "interrupted")
            self.assertEqual(recovered["error_message"], "server restarted")

    def test_start_and_stop_control_project_supervisor(self) -> None:
        provider = ProviderConfig("fake", True, 10, ("unused",), 5)

        async def execute(config, request, challenge_dir, work_dir, **kwargs):
            work_dir.mkdir(parents=True, exist_ok=True)
            stdout = work_dir / "stdout.txt"
            stderr = work_dir / "stderr.txt"
            if "BLACKBOARD_INTENT:" in request:
                snapshot = request.split("Graph snapshot:\n", 1)[1]
                payload = json.loads(snapshot)
                origin = next(
                    node["id"]
                    for node in payload["nodes"]
                    if node["type"] == "origin"
                )
                goal = next(
                    node["id"]
                    for node in payload["nodes"]
                    if node["type"] == "goal"
                )
                output = "BLACKBOARD_INTENT: " + json.dumps(
                    {
                        "action": "plan",
                        "source_node_id": origin,
                        "target_node_id": goal,
                        "content": "Long work",
                    }
                )
                status = "success"
            else:
                await kwargs["cancel_event"].wait()
                output = ""
                status = "cancelled"
            stdout.write_text(output, encoding="utf-8")
            stderr.write_text("", encoding="utf-8")
            return WorkerResult(
                config.name,
                10,
                status,
                0 if status == "success" else None,
                0.01,
                output,
                str(stdout),
                str(stderr),
                str(work_dir),
            )

        app = create_app(
            Path(self.temporary.name),
            provider_configs=(provider,),
            execute=execute,
        )
        with TestClient(app) as client:
            project = client.post(
                "/api/projects",
                json=self.project_payload("Running", ["fake"]),
            ).json()

            started = client.post(f"/api/projects/{project['id']}/start")
            stopped = client.post(f"/api/projects/{project['id']}/stop")

            self.assertEqual(started.status_code, 202)
            self.assertEqual(stopped.status_code, 200)
            self.assertEqual(stopped.json()["status"], "stopped")

    def test_provider_health_is_visible_via_api_and_websocket(self) -> None:
        provider = ProviderConfig(
            "missing",
            True,
            10,
            ("unused",),
            5,
            health_check=HealthCheckConfig(
                ("unused", "--version"),
                1,
                1,
            ),
        )

        async def health_probe(config):
            return ProbeResult("missing", "not found")

        async def execute(*args, **kwargs):
            raise AssertionError("missing provider must not execute")

        app = create_app(
            Path(self.temporary.name),
            provider_configs=(provider,),
            execute=execute,
            health_probe=health_probe,
        )
        with TestClient(app) as client:
            project = client.post(
                "/api/projects",
                json=self.project_payload("Health", ["missing"]),
            ).json()
            project_id = project["id"]
            sequence = client.get(
                f"/api/projects/{project_id}/graph"
            ).json()["sequence"]

            with client.websocket_connect(
                f"/ws/projects/{project_id}?after={sequence}"
            ) as socket:
                started = client.post(f"/api/projects/{project_id}/start")
                event = socket.receive_json()

            providers = client.get(
                f"/api/projects/{project_id}/providers"
            ).json()
            detail = client.get(f"/api/projects/{project_id}").json()
            graph = client.get(f"/api/projects/{project_id}/graph").json()

            self.assertEqual(started.status_code, 202)
            self.assertEqual(started.json()["status"], "degraded")
            self.assertEqual(started.json()["providers"][0]["status"], "missing")
            self.assertEqual(event["type"], "provider.health_changed")
            self.assertEqual(event["payload"]["status"], "missing")
            self.assertEqual(providers[0]["status"], "missing")
            self.assertEqual(detail["provider_health"][0]["status"], "missing")
            self.assertEqual(graph["provider_health"][0]["status"], "missing")


if __name__ == "__main__":
    unittest.main()
