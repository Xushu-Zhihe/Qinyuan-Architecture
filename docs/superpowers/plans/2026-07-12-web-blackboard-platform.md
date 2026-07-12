# Web Blackboard Platform Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a localhost Web application where isolated projects visualize Goal, Origin, Fact, and Hint as editable nodes, model Intent as first-class edges, run multiple asynchronous CLI Agents per project, and stream each Agent's real stdout/stderr into the selected graph element.

**Architecture:** Migrate the existing round-based Intent-node model to a generation-based graph with Intent edges and per-Agent executions. Add a project registry, asynchronous project supervisors, FastAPI REST/WebSocket adapters, and a React Flow frontend. Preserve one SQLite database and one process supervisor per project so projects can run concurrently without sharing graph, log, or process state.

**Tech Stack:** Python 3.10, SQLite, asyncio subprocesses, FastAPI, WebSocket, React 19, TypeScript, Vite, `@xyflow/react`, Vitest, Testing Library, Playwright.

---

## Scope And File Map

Backend files to modify:

- `orchestrator/models.py`: Intent-edge, planning, result, execution, and project result dataclasses.
- `orchestrator/protocol.py`: two-stage `BLACKBOARD_INTENT` and `BLACKBOARD_RESULT` rendering and parsing.
- `orchestrator/store.py`: project graph schema, generation, Intent edges, executions, outputs, events, and asynchronous consensus.
- `orchestrator/scheduler.py`: remove round orchestration and expose a compatibility wrapper around the new supervisor.
- `worker/models.py`: streamed worker result and log chunk types.
- `worker/executor.py`: cancellable process-tree execution with live stdout/stderr callbacks.
- `main.py`: migrate the existing CLI to the new Intent-edge runtime.
- `requirements.txt`: FastAPI runtime and API-test dependencies.

Backend files to create:

- `orchestrator/events.py`: project-scoped event hub and sequence-aware subscriptions.
- `orchestrator/supervisor.py`: Agent loops and project lifecycle.
- `orchestrator/projects.py`: project registry, directory isolation, and project service.
- `orchestrator/layout.py`: revisioned `layout.json` reads and atomic writes.
- `web/__init__.py`: Web package marker.
- `web/schemas.py`: Pydantic request and response schemas.
- `web/app.py`: FastAPI application factory, REST routes, WebSocket route, and static frontend mount.
- `web_main.py`: localhost server entry point.
- `tests/fixtures/fake_agent.py`: deterministic planning/execution CLI used by integration tests.

Backend tests to create or replace:

- `tests/test_models.py`
- `tests/test_protocol.py`
- `tests/test_blackboard_store.py`
- `tests/test_streaming_executor.py`
- `tests/test_supervisor.py`
- `tests/test_projects.py`
- `tests/test_layout.py`
- `tests/test_web_api.py`
- `tests/test_websocket.py`
- `tests/test_main.py`

Frontend files to create:

- `frontend/package.json`, `frontend/tsconfig.json`, `frontend/vite.config.ts`, `frontend/index.html`
- `frontend/src/main.tsx`, `frontend/src/App.tsx`, `frontend/src/styles.css`
- `frontend/src/types.ts`
- `frontend/src/api/client.ts`, `frontend/src/api/projectSocket.ts`
- `frontend/src/state/projectStore.ts`
- `frontend/src/pages/ProjectDashboard.tsx`, `frontend/src/pages/BlackboardWorkspace.tsx`
- `frontend/src/components/NewProjectDialog.tsx`
- `frontend/src/components/ProjectCard.tsx`
- `frontend/src/components/GraphToolbar.tsx`
- `frontend/src/components/InspectorPanel.tsx`
- `frontend/src/components/LogConsole.tsx`
- `frontend/src/graph/BlackboardGraph.tsx`
- `frontend/src/graph/BlackboardNode.tsx`
- `frontend/src/graph/IntentEdge.tsx`
- focused `*.test.tsx` files beside each component
- `frontend/e2e/blackboard.spec.ts`

## Task 1: Replace Intent Nodes With Domain-Level Intent Edges

**Files:**
- Modify: `orchestrator/models.py`
- Create: `tests/test_models.py`

- [ ] **Step 1: Write failing domain-model tests**

```python
import unittest

from orchestrator.models import (
    AGENT_NODE_TYPES,
    NODE_TYPES,
    IntentDecision,
    IntentProposal,
)


class DomainModelTests(unittest.TestCase):
    def test_intent_is_not_a_node_type(self) -> None:
        self.assertEqual(NODE_TYPES, frozenset({"goal", "origin", "fact", "hint"}))
        self.assertEqual(AGENT_NODE_TYPES, frozenset({"fact"}))

    def test_plan_decision_requires_first_class_intent(self) -> None:
        proposal = IntentProposal("fact-1", "goal-1", "Verify the key source")
        decision = IntentDecision(action="plan", intent=proposal, reason=None)
        self.assertEqual(decision.intent.source_node_id, "fact-1")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests and confirm the old model fails**

Run: `python -m unittest tests.test_models -v`

Expected: FAIL because `IntentDecision` and `IntentProposal` do not exist and `NODE_TYPES` still includes `intent`.

- [ ] **Step 3: Replace the model contract**

```python
from dataclasses import dataclass


NODE_TYPES = frozenset({"goal", "origin", "fact", "hint"})
AGENT_NODE_TYPES = frozenset({"fact"})
RELATION_EDGE_TYPES = frozenset(
    {
        "supports",
        "contradicts",
        "derived_from",
        "addresses",
        "similar_to",
        "equivalent_to",
    }
)


@dataclass(frozen=True, slots=True)
class IntentProposal:
    source_node_id: str
    target_node_id: str
    content: str


@dataclass(frozen=True, slots=True)
class IntentDecision:
    action: str
    intent: IntentProposal | None
    reason: str | None


@dataclass(frozen=True, slots=True)
class FactProposal:
    ref: str
    content: str


@dataclass(frozen=True, slots=True)
class RelationProposal:
    type: str
    from_ref: str
    to_ref: str


@dataclass(frozen=True, slots=True)
class ExecutionUpdate:
    intent_id: str
    facts: tuple[FactProposal, ...]
    edges: tuple[RelationProposal, ...]
    goal_achieved: bool
    final_answer: str | None


@dataclass(frozen=True, slots=True)
class IntentParseResult:
    status: str
    decision: IntentDecision | None
    error: str | None


@dataclass(frozen=True, slots=True)
class ResultParseResult:
    status: str
    update: ExecutionUpdate | None
    error: str | None
```

Keep `ProviderConfig`, process results, and project result dataclasses separate from graph proposals. Remove `NodeProposal(type="intent")`, `AgentRoundResult`, and round-specific result names after downstream tests migrate.

- [ ] **Step 4: Run the domain tests**

Run: `python -m unittest tests.test_models -v`

Expected: 2 tests pass.

- [ ] **Step 5: Commit the domain contract**

```powershell
git add orchestrator/models.py tests/test_models.py
git commit -m "refactor: model intent as graph edges"
```

## Task 2: Implement The Two-Stage Agent Protocol

**Files:**
- Replace: `orchestrator/protocol.py`
- Replace: `tests/test_protocol.py`

- [ ] **Step 1: Write protocol tests for plan, idle, and result records**

```python
import json
import unittest

