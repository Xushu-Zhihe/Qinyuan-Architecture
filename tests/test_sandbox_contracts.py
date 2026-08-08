import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sandbox.artifacts import ArtifactStore
from sandbox.bridge import BridgeError, SandboxBridgeServer
from sandbox.contracts import BridgeProtocolError, CommandRequest, CommandResult, RunContext
from sandbox.models import RuntimeContainer
from sandbox.service import SandboxService
from sandbox.settings import SandboxSettings


class FakeRuntime:
    def __init__(self, *, create_delay: float = 0.0, destroy_failures: int = 0) -> None:
        self.create_delay = create_delay
        self.destroy_failures = destroy_failures
        self.create_calls = 0
        self.execute_calls = 0
        self.destroy_calls = 0
        self.created: list[str] = []

    async def create(self, agent_run_id: str) -> RuntimeContainer:
        self.create_calls += 1
        self.created.append(agent_run_id)
        if self.create_delay:
            await asyncio.sleep(self.create_delay)
        return RuntimeContainer(f"sandbox-{agent_run_id}", "fake:nmap", "sha256:fake")

    async def execute(self, container, request, *, artifact_dir=None) -> CommandResult:
        self.execute_calls += 1
        return CommandResult(call_id=request.call_id, stdout="ok", artifact_refs=())

    async def cancel(self, container, call_id: str) -> bool:
        return True

    async def destroy(self, container) -> None:
        self.destroy_calls += 1
        if self.destroy_failures:
            self.destroy_failures -= 1
            raise RuntimeError("destroy failed")

    async def close(self) -> None:
        return None


def command(call_id: str = "call-1") -> CommandRequest:
    return CommandRequest(1, call_id, "nmap", ("-sT", "127.0.0.1"), 5)


class SandboxContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_is_strict_and_typed(self) -> None:
        with self.assertRaisesRegex(BridgeProtocolError, "unexpected"):
            CommandRequest.from_dict(command("x").to_dict() | {"agent_run_id": "run"})
        with self.assertRaisesRegex(BridgeProtocolError, "NUL"):
            CommandRequest(1, "call", "nmap", ("bad\x00arg",), 5)
        with self.assertRaisesRegex(BridgeProtocolError, "protocol_version"):
            CommandResult(protocol_version=2, call_id="call")

    async def test_lazy_per_run_idempotence_and_destroy_retry(self) -> None:
        runtime = FakeRuntime(create_delay=0.05, destroy_failures=1)
        service = SandboxService(runtime)
        self.assertEqual(runtime.create_calls, 0)
        await asyncio.gather(
            service.start_for_agent("run-a"),
            service.start_for_agent("run-a"),
            service.start_for_agent("run-a"),
        )
        self.assertEqual(runtime.create_calls, 1)
        first = await service.stop_for_agent("run-a")
        self.assertFalse(first)
        self.assertIn("run-a", service.sessions)
        self.assertTrue(await service.stop_for_agent("run-a"))
        self.assertTrue(await service.stop_for_agent("never-started"))

    async def test_bridge_policy_replay_expiry_and_cross_run(self) -> None:
        runtime = FakeRuntime()
        service = SandboxService(runtime)
        bridge = SandboxBridgeServer(service)
        deadline = datetime.now(timezone.utc) + timedelta(minutes=1)
        issued_a = bridge.issue_token("run-a", "nmap_optional", deadline)
        issued_b = bridge.issue_token("run-b", "nmap_optional", deadline)
        result = await bridge.execute(issued_a.token, command("same").to_dict())
        self.assertEqual(result.status, "completed")
        replay = await bridge.execute(issued_a.token, command("same").to_dict())
        self.assertEqual(replay.stdout, "ok")
        with self.assertRaisesRegex(BridgeError, "unexpected"):
            await bridge.execute(issued_b.token, command("cross").to_dict() | {"agent_run_id": "run-a"})
        with self.assertRaisesRegex(BridgeError, "does not permit"):
            no_tools = bridge.issue_token("run-c", "no_tools", deadline)
            await bridge.execute(no_tools.token, command("no-tools").to_dict())
        bridge.revoke_token("run-a")
        with self.assertRaisesRegex(BridgeError, "revoked|invalid"):
            await bridge.execute(issued_a.token, command("after-revoke").to_dict())
        await bridge.close()

    async def test_loopback_http_contract_and_expired_token(self) -> None:
        runtime = FakeRuntime()
        service = SandboxService(runtime)
        bridge = SandboxBridgeServer(service)
        endpoint = await bridge.start()
        issued = bridge.issue_token(
            "run-http", "nmap_optional",
            datetime.now(timezone.utc) + timedelta(minutes=1),
        )
        reader, writer = await asyncio.open_connection("127.0.0.1", bridge.port)
        body = json.dumps(command("http").to_dict()).encode("utf-8")
        writer.write(
            (
                "POST /execute HTTP/1.1\r\n"
                "Host: 127.0.0.1\r\n"
                f"Authorization: Bearer {issued.token}\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\n\r\n"
            ).encode("ascii") + body
        )
        await writer.drain()
        response = await reader.read()
        writer.close()
        await writer.wait_closed()
        self.assertIn(b"200", response.split(b"\r\n", 1)[0])
        self.assertIn(b'"call_id":"http"', response)
        expired = bridge.issue_token(
            "run-expired", "nmap_optional",
            datetime.now(timezone.utc) - timedelta(seconds=1),
        )
        with self.assertRaisesRegex(BridgeError, "expired"):
            await bridge.execute(expired.token, command("expired").to_dict())
        await bridge.close()

    async def test_run_context_round_trip_and_artifact_ownership(self) -> None:
        context = RunContext(
            1, "run-a", "thread-a", "nmap_optional", ("127.0.0.1",),
            (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat(),
            "C:/run-a", "C:/run-a/artifacts", {"prefix": "BLACKBOARD_RESULT:"},
            "http://127.0.0.1:1234",
        )
        self.assertEqual(RunContext.from_dict(json.loads(json.dumps(context.to_dict()))), context)
        with tempfile.TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory))
            ref, truncated = store.write_stream("run-a", "call-a", "stdout", b"secret")
            self.assertFalse(truncated)
            self.assertEqual(store.read("run-a", ref), b"secret")
            with self.assertRaises(PermissionError):
                store.read("run-b", ref)


if __name__ == "__main__":
    unittest.main()
