from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from sandbox.auth import AuthError, ExpiredToken
from sandbox.contracts import BridgeProtocolError, CommandRequest, CommandResult
from sandbox.protocol import MAX_REQUEST_BYTES, decode_command_body, error_payload
from sandbox.run_registry import RunRegistry
from sandbox.run_registry import IssuedToken
from sandbox.service import SandboxClosed, SandboxService, SandboxUnavailable


class BridgeError(RuntimeError):
    def __init__(self, category: str, detail: str, status_code: int = 400) -> None:
        super().__init__(detail)
        self.category = category
        self.status_code = status_code
        self.detail = detail


class SandboxBridgeServer:
    """Loopback-only HTTP bridge exposing execute/cancel, never Docker APIs."""

    def __init__(
        self,
        service: SandboxService,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        registry: RunRegistry | None = None,
        max_body_bytes: int = MAX_REQUEST_BYTES,
    ) -> None:
        if host != "127.0.0.1":
            raise ValueError("Sandbox Bridge must bind to loopback")
        self.service = service
        self.host = "127.0.0.1" if host == "localhost" else host
        self.port = port
        self.registry = registry or RunRegistry()
        self.max_body_bytes = max_body_bytes
        self._server: asyncio.AbstractServer | None = None
        self._closed = False
        self._close_result: tuple[str, ...] | None = None

    @property
    def running(self) -> bool:
        return self._server is not None and not self._closed

    @property
    def endpoint(self) -> str:
        if self._server is None:
            return f"http://{self.host}:{self.port}"
        socket = self._server.sockets[0].getsockname()
        return f"http://{self.host}:{socket[1]}"

    async def start(self) -> str:
        if self._closed:
            raise RuntimeError("bridge is closed")
        if self._server is not None:
            return self.endpoint
        self._server = await asyncio.start_server(self._handle_client, self.host, self.port)
        self.port = int(self._server.sockets[0].getsockname()[1])
        return self.endpoint

    def issue_token(self, agent_run_id: str, profile: str, deadline: datetime, *, token_file: Path | None = None) -> IssuedToken:
        return self.registry.register(agent_run_id, profile, deadline, token_file=token_file)

    def revoke_token(self, agent_run_id: str) -> bool:
        return self.registry.revoke(agent_run_id)

    async def execute(self, token: str, payload: object) -> CommandResult:
        try:
            lease = self.registry.tokens.authenticate(token)
        except ExpiredToken as exc:
            raise BridgeError("expired", str(exc), 410) from exc
        except AuthError as exc:
            raise BridgeError("auth", str(exc), 401) from exc
        try:
            encoded_size = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        except (TypeError, ValueError) as exc:
            raise BridgeError("protocol", "request body must be JSON", 400) from exc
        if encoded_size > self.max_body_bytes:
            raise BridgeError("protocol", "request body is too large", 413)
        try:
            request = CommandRequest.from_dict(payload)
        except BridgeProtocolError as exc:
            raise BridgeError("protocol", str(exc), 400) from exc
        if lease.tool_profile == "no_tools":
            raise BridgeError("policy", "tool profile does not permit nmap", 403)
        if request.call_id in lease.calls:
            cached = lease.calls[request.call_id]
            if isinstance(cached, CommandResult):
                return cached
            raise BridgeError("policy", "call_id is already active", 409)
        if lease.command_lock.locked():
            raise BridgeError("policy", "only one command may run for a token", 409)
        async with lease.command_lock:
            lease.calls[request.call_id] = object()
            try:
                result = await self.service.execute(lease.agent_run_id, request)
            except SandboxClosed as exc:
                lease.calls.pop(request.call_id, None)
                raise BridgeError("cancelled", str(exc), 409) from exc
            except SandboxUnavailable as exc:
                lease.calls.pop(request.call_id, None)
                raise BridgeError("sandbox_unavailable", str(exc), 503) from exc
            except asyncio.CancelledError:
                lease.calls.pop(request.call_id, None)
                raise
            except Exception as exc:
                lease.calls.pop(request.call_id, None)
                raise BridgeError("internal", type(exc).__name__, 500) from exc
            lease.calls[request.call_id] = result
            return result

    async def cancel(self, token: str, payload: object) -> bool:
        try:
            lease = self.registry.tokens.authenticate(token)
        except ExpiredToken as exc:
            raise BridgeError("expired", str(exc), 410) from exc
        except AuthError as exc:
            raise BridgeError("auth", str(exc), 401) from exc
        if not isinstance(payload, dict) or set(payload) != {"call_id"} or not isinstance(payload.get("call_id"), str) or not payload["call_id"]:
            raise BridgeError("protocol", "cancel body must contain only call_id", 400)
        return await self.service.cancel(lease.agent_run_id, payload["call_id"])

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_line = await reader.readline()
            if not request_line or len(request_line) > 4096:
                return
            method, path, _ = request_line.decode("ascii").split(" ", 2)
            headers: dict[str, str] = {}
            while True:
                line = await reader.readline()
                if not line or line in {b"\r\n", b"\n"}:
                    break
                if len(line) > 8192 or b":" not in line:
                    raise BridgeError("protocol", "invalid HTTP headers", 400)
                key, value = line.decode("iso-8859-1").split(":", 1)
                headers[key.casefold().strip()] = value.strip()
            try:
                content_length = int(headers.get("content-length", "0"))
            except ValueError as exc:
                raise BridgeError("protocol", "invalid content length", 400) from exc
            if content_length < 0 or content_length > self.max_body_bytes:
                raise BridgeError("protocol", "request body is too large", 413)
            body = await reader.readexactly(content_length)
            authorization = headers.get("authorization", "")
            if not authorization.startswith("Bearer "):
                raise BridgeError("auth", "missing bearer token", 401)
            token = authorization[7:]
            if method != "POST":
                raise BridgeError("protocol", "only POST is supported", 405)
            if path == "/execute":
                result = await self.execute(token, json.loads(body.decode("utf-8")))
                payload = result.to_dict()
                status_code = 200
            elif path == "/cancel":
                payload = {"cancelled": await self.cancel(token, json.loads(body.decode("utf-8")))}
                status_code = 200
            else:
                raise BridgeError("protocol", "unknown bridge route", 404)
        except BridgeError as exc:
            payload, status_code = error_payload(exc.category, exc.detail), exc.status_code
        except (json.JSONDecodeError, UnicodeDecodeError, asyncio.IncompleteReadError, ValueError):
            payload, status_code = error_payload("protocol", "invalid JSON body"), 400
        except Exception:
            payload, status_code = error_payload("internal", "bridge request failed"), 500
        try:
            encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            writer.write((f"HTTP/1.1 {status_code} OK\r\nContent-Type: application/json\r\nContent-Length: {len(encoded)}\r\nConnection: close\r\n\r\n").encode("ascii") + encoded)
            await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()

    async def close(self) -> tuple[str, ...]:
        if self._close_result is not None:
            return self._close_result
        self.registry.revoke_all()
        self._closed = True
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        self._close_result = await self.service.close()
        return self._close_result
