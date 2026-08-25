import tempfile
import time
import unittest
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

from fastapi.testclient import TestClient

from orchestrator.models import IntentProposal
from web.app import create_app
from worker.models import ProviderConfig, WorkerResult


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

    def test_create_project_forwards_flag_goal_settings(self) -> None:
        response = self.client.post(
            "/api/projects",
            json={
                "name": "Flag goal",
                "goal": "Recover the flag",
                "origin": "Challenge statement",
                "mode": "manual",
                "providers": [],
                "agent_limit": 1,
                "goal_format": "flag_objective",
                "flag_pattern": r"NSSCTF\{[^{}\r\n]{1,128}\}",
                "flag_pattern_id": "nssctf-v1",
            },
        )

        self.assertEqual(response.status_code, 201, response.text)
        project = self.client.app.state.projects.get_handle(response.json()["id"])
        stored = project.store.snapshot()["project"]
        self.assertEqual(stored["goal_format"], "flag_objective")
        self.assertEqual(stored["flag_pattern"], r"NSSCTF\{[^{}\r\n]{1,128}\}")
        self.assertEqual(stored["flag_pattern_id"], "nssctf-v1")

    def test_deletes_project_to_recoverable_trash(self) -> None:
        challenge = self.data_root / "challenge"
        challenge.mkdir()
        challenge_file = challenge / "keep.txt"
        challenge_file.write_text("keep", encoding="utf-8")
        response = self.client.post(
            "/api/projects",
            json={
                "name": "Disposable",
                "goal": "Goal",
                "origin": "Origin",
                "hints": [],
                "mode": "manual",
                "providers": [],
                "agent_limit": 1,
                "challenge_path": str(challenge),
            },
        )
        project_id = response.json()["id"]

        deleted = self.client.delete(f"/api/projects/{project_id}")

        self.assertEqual(deleted.status_code, 204, deleted.text)
        self.assertEqual(self.client.get(f"/api/projects/{project_id}").status_code, 404)
        trash = tuple((self.data_root / ".trash").glob(f"{project_id}-*"))
        self.assertEqual(len(trash), 1)
        self.assertTrue((trash[0] / "blackboard.db").is_file())
        self.assertEqual(challenge_file.read_text(encoding="utf-8"), "keep")

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

    def test_archives_restores_node_and_filters_connected_edges(self) -> None:
        project = self.create_project()
        project_id = project["id"]
        graph = self.client.get(f"/api/projects/{project_id}/graph").json()
        goal = next(node for node in graph["nodes"] if node["type"] == "goal")
        fact = self.client.post(
            f"/api/projects/{project_id}/nodes",
            json={"type": "fact", "content": "Temporary fact"},
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

        archived = self.client.delete(
            f"/api/projects/{project_id}/nodes/{fact['id']}"
        )
        active_graph = self.client.get(
            f"/api/projects/{project_id}/graph"
        ).json()
        audit_graph = self.client.get(
            f"/api/projects/{project_id}/graph?include_archived=true"
        ).json()

        self.assertEqual(archived.status_code, 204)
        self.assertNotIn(fact["id"], [node["id"] for node in active_graph["nodes"]])
        self.assertNotIn(edge["id"], [item["id"] for item in active_graph["edges"]])
        self.assertIn(fact["id"], [node["id"] for node in audit_graph["nodes"]])
        self.assertIn(edge["id"], [item["id"] for item in audit_graph["edges"]])

        restored = self.client.post(
            f"/api/projects/{project_id}/nodes/{fact['id']}/restore"
        )
        restored_graph = self.client.get(
            f"/api/projects/{project_id}/graph"
        ).json()
        self.assertEqual(restored.status_code, 204)
        self.assertIn(fact["id"], [node["id"] for node in restored_graph["nodes"]])
        self.assertIn(edge["id"], [item["id"] for item in restored_graph["edges"]])

    def test_goal_and_execution_output_cannot_be_archived(self) -> None:
        project = self.create_project()
        project_id = project["id"]
        handle = self.client.app.state.projects.get_handle(project_id)
        goal_id = handle.store.node_id("goal")

        response = self.client.delete(
            f"/api/projects/{project_id}/nodes/{goal_id}"
        )

        self.assertEqual(response.status_code, 409)
        self.assertIn("human Fact or Hint", response.json()["detail"])

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

    def test_planning_attempts_and_logs_support_byte_paging(self) -> None:
        project = self.create_project("Audit paging")
        project_id = str(project["id"])
        handle = self.client.app.state.projects.get_handle(project_id)
        work_dir = handle.record.path / "executions" / "plan-page"
        work_dir.mkdir(parents=True)
        stdout = work_dir / "stdout.txt"
        stderr = work_dir / "stderr.txt"
        stdout.write_text("abcdefghij", encoding="utf-8")
        stderr.write_text("error-data", encoding="utf-8")
        (work_dir / "combined.txt").write_text(
            "[stderr]\nerror-data[stdout]\nabcdefghij",
            encoding="utf-8",
        )
        attempt_id = handle.store.start_planning_attempt(
            "codex",
            3,
            str(stdout),
            str(stderr),
            str(work_dir),
        )
        handle.store.finish_planning_attempt(
            attempt_id,
            "idle",
            "done",
        )

        listed = self.client.get(
            f"/api/projects/{project_id}/planning-attempts"
        )
        detail = self.client.get(
            f"/api/projects/{project_id}/planning-attempts/{attempt_id}"
        )
        first = self.client.get(
            f"/api/projects/{project_id}/planning-attempts/"
            f"{attempt_id}/logs?limit=4"
        )
        second = self.client.get(
            f"/api/projects/{project_id}/planning-attempts/"
            f"{attempt_id}/logs?stdout_offset=4&stderr_offset=4&all_offset=4&limit=4"
        )

        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()[0]["id"], attempt_id)
        self.assertEqual(detail.json()["status"], "idle")
        self.assertEqual(first.json()["stdout"], "abcd")
        self.assertEqual(first.json()["stderr"], "erro")
        self.assertEqual(first.json()["all"], "[std")
        self.assertEqual(
            first.json()["pagination"]["stdout"]["next_offset"],
            4,
        )
        self.assertTrue(first.json()["pagination"]["stdout"]["has_more"])
        self.assertEqual(second.json()["stdout"], "efgh")
        self.assertEqual(second.json()["all"], "err]")

    def test_audit_export_endpoint_returns_redacted_zip(self) -> None:
        project = self.create_project("Audit export")
        project_id = str(project["id"])
        handle = self.client.app.state.projects.get_handle(project_id)
        work_dir = handle.record.path / "executions" / "plan-secret"
        work_dir.mkdir(parents=True)
        stdout = work_dir / "stdout.txt"
        stderr = work_dir / "stderr.txt"
        stdout.write_text(
            "Authorization: Bearer sk-web-secret-value\n",
            encoding="utf-8",
        )
        stderr.write_text("", encoding="utf-8")
        attempt_id = handle.store.start_planning_attempt(
            "codex",
            1,
            str(stdout),
            str(stderr),
            str(work_dir),
        )
        handle.store.finish_planning_attempt(attempt_id, "idle", "done")

        response = self.client.get(
            f"/api/projects/{project_id}/audit/export"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "application/zip")
        with ZipFile(BytesIO(response.content)) as archive:
            payload = b"\n".join(
                archive.read(name) for name in archive.namelist()
            )
        self.assertNotIn(b"sk-web-secret-value", payload)
        self.assertIn(b"[redacted]", payload)

    def test_runtime_snapshot_reports_limits_health_and_active_tasks(self) -> None:
        project = self.create_project("Runtime")
        project_id = str(project["id"])
        handle = self.client.app.state.projects.get_handle(project_id)
        intent = handle.store.create_or_merge_intent(
            "human",
            IntentProposal(
                handle.store.node_id("origin"),
                handle.store.node_id("goal"),
                "Inspect runtime",
            ),
        )
        execution = handle.store.start_execution(
            intent.id,
            "codex",
            "stdout",
            "stderr",
            "work",
            defer_start=True,
        )

        response = self.client.get("/api/runtime")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["limits"]["max_global_running"], 16)
        self.assertEqual(payload["projects"][0]["id"], project_id)
        self.assertEqual(payload["projects"][0]["active_task_count"], 1)
        self.assertEqual(payload["active_tasks"][0]["id"], execution.id)
        self.assertEqual(payload["active_tasks"][0]["kind"], "execution")
        self.assertEqual(payload["projects"][0]["event_metrics"]["queue_capacity"], 500)
        self.assertEqual(payload["projects"][0]["event_metrics"]["backfilled_events"], 0)

    def test_reopen_achieved_project_isolates_previous_answer(self) -> None:
        project = self.create_project("Reopen")
        project_id = str(project["id"])
        handle = self.client.app.state.projects.get_handle(project_id)
        handle.store.record_answer("codex", "flag{old}")
        handle.store.set_project_status("achieved", "flag{old}")
        self.client.app.state.projects.registry.update_status(
            project_id,
            "achieved",
        )

        response = self.client.post(f"/api/projects/{project_id}/reopen")
        graph = self.client.get(f"/api/projects/{project_id}/graph").json()

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["goal_version"], 2)
        self.assertEqual(graph["project"]["status"], "idle")
        self.assertEqual(graph["project"]["goal_version"], 2)
        self.assertEqual(graph["candidates"], [])
        repeated = self.client.post(f"/api/projects/{project_id}/reopen")
        self.assertEqual(repeated.status_code, 409)

    def test_manual_retry_endpoint_creates_linked_execution(self) -> None:
        async def execute(config, request, challenge_dir, work_dir, **kwargs):
            work_dir.mkdir(parents=True, exist_ok=True)
            stdout = work_dir / "stdout.txt"
            stderr = work_dir / "stderr.txt"
            output = (
                'BLACKBOARD_RESULT: {"intent_id":"'
                + intent.id
                + '","facts":[],"edges":[],"goal_achieved":false,'
                '"final_answer":null}'
            )
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

        context = TestClient(
            create_app(
                self.data_root / "retry-data",
                provider_configs=(
                    ProviderConfig("codex", True, 10, ("unused",), 5),
                ),
                execute=execute,
            )
        )
        client = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        project = client.post(
            "/api/projects",
            json={
                "name": "Retry",
                "goal": "Recover the flag",
                "origin": "Challenge statement",
                "hints": [],
                "mode": "manual",
                "providers": ["codex"],
                "agent_limit": 1,
                "challenge_path": None,
            },
        ).json()
        handle = client.app.state.projects.get_handle(project["id"])
        intent = handle.store.create_or_merge_intent(
            "human",
            IntentProposal(
                handle.store.node_id("origin"),
                handle.store.node_id("goal"),
                "Retry this intent",
            ),
        )
        source = handle.store.start_execution(
            intent.id,
            "codex",
            "old-stdout",
            "old-stderr",
            "old-work",
        )
        handle.store.fail_execution(source.id, "failed", "failed once")

        response = client.post(
            f"/api/projects/{project['id']}/executions/{source.id}/retry"
        )

        self.assertEqual(response.status_code, 202, response.text)
        retried = response.json()
        self.assertNotEqual(retried["id"], source.id)
        self.assertEqual(retried["retry_of_execution_id"], source.id)
        for _ in range(50):
            current = client.get(
                f"/api/projects/{project['id']}/executions/{retried['id']}"
            ).json()
            if current["status"] == "completed":
                break
            time.sleep(0.01)
        self.assertEqual(current["status"], "completed")
        self.assertEqual(handle.store.execution(source.id)["status"], "failed")

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