from orchestrator.protocol import parse_intent, parse_result


class ProtocolTests(unittest.TestCase):
    def test_parses_plan_record(self) -> None:
        output = 'BLACKBOARD_INTENT: {"action":"plan","source_node_id":"fact-1","target_node_id":"goal-1","content":"Verify key"}'
        result = parse_intent(output, {"fact-1": "fact", "goal-1": "goal"})
        self.assertEqual(result.status, "valid")
        self.assertEqual(result.decision.intent.content, "Verify key")

    def test_allows_origin_only_when_no_fact_exists(self) -> None:
        output = 'BLACKBOARD_INTENT: {"action":"plan","source_node_id":"origin-1","target_node_id":"goal-1","content":"Bootstrap"}'
        valid = parse_intent(output, {"origin-1": "origin", "goal-1": "goal"})
        invalid = parse_intent(output, {"origin-1": "origin", "goal-1": "goal", "fact-1": "fact"})
        self.assertEqual(valid.status, "valid")
        self.assertEqual(invalid.status, "protocol_error")

    def test_parses_explicit_idle(self) -> None:
        output = 'BLACKBOARD_INTENT: {"action":"idle","reason":"No unresolved work"}'
        result = parse_intent(output, {"fact-1": "fact"})
        self.assertEqual(result.decision.action, "idle")

    def test_result_must_reference_assigned_intent(self) -> None:
        payload = {"intent_id":"intent-wrong","facts":[],"edges":[],"goal_achieved":False,"final_answer":None}
        result = parse_result("BLACKBOARD_RESULT: " + json.dumps(payload), "intent-expected", {"goal-1"})
        self.assertEqual(result.status, "protocol_error")
```

- [ ] **Step 2: Run the tests and confirm both parsers are missing**

Run: `python -m unittest tests.test_protocol -v`

Expected: FAIL importing `parse_intent` and `parse_result`.

- [ ] **Step 3: Implement strict parsing and request rendering**

```python
INTENT_PREFIX = "BLACKBOARD_INTENT:"
RESULT_PREFIX = "BLACKBOARD_RESULT:"


def parse_intent(stdout: str, node_types: dict[str, str]) -> IntentParseResult:
    payload = _last_payload(stdout, INTENT_PREFIX)
    action = payload.get("action")
    if action == "idle":
        _require_exact_fields(payload, {"action", "reason"}, "intent")
        reason = _non_empty_string(payload["reason"], "reason")
        return IntentParseResult("valid", IntentDecision("idle", None, reason), None)
    _require_exact_fields(
        payload,
        {"action", "source_node_id", "target_node_id", "content"},
        "intent",
    )
    if action != "plan":
        raise ValueError("intent action must be plan or idle")
    source = _non_empty_string(payload["source_node_id"], "source_node_id")
    target = _non_empty_string(payload["target_node_id"], "target_node_id")
    facts_exist = "fact" in node_types.values()
    if node_types.get(source) != "fact" and not (
        node_types.get(source) == "origin" and not facts_exist
    ):
        raise ValueError("source must be a fact or bootstrap origin")
    if node_types.get(target) not in {"goal", "fact", "hint"}:
        raise ValueError("unsupported intent target")
    proposal = IntentProposal(source, target, _non_empty_string(payload["content"], "content"))
    return IntentParseResult("valid", IntentDecision("plan", proposal, None), None)
```

Implement `render_intent_request(snapshot, challenge_dir)` and `render_execution_request(snapshot, intent, challenge_dir)` with exact JSON contracts. Implement `parse_result` so Agents may only propose Fact records and ordinary relation edges, may not create Intent, Goal, Origin, or Hint, and must reference the assigned Intent id.

- [ ] **Step 4: Run protocol tests**

Run: `python -m unittest tests.test_protocol -v`

Expected: all plan, idle, spoofing, unknown-reference, and final-answer tests pass.

- [ ] **Step 5: Commit the protocol migration**

```powershell
git add orchestrator/protocol.py tests/test_protocol.py
git commit -m "feat: add two-stage agent protocol"
```

## Task 3: Replace The Round Store With A Project Graph Store

**Files:**
- Replace: `orchestrator/store.py`
- Replace: `tests/test_blackboard_store.py`

- [ ] **Step 1: Write schema and generation tests**

```python
class BlackboardStoreTests(StoreTestCase):
    def test_schema_rejects_intent_nodes(self) -> None:
        store = self.make_store()
        with self.assertRaises(sqlite3.IntegrityError):
            store.connection.execute(
                "INSERT INTO nodes(id, project_id, type, content, state, generation, created_at) VALUES (?,?,?,?,?,?,?)",
                ("intent-bad", store.project_id, "intent", "x", "active", 1, "now"),
            )

    def test_bootstrap_intent_uses_generation_one(self) -> None:
        store = self.make_store()
        origin = store.node_id("origin")
        goal = store.node_id("goal")
        intent = store.create_or_merge_intent(
            "codex", IntentProposal(origin, goal, "Inspect the challenge")
        )
        self.assertEqual(intent.generation, 1)

    def test_same_semantic_intent_merges_edge(self) -> None:
        store = self.make_store_with_fact()
        first = store.create_or_merge_intent("codex", self.intent("Verify key"))
        second = store.create_or_merge_intent("claude", self.intent("  Verify  key "))
        self.assertEqual(first.id, second.id)
        self.assertEqual(store.intent_sources(first.id), ("claude", "codex"))
