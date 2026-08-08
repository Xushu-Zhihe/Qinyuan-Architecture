# Qinyuan Main / Tool Sandbox Protocol v1

This document freezes the process boundary owned by qinyuan-bat. Qinyuan Main
is a normal host process. It receives one UTF-8 task on stdin and returns normal
stdout/stderr, an exit code, and exactly one final `BLACKBOARD_RESULT:` line.
Neither repository imports the other repository's Python implementation.

## Per-run files

Before process start, bat creates a private run directory containing:

- `run_context.json`, passed as `QINYUAN_RUN_CONTEXT_FILE`;
- `sandbox.token`, passed as `QINYUAN_SANDBOX_TOKEN_FILE` and deleted after the
  provider exits;
- no model API key, Docker credential, bearer token in context JSON, image
  selector, container name, or executable path.

`run_context.json` has exactly these fields:

```json
{
  "protocol_version": 1,
  "agent_run_id": "opaque-uuid",
  "thread_id": "execution-attempt-id",
  "tool_profile": "no_tools",
  "authorized_targets": [],
  "deadline": "2026-08-08T12:00:00+00:00",
  "work_dir": "managed-run-directory",
  "artifact_scope": "managed-artifact-directory",
  "result_contract": {
    "prefix": "BLACKBOARD_RESULT:",
    "version": 1,
    "exactly_one": true
  },
  "sandbox_endpoint": "http://127.0.0.1:49152"
}
```

Planning and timeout recovery always use `no_tools`. Intent execution uses
`nmap_optional` only when the provider config explicitly enables the Nmap tool
profile and confirms host tools are disabled.

## Bridge request

The Bridge is a dedicated random-port listener bound only to `127.0.0.1`.
Clients send `POST /execute`, `Authorization: Bearer <token>`, and exactly:

```json
{
  "protocol_version": 1,
  "call_id": "tool-call-id",
  "tool": "nmap",
  "arguments": ["-sT", "-Pn", "-n", "-p", "80,443", "127.0.0.1"],
  "timeout_seconds": 60
}
```

The request cannot select a run, container, image, network, URL, or executable.
The token digest selects one run/profile/deadline. The first valid request lazily
creates that run's tool container. Commands for one token are serial; a completed
`call_id` replay returns its cached result.

## Bridge result

```json
{
  "protocol_version": 1,
  "call_id": "tool-call-id",
  "status": "completed",
  "exit_code": 0,
  "stdout": "...",
  "stderr": "",
  "timed_out": false,
  "stdout_truncated": false,
  "stderr_truncated": false,
  "artifact_refs": []
}
```

Stable statuses are `completed`, `failed`, `timeout`, `cancelled`, `aborted`,
and `outcome_unknown`. Stable error classes are `auth`, `expired`, `protocol`,
`policy`, `sandbox_unavailable`, `tool_timeout`, `cancelled`, and `internal`.
Unknown fields, wrong types, NUL bytes, unsupported versions, invalid timeouts,
expired/revoked tokens, and oversized bodies are rejected before Docker access.

## Lifecycle and security

One provider invocation owns one `agent_run_id`, one token, and at most one lazy
container. Provider completion follows this order: preserve provider output,
revoke token, cancel an active command, destroy the container, delete the token
file, and record cleanup separately. A destroy error remains retryable and does
not replace the provider's original result.

The tool image contains Nmap and `timeout`, runs as UID 65532 with a read-only
root filesystem, `no-new-privileges`, all capabilities dropped, and bounded
PIDs/CPU/memory. It receives no provider environment, model key, Docker socket,
host home directory, SSH material, provider work directory, privileged mode, or
host network.
