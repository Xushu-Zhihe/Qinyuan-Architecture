# Blackboard Result Protocol Skill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make blackboard intent executions produce valid structured results reliably and retry one malformed protocol result on the same Intent before returning failure.

**Architecture:** Keep 'parse_result()' strict and make its contract explicit in every execution prompt. Add a project-local Codex Skill with a validator that calls the production parser, then refactor the Supervisor so a protocol error creates one new execution for the same Intent with parser feedback and the rejected protocol line.

**Tech Stack:** Python 3.10, asyncio, unittest, SQLite-backed BlackboardStore, Codex Agent Skills

---

## File Map

- Modify 'orchestrator/protocol.py': own the exact result contract, correction context, and rejected-line extraction.
- Modify 'orchestrator/supervisor.py': run at most two protocol attempts for one Intent while preserving separate executions.
- Modify 'tests/test_protocol.py': cover the inline schema, Skill invocation, correction prompt, and result-line extraction.
- Modify 'tests/test_supervisor.py': cover successful correction and bounded double failure.
- Create '.agents/skills/blackboard-result-protocol/SKILL.md': low-freedom output procedure for Codex.
- Create '.agents/skills/blackboard-result-protocol/agents/openai.yaml': discovery metadata.
- Create '.agents/skills/blackboard-result-protocol/scripts/validate_result.py': deterministic validation through 'parse_result()'.
- Create 'tests/test_blackboard_result_skill.py': validate Skill content and validator behavior.
- Modify 'README.md': document object-shaped facts and edges plus the single retry.

## Task 1: Make The Execution Contract Exact

**Files:**
- Modify: 'tests/test_protocol.py:4-180'
- Modify: 'orchestrator/protocol.py:23-108'

- [ ] **Step 1: Write failing prompt-contract tests**

Update the imports in 'tests/test_protocol.py':

~~~python
from orchestrator.models import RELATION_EDGE_TYPES
from orchestrator.protocol import (
    last_result_line,
    parse_intent,
    parse_result,
    render_execution_request,
    render_intent_request,
)
~~~

Add these methods to 'ResultProtocolTests':

~~~python
def test_render_execution_request_contains_exact_result_contract(self) -> None:
    request = render_execution_request(
        snapshot={
            "nodes": [
                {"id": "goal-1", "type": "goal", "state": "active"}
            ]
        },
        intent={
            "id": "intent-1",
            "source_node_id": "origin-1",
            "target_node_id": "goal-1",
            "content": "Recover the flag",
            "generation": 1,
        },
        challenge_dir=None,
    )

    self.assertIn("use $blackboard-result-protocol", request)
    self.assertIn("If the Skill is unavailable", request)
    self.assertIn(
        '{"ref":"fact-ref","content":"verified fact"}',
        request,
    )
    self.assertIn(
        '{"type":"supports","from":"fact-ref","to":"goal-1"}',
        request,
    )
    self.assertIn('"goal_achieved":true', request)
    self.assertIn('"final_answer":"verified answer"', request)
    for edge_type in RELATION_EDGE_TYPES:
        self.assertIn(edge_type, request)

def test_render_execution_request_contains_correction_context(self) -> None:
    rejected = (
        'BLACKBOARD_RESULT: {"intent_id":"intent-1",'
        '"facts":["flag found"],"edges":[],"goal_achieved":true,'
        '"final_answer":"flag{same}"}'
    )

    request = render_execution_request(
        snapshot={"nodes": []},
        intent={
            "id": "intent-1",
            "source_node_id": "origin-1",
            "target_node_id": "goal-1",
            "content": "Recover the flag",
            "generation": 1,
        },
        challenge_dir=None,
        protocol_error="fact proposal must be an object",
        rejected_result_line=rejected,
    )

    self.assertIn("correct the previous result", request)
    self.assertIn("fact proposal must be an object", request)
    self.assertIn(json.dumps(rejected), request)
    self.assertIn("Do not create or plan another Intent", request)

def test_last_result_line_returns_last_prefixed_line(self) -> None:
    output = "\n".join(
        (
            'BLACKBOARD_RESULT: {"first":true}',
            "analysis",
            'BLACKBOARD_RESULT: {"second":true}',
        )
    )

    self.assertEqual(
        last_result_line(output),
        'BLACKBOARD_RESULT: {"second":true}',
    )
    self.assertIsNone(last_result_line("analysis only"))
~~~

