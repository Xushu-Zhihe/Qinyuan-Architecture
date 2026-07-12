import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from web.app import create_app
from worker.models import ProviderConfig, WorkerResult


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


if __name__ == "__main__":
    unittest.main()