```

- [ ] **Step 2: Run the tests and confirm the old round schema fails**

Run: `python -m unittest tests.test_blackboard_store.BlackboardStoreTests -v`

Expected: FAIL because the existing schema accepts Intent nodes and has no generation or Intent-edge methods.

- [ ] **Step 3: Implement the project schema**

Use one project database with `PRAGMA foreign_keys=ON`, `PRAGMA journal_mode=WAL`, and `PRAGMA busy_timeout=5000`. Replace `runs`, `rounds`, and `agent_results` with the following tables:

```sql
CREATE TABLE projects (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    goal_version INTEGER NOT NULL DEFAULT 1,
    final_answer TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE nodes (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    type TEXT NOT NULL CHECK(type IN ('goal','origin','fact','hint')),
    content TEXT NOT NULL CHECK(length(trim(content)) > 0),
    fingerprint TEXT,
    state TEXT NOT NULL DEFAULT 'active',
    generation INTEGER NOT NULL CHECK(generation >= 0),
    created_at TEXT NOT NULL
);
CREATE TABLE edges (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    type TEXT NOT NULL,
    from_node TEXT NOT NULL REFERENCES nodes(id),
    to_node TEXT NOT NULL REFERENCES nodes(id),
    generation INTEGER NOT NULL CHECK(generation >= 0),
    created_at TEXT NOT NULL
);
CREATE TABLE intents (
    edge_id TEXT PRIMARY KEY REFERENCES edges(id) ON DELETE CASCADE,
    content TEXT NOT NULL,
    fingerprint TEXT NOT NULL UNIQUE
);
CREATE TABLE node_provenance (
    id TEXT PRIMARY KEY,
    node_id TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    original_content TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE edge_provenance (
    id TEXT PRIMARY KEY,
    edge_id TEXT NOT NULL REFERENCES edges(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(edge_id, source)
);
CREATE TABLE node_versions (
    id TEXT PRIMARY KEY,
    node_id TEXT NOT NULL REFERENCES nodes(id),
    old_content TEXT NOT NULL,
    new_content TEXT NOT NULL,
    source TEXT NOT NULL,
    created_at TEXT NOT NULL
);
```

Implement `BlackboardStore.create`, `snapshot`, `node_types`, `node_id`, `add_human_node`, `archive_node`, `add_relation`, `create_or_merge_intent`, and Fact normalization. Intent generation is `source.generation + 1`; result Facts inherit the Intent generation.

- [ ] **Step 4: Run store schema tests**

Run: `python -m unittest tests.test_blackboard_store.BlackboardStoreTests -v`

Expected: schema, initial nodes, generation, foreign-key, provenance, and Intent merge tests pass.

- [ ] **Step 5: Commit the graph store foundation**

```powershell
git add orchestrator/store.py tests/test_blackboard_store.py
git commit -m "refactor: store project graph with intent edges"
```

## Task 4: Add Intent Executions, Outputs, Events, And Async Consensus

**Files:**
- Modify: `orchestrator/store.py`
- Modify: `tests/test_blackboard_store.py`

- [ ] **Step 1: Add failing execution and consensus tests**

```python
def test_execution_output_links_fact_to_intent(self) -> None:
    store, intent = self.make_store_and_intent()
    execution = store.start_execution(intent.id, "codex", self.paths())
    store.finish_execution(execution.id, self.result_with_fact("JWT uses HS256"), 0, "completed")
    snapshot = store.snapshot()
    fact = next(node for node in snapshot["nodes"] if node["type"] == "fact")
    self.assertEqual(fact["produced_by"]["intent_id"], intent.id)
    self.assertEqual(fact["produced_by"]["execution_id"], execution.id)

def test_consensus_spans_asynchronous_executions(self) -> None:
    store = self.make_store()
    store.record_answer("codex", "flag{same}")
    self.assertIsNone(store.consensus(available_providers=2))
    store.record_answer("claude", " flag{same} ")
    self.assertEqual(store.consensus(available_providers=2), "flag{same}")
```

- [ ] **Step 2: Run the focused tests and confirm missing methods**

Run: `python -m unittest tests.test_blackboard_store.BlackboardExecutionTests -v`

Expected: FAIL because execution, output, event, and asynchronous answer tables do not exist.

- [ ] **Step 3: Add execution and event tables plus transactional methods**

```sql
CREATE TABLE intent_executions (
    id TEXT PRIMARY KEY,
    intent_edge_id TEXT NOT NULL REFERENCES intents(edge_id),
    provider TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    exit_code INTEGER,
    stdout_path TEXT NOT NULL,
    stderr_path TEXT NOT NULL,
    work_dir TEXT NOT NULL,
    error_message TEXT
);
CREATE TABLE intent_outputs (
    intent_edge_id TEXT NOT NULL REFERENCES intents(edge_id),
    execution_id TEXT NOT NULL REFERENCES intent_executions(id),
    node_id TEXT NOT NULL REFERENCES nodes(id),
    PRIMARY KEY(execution_id, node_id)
);
CREATE TABLE final_answers (
    id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    answer TEXT NOT NULL,
    goal_version INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    source TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
```

Implement `start_execution`, `finish_execution`, `mark_interrupted`, `record_answer`, `consensus`, `events_after`, and Intent aggregate status. `finish_execution` must validate and merge the entire `ExecutionUpdate` inside one transaction; any bad reference rolls back Facts, relations, outputs, answers, and status changes together.

```python
def finish_execution(
    self,
    execution_id: str,
    update: ExecutionUpdate,
    exit_code: int | None,
    status: str,
) -> tuple[str, ...]:
    with self.connection:
        execution = self._execution(execution_id)
        if update.intent_id != execution["intent_edge_id"]:
            raise ValueError("result references the wrong intent")
        refs = self._merge_facts(update.facts, execution)
        self._insert_relations(update.edges, refs)
        self._link_intent_outputs(execution, refs.values())
        if update.goal_achieved:
            self.record_answer(execution["provider"], update.final_answer)
        self._set_execution_status(execution_id, status, exit_code)
        return tuple(refs.values())
```

- [ ] **Step 4: Run all store tests**

Run: `python -m unittest tests.test_blackboard_store -v`

Expected: all graph, execution, rollback, event sequence, and consensus tests pass.

- [ ] **Step 5: Commit execution persistence**

```powershell
git add orchestrator/store.py tests/test_blackboard_store.py
git commit -m "feat: persist intent executions and outputs"
```

## Task 5: Stream And Cancel Provider Processes

**Files:**
- Modify: `worker/models.py`
- Replace: `worker/executor.py`
- Create: `tests/test_streaming_executor.py`
- Delete after migration: `tests/test_executor.py`

- [ ] **Step 1: Write streaming and cancellation tests**

```python
class StreamingExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def test_streams_stdout_and_stderr_before_exit(self) -> None:
        chunks = []
        result = await execute_provider(
            provider_for("import sys,time;print('one',flush=True);print('warn',file=sys.stderr,flush=True);time.sleep(.05);print('two',flush=True)"),
            "request",
            None,
            self.work_dir(),
            on_chunk=chunks.append,
        )
        self.assertEqual(result.status, "success")
        self.assertEqual([(c.stream, c.text.strip()) for c in chunks], [("stdout", "one"), ("stderr", "warn"), ("stdout", "two")])

    async def test_cancel_event_kills_process_tree(self) -> None:
        cancel = asyncio.Event()
        task = asyncio.create_task(execute_provider(self.slow_provider(), "x", None, self.work_dir(), cancel_event=cancel))
        await asyncio.sleep(.05)
        cancel.set()
        result = await task
        self.assertEqual(result.status, "cancelled")
```

- [ ] **Step 2: Run the tests and confirm the existing communicate-based executor fails**

Run: `python -m unittest tests.test_streaming_executor -v`

Expected: FAIL because `LogChunk`, `on_chunk`, and `cancel_event` are missing.

- [ ] **Step 3: Implement concurrent stream readers**

```python
@dataclass(frozen=True, slots=True)
class LogChunk:
    stream: str
    text: str


async def _read_stream(stream, name, path, on_chunk):
    parts: list[str] = []
    with path.open("w", encoding="utf-8", newline="") as handle:
        while True:
            raw = await stream.readline()
            if not raw:
                break
            text = raw.decode("utf-8", errors="replace")
            handle.write(text)
            handle.flush()
            parts.append(text)
            if on_chunk is not None:
                value = on_chunk(LogChunk(name, text))
                if inspect.isawaitable(value):
                    await value
    return "".join(parts)
```

Run stdout reader, stderr reader, process wait, timeout, and cancel wait concurrently. Preserve Windows `CREATE_NEW_PROCESS_GROUP` and `taskkill /T /F`; preserve POSIX process groups. Return `cancelled`, `timeout`, `failed`, `success`, or `unavailable` without losing partial logs.

- [ ] **Step 4: Run executor tests with process permission**

Run: `python -m unittest tests.test_streaming_executor -v`

Expected: all tests pass. If the sandbox returns `unavailable` for local Python subprocesses, rerun the same command with elevation.

- [ ] **Step 5: Commit the streaming executor**

```powershell
git add worker/models.py worker/executor.py tests/test_streaming_executor.py tests/test_executor.py
git commit -m "feat: stream and cancel agent processes"
```

## Task 6: Build Project-Scoped Event Hub And Async Agent Supervisor

**Files:**
- Create: `orchestrator/events.py`
- Create: `orchestrator/supervisor.py`
- Create: `tests/test_supervisor.py`
- Create: `tests/fixtures/fake_agent.py`

- [ ] **Step 1: Write one-cycle and multi-Agent isolation tests**

```python
class SupervisorTests(unittest.IsolatedAsyncioTestCase):
    async def test_agent_plans_before_execution(self) -> None:
        supervisor, store, calls = self.make_supervisor(["codex"])
        await supervisor.run_agent_once("codex")
        self.assertEqual(calls, ["planning", "execution"])
        self.assertEqual(store.snapshot()["edges"][0]["type"], "intent")

    async def test_one_agent_failure_does_not_stop_another(self) -> None:
        supervisor = self.make_supervisor(["broken", "working"])[0]
        await supervisor.start()
        await supervisor.wait_until_idle(timeout=1)
        statuses = supervisor.agent_statuses()
        self.assertEqual(statuses["broken"], "failed")
        self.assertIn(statuses["working"], {"idle", "completed"})
```

- [ ] **Step 2: Run tests and confirm supervisor modules are missing**

Run: `python -m unittest tests.test_supervisor -v`

Expected: FAIL importing `ProjectSupervisor` and `ProjectEventHub`.

- [ ] **Step 3: Implement the event hub**

```python
class ProjectEventHub:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[dict[str, object]]] = set()

    async def publish(self, event: dict[str, object]) -> None:
        for queue in tuple(self._subscribers):
            await queue.put(event)

    @asynccontextmanager
    async def subscribe(self):
        queue: asyncio.Queue[dict[str, object]] = asyncio.Queue(maxsize=500)
        self._subscribers.add(queue)
        try:
            yield queue
        finally:
            self._subscribers.discard(queue)
```

- [ ] **Step 4: Implement the Agent loop and project lifecycle**

```python
class ProjectSupervisor:
    async def run_agent_once(self, provider_name: str) -> str:
        provider = self.providers[provider_name]
        manual = await self._take_manual_intent(provider_name)
        intent = manual or await self._plan_intent(provider)
        if intent is None:
            return "idle"
        execution = await self._start_execution(intent, provider)
        result = await self._execute_intent(execution, provider)
        await self._commit_result(execution, result)
        return result.status

    async def start(self) -> None:
        self.cancel_event.clear()
        self.tasks = {
            name: asyncio.create_task(self._agent_loop(name))
            for name in self.providers
        }

    async def stop(self) -> None:
        self.cancel_event.set()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
```

Use a project-level write lock, a shared global CLI semaphore, failure threshold 3, wake-up event for new graph work, and project statuses `idle`, `running`, `degraded`, `achieved`, and `stopped`. Publish persisted graph events and non-persisted `intent.log_chunk` events only to this project's hub.

- [ ] **Step 5: Run supervisor tests**

Run: `python -m unittest tests.test_supervisor -v`

Expected: planning precedes execution, multiple Agents run concurrently, one failure is isolated, stop cancels all project executions, and consensus stops the project.

- [ ] **Step 6: Commit the asynchronous runtime**

```powershell
git add orchestrator/events.py orchestrator/supervisor.py tests/test_supervisor.py tests/fixtures/fake_agent.py
git commit -m "feat: run asynchronous project agents"
```

## Task 7: Add Project Registry And Revisioned Layout Storage

**Files:**
- Create: `orchestrator/projects.py`
- Create: `orchestrator/layout.py`
- Create: `tests/test_projects.py`
- Create: `tests/test_layout.py`

- [ ] **Step 1: Write registry path-isolation tests**

```python
def test_project_directory_is_generated_under_root(self) -> None:
    project = self.registry.create("Demo", "manual", None)
    self.assertEqual(project.path.parent, self.projects_root)
    self.assertTrue(project.path.name.startswith("project-"))

def test_execution_path_cannot_cross_project_boundary(self) -> None:
    with self.assertRaises(ProjectPathError):
        self.registry.resolve_project_path("project-a", "../project-b/blackboard.db")
```

- [ ] **Step 2: Write layout revision tests**

```python
def test_layout_update_requires_current_revision(self) -> None:
    store = LayoutStore(self.project_dir)
    first = store.save(0, {"nodes": {"fact-1": {"x": 10, "y": 20}}, "edges": {}, "viewport": {}})
    self.assertEqual(first["revision"], 1)
    with self.assertRaises(LayoutConflict):
        store.save(0, {"nodes": {}, "edges": {}, "viewport": {}})
```

- [ ] **Step 3: Run tests and confirm modules are missing**

Run: `python -m unittest tests.test_projects tests.test_layout -v`

Expected: FAIL importing registry and layout classes.

- [ ] **Step 4: Implement registry and project service boundaries**

Create a global `projects.db` with `id`, `name`, `mode`, `status`, `project_path`, `challenge_path`, provider JSON, per-project Agent limit, and timestamps. Implement `ProjectRegistry.create/list/get/update_status` and `ProjectService.create/start/stop/get_snapshot`. The service owns `ProjectRuntimeManager`, which guarantees one Supervisor per project id.

```python
@dataclass(slots=True)
class ProjectHandle:
    id: str
    path: Path
    store: BlackboardStore
    layout: LayoutStore
    hub: ProjectEventHub
    providers: tuple[ProviderConfig, ...]
    challenge_dir: Path | None


class ProjectRuntimeManager:
    def __init__(self, semaphore: asyncio.Semaphore) -> None:
        self._active: dict[str, ProjectSupervisor] = {}
        self._lock = asyncio.Lock()
        self._semaphore = semaphore

    async def start(self, project: ProjectHandle) -> ProjectSupervisor:
        async with self._lock:
            existing = self._active.get(project.id)
            if existing is not None:
                return existing
            supervisor = ProjectSupervisor(project, self._semaphore)
            self._active[project.id] = supervisor
            await supervisor.start()
            return supervisor
```

- [ ] **Step 5: Implement atomic layout writes**

```python
def save(self, expected_revision: int, payload: dict[str, object]) -> dict[str, object]:
    current = self.load()
    if current["revision"] != expected_revision:
        raise LayoutConflict(current["revision"])
    next_value = {**payload, "revision": expected_revision + 1}
    temporary = self.path.with_suffix(".tmp")
    temporary.write_text(json.dumps(next_value, ensure_ascii=False), encoding="utf-8")
    temporary.replace(self.path)
    return next_value
```

- [ ] **Step 6: Run registry and layout tests**

Run: `python -m unittest tests.test_projects tests.test_layout -v`

Expected: project creation, lookup, duplicate supervisor prevention, path traversal rejection, layout save, and revision conflict tests pass.

- [ ] **Step 7: Commit project persistence**

```powershell
git add orchestrator/projects.py orchestrator/layout.py tests/test_projects.py tests/test_layout.py
git commit -m "feat: isolate project metadata and layouts"
```

## Task 8: Expose Project And Graph REST APIs

**Files:**
- Modify: `requirements.txt`
- Create: `web/__init__.py`
- Create: `web/schemas.py`
- Create: `web/app.py`
- Create: `tests/test_web_api.py`

- [ ] **Step 1: Add backend dependencies**

```text
fastapi>=0.116,<1
uvicorn[standard]>=0.35,<1
httpx>=0.28,<1
```

Run: `python -m pip install -r requirements.txt`

Expected: FastAPI, Uvicorn, and HTTPX install successfully.

- [ ] **Step 2: Write API tests before routes**

```python
class WebApiTests(unittest.TestCase):
    def test_creates_manual_project_and_returns_graph(self) -> None:
        response = self.client.post("/api/projects", json={
            "name": "Demo",
            "goal": "Recover flag",
            "origin": "Challenge statement",
            "hints": ["Inspect config"],
            "mode": "manual",
            "providers": [],
            "agent_limit": 1,
            "challenge_path": None,
        })
        self.assertEqual(response.status_code, 201)
        project_id = response.json()["id"]
        graph = self.client.get(f"/api/projects/{project_id}/graph").json()
        self.assertEqual([node["type"] for node in graph["nodes"]], ["goal", "origin", "hint"])

    def test_cannot_read_execution_from_another_project(self) -> None:
        response = self.client.get(f"/api/projects/{self.project_a}/executions/{self.project_b_execution}")
        self.assertEqual(response.status_code, 404)
```

- [ ] **Step 3: Run API tests and confirm routes are absent**

Run: `python -m unittest tests.test_web_api -v`

Expected: FAIL importing `create_app` or receiving 404 from all routes.

- [ ] **Step 4: Implement Pydantic schemas and app factory**

```python
class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    goal: str = Field(min_length=1)
    origin: str = Field(min_length=1)
    hints: list[str] = Field(default_factory=list)
    mode: Literal["manual", "run"]
    providers: list[str]
    agent_limit: int = Field(ge=1, le=16)
    challenge_path: str | None = None


def create_app(data_root: Path | None = None) -> FastAPI:
    app = FastAPI(title="Blackboard")
    service = ProjectService(data_root or DEFAULT_DATA_ROOT)
    app.state.projects = service
    register_routes(app)
    return app
```

Implement project list/create/detail/update/start/stop, graph snapshot, human node CRUD, relation/Intent edge CRUD, layout PUT, execution detail, and saved log endpoints. Convert path, validation, revision, and missing-entity errors into explicit 400, 404, or 409 responses.

- [ ] **Step 5: Run API tests**

Run: `python -m unittest tests.test_web_api -v`

Expected: all CRUD, archive, human provenance, revision conflict, and cross-project isolation tests pass.

- [ ] **Step 6: Commit the REST API**

```powershell
git add requirements.txt web tests/test_web_api.py
git commit -m "feat: expose project graph api"
```

## Task 9: Add Project WebSockets And Runtime Control

**Files:**
- Modify: `web/app.py`
- Create: `tests/test_websocket.py`

- [ ] **Step 1: Write WebSocket sequence and log tests**

```python
def test_websocket_stream_is_project_scoped(self) -> None:
    with self.client.websocket_connect(f"/ws/projects/{self.project_a}?after=0") as socket:
        self.service.publish_test_event(self.project_b, "graph.node_added", {"id": "wrong"})
        self.service.publish_test_event(self.project_a, "intent.log_chunk", {"text": "right"})
        event = socket.receive_json()
        self.assertEqual(event["project_id"], self.project_a)
        self.assertEqual(event["payload"]["text"], "right")

def test_reconnect_replays_persisted_events_after_sequence(self) -> None:
    events = self.service.store(self.project_a).events_after(1)
    self.assertTrue(all(event["sequence"] > 1 for event in events))
```

- [ ] **Step 2: Run the tests and confirm WebSocket support is absent**

Run: `python -m unittest tests.test_websocket -v`

Expected: FAIL because `/ws/projects/{project_id}` is not registered.

- [ ] **Step 3: Implement snapshot-plus-stream semantics**

```python
@app.websocket("/ws/projects/{project_id}")
async def project_socket(websocket: WebSocket, project_id: str, after: int = 0):
    await websocket.accept()
    project = service.get(project_id)
    for event in project.store.events_after(after):
        await websocket.send_json({"project_id": project_id, **event})
    async with project.hub.subscribe() as queue:
        while True:
            event = await queue.get()
            await websocket.send_json({"project_id": project_id, **event})
```

Persist graph and status events before publishing them. Publish live log chunks with the current execution id and a sequence generated by the project event service. If a client reports a sequence gap, the client will refetch `/graph`; the server must never send another project's event.

Persist log event metadata as `{execution_id, stream, byte_offset, byte_length}` instead of duplicating full log text in SQLite. Live WebSocket payloads additionally include `text`. Replayed log metadata instructs the frontend to fetch the current saved log file before accepting later live chunks.

- [ ] **Step 4: Run WebSocket and API tests together**

Run: `python -m unittest tests.test_web_api tests.test_websocket -v`

Expected: REST and WebSocket tests pass, including start, stop, event replay, log chunks, and project isolation.

- [ ] **Step 5: Commit realtime delivery**

```powershell
git add web/app.py tests/test_websocket.py
git commit -m "feat: stream project events over websocket"
```

## Task 10: Migrate The Existing CLI To The New Runtime

**Files:**
- Replace: `orchestrator/scheduler.py`
- Modify: `main.py`
- Replace: `tests/test_scheduler.py`
- Replace: `tests/test_main.py`

- [ ] **Step 1: Write compatibility-wrapper tests**

```python
class SchedulerCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_cli_wrapper_uses_intent_edge_runtime(self) -> None:
        result = await run_orchestration(
            providers=self.fake_providers(),
            goal="Goal",
            origin="Origin",
            hints=(),
            challenge_dir=None,
            runs_root=self.root,
            max_iterations=3,
        )
        self.assertEqual(result.status, "achieved")
        with sqlite3.connect(Path(result.run_dir) / "blackboard.db") as connection:
            intent_nodes = connection.execute("SELECT COUNT(*) FROM nodes WHERE type='intent'").fetchone()[0]
            intent_edges = connection.execute("SELECT COUNT(*) FROM intents").fetchone()[0]
        self.assertEqual(intent_nodes, 0)
        self.assertGreater(intent_edges, 0)
```

- [ ] **Step 2: Run CLI tests and confirm old round arguments fail**

Run: `python -m unittest tests.test_scheduler tests.test_main -v`

Expected: FAIL because the old scheduler still creates rounds and Intent nodes.

- [ ] **Step 3: Add a bounded supervisor wrapper**

Implement `run_orchestration` as a single-project compatibility wrapper that creates a project database, starts a `ProjectSupervisor`, waits until achieved, stopped, all Agents idle, or `max_iterations` per Agent is reached, then returns `OrchestrationResult`. Rename CLI `--max-rounds` to `--max-iterations`, print highest generation instead of rounds, and keep Goal, Origin, Hint, challenge, and provider config inputs.

```python
async def run_orchestration(..., max_iterations: int = 5) -> OrchestrationResult:
    service = ProjectService(runs_root, registry_name="cli-projects.db")
    project = service.create_ephemeral(goal, origin, hints, providers, challenge_dir)
    supervisor = ProjectSupervisor(
        project=project,
        providers=tuple(provider for provider in providers if provider.enabled),
        max_iterations=max_iterations,
    )
    await supervisor.start()
    await supervisor.wait_until_terminal()
    snapshot = project.store.snapshot()
    return OrchestrationResult.from_snapshot(project.path, snapshot)
```

Update `OrchestrationResult` in `orchestrator/models.py` during this task:

```python
@dataclass(frozen=True, slots=True)
class OrchestrationResult:
    project_id: str
    run_dir: str
    status: str
    highest_generation: int
    final_answer: str | None
    candidates: tuple[str, ...]

    @classmethod
    def from_snapshot(cls, path: Path, snapshot: dict[str, object]) -> "OrchestrationResult":
        project = snapshot["project"]
        return cls(
            project_id=project["id"],
            run_dir=str(path.resolve()),
            status=project["status"],
            highest_generation=snapshot["highest_generation"],
            final_answer=project["final_answer"],
            candidates=tuple(snapshot["candidates"]),
        )
```

- [ ] **Step 4: Run CLI tests and the deterministic smoke fixture**

Run: `python -m unittest tests.test_scheduler tests.test_main -v`

Expected: all tests pass.

Run: `python main.py --config tests\fixtures\smoke_providers.json --goal "Recover flag" --origin "Smoke" --max-iterations 2`

Expected: status is achieved and the generated database contains Intent edges, not Intent nodes. Update `smoke_providers.json` or point it at `tests/fixtures/fake_agent.py` so it emits both protocol phases.

- [ ] **Step 5: Commit CLI compatibility**

```powershell
git add orchestrator/scheduler.py main.py tests/test_scheduler.py tests/test_main.py tests/fixtures/smoke_providers.json
git commit -m "refactor: run cli on asynchronous intent edges"
```

## Task 11: Scaffold The React Application And Typed Data Layer

**Files:**
- Create: `frontend/package.json`
- Create: `frontend/tsconfig.json`
- Create: `frontend/vite.config.ts`
- Create: `frontend/index.html`
- Create: `frontend/src/main.tsx`
- Create: `frontend/src/App.tsx`
- Create: `frontend/src/styles.css`
- Create: `frontend/src/types.ts`
- Create: `frontend/src/api/client.ts`
- Create: `frontend/src/api/projectSocket.ts`
- Create: `frontend/src/state/projectStore.ts`
- Create: `frontend/src/state/projectStore.test.ts`

- [ ] **Step 1: Create package metadata**

```json
{
  "name": "blackboard-web",
  "private": true,
  "version": "0.1.0",
  "type": "module",
  "scripts": {
    "dev": "vite",
    "build": "tsc -b && vite build",
    "test": "vitest",
    "test:e2e": "playwright test"
  },
  "dependencies": {
    "@xyflow/react": "^12.8.0",
    "lucide-react": "^0.468.0",
    "react": "^19.1.0",
    "react-dom": "^19.1.0"
  },
  "devDependencies": {
    "@playwright/test": "^1.53.0",
    "@testing-library/jest-dom": "^6.6.0",
    "@testing-library/react": "^16.3.0",
    "@types/react": "^19.1.0",
    "@types/react-dom": "^19.1.0",
    "@vitejs/plugin-react": "^4.5.0",
    "jsdom": "^26.1.0",
    "typescript": "^5.8.0",
    "vite": "^7.0.0",
    "vitest": "^3.2.0"
  }
}
```

Run: `cd frontend; npm install`

Expected: dependencies install and `package-lock.json` is created.

- [ ] **Step 2: Write the failing store sequence-gap test**

```typescript
it('requests a snapshot when websocket sequence has a gap', () => {
  const state = createProjectState({ sequence: 5 })
  const next = reduceProjectEvent(state, { sequence: 7, type: 'graph.node_added', payload: {} })
  expect(next.needsRefresh).toBe(true)
})
```

- [ ] **Step 3: Implement shared types, REST client, socket client, and reducer**

```typescript
export type BlackboardNodeType = 'goal' | 'origin' | 'fact' | 'hint'
export type IntentStatus = 'planned' | 'running' | 'completed' | 'failed' | 'cancelled'

export interface ProjectSnapshot {
  project: ProjectSummary
  nodes: BlackboardNode[]
  edges: BlackboardEdge[]
  layout: LayoutSnapshot
  sequence: number
  highestGeneration: number
}

export function reduceProjectEvent(state: ProjectState, event: ProjectEvent): ProjectState {
  if (event.sequence !== state.sequence + 1) return { ...state, needsRefresh: true }
  return applyEvent({ ...state, sequence: event.sequence }, event)
}
```

Configure Vite proxies for `/api` and `/ws` to `http://127.0.0.1:8000`. `App.tsx` should render the dashboard at `/` and workspace at `/projects/:id` using a minimal pathname router; do not add a routing dependency unless navigation complexity later requires it.

- [ ] **Step 4: Run frontend unit tests and build**

Run: `cd frontend; npm test -- --run src/state/projectStore.test.ts`

Expected: test passes.

Run: `cd frontend; npm run build`

Expected: TypeScript and Vite build succeed.

- [ ] **Step 5: Commit the frontend foundation**

```powershell
git add frontend
git commit -m "feat: scaffold typed blackboard frontend"
```

## Task 12: Build Project Dashboard And Creation Workflow

**Files:**
- Create: `frontend/src/pages/ProjectDashboard.tsx`
- Create: `frontend/src/pages/ProjectDashboard.test.tsx`
- Create: `frontend/src/components/ProjectCard.tsx`
- Create: `frontend/src/components/NewProjectDialog.tsx`
- Create: `frontend/src/components/NewProjectDialog.test.tsx`
- Modify: `frontend/src/styles.css`

- [ ] **Step 1: Write dashboard interaction tests**

```typescript
it('filters running projects and opens the selected project', async () => {
  render(<ProjectDashboard projects={projects} onOpen={onOpen} />)
  await userEvent.click(screen.getByRole('button', { name: '运行中' }))
  expect(screen.queryByText('Manual Board')).not.toBeInTheDocument()
  await userEvent.click(screen.getByText('Web Challenge 17'))
  expect(onOpen).toHaveBeenCalledWith('project-web')
})

it('creates a manual project without providers', async () => {
  render(<NewProjectDialog open onSubmit={onSubmit} onClose={() => {}} providers={providers} />)
  await userEvent.type(screen.getByLabelText('项目名称'), 'Manual Board')
  await userEvent.type(screen.getByLabelText('Goal'), 'Recover the flag')
  await userEvent.type(screen.getByLabelText('Origin'), 'Challenge statement')
  await userEvent.click(screen.getByLabelText('仅黑板'))
  await userEvent.click(screen.getByRole('button', { name: '创建项目' }))
  expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({ mode: 'manual', providers: [] }))
})
```

- [ ] **Step 2: Run dashboard tests and confirm components are missing**

Run: `cd frontend; npm test -- --run src/pages/ProjectDashboard.test.tsx src/components/NewProjectDialog.test.tsx`

Expected: FAIL importing dashboard components.

- [ ] **Step 3: Implement the operational dashboard**

Render a compact header, search input, status tabs, project grid, and icon-based create button using Lucide icons. Project cards show status, Goal summary, Fact count, Intent count, highest generation, provider states, and last activity. Keep card radius at 8px or less and avoid nested cards.

Implement the creation dialog with project name, Goal, Origin, repeatable Hint inputs, optional challenge path, manual/run segmented mode, provider checkboxes, and Agent concurrency stepper. Disable provider selection in manual mode but preserve prior selections if the user switches back to run mode.

```tsx
export function ProjectDashboard() {
  const { projects, refresh, createProject } = useProjectStore()
  const [filter, setFilter] = useState<ProjectFilter>('all')
  const [dialogOpen, setDialogOpen] = useState(false)
  const visible = filterProjects(projects, filter)

  return (
    <main className="dashboard-shell">
      <DashboardHeader onCreate={() => setDialogOpen(true)} />
      <ProjectFilters value={filter} onChange={setFilter} />
      <section className="project-grid" aria-label="项目列表">
        {visible.map(project => <ProjectCard key={project.id} project={project} />)}
      </section>
      <NewProjectDialog open={dialogOpen} onSubmit={createProject} onClose={() => setDialogOpen(false)} />
    </main>
  )
}
```

- [ ] **Step 4: Run dashboard tests and build**

Run: `cd frontend; npm test -- --run src/pages/ProjectDashboard.test.tsx src/components/NewProjectDialog.test.tsx`

Expected: all tests pass.

Run: `cd frontend; npm run build`

Expected: build succeeds with no text overflow warnings from tests.

- [ ] **Step 5: Commit the dashboard**

```powershell
git add frontend/src/pages frontend/src/components/ProjectCard.tsx frontend/src/components/NewProjectDialog* frontend/src/styles.css
git commit -m "feat: add project dashboard workflow"
```

## Task 13: Build The React Flow Blackboard Editor

**Files:**
- Create: `frontend/src/pages/BlackboardWorkspace.tsx`
- Create: `frontend/src/graph/BlackboardGraph.tsx`
- Create: `frontend/src/graph/BlackboardGraph.test.tsx`
- Create: `frontend/src/graph/BlackboardNode.tsx`
- Create: `frontend/src/graph/IntentEdge.tsx`
- Create: `frontend/src/components/GraphToolbar.tsx`
- Modify: `frontend/src/styles.css`

- [ ] **Step 1: Write graph selection, edit-mode, and reconnect tests**

```typescript
it('locks semantic editing in browse mode', () => {
  render(<BlackboardGraph snapshot={snapshot} mode="browse" />)
  expect(screen.getByRole('button', { name: '添加 Fact' })).toBeDisabled()
})

it('builds a reconnect command only in edit mode', () => {
  expect(buildReconnectCommand('browse', 'intent-1', 'fact-1', 'goal-2')).toBeNull()
  expect(buildReconnectCommand('edit', 'intent-1', 'fact-1', 'goal-2')).toEqual({
    edgeId: 'intent-1', source: 'fact-1', target: 'goal-2',
  })
})
```

- [ ] **Step 2: Run graph tests and confirm components are missing**

Run: `cd frontend; npm test -- --run src/graph/BlackboardGraph.test.tsx`

Expected: FAIL importing graph components.

- [ ] **Step 3: Implement stable graph mapping**

```typescript
const nodeTypes = { blackboard: BlackboardNode }
const edgeTypes = { intent: IntentEdge }

export function buildReconnectCommand(
  mode: WorkspaceMode,
  edgeId: string,
  source: string,
  target: string,
): ReconnectCommand | null {
  return mode === 'edit' ? { edgeId, source, target } : null
}

const flowNodes = snapshot.nodes.map(node => ({
  id: node.id,
  type: 'blackboard',
  position: layout.nodes[node.id] ?? autoPosition(node),
  data: { node, isLatest: node.generation === snapshot.highestGeneration },
}))

const flowEdges = snapshot.edges.map(edge => ({
  id: edge.id,
  type: edge.type === 'intent' ? 'intent' : 'default',
  source: edge.fromNode,
  target: edge.toNode,
  data: { edge },
}))
```

Use fixed node min/max dimensions, React Flow handles, MiniMap, controls, and background grid. Node drag and edge control-point changes update layout only. Endpoint reconnect calls the semantic edge API only in edit mode. Intent edges use status-aware stroke and a restrained running animation; ordinary relations use labeled static edges.

- [ ] **Step 4: Implement the workspace shell and toolbar**

The workspace top bar contains back, project status, generation filter, browse/edit segmented control, start/stop, and fit-view commands. The left toolbar uses Lucide icons with tooltips for select, add node, connect, and fit. The canvas takes remaining width and height without being wrapped in a decorative card.

```tsx
return (
  <main className="workspace-shell">
    <WorkspaceHeader project={project} mode={mode} onModeChange={setMode} />
    <GraphToolbar mode={mode} onFit={() => flow.fitView()} />
    <BlackboardGraph snapshot={snapshot} mode={mode} onSelect={setSelection} />
    <InspectorPanel selection={selection} snapshot={snapshot} />
    <LogConsole execution={selectedExecution} />
  </main>
)
```

- [ ] **Step 5: Run graph tests and build**

Run: `cd frontend; npm test -- --run src/graph/BlackboardGraph.test.tsx`

Expected: graph tests pass.

Run: `cd frontend; npm run build`

Expected: build succeeds.

- [ ] **Step 6: Commit the graph editor**

```powershell
git add frontend/src/pages/BlackboardWorkspace.tsx frontend/src/graph frontend/src/components/GraphToolbar.tsx frontend/src/styles.css
git commit -m "feat: add editable blackboard graph"
```

## Task 14: Add Inspector, Multi-Agent Execution Tabs, And Live Logs

**Files:**
- Create: `frontend/src/components/InspectorPanel.tsx`
- Create: `frontend/src/components/InspectorPanel.test.tsx`
- Create: `frontend/src/components/LogConsole.tsx`
- Create: `frontend/src/components/LogConsole.test.tsx`
- Modify: `frontend/src/pages/BlackboardWorkspace.tsx`
- Modify: `frontend/src/state/projectStore.ts`
- Modify: `frontend/src/styles.css`

- [ ] **Step 1: Write selection and live-log tests**

```typescript
it('selecting a latest fact opens its producing intent execution', () => {
  render(<InspectorPanel selection={latestFact} snapshot={snapshot} />)
  expect(screen.getByText('Intent #27')).toBeInTheDocument()
  expect(screen.getByRole('tab', { name: 'Codex' })).toHaveAttribute('aria-selected', 'true')
})

it('appends log chunks only to the matching execution', () => {
  const next = reduceProjectEvent(state, {
    sequence: 9,
    type: 'intent.log_chunk',
    payload: { executionId: 'exec-a', stream: 'stdout', text: 'working\n' },
  })
  expect(next.logs['exec-a'].stdout).toContain('working')
  expect(next.logs['exec-b']).toEqual(state.logs['exec-b'])
})
```

- [ ] **Step 2: Run tests and confirm inspector/log components are missing**

Run: `cd frontend; npm test -- --run src/components/InspectorPanel.test.tsx src/components/LogConsole.test.tsx`

Expected: FAIL importing components.

- [ ] **Step 3: Implement the inspector**

Node detail shows type, content, generation, state, provenance, ordinary relations, and producing Intent. Intent detail shows content, source, target, aggregate status, provenance, and one tab per `intent_execution`. Agent-created semantic content is read-only; human-created content exposes archive or versioned-edit commands according to the API rules.

```tsx
export function InspectorPanel({ selection, snapshot }: Props) {
  if (!selection) return <aside className="inspector empty" aria-label="详情" />
  if (selection.kind === 'node') {
    const producer = selection.node.producedBy
    return <NodeInspector node={selection.node} producer={producer} snapshot={snapshot} />
  }
  return <IntentInspector edge={selection.edge} executions={selection.edge.executions} />
}
```

- [ ] **Step 4: Implement the log console**

Use a stable-height bottom panel with stdout/stderr tabs, follow-output toggle, clear-view command that does not delete stored logs, status badge, exit code, duration, and work-directory link text. Append matching WebSocket chunks, cap in-memory display to a documented line count, and fetch saved logs when a historical execution is selected.

```tsx
export function LogConsole({ execution }: { execution: IntentExecution | null }) {
  const logs = useExecutionLogs(execution?.id)
  return (
    <section className="log-console" aria-label="Agent 实时日志">
      <LogHeader execution={execution} />
      <LogTabs value={logs.stream} onChange={logs.setStream} />
      <pre ref={logs.viewportRef}>{logs.visibleText}</pre>
    </section>
  )
}
```

- [ ] **Step 5: Run frontend tests and build**

Run: `cd frontend; npm test -- --run`

Expected: all frontend unit tests pass.

Run: `cd frontend; npm run build`

Expected: build succeeds.

- [ ] **Step 6: Commit inspector and logs**

```powershell
git add frontend/src/components/InspectorPanel* frontend/src/components/LogConsole* frontend/src/pages/BlackboardWorkspace.tsx frontend/src/state/projectStore.ts frontend/src/styles.css
git commit -m "feat: inspect agent work and live logs"
```

## Task 15: Serve The Built Frontend, Add End-To-End Tests, And Document Operation

**Files:**
- Create: `web_main.py`
- Modify: `web/app.py`
- Create: `frontend/playwright.config.ts`
- Create: `frontend/e2e/blackboard.spec.ts`
- Modify: `README.md`
- Modify: `.gitignore`

- [ ] **Step 1: Add production static serving and server entry point**

```python
from pathlib import Path

import uvicorn


if __name__ == "__main__":
    uvicorn.run(
        "web.app:create_app",
        factory=True,
        host="127.0.0.1",
        port=8000,
        reload=False,
    )
```

In `create_app`, mount `frontend/dist/assets` and return `frontend/dist/index.html` for non-API routes only when the build exists. During development, Vite continues to proxy API and WebSocket traffic.

- [ ] **Step 2: Write Playwright multi-project isolation tests**

```typescript
test('two running projects keep graph and logs isolated', async ({ browser }) => {
  const first = await browser.newPage()
  const second = await browser.newPage()
  await createRunningProject(first, 'Alpha', ['fake-a'])
  await createRunningProject(second, 'Beta', ['fake-b'])
  await expect(first.getByText('Alpha')).toBeVisible()
  await expect(first.getByText('fake-a output')).toBeVisible()
  await expect(first.getByText('fake-b output')).not.toBeVisible()
  await expect(second.getByText('fake-b output')).toBeVisible()
})

test('manual board can add fact and intent before agents start', async ({ page }) => {
  await createManualProject(page)
  await addFact(page, 'JWT uses HS256')
  await connectIntent(page, 'JWT uses HS256', 'Recover flag', 'Find the secret')
  await page.getByRole('button', { name: '启动 Agent' }).click()
  await expect(page.getByText('running')).toBeVisible()
})
```

- [ ] **Step 3: Configure Playwright web servers**

Configure Playwright to start the FastAPI test server on port 8000 and Vite on port 5173, using deterministic fake provider configuration and a temporary `web-data` root. Add desktop `1440x900` and narrow `390x844` projects.

- [ ] **Step 4: Run full backend and frontend verification**

Run: `python -m unittest discover -s tests -v`

Expected: all backend tests pass. Executor and supervisor subprocess tests may require elevation in the managed sandbox.

Run: `python -m compileall main.py web_main.py orchestrator worker web tests`

Expected: exit code 0.

Run: `cd frontend; npm test -- --run`

Expected: all Vitest tests pass.

Run: `cd frontend; npm run build`

Expected: production build succeeds.

Run: `cd frontend; npm run test:e2e`

Expected: desktop and narrow-screen Playwright projects pass.

- [ ] **Step 5: Perform visual and canvas verification**

Use Playwright screenshots for project overview and workspace at `1440x900` and `390x844`. Verify the React Flow canvas contains non-background pixels, nodes are inside the viewport, the inspector and log console do not overlap controls, and the longest project name and Fact content do not overflow their containers.

- [ ] **Step 6: Update README and ignore generated output**

Document:

```powershell
python -m pip install -r requirements.txt
cd frontend
npm install
npm run build
cd ..
python web_main.py
```

Document the localhost URL `http://127.0.0.1:8000`, manual versus run project modes, provider configuration, project isolation, and test commands. Add `web-data/`, `frontend/dist/`, `frontend/test-results/`, and `frontend/playwright-report/` to `.gitignore`; keep `frontend/package-lock.json` tracked.

- [ ] **Step 7: Commit the complete Web application**

```powershell
git add web_main.py web/app.py frontend/playwright.config.ts frontend/e2e README.md .gitignore
git commit -m "feat: deliver web blackboard workspace"
```

## Final Verification Gate

- [ ] Run `git status --short` and confirm no unintended generated files are tracked.
- [ ] Run `python -m unittest discover -s tests -v` and record the passing count.
- [ ] Run `python -m compileall main.py web_main.py orchestrator worker web tests`.
- [ ] Run `cd frontend; npm test -- --run`.
- [ ] Run `cd frontend; npm run build`.
- [ ] Run `cd frontend; npm run test:e2e`.
- [ ] Start `python web_main.py`, open `http://127.0.0.1:8000`, and verify project creation, graph editing, multiple Agent execution, project switching, and live logs with fake providers.
- [ ] Review the final diff for accidental changes to historical `worker/runs` or unrelated documentation.