- [ ] **Step 2: Run the tests and confirm RED**

Run:

~~~powershell
python -m unittest tests.test_protocol.ResultProtocolTests.test_render_execution_request_contains_exact_result_contract tests.test_protocol.ResultProtocolTests.test_render_execution_request_contains_correction_context tests.test_protocol.ResultProtocolTests.test_last_result_line_returns_last_prefixed_line -v
~~~

Expected: FAIL because the request lacks the schema and correction arguments, and 'last_result_line' does not exist.

- [ ] **Step 3: Add the contract and correction rendering**

Add the following helpers after '_challenge_text()' in 'orchestrator/protocol.py':

~~~python
def last_result_line(stdout: str) -> str | None:
    candidates = [
        line
        for line in stdout.splitlines()
        if line.startswith(RESULT_PREFIX)
    ]
    return candidates[-1] if candidates else None


def _result_contract(intent: dict[str, object]) -> str:
    intent_id = str(intent["id"])
    target_node_id = str(intent["target_node_id"])
    example = {
        "intent_id": intent_id,
        "facts": [{"ref": "fact-ref", "content": "verified fact"}],
        "edges": [
            {
                "type": "supports",
                "from": "fact-ref",
                "to": target_node_id,
            }
        ],
        "goal_achieved": True,
        "final_answer": "verified answer",
    }
    edge_types = ", ".join(sorted(RELATION_EDGE_TYPES))
    return (
        "Codex: use $blackboard-result-protocol before producing the result. "
        "If the Skill is unavailable, continue with this inline contract.\n"
        "The final line must start with BLACKBOARD_RESULT: followed by one "
        "strict JSON object on the same line. The object must contain exactly "
        "intent_id, facts, edges, goal_achieved, and final_answer.\n"
        'Each fact must be {"ref":"temporary-ref","content":"non-empty fact"}. '
        'Each edge must be {"type":"relation","from":"node-or-ref",'
        '"to":"node-or-ref"}. Temporary fact refs must be unique. Edges may '
        "reference existing node IDs or fact refs from this result.\n"
        f"Allowed edge types: {edge_types}. goal_achieved must be a JSON "
        "boolean. When true, final_answer must be a non-empty string; "
        "otherwise final_answer may be null. Use empty arrays when there are "
        "no facts or edges. Do not use Python dict syntax, key=value syntax, "
        "tuples, or a Markdown fence for the protocol line.\n"
        "Valid shape using the assigned IDs:\n"
        f"{RESULT_PREFIX} "
        + json.dumps(example, ensure_ascii=False, separators=(",", ":"))
        + "\n"
    )
~~~

Replace 'render_execution_request()' with:

~~~python
def render_execution_request(
    snapshot: dict[str, object],
    intent: dict[str, object],
    challenge_dir: str | Path | None,
    protocol_error: str | None = None,
    rejected_result_line: str | None = None,
) -> str:
    correction = ""
    if protocol_error is not None:
        correction_payload = {
            "parser_error": protocol_error,
            "rejected_result_line": rejected_result_line,
        }
        correction = (
            "This is a protocol correction attempt. Correct the previous "
            "result while executing the same assigned Intent. Do not create "
            "or plan another Intent. The rejected output was:\n"
            + json.dumps(
                correction_payload,
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        )
    return (
        "Execute the assigned blackboard intent.\n"
        f"Challenge directory (read-only): {_challenge_text(challenge_dir)}\n"
        "You may propose facts and ordinary relation edges only. Do not create "
        "goal, origin, hint, or another intent.\n"
        + _result_contract(intent)
        + correction
        + "\nAssigned intent:\n"
        + json.dumps(intent, ensure_ascii=False, indent=2)
        + "\n\nGraph snapshot:\n"
        + json.dumps(snapshot, ensure_ascii=False, indent=2)
        + "\n"
    )
~~~

- [ ] **Step 4: Run protocol tests and confirm GREEN**

Run:

~~~powershell
python -m unittest tests.test_protocol -v
~~~

Expected: all protocol tests PASS.

- [ ] **Step 5: Commit the prompt contract**

~~~powershell
git add orchestrator/protocol.py tests/test_protocol.py
git commit -m "fix: define blackboard result contract"
~~~

## Task 2: Retry One Protocol Error On The Same Intent

**Files:**
- Modify: 'tests/test_supervisor.py:17-264'
- Modify: 'orchestrator/supervisor.py:1-246'

- [ ] **Step 1: Write the successful-correction regression test**

Add this method to 'SupervisorTests':

~~~python
async def test_protocol_error_retries_same_intent_and_reaches_goal(self) -> None:
    store, root = self.make_store()
    planning_calls = 0
    execution_requests: list[str] = []

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
                    {"ref": "flag-fact", "content": "flag = flag{same}"}
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

    supervisor = ProjectSupervisor(
        store=store,
        providers=(provider("codex"),),
        challenge_dir=None,
        project_dir=root,
        execute=execute,
        max_iterations=1,
    )

    status = await supervisor.run_agent_once("codex")
    snapshot = store.snapshot()
    intent = next(
        edge for edge in snapshot["edges"] if edge["type"] == "intent"
    )

    self.assertEqual(status, "completed")
    self.assertEqual(planning_calls, 1)
    self.assertEqual(len(execution_requests), 2)
    self.assertIn("fact proposal must be an object", execution_requests[1])
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
    self.assertEqual(snapshot["project"]["final_answer"], "flag{same}")
~~~

- [ ] **Step 2: Write the bounded-failure regression test**

Add:

~~~python
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
~~~

- [ ] **Step 3: Run both tests and confirm RED**

Run:

~~~powershell
python -m unittest tests.test_supervisor.SupervisorTests.test_protocol_error_retries_same_intent_and_reaches_goal tests.test_supervisor.SupervisorTests.test_protocol_correction_stops_after_second_error -v
~~~

Expected: FAIL because the current Supervisor returns after the first protocol error.

- [ ] **Step 4: Add an attempt result type and bounded retry**

Update imports in 'orchestrator/supervisor.py':

~~~python
import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from orchestrator.protocol import (
    last_result_line,
    parse_intent,
    parse_result,
    render_execution_request,
    render_intent_request,
)
~~~

Add after 'Executor':

~~~python
@dataclass(frozen=True, slots=True)
class IntentAttemptResult:
    status: str
    protocol_error: str | None = None
    rejected_result_line: str | None = None
~~~

Replace '_execute_intent()' with the coordinator below and move the existing single-attempt body into '_execute_intent_attempt()':

~~~python
async def _execute_intent(
    self,
    provider: ProviderConfig,
    intent: IntentRecord,
) -> str:
    protocol_error: str | None = None
    rejected_result_line: str | None = None
    for attempt_number in range(2):
        attempt = await self._execute_intent_attempt(
            provider,
            intent,
            protocol_error=protocol_error,
            rejected_result_line=rejected_result_line,
        )
        if attempt.status == "completed":
            return "completed"
        if attempt.status != "protocol_error" or attempt_number == 1:
            return "failed"
        protocol_error = attempt.protocol_error
        rejected_result_line = attempt.rejected_result_line
    raise AssertionError("unreachable intent attempt loop")


async def _execute_intent_attempt(
    self,
    provider: ProviderConfig,
    intent: IntentRecord,
    protocol_error: str | None,
    rejected_result_line: str | None,
) -> IntentAttemptResult:
    execution_dir = (
        self.project_dir
        / "executions"
        / f"run-{provider.name}-{uuid4().hex}"
    )
    async with self.write_lock:
        sequence = int(self.store.snapshot()["sequence"])
        execution = self.store.start_execution(
            intent.id,
            provider.name,
            str((execution_dir / "stdout.txt").resolve()),
            str((execution_dir / "stderr.txt").resolve()),
            str(execution_dir.resolve()),
        )
        events = self.store.events_after(sequence)
    for event in events:
        await self.hub.publish(event)

    offsets = {"stdout": 0, "stderr": 0}

    async def on_chunk(chunk: LogChunk) -> None:
        offset = offsets[chunk.stream]
        byte_length = len(chunk.text.encode("utf-8"))
        offsets[chunk.stream] += byte_length
        metadata = {
            "intent_id": intent.id,
            "execution_id": execution.id,
            "stream": chunk.stream,
            "byte_offset": offset,
            "byte_length": byte_length,
        }
        async with self.write_lock:
            event = self.store.append_event(
                "intent.log_chunk",
                provider.name,
                metadata,
            )
        await self.hub.publish(
            {**event, "payload": {**metadata, "text": chunk.text}}
        )

    await self._set_agent_status(provider.name, "running")
    snapshot = self.store.snapshot()
    result = await self._run_provider(
        provider,
        render_execution_request(
            snapshot,
            {
                "id": intent.id,
                "source_node_id": intent.source_node_id,
                "target_node_id": intent.target_node_id,
                "content": intent.content,
                "generation": intent.generation,
            },
            self.challenge_dir,
            protocol_error=protocol_error,
            rejected_result_line=rejected_result_line,
        ),
        execution_dir,
        on_chunk=on_chunk,
    )
    if result.status != "success":
        async with self.write_lock:
            self.store.fail_execution(
                execution.id,
                result.status,
                f"provider process status was {result.status}",
                result.exit_code,
            )
        return IntentAttemptResult("failed")

    parsed_result = parse_result(
        result.output,
        assigned_intent_id=intent.id,
        existing_node_ids=set(self.store.node_types()),
    )
    if parsed_result.status != "valid" or parsed_result.update is None:
        error = parsed_result.error or "invalid result protocol"
        async with self.write_lock:
            self.store.fail_execution(
                execution.id,
                "protocol_error",
                error,
                result.exit_code,
            )
        return IntentAttemptResult(
            status="protocol_error",
            protocol_error=error,
            rejected_result_line=last_result_line(result.output),
        )

    async with self.write_lock:
        sequence = int(self.store.snapshot()["sequence"])
        self.store.finish_execution(
            execution.id,
            parsed_result.update,
            result.exit_code,
            "completed",
        )
        answer = self.store.consensus(len(self.providers))
        if answer is not None:
            self.store.set_project_status("achieved", answer)
            self.cancel_event.set()
        events = self.store.events_after(sequence)
    for event in events:
        await self.hub.publish(event)
    self.wake_event.set()
    return IntentAttemptResult("completed")
~~~

- [ ] **Step 5: Run supervisor tests and confirm GREEN**

Run:

~~~powershell
python -m unittest tests.test_supervisor -v
~~~

Expected: all Supervisor tests PASS, including one Intent with two preserved executions.

- [ ] **Step 6: Commit the retry behavior**

~~~powershell
git add orchestrator/supervisor.py tests/test_supervisor.py
git commit -m "fix: retry malformed intent result once"
~~~

## Task 3: Create And Test The Protocol Skill

**Files:**
- Create: 'tests/test_blackboard_result_skill.py'
- Create: '.agents/skills/blackboard-result-protocol/SKILL.md'
- Create: '.agents/skills/blackboard-result-protocol/agents/openai.yaml'
- Create: '.agents/skills/blackboard-result-protocol/scripts/validate_result.py'

- [ ] **Step 1: Write failing Skill and validator tests**

Create 'tests/test_blackboard_result_skill.py':

~~~python
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = (
    ROOT / ".agents" / "skills" / "blackboard-result-protocol"
)
VALIDATOR = SKILL_ROOT / "scripts" / "validate_result.py"


class BlackboardResultSkillTests(unittest.TestCase):
    def run_validator(
        self,
        payload: object,
        existing_node_ids: tuple[str, ...] = ("goal-1",),
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as directory:
            payload_path = Path(directory) / "result.json"
            payload_path.write_text(
                json.dumps(payload),
                encoding="utf-8",
            )
            command = [
                sys.executable,
                str(VALIDATOR),
                "--intent-id",
                "intent-1",
            ]
            for node_id in existing_node_ids:
                command.extend(("--existing-node-id", node_id))
            command.extend(("--file", str(payload_path)))
            return subprocess.run(
                command,
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

    def test_skill_documents_exact_protocol_and_failures(self) -> None:
        skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        metadata = (
            SKILL_ROOT / "agents" / "openai.yaml"
        ).read_text(encoding="utf-8")

        self.assertIn("BLACKBOARD_RESULT:", skill)
        self.assertIn('"ref"', skill)
        self.assertIn('"content"', skill)
        self.assertIn('"type"', skill)
        self.assertIn("fact proposal must be an object", skill)
        self.assertIn("key=value", skill)
        self.assertIn("$blackboard-result-protocol", metadata)

    def test_validator_accepts_parser_compatible_result(self) -> None:
        result = self.run_validator(
            {
                "intent_id": "intent-1",
                "facts": [
                    {"ref": "flag-fact", "content": "flag = flag{same}"}
                ],
                "edges": [
                    {
                        "type": "supports",
                        "from": "flag-fact",
                        "to": "goal-1",
                    }
                ],
                "goal_achieved": True,
                "final_answer": "flag{same}",
            }
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "valid")

    def test_validator_rejects_string_fact(self) -> None:
        result = self.run_validator(
            {
                "intent_id": "intent-1",
                "facts": ["flag = flag{same}"],
                "edges": [],
                "goal_achieved": True,
                "final_answer": "flag{same}",
            }
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("fact proposal must be an object", result.stderr)

    def test_validator_rejects_unknown_edge_reference(self) -> None:
        result = self.run_validator(
            {
                "intent_id": "intent-1",
                "facts": [],
                "edges": [
                    {
                        "type": "supports",
                        "from": "missing",
                        "to": "goal-1",
                    }
                ],
                "goal_achieved": False,
                "final_answer": None,
            }
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("unknown node reference", result.stderr)


if __name__ == "__main__":
    unittest.main()
~~~

- [ ] **Step 2: Run the Skill tests and confirm RED**

Run:

~~~powershell
python -m unittest tests.test_blackboard_result_skill -v
~~~

Expected: ERROR or FAIL because the Skill and validator files do not exist.

- [ ] **Step 3: Initialize the Skill with the official scaffold**

Run:

~~~powershell
python C:\Users\37503\.codex\skills\.system\skill-creator\scripts\init_skill.py blackboard-result-protocol --path .agents/skills --resources scripts --interface "display_name=Blackboard Result Protocol" --interface "short_description=Validate strict BLACKBOARD_RESULT output" --interface "default_prompt=Use $blackboard-result-protocol to produce and validate a strict BLACKBOARD_RESULT line."
~~~

Expected: '.agents/skills/blackboard-result-protocol/' is created with 'SKILL.md', 'agents/openai.yaml', and 'scripts/'.

- [ ] **Step 4: Replace the scaffold with the minimal tested Skill**

Set '.agents/skills/blackboard-result-protocol/SKILL.md' to:

~~~~markdown
---
name: blackboard-result-protocol
description: Use when executing a blackboard Intent, producing BLACKBOARD_RESULT output, or correcting protocol_error failures such as fact proposal must be an object.
---

# Blackboard Result Protocol

## Core Rule

Finish with exactly one single-line 'BLACKBOARD_RESULT: ' record whose suffix is strict JSON accepted by the project parser. Finding an answer does not bypass validation.

## Required Shape

The JSON object must contain exactly:

- 'intent_id': the assigned Intent ID.
- 'facts': objects shaped '{"ref":"unique-temp-ref","content":"non-empty fact"}'.
- 'edges': objects shaped '{"type":"supports","from":"node-or-ref","to":"node-or-ref"}'.
- 'goal_achieved': JSON boolean.
- 'final_answer': non-empty string when achieved, otherwise string or null.

Allowed edge types: 'supports', 'contradicts', 'derived_from', 'addresses', 'similar_to', 'equivalent_to'.

Edges may reference existing graph node IDs or temporary fact refs declared in the same result. Use empty arrays when no facts or edges are needed.

## Workflow

1. Execute only the assigned Intent. Do not plan another Intent.
2. Write the JSON object to 'blackboard-result.json' without the protocol prefix.
3. Run the bundled validator with the assigned Intent ID and every existing node ID referenced by an edge:

   'python <skill-directory>/scripts/validate_result.py --intent-id <assigned-id> --existing-node-id <node-id> --file blackboard-result.json'

4. Fix every validation error and rerun until it prints 'valid'.
5. Emit one final line: 'BLACKBOARD_RESULT: ' followed by the compact JSON file content.

## Valid Example

~~~text
BLACKBOARD_RESULT: {"intent_id":"intent-1","facts":[{"ref":"flag-fact","content":"flag = flag{example}"}],"edges":[{"type":"supports","from":"flag-fact","to":"goal-1"}],"goal_achieved":true,"final_answer":"flag{example}"}
~~~

## Invalid Forms

- 'BLACKBOARD_RESULT: intent_id="intent-1" facts=[]' is key=value text, not JSON.
- '"facts":["flag found"]' fails with 'fact proposal must be an object'.
- '"edges":[["fact-1","goal-1"]]' fails because every edge must be an object with 'type', 'from', and 'to'.
- Do not wrap the final protocol line in a Markdown fence.
~~~~

Set '.agents/skills/blackboard-result-protocol/agents/openai.yaml' to:

~~~yaml
interface:
  display_name: "Blackboard Result Protocol"
  short_description: "Validate strict BLACKBOARD_RESULT output"
  default_prompt: "Use $blackboard-result-protocol to produce and validate a strict BLACKBOARD_RESULT line."
~~~

- [ ] **Step 5: Implement the validator through the production parser**

Set '.agents/skills/blackboard-result-protocol/scripts/validate_result.py' to:

~~~python
import argparse
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from orchestrator.protocol import RESULT_PREFIX, parse_result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate a BLACKBOARD_RESULT JSON payload."
    )
    parser.add_argument("--intent-id", required=True)
    parser.add_argument(
        "--existing-node-id",
        action="append",
        default=[],
    )
    parser.add_argument("--file", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        payload = args.file.read_text(encoding="utf-8").strip()
    except OSError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    result = parse_result(
        f"{RESULT_PREFIX} {payload}",
        assigned_intent_id=args.intent_id,
        existing_node_ids=set(args.existing_node_id),
    )
    if result.status != "valid":
        print(result.error or "invalid result protocol", file=sys.stderr)
        return 1
    print("valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
~~~

- [ ] **Step 6: Run Skill tests and structural validation**

Run:

~~~powershell
python -m unittest tests.test_blackboard_result_skill -v
python C:\Users\37503\.codex\skills\.system\skill-creator\scripts\quick_validate.py .agents/skills/blackboard-result-protocol
~~~

Expected: all four unit tests PASS and 'quick_validate.py' reports the Skill as valid.

- [ ] **Step 7: Commit the Skill**

~~~powershell
git add .agents/skills/blackboard-result-protocol tests/test_blackboard_result_skill.py
git commit -m "feat: add blackboard result protocol skill"
~~~

## Task 4: Document The Runtime Behavior

**Files:**
- Modify: 'README.md:175-195'

- [ ] **Step 1: Replace the incomplete result example**

Replace the execution protocol block with:

~~~~markdown
执行阶段必须输出单行严格 JSON。'facts' 和 'edges' 的成员都是对象，不是字符串或二维数组：

~~~text
BLACKBOARD_RESULT: {"intent_id":"intent-...","facts":[{"ref":"flag-fact","content":"flag = flag{example}"}],"edges":[{"type":"supports","from":"flag-fact","to":"goal-..."}],"goal_achieved":true,"final_answer":"flag{example}"}
~~~

'facts' 项只能包含 'ref'、'content'；'edges' 项只能包含 'type'、'from'、'to'。执行进程成功但协议解析失败时，Supervisor 会保留失败 execution，并针对同一 Intent 创建一次带解析错误反馈的纠错 execution。第二次仍失败才把本轮计为失败。
~~~~

- [ ] **Step 2: Check README consistency**

Run:

~~~powershell
Select-String -Path README.md -Pattern 'facts.*字符串','二维数组','纠错 execution','BLACKBOARD_RESULT'
~~~

Expected: the new object-shape warning, complete example, and one-retry description are present.

- [ ] **Step 3: Commit documentation**

~~~powershell
git add README.md
git commit -m "docs: clarify blackboard result protocol"
~~~

## Task 5: Full Verification

**Files:**
- Verify only; do not modify unrelated files.

- [ ] **Step 1: Run focused regression tests**

Run:

~~~powershell
python -m unittest tests.test_protocol tests.test_supervisor tests.test_blackboard_result_skill -v
~~~

Expected: all focused tests PASS with zero failures and zero errors.

- [ ] **Step 2: Run the complete Python test suite**

Run:

~~~powershell
python -m unittest discover -s tests -v
~~~

Expected: the complete suite PASS with zero failures and zero errors.

- [ ] **Step 3: Validate the Skill again**

Run:

~~~powershell
python C:\Users\37503\.codex\skills\.system\skill-creator\scripts\quick_validate.py .agents/skills/blackboard-result-protocol
~~~

Expected: the Skill is valid.

- [ ] **Step 4: Check repository hygiene**

Run:

~~~powershell
git diff --check
git status --short
~~~

Expected: no whitespace errors. The existing untracked 'worksapce/' directory remains untouched; only intended implementation files may appear before their commits.

- [ ] **Step 5: Inspect the final history and behavior**

Run:

~~~powershell
git log -5 --oneline
~~~

Expected: separate commits for the contract, retry, Skill, and README appear after the approved design commit '0ab84f0'.
