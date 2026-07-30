import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from web.app import create_app


class WebApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.data_root = Path(self.temporary.name)
        self.client_context = TestClient(create_app(self.data_root))
        self.client = self.client_context.__enter__()
        self.addCleanup(
            self.client_context.__exit__,
            None,
            None,
            None,
        )

    def create_project(self, name: str = "Demo") -> dict[str, object]:
        response = self.client.post(
            "/api/projects",
            json={
                "name": name,
                "goal": "Recover the flag",
                "origin": "Challenge statement",
                "hints": ["Inspect config"],
                "mode": "manual",
                "providers": [],
                "agent_limit": 1,
                "challenge_path": None,
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def test_creates_manual_project_and_returns_graph(self) -> None:
        project = self.create_project()

        graph = self.client.get(
            f"/api/projects/{project['id']}/graph"
        )

        self.assertEqual(graph.status_code, 200)
        self.assertEqual(graph.json()["project"]["name"], "Demo")
        self.assertEqual(
            [node["type"] for node in graph.json()["nodes"]],
            ["goal", "origin", "hint"],
        )
        self.assertEqual(graph.json()["layout"]["revision"], 0)

    def test_reopens_persisted_project_in_new_app(self) -> None:
        project = self.create_project("Persistent")

        second_context = TestClient(create_app(self.data_root))
        second = second_context.__enter__()
        self.addCleanup(second_context.__exit__, None, None, None)

        response = second.get(f"/api/projects/{project['id']}/graph")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["project"]["id"], project["id"])

    def test_project_status_is_consistent_across_all_read_apis(self) -> None:
        project = self.create_project("Status")
        project_id = str(project["id"])
        service = self.client.app.state.projects
        handle = service.get_handle(project_id)

        for expected in ("achieved", "degraded", "idle", "stopped"):
            answer = "flag{done}" if expected == "achieved" else None
            handle.store.set_project_status(expected, answer)
            service.registry.update_status(project_id, "stale-cache")

            projects = self.client.get("/api/projects").json()
            detail = self.client.get(f"/api/projects/{project_id}").json()
            graph = self.client.get(
                f"/api/projects/{project_id}/graph"
            ).json()

            listed = next(item for item in projects if item["id"] == project_id)
            self.assertEqual(listed["status"], expected)
            self.assertEqual(detail["status"], expected)
            self.assertEqual(graph["project"]["status"], expected)
            self.assertEqual(service.registry.get(project_id).status, expected)

    def test_adds_human_fact_and_intent_edge(self) -> None:
        project = self.create_project()
        project_id = project["id"]
        graph = self.client.get(f"/api/projects/{project_id}/graph").json()
        goal = next(node for node in graph["nodes"] if node["type"] == "goal")

        fact_response = self.client.post(
            f"/api/projects/{project_id}/nodes",
            json={"type": "fact", "content": "JWT uses HS256"},
        )
        self.assertEqual(fact_response.status_code, 201)
        fact = fact_response.json()
        edge_response = self.client.post(
            f"/api/projects/{project_id}/edges",
            json={
                "type": "intent",
                "from_node_id": fact["id"],
                "to_node_id": goal["id"],
                "content": "Find the signing secret",
            },
        )

        self.assertEqual(edge_response.status_code, 201)
        self.assertEqual(edge_response.json()["type"], "intent")
        refreshed = self.client.get(
            f"/api/projects/{project_id}/graph"
        ).json()
        self.assertNotIn(
            "intent",
            [node["type"] for node in refreshed["nodes"]],
        )

    def test_layout_conflict_returns_409(self) -> None:
        project = self.create_project()
        project_id = project["id"]
        payload = {
            "revision": 0,
            "nodes": {"fact-1": {"x": 1, "y": 2}},
            "edges": {},
            "viewport": {},
        }

        first = self.client.put(
            f"/api/projects/{project_id}/layout",
            json=payload,
        )
        conflict = self.client.put(
            f"/api/projects/{project_id}/layout",
            json=payload,
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json()["detail"]["current_revision"], 1)

    def test_reconnects_and_deletes_unexecuted_intent_edge(self) -> None:
        project = self.create_project()
        project_id = project["id"]
        graph = self.client.get(f"/api/projects/{project_id}/graph").json()
        goal = next(node for node in graph["nodes"] if node["type"] == "goal")
        hint = self.client.post(
            f"/api/projects/{project_id}/nodes",
            json={"type": "hint", "content": "Check another path"},
        ).json()
        fact = self.client.post(
            f"/api/projects/{project_id}/nodes",
            json={"type": "fact", "content": "Known fact"},
        ).json()
        edge = self.client.post(
            f"/api/projects/{project_id}/edges",
            json={
                "type": "intent",
                "from_node_id": fact["id"],
                "to_node_id": goal["id"],
                "content": "Investigate",
            },
        ).json()

        reconnected = self.client.patch(
            f"/api/projects/{project_id}/edges/{edge['id']}",
            json={
                "from_node_id": fact["id"],
                "to_node_id": hint["id"],
            },
        )
        deleted = self.client.delete(
            f"/api/projects/{project_id}/edges/{edge['id']}"
        )

        self.assertEqual(reconnected.status_code, 200)
        self.assertEqual(reconnected.json()["to_node"], hint["id"])
        self.assertEqual(deleted.status_code, 204)
        refreshed = self.client.get(
            f"/api/projects/{project_id}/graph"
        ).json()
        self.assertNotIn(edge["id"], [item["id"] for item in refreshed["edges"]])

    def test_execution_from_another_project_is_not_visible(self) -> None:
        first = self.create_project("First")
        second = self.create_project("Second")
        first_handle = self.client.app.state.projects.get_handle(first["id"])
        fact = first_handle.store.add_human_node("fact", "Known fact")
        intent = first_handle.store.create_or_merge_intent(
            "human",
            __import__("orchestrator.models", fromlist=["IntentProposal"])
            .IntentProposal(fact, first_handle.store.node_id("goal"), "Work"),
        )
        execution = first_handle.store.start_execution(
            intent.id,
            "codex",
            "stdout",
            "stderr",
            "work",
        )

        response = self.client.get(
            f"/api/projects/{second['id']}/executions/{execution.id}"
        )

        self.assertEqual(response.status_code, 404)

    def test_serves_built_frontend_and_spa_routes_without_masking_api_404(self) -> None:
        frontend_dist = self.data_root / "frontend-dist"
        assets = frontend_dist / "assets"
        assets.mkdir(parents=True)
        (frontend_dist / "index.html").write_text(
            "<html><body>built blackboard</body></html>",
            encoding="utf-8",
        )
        (assets / "app.js").write_text("console.log('built')", encoding="utf-8")
        context = TestClient(
            create_app(self.data_root / "static-data", frontend_dist=frontend_dist)
        )
        client = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)

        asset = client.get("/assets/app.js")
        workspace = client.get("/projects/project-example")
        missing_api = client.get("/api/does-not-exist")

        self.assertEqual(asset.status_code, 200)
        self.assertIn("console.log", asset.text)
        self.assertEqual(workspace.status_code, 200)
        self.assertIn("built blackboard", workspace.text)
        self.assertEqual(missing_api.status_code, 404)
        self.assertNotIn("built blackboard", missing_api.text)


if __name__ == "__main__":
    unittest.main()
