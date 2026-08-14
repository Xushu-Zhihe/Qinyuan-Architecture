import asyncio
import json
import hashlib
import os
from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import shutil
import sqlite3
import subprocess
import threading
import unittest
from urllib.parse import urlparse
from unittest.mock import patch
from uuid import uuid4

from orchestrator.protocol import (
    parse_intent,
    parse_result,
    render_execution_request,
    render_intent_request,
)
from orchestrator.models import IntentProposal
from orchestrator.projects import ProjectService
from worker.config import load_provider_configs
from worker.executor import execute_provider


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROVIDERS = ROOT / "worker" / "providers.json"
RUN_DOCKER_TESTS = os.environ.get("QINYUAN_RUN_DOCKER_TESTS") == "1"
RUN_REAL_MODEL_TESTS = os.environ.get("QINYUAN_RUN_REAL_MODEL_TESTS") == "1"
RUN_MODEL_MCP_TESTS = os.environ.get("QINYUAN_RUN_MODEL_MCP_TESTS") == "1"
AUTHORIZED_TARGET_URL = os.environ.get("QINYUAN_AUTHORIZED_TARGET_URL", "")


@contextmanager
def _workspace_directory():
    directory = ROOT / ".test-work" / uuid4().hex
    directory.mkdir(parents=True)
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)
        try:
            directory.parent.rmdir()
        except OSError:
            pass


class _OpenAIHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    execution_started: threading.Event | None = None
    execution_release: threading.Event | None = None

    def do_POST(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(content_length))
        user_content = str(request["messages"][-1]["content"])
        if user_content.lstrip().startswith("Execute the assigned"):
            if self.execution_started is not None:
                self.execution_started.set()
            if self.execution_release is not None:
                self.execution_release.wait(timeout=30)
            content = (
                'BLACKBOARD_RESULT: {"intent_id":"intent-1","facts":[],'
                '"edges":[],"goal_achieved":false,"answer_key":null,'
                '"final_answer":null}'
            )
        else:
            content = (
                'BLACKBOARD_INTENT: {"action":"idle",'
                '"reason":"offline integration complete"}'
            )
        response = json.dumps(
            {
                "id": "chatcmpl-qinyuan-test",
                "object": "chat.completion",
                "created": 0,
                "model": "qinyuan-test",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            }
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, format: str, *args: object) -> None:
        return


class _ProjectFlowHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    scenario = "complete"
    execution_calls = 0
    lock = threading.Lock()

    @classmethod
    def reset(cls, scenario: str) -> None:
        cls.scenario = scenario
        cls.execution_calls = 0

    def do_POST(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(content_length))
        user_content = str(request["messages"][-1]["content"])
        if user_content.lstrip().startswith("Choose the next useful action"):
            snapshot = json.loads(user_content.split("Graph snapshot:\n", 1)[1])
            source = next(
                node["id"] for node in snapshot["nodes"] if node["type"] == "origin"
            )
            target = next(
                node["id"] for node in snapshot["nodes"] if node["type"] == "goal"
            )
            content = "BLACKBOARD_INTENT: " + json.dumps(
                {
                    "action": "plan",
                    "source_node_id": source,
                    "target_node_id": target,
                    "content": "Verify the offline integration flow",
                },
                separators=(",", ":"),
            )
        else:
            intent_text = user_content.split("\nAssigned intent:\n", 1)[1]
            intent = json.loads(intent_text.split("\n\nGraph snapshot:\n", 1)[0])
            with self.lock:
                type(self).execution_calls += 1
                execution_number = type(self).execution_calls
            if self.scenario == "protocol" and execution_number == 1:
                facts: list[object] = ["invalid legacy fact"]
                edges: list[dict[str, str]] = []
                achieved = False
                answer_key = None
                final_answer = None
            else:
                facts = [
                    {
                        "ref": "offline-fact",
                        "content": "offline provider flow verified",
                    }
                ]
                edges = [
                    {
                        "type": "supports",
                        "from": "offline-fact",
                        "to": intent["target_node_id"],
                    }
                ]
                achieved = self.scenario != "manual"
                answer_key = "offline-verified" if achieved else None
                final_answer = "offline integration verified" if achieved else None
            content = "BLACKBOARD_RESULT: " + json.dumps(
                {
                    "intent_id": intent["id"],
                    "facts": facts,
                    "edges": edges,
                    "goal_achieved": achieved,
                    "answer_key": answer_key,
                    "final_answer": final_answer,
                },
                separators=(",", ":"),
            )

        response = json.dumps(
            {
                "id": "chatcmpl-qinyuan-project-test",
                "object": "chat.completion",
                "created": 0,
                "model": "qinyuan-test",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            }
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, format: str, *args: object) -> None:
        return


class QinyuanAgentProviderIntegrationTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _provider():
        executable = shutil.which("qinyuan-agent")
        if executable is None:
            local = ROOT / ".venv" / "Scripts" / "qinyuan-agent.exe"
            executable = str(local) if local.is_file() else None
        if executable is None:
            return None

        configured = next(
            provider
            for provider in load_provider_configs(DEFAULT_PROVIDERS)
            if provider.name == "qinyuan-agent"
        )
        health_check = configured.health_check
        if health_check is not None:
            configured = replace(
                configured,
                health_check=replace(
                    health_check,
                    command=(executable, *health_check.command[1:]),
                ),
            )
        return replace(
            configured,
            command=(executable, *configured.command[1:]),
            timeout_seconds=30,
        )

    @staticmethod
    def _environment(endpoint: str) -> dict[str, str]:
        return {
            "QINYUAN_LLM_BACKEND": "openai",
            "QINYUAN_MODEL": "qinyuan-test",
            "OPENAI_API_KEY": "test-key",
            "OPENAI_BASE_URL": endpoint,
        }

    def _assert_sandboxes_removed(self, work_dirs: list[str]) -> None:
        for work_dir in work_dirs:
            identity = (
                "qinyuan-work-"
                + hashlib.sha256(
                    str(Path(work_dir).resolve()).encode("utf-8")
                ).hexdigest()[:24]
            )
            probe = subprocess.run(
                [
                    "docker",
                    "ps",
                    "-a",
                    "--filter",
                    f"label=qinyuan.agent_id={identity}",
                    "--format",
                    "{{.ID}}",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(probe.stdout.strip(), "")

    async def test_planning_request_crosses_real_provider_process(self) -> None:
        provider = self._provider()
        if provider is None:
            self.skipTest("qinyuan-agent is not installed")
        server = ThreadingHTTPServer(("127.0.0.1", 0), _OpenAIHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        try:
            endpoint = f"http://127.0.0.1:{server.server_port}/v1"
            environment = {
                "QINYUAN_LLM_BACKEND": "openai",
                "QINYUAN_MODEL": "qinyuan-test",
                "OPENAI_API_KEY": "test-key",
                "OPENAI_BASE_URL": endpoint,
            }
            with _workspace_directory() as directory:
                with patch.dict(os.environ, environment):
                    result = await execute_provider(
                        provider,
                        render_intent_request({"nodes": [], "edges": []}, None),
                        None,
                        directory / "work",
                    )
                stderr = Path(result.stderr_path).read_text(encoding="utf-8")

            self.assertEqual(result.status, "success", stderr or result.output)
            parsed = parse_intent(result.output, {})
            self.assertEqual(parsed.status, "valid", parsed.error)
            self.assertIsNotNone(parsed.decision)
            self.assertEqual(parsed.decision.action, "idle")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    @unittest.skipUnless(RUN_DOCKER_TESTS, "set QINYUAN_RUN_DOCKER_TESTS=1")
    async def test_execution_crosses_provider_and_tool_runtime(self) -> None:
        provider = self._provider()
        if provider is None:
            self.skipTest("qinyuan-agent is not installed")
        provider = replace(provider, timeout_seconds=60)
        server = ThreadingHTTPServer(("127.0.0.1", 0), _OpenAIHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        try:
            endpoint = f"http://127.0.0.1:{server.server_port}/v1"
            environment = {
                "QINYUAN_LLM_BACKEND": "openai",
                "QINYUAN_MODEL": "qinyuan-test",
                "OPENAI_API_KEY": "test-key",
                "OPENAI_BASE_URL": endpoint,
            }
            snapshot = {
                "nodes": [
                    {"id": "origin-1", "type": "origin"},
                    {"id": "goal-1", "type": "goal"},
                ],
                "edges": [],
            }
            intent = {
                "id": "intent-1",
                "source_node_id": "origin-1",
                "target_node_id": "goal-1",
                "content": "Perform an offline integration check",
                "generation": 1,
            }
            with _workspace_directory() as directory:
                with patch.dict(os.environ, environment):
                    result = await execute_provider(
                        provider,
                        render_execution_request(snapshot, intent, None),
                        None,
                        directory / "work",
                    )
                stderr = Path(result.stderr_path).read_text(encoding="utf-8")

            self.assertEqual(result.status, "success", stderr or result.output)
            parsed = parse_result(
                result.output,
                assigned_intent_id="intent-1",
                existing_node_ids={"origin-1", "goal-1"},
            )
            self.assertEqual(parsed.status, "valid", parsed.error)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    @unittest.skipUnless(RUN_DOCKER_TESTS, "set QINYUAN_RUN_DOCKER_TESTS=1")
    async def test_cancel_cleans_provider_sandbox(self) -> None:
        provider = self._provider()
        if provider is None:
            self.skipTest("qinyuan-agent is not installed")
        provider = replace(provider, timeout_seconds=60)
        server = ThreadingHTTPServer(("127.0.0.1", 0), _OpenAIHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        started = threading.Event()
        release = threading.Event()
        _OpenAIHandler.execution_started = started
        _OpenAIHandler.execution_release = release

        try:
            endpoint = f"http://127.0.0.1:{server.server_port}/v1"
            environment = {
                "QINYUAN_LLM_BACKEND": "openai",
                "QINYUAN_MODEL": "qinyuan-test",
                "OPENAI_API_KEY": "test-key",
                "OPENAI_BASE_URL": endpoint,
            }
            snapshot = {
                "nodes": [
                    {"id": "origin-1", "type": "origin"},
                    {"id": "goal-1", "type": "goal"},
                ],
                "edges": [],
            }
            intent = {
                "id": "intent-1",
                "source_node_id": "origin-1",
                "target_node_id": "goal-1",
                "content": "Perform a cancellation integration check",
                "generation": 1,
            }
            cancel = asyncio.Event()
            with _workspace_directory() as directory:
                work_dir = directory / "work"
                with patch.dict(os.environ, environment):
                    task = asyncio.create_task(
                        execute_provider(
                            provider,
                            render_execution_request(snapshot, intent, None),
                            None,
                            work_dir,
                            cancel_event=cancel,
                        )
                    )
                    started_ok = await asyncio.to_thread(started.wait, 15)
                    if not started_ok:
                        release.set()
                        result = await asyncio.wait_for(task, timeout=30)
                        stderr = Path(result.stderr_path).read_text(
                            encoding="utf-8"
                        )
                        self.fail(
                            f"fake LLM execution request was not received: "
                            f"status={result.status!r}, stderr={stderr!r}"
                        )
                    cancel.set()
                    release.set()
                    result = await asyncio.wait_for(task, timeout=15)

                self.assertEqual(result.status, "cancelled")
                identity = (
                    "qinyuan-work-"
                    + hashlib.sha256(
                        str(work_dir.resolve()).encode("utf-8")
                    ).hexdigest()[:24]
                )
                probe = subprocess.run(
                    [
                        "docker",
                        "ps",
                        "-a",
                        "--filter",
                        f"label=qinyuan.agent_id={identity}",
                        "--format",
                        "{{.ID}}",
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(probe.stdout.strip(), "")
        finally:
            _OpenAIHandler.execution_started = None
            _OpenAIHandler.execution_release = None
            release.set()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    @unittest.skipUnless(RUN_DOCKER_TESTS, "set QINYUAN_RUN_DOCKER_TESTS=1")
    async def test_complete_project_flow_persists_intent_execution_and_fact(
        self,
    ) -> None:
        provider = self._provider()
        if provider is None:
            self.skipTest("qinyuan-agent is not installed")
        provider = replace(provider, timeout_seconds=60)
        _ProjectFlowHandler.reset("complete")
        server = ThreadingHTTPServer(("127.0.0.1", 0), _ProjectFlowHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        try:
            endpoint = f"http://127.0.0.1:{server.server_port}/v1"
            with _workspace_directory() as directory:
                service = ProjectService(
                    directory / "data",
                    provider_configs=(provider,),
                )
                try:
                    handle = service.create_project(
                        name="Offline qinyuan flow",
                        goal="Verify the provider integration",
                        origin="Offline integration fixture",
                        hints=(),
                        mode="manual",
                        provider_names=("qinyuan-agent",),
                        agent_limit=1,
                        challenge_path=None,
                    )
                    with patch.dict(os.environ, self._environment(endpoint)):
                        supervisor = await service.start_project(handle.id)
                        await supervisor.wait_until_idle(timeout=120)

                    snapshot = handle.store.snapshot()
                    planning = handle.store.planning_attempts()
                    executions = handle.store.executions()
                    events = handle.store.events_after(0)
                    work_dirs = [str(item["work_dir"]) for item in executions]
                    self.assertEqual(snapshot["project"]["status"], "achieved")
                    self.assertEqual(
                        snapshot["project"]["final_answer"],
                        "offline integration verified",
                    )
                    self.assertEqual(len(planning), 1)
                    self.assertEqual(planning[0]["status"], "valid")
                    self.assertEqual(len(executions), 1)
                    self.assertEqual(executions[0]["provider"], "qinyuan-agent")
                    self.assertEqual(executions[0]["status"], "completed")
                    self.assertTrue(Path(str(planning[0]["stdout_path"])).is_file())
                    self.assertTrue(Path(str(executions[0]["stdout_path"])).is_file())
                    self.assertTrue(
                        any(node["type"] == "fact" for node in snapshot["nodes"])
                    )
                    self.assertIn(
                        "intent.execution_finished",
                        {str(event["type"]) for event in events},
                    )

                    database = handle.record.path / "blackboard.db"
                    with sqlite3.connect(database) as connection:
                        row = connection.execute(
                            """
                            SELECT p.status, e.provider, e.status, n.content
                            FROM planning_attempts AS p
                            JOIN intent_executions AS e
                              ON e.intent_edge_id = p.intent_edge_id
                            JOIN nodes AS n
                              ON n.type = 'fact'
                            """
                        ).fetchone()
                    self.assertEqual(
                        row,
                        (
                            "valid",
                            "qinyuan-agent",
                            "completed",
                            "offline provider flow verified",
                        ),
                    )
                    self._assert_sandboxes_removed(work_dirs)
                finally:
                    await service.aclose()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    @unittest.skipUnless(RUN_DOCKER_TESTS, "set QINYUAN_RUN_DOCKER_TESTS=1")
    async def test_protocol_correction_retries_and_cleans_both_sandboxes(
        self,
    ) -> None:
        provider = self._provider()
        if provider is None:
            self.skipTest("qinyuan-agent is not installed")
        provider = replace(provider, timeout_seconds=60)
        _ProjectFlowHandler.reset("protocol")
        server = ThreadingHTTPServer(("127.0.0.1", 0), _ProjectFlowHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        try:
            endpoint = f"http://127.0.0.1:{server.server_port}/v1"
            with _workspace_directory() as directory:
                service = ProjectService(
                    directory / "data",
                    provider_configs=(provider,),
                )
                try:
                    handle = service.create_project(
                        name="Protocol correction",
                        goal="Correct an invalid result",
                        origin="Offline integration fixture",
                        hints=(),
                        mode="manual",
                        provider_names=("qinyuan-agent",),
                        agent_limit=1,
                        challenge_path=None,
                    )
                    with patch.dict(os.environ, self._environment(endpoint)):
                        supervisor = await service.start_project(handle.id)
                        await supervisor.wait_until_idle(timeout=180)

                    executions = handle.store.executions()
                    self.assertEqual(_ProjectFlowHandler.execution_calls, 2)
                    self.assertEqual(
                        [item["status"] for item in executions],
                        ["protocol_error", "completed"],
                    )
                    self.assertEqual(
                        [item["retry_count"] for item in executions],
                        [0, 1],
                    )
                    self.assertEqual(len({item["work_dir"] for item in executions}), 2)
                    self.assertEqual(
                        handle.store.snapshot()["project"]["status"],
                        "achieved",
                    )
                    self._assert_sandboxes_removed(
                        [str(item["work_dir"]) for item in executions]
                    )
                finally:
                    await service.aclose()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    @unittest.skipUnless(RUN_DOCKER_TESTS, "set QINYUAN_RUN_DOCKER_TESTS=1")
    async def test_manual_retry_uses_real_provider_and_cleans_sandbox(self) -> None:
        provider = self._provider()
        if provider is None:
            self.skipTest("qinyuan-agent is not installed")
        provider = replace(provider, timeout_seconds=60)
        _ProjectFlowHandler.reset("manual")
        server = ThreadingHTTPServer(("127.0.0.1", 0), _ProjectFlowHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        try:
            endpoint = f"http://127.0.0.1:{server.server_port}/v1"
            with _workspace_directory() as directory:
                service = ProjectService(
                    directory / "data",
                    provider_configs=(provider,),
                )
                try:
                    handle = service.create_project(
                        name="Manual retry",
                        goal="Retry a failed execution",
                        origin="Offline integration fixture",
                        hints=(),
                        mode="manual",
                        provider_names=("qinyuan-agent",),
                        agent_limit=1,
                        challenge_path=None,
                    )
                    intent = handle.store.create_or_merge_intent(
                        "human",
                        IntentProposal(
                            handle.store.node_id("origin"),
                            handle.store.node_id("goal"),
                            "Retry through qinyuan-agent",
                        ),
                    )
                    source = handle.store.start_execution(
                        intent.id,
                        "qinyuan-agent",
                        "old-stdout",
                        "old-stderr",
                        "old-work",
                    )
                    handle.store.fail_execution(source.id, "failed", "failed once")

                    with patch.dict(os.environ, self._environment(endpoint)):
                        retried = await service.retry_execution(handle.id, source.id)
                        supervisor = service._supervisors[handle.id]
                        task = supervisor.manual_tasks[str(retried["id"])]
                        await asyncio.wait_for(task, timeout=120)

                    current = handle.store.execution(str(retried["id"]))
                    self.assertEqual(current["status"], "completed")
                    self.assertEqual(current["retry_count"], 1)
                    self.assertEqual(current["retry_of_execution_id"], source.id)
                    self.assertEqual(
                        handle.store.execution(source.id)["status"],
                        "failed",
                    )
                    self.assertEqual(
                        handle.store.snapshot()["project"]["status"],
                        "idle",
                    )
                    self._assert_sandboxes_removed([str(current["work_dir"])])
                finally:
                    await service.aclose()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    @unittest.skipUnless(
        RUN_DOCKER_TESTS and RUN_REAL_MODEL_TESTS,
        "set QINYUAN_RUN_DOCKER_TESTS=1 and QINYUAN_RUN_REAL_MODEL_TESTS=1",
    )
    async def test_real_model_planning_and_execution_smoke(self) -> None:
        provider = self._provider()
        if provider is None:
            self.skipTest("qinyuan-agent is not installed")
        provider = replace(
            provider,
            planning_timeout_seconds=120,
            timeout_seconds=180,
        )
        snapshot = {
            "nodes": [
                {
                    "id": "real-smoke-origin",
                    "type": "origin",
                    "state": "active",
                    "content": "Authorized local integration smoke",
                },
                {
                    "id": "real-smoke-goal",
                    "type": "goal",
                    "state": "active",
                    "content": "Verify the real model provider boundary",
                },
            ],
            "edges": [],
        }
        intent = {
            "id": "real-smoke-intent",
            "source_node_id": "real-smoke-origin",
            "target_node_id": "real-smoke-goal",
            "content": (
                "This is an authorized provider integration smoke test. "
                "Do not call tools, inspect files, or access any network "
                "target. Return no facts or edges and do not mark the goal "
                "achieved."
            ),
            "generation": 1,
        }

        with _workspace_directory() as directory:
            planning = await execute_provider(
                provider,
                render_intent_request(snapshot, None),
                None,
                directory / "planning",
            )
            planning_stderr = Path(planning.stderr_path).read_text(
                encoding="utf-8"
            )
            self.assertEqual(
                planning.status,
                "success",
                planning_stderr[-2_000:],
            )
            parsed_intent = parse_intent(
                planning.output,
                {
                    "real-smoke-origin": "origin",
                    "real-smoke-goal": "goal",
                },
            )
            self.assertEqual(
                parsed_intent.status,
                "valid",
                parsed_intent.error,
            )

            execution_work = directory / "execution"
            execution = await execute_provider(
                provider,
                render_execution_request(snapshot, intent, None),
                None,
                execution_work,
            )
            execution_stderr = Path(execution.stderr_path).read_text(
                encoding="utf-8"
            )
            self.assertEqual(
                execution.status,
                "success",
                execution_stderr[-2_000:],
            )
            parsed_result = parse_result(
                execution.output,
                assigned_intent_id="real-smoke-intent",
                existing_node_ids={"real-smoke-origin", "real-smoke-goal"},
            )
            self.assertEqual(parsed_result.status, "valid", parsed_result.error)
            self._assert_sandboxes_removed([str(execution_work)])

    @unittest.skipUnless(
        RUN_DOCKER_TESTS
        and RUN_REAL_MODEL_TESTS
        and RUN_MODEL_MCP_TESTS
        and bool(AUTHORIZED_TARGET_URL),
        "set Docker, real-model, model-MCP gates and authorized target URL",
    )
    async def test_real_model_selects_mcp_for_authorized_target(self) -> None:
        provider = self._provider()
        if provider is None:
            self.skipTest("qinyuan-agent is not installed")
        provider = replace(provider, timeout_seconds=300)
        parsed_target = urlparse(AUTHORIZED_TARGET_URL)
        self.assertIn(parsed_target.scheme, {"http", "https"})
        self.assertTrue(parsed_target.hostname)
        snapshot = {
            "nodes": [
                {
                    "id": "model-mcp-origin",
                    "type": "origin",
                    "state": "active",
                    "content": "Explicitly authorized local web service",
                },
                {
                    "id": "model-mcp-goal",
                    "type": "goal",
                    "state": "active",
                    "content": "Record one verified read-only HTTP response",
                },
            ],
            "edges": [],
        }
        intent = {
            "id": "model-mcp-intent",
            "source_node_id": "model-mcp-origin",
            "target_node_id": "model-mcp-goal",
            "content": (
                f"Make one read-only HTTP GET request to the explicitly "
                f"authorized service {AUTHORIZED_TARGET_URL}. Decide which "
                "available MCP tool is appropriate and invoke it. The "
                "request must execute in the sandbox; do not execute a host "
                "command, change server state, or access another host. "
                "Return only an observation verified by tool output and do "
                "not mark the overall goal achieved."
            ),
            "generation": 1,
        }

        with _workspace_directory() as directory:
            execution_work = directory / "model-mcp"
            execution = await execute_provider(
                provider,
                render_execution_request(snapshot, intent, None),
                None,
                execution_work,
            )
            execution_stderr = Path(execution.stderr_path).read_text(
                encoding="utf-8"
            )
            self.assertEqual(
                execution.status,
                "success",
                execution_stderr[-4_000:],
            )
            parsed_result = parse_result(
                execution.output,
                assigned_intent_id="model-mcp-intent",
                existing_node_ids={"model-mcp-origin", "model-mcp-goal"},
            )
            self.assertEqual(parsed_result.status, "valid", parsed_result.error)
            audit_path = execution_work / "mcp-audit.jsonl"
            self.assertTrue(audit_path.is_file())
            records = [
                json.loads(line)
                for line in audit_path.read_text(encoding="utf-8").splitlines()
            ]
            finished = [
                record
                for record in records
                if record["event"] == "finished"
                and record["status"] == "completed"
            ]
            self.assertTrue(finished, records)
            self.assertTrue(
                all(record.get("sandbox_id") for record in finished),
                finished,
            )
            target_host = str(parsed_target.hostname)
            self.assertTrue(
                any(
                    target_host
                    in json.dumps(record.get("tool_arguments", {}))
                    for record in finished
                ),
                finished,
            )
            self._assert_sandboxes_removed([str(execution_work)])


if __name__ == "__main__":
    unittest.main()
