# Blackboard Orchestrator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the one-shot provider runner with a round-based, SQLite-backed graph blackboard that tracks Goal, Origin, Fact, Intent, and Hint nodes with provenance, conservative Fact consolidation, and consensus-based completion.

**Architecture:** Keep `worker` as a provider-neutral CLI configuration and subprocess layer. Add an `orchestrator` package that owns graph models, transactional SQLite storage, the structured stdout protocol, JSON snapshots, concurrent round scheduling, human Hints, and final-answer consensus.

**Tech Stack:** Python 3.10+, standard-library `argparse`, `asyncio`, `dataclasses`, `difflib`, `hashlib`, `json`, `pathlib`, `sqlite3`, `unicodedata`, `uuid`, and `unittest`.

---

## Repository Note

The current `E:\项目\钦原\.git` directory is not valid Git metadata. Every task includes the intended commit command, but commits can run only after the repository metadata is restored or initialized by the user.

## File Map

- Create `orchestrator/__init__.py`: public orchestration exports.
- Create `orchestrator/models.py`: node, edge, agent update, round result, and orchestration result dataclasses.
- Create `orchestrator/store.py`: SQLite schema, graph transactions, Fact consolidation, provenance, events, and snapshot export.
- Create `orchestrator/protocol.py`: graph prompt rendering and `BLACKBOARD_UPDATE` parsing.
- Create `orchestrator/scheduler.py`: multi-round concurrent execution, Hint collection, consensus, and final status.
- Modify `worker/models.py`: keep provider process models only.
- Modify `worker/__init__.py`: export only worker-layer models.
- Modify `worker/executor.py`: accept an already-rendered request without adding orchestration semantics.
- Delete `worker/runner.py`: replaced by `orchestrator/scheduler.py`.
- Modify `main.py`: explicit Goal, Origin, Hint, and maximum-round CLI.
- Create `tests/test_blackboard_store.py`: schema, provenance, snapshots, transactions, Fact consolidation, and Intent replacement.
- Create `tests/test_protocol.py`: request rendering and structured update validation.
- Modify `tests/test_executor.py`: raw request delivery tests.
- Create `tests/test_scheduler.py`: rounds, concurrency, Hints, consensus, conflicts, and failure isolation.
- Modify `tests/test_main.py`: new CLI contract and output behavior.
- Create `tests/fixtures/smoke_providers.json`: two deterministic subprocess providers for a real CLI smoke run.
- Delete `tests/test_runner.py`: obsolete one-shot runner tests.
- Modify `README.md`: blackboard concepts, CLI, persistence, protocol, consensus, and output layout.

### Task 1: Orchestrator Models and SQLite Schema

**Files:**
- Create: `orchestrator/__init__.py`
- Create: `orchestrator/models.py`
- Create: `orchestrator/store.py`
- Create: `tests/test_blackboard_store.py`

- [ ] **Step 1: Write failing schema and initialization tests**

Create `tests/test_blackboard_store.py` with the initial tests:

```python
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from orchestrator.store import BlackboardStore


class BlackboardStoreTests(unittest.TestCase):
    def test_create_initializes_schema_and_human_nodes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "blackboard.db"
            store = BlackboardStore.create(
                database,
                run_id="run-1",
                max_rounds=5,
                goal="Recover the flag",
                origin="The binary reads config.dat",
                hints=("Inspect config.dat first",),
            )
            self.addCleanup(store.close)

            snapshot = store.snapshot()

            self.assertEqual(snapshot["run"]["status"], "running")
            self.assertEqual(snapshot["run"]["max_rounds"], 5)
            self.assertEqual(
                [node["type"] for node in snapshot["nodes"]],
                ["goal", "origin", "hint"],
            )
            self.assertTrue(
                all(item["sources"] == ["human"] for item in snapshot["nodes"])
            )
            self.assertEqual(snapshot["edges"][0]["type"], "addresses")

    def test_schema_enforces_edge_foreign_keys(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "blackboard.db"
            store = BlackboardStore.create(
                database,
                run_id="run-1",
                max_rounds=5,
                goal="Goal",
                origin="Origin",
                hints=(),
            )
            self.addCleanup(store.close)

            with self.assertRaises(sqlite3.IntegrityError):
                store.connection.execute(
                    """
                    INSERT INTO edges(
                        id, run_id, type, from_node, to_node, source, round_number
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    ("edge-bad", "run-1", "supports", "missing-a", "missing-b", "codex", 1),
                )

    def test_export_snapshot_writes_stable_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = BlackboardStore.create(
                root / "blackboard.db",
                run_id="run-1",
                max_rounds=5,
                goal="Goal",
                origin="Origin",
                hints=(),
            )
            self.addCleanup(store.close)

            path = store.export_snapshot(0, root / "snapshots")
            payload = json.loads(path.read_text(encoding="utf-8"))

            self.assertEqual(path.name, "round-000.json")
            self.assertEqual(payload, store.snapshot())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the store tests and verify the expected import failure**

Run:

```powershell
python -m unittest tests.test_blackboard_store -v
```

Expected: `ERROR` with `ModuleNotFoundError: No module named 'orchestrator'`.

- [ ] **Step 3: Add immutable orchestration models**

Create `orchestrator/models.py`:

```python
from dataclasses import dataclass


NODE_TYPES = frozenset({"goal", "origin", "fact", "intent", "hint"})
AGENT_NODE_TYPES = frozenset({"fact", "intent"})
EDGE_TYPES = frozenset(
    {
        "supports",
        "contradicts",
        "derived_from",
        "addresses",
        "plans_from",
        "supersedes",
        "similar_to",
        "equivalent_to",
    }
)


@dataclass(frozen=True, slots=True)
class NodeProposal:
    ref: str
    type: str
    content: str


@dataclass(frozen=True, slots=True)
class EdgeProposal:
    type: str
    from_ref: str
    to_ref: str


@dataclass(frozen=True, slots=True)
class AgentUpdate:
    nodes: tuple[NodeProposal, ...]
    edges: tuple[EdgeProposal, ...]
    goal_achieved: bool
    final_answer: str | None


@dataclass(frozen=True, slots=True)
class ProtocolResult:
    status: str
    update: AgentUpdate | None
    error: str | None


@dataclass(frozen=True, slots=True)
class AgentRoundResult:
    provider: str
    process_status: str
    protocol_status: str
    exit_code: int | None
    duration_seconds: float
    output: str
    stdout_path: str
    stderr_path: str
    work_dir: str
    update: AgentUpdate | None
    protocol_error: str | None


@dataclass(frozen=True, slots=True)
class OrchestrationResult:
    run_id: str
    run_dir: str
    status: str
    rounds_completed: int
    final_answer: str | None
    candidates: tuple[str, ...]

    @property
    def exit_code(self) -> int:
        return 0 if self.status == "achieved" else 1
```

Create `orchestrator/__init__.py`:

```python
from orchestrator.models import OrchestrationResult

__all__ = ["OrchestrationResult"]
```

- [ ] **Step 4: Implement the SQLite schema and initial graph creation**

Create `orchestrator/store.py` with:

```python
import json
import sqlite3
from pathlib import Path
from uuid import uuid4


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE runs (
    run_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    max_rounds INTEGER NOT NULL CHECK(max_rounds > 0),
    final_answer TEXT
);

CREATE TABLE rounds (
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    round_number INTEGER NOT NULL CHECK(round_number >= 0),
    status TEXT NOT NULL,
    snapshot_path TEXT,
    PRIMARY KEY(run_id, round_number)
);

CREATE TABLE nodes (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    type TEXT NOT NULL CHECK(type IN ('goal', 'origin', 'fact', 'intent', 'hint')),
    content TEXT NOT NULL CHECK(length(trim(content)) > 0),
    fingerprint TEXT,
    state TEXT NOT NULL DEFAULT 'active',
    created_round INTEGER NOT NULL CHECK(created_round >= 0)
);

CREATE UNIQUE INDEX one_goal_per_run ON nodes(run_id) WHERE type = 'goal';
CREATE UNIQUE INDEX one_origin_per_run ON nodes(run_id) WHERE type = 'origin';
CREATE INDEX nodes_by_run_type ON nodes(run_id, type, state);
CREATE INDEX fact_fingerprint ON nodes(run_id, fingerprint) WHERE type = 'fact';

CREATE TABLE node_provenance (
    id TEXT PRIMARY KEY,
    node_id TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    round_number INTEGER NOT NULL CHECK(round_number >= 0),
    original_content TEXT NOT NULL,
    UNIQUE(node_id, source, round_number, original_content)
);

CREATE TABLE edges (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    type TEXT NOT NULL,
    from_node TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    to_node TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    round_number INTEGER NOT NULL CHECK(round_number >= 0),
    UNIQUE(run_id, type, from_node, to_node, source, round_number)
);

CREATE TABLE agent_results (
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    round_number INTEGER NOT NULL,
    provider TEXT NOT NULL,
    process_status TEXT NOT NULL,
    protocol_status TEXT NOT NULL,
    exit_code INTEGER,
    duration_seconds REAL NOT NULL,
    stdout_path TEXT NOT NULL,
    stderr_path TEXT NOT NULL,
    work_dir TEXT NOT NULL,
    final_answer TEXT,
    PRIMARY KEY(run_id, round_number, provider)
);

CREATE TABLE events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    round_number INTEGER NOT NULL,
    type TEXT NOT NULL,
    source TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
"""


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex}"


class BlackboardStore:
    def __init__(self, path: Path, connection: sqlite3.Connection, run_id: str):
        self.path = path
        self.connection = connection
        self.run_id = run_id

    @classmethod
    def create(
        cls,
        path: Path,
        run_id: str,
        max_rounds: int,
        goal: str,
        origin: str,
        hints: tuple[str, ...],
    ) -> "BlackboardStore":
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        connection.executescript(SCHEMA)
        store = cls(path, connection, run_id)
        with connection:
            connection.execute(
                "INSERT INTO runs(run_id, status, max_rounds) VALUES (?, 'running', ?)",
                (run_id, max_rounds),
            )
            connection.execute(
                "INSERT INTO rounds(run_id, round_number, status) VALUES (?, 0, 'initialized')",
                (run_id,),
            )
            goal_id = store._insert_node("goal", goal, "human", 0)
            store._insert_node("origin", origin, "human", 0)
            for hint in hints:
                hint_id = store._insert_node("hint", hint, "human", 0)
                store._insert_edge("addresses", hint_id, goal_id, "human", 0)
        return store

    def close(self) -> None:
        self.connection.close()

    def _insert_node(
        self,
        node_type: str,
        content: str,
        source: str,
        round_number: int,
        fingerprint: str | None = None,
    ) -> str:
        node_id = _id(node_type)
        self.connection.execute(
            """
            INSERT INTO nodes(id, run_id, type, content, fingerprint, created_round)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (node_id, self.run_id, node_type, content.strip(), fingerprint, round_number),
        )
        self.connection.execute(
            """
            INSERT INTO node_provenance(
                id, node_id, source, round_number, original_content
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (_id("provenance"), node_id, source, round_number, content),
        )
        self._event(round_number, "node_created", source, {"node_id": node_id, "type": node_type})
        return node_id

    def _insert_edge(
        self,
        edge_type: str,
        from_node: str,
        to_node: str,
        source: str,
        round_number: int,
    ) -> str:
        edge_id = _id("edge")
        self.connection.execute(
            """
            INSERT INTO edges(id, run_id, type, from_node, to_node, source, round_number)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (edge_id, self.run_id, edge_type, from_node, to_node, source, round_number),
        )
        self._event(round_number, "edge_created", source, {"edge_id": edge_id, "type": edge_type})
        return edge_id

    def _event(
        self,
        round_number: int,
        event_type: str,
        source: str,
        payload: dict[str, object],
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO events(run_id, round_number, type, source, payload_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (self.run_id, round_number, event_type, source, json.dumps(payload, ensure_ascii=False)),
        )

    def snapshot(self) -> dict[str, object]:
        run = dict(
            self.connection.execute(
                "SELECT run_id, status, max_rounds, final_answer FROM runs WHERE run_id = ?",
                (self.run_id,),
            ).fetchone()
        )
        node_rows = self.connection.execute(
            """
            SELECT id, type, content, state, created_round
            FROM nodes WHERE run_id = ?
            ORDER BY created_round, CASE type
                WHEN 'goal' THEN 0 WHEN 'origin' THEN 1 WHEN 'hint' THEN 2
                WHEN 'fact' THEN 3 ELSE 4 END, id
            """,
            (self.run_id,),
        ).fetchall()
        nodes = []
        for row in node_rows:
            sources = [
                item[0]
                for item in self.connection.execute(
                    "SELECT DISTINCT source FROM node_provenance WHERE node_id = ? ORDER BY source",
                    (row["id"],),
                ).fetchall()
            ]
            nodes.append({**dict(row), "sources": sources})
        edges = [
            dict(row)
            for row in self.connection.execute(
                """
                SELECT id, type, from_node, to_node, source, round_number
                FROM edges WHERE run_id = ? ORDER BY round_number, id
                """,
                (self.run_id,),
            ).fetchall()
        ]
        return {"run": run, "nodes": nodes, "edges": edges}

    def export_snapshot(self, round_number: int, directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"round-{round_number:03d}.json"
        path.write_text(
            json.dumps(self.snapshot(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO rounds(run_id, round_number, status, snapshot_path)
                VALUES (?, ?, 'snapshot_ready', ?)
                ON CONFLICT(run_id, round_number) DO UPDATE SET
                    status = excluded.status,
                    snapshot_path = excluded.snapshot_path
                """,
                (self.run_id, round_number, str(path.resolve())),
            )
        return path
```

- [ ] **Step 5: Run the store tests and verify they pass**

Run:

```powershell
python -m unittest tests.test_blackboard_store -v
```

Expected: three tests report `ok` and the command ends with `OK`.

- [ ] **Step 6: Commit the schema layer when Git metadata is valid**

```powershell
git add orchestrator/__init__.py orchestrator/models.py orchestrator/store.py tests/test_blackboard_store.py
git commit -m "feat: add blackboard graph storage"
```

### Task 2: Transactional Graph Updates, Fact Consolidation, and Intent History

**Files:**
- Modify: `orchestrator/store.py`
- Modify: `tests/test_blackboard_store.py`

- [ ] **Step 1: Add failing graph merge tests**

Append these imports and tests to `tests/test_blackboard_store.py`:

```python
from orchestrator.models import AgentUpdate, EdgeProposal, NodeProposal


class BlackboardMergeTests(unittest.TestCase):
    def make_store(self, root: Path) -> BlackboardStore:
        return BlackboardStore.create(
            root / "blackboard.db",
            run_id="run-1",
            max_rounds=5,
            goal="Recover the flag",
            origin="Challenge origin",
            hints=(),
        )

    def test_identical_facts_share_one_node_and_keep_both_sources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(Path(directory))
            self.addCleanup(store.close)
            update = AgentUpdate(
                nodes=(NodeProposal("f1", "fact", "Binary  uses  AES-CBC"),),
                edges=(),
                goal_achieved=False,
                final_answer=None,
            )
            same = AgentUpdate(
                nodes=(NodeProposal("f2", "fact", "Binary uses AES-CBC"),),
                edges=(),
                goal_achieved=False,
                final_answer=None,
            )

            store.apply_round(1, (("codex", update), ("claude", same)))
            facts = [node for node in store.snapshot()["nodes"] if node["type"] == "fact"]

            self.assertEqual(len(facts), 1)
            self.assertEqual(facts[0]["sources"], ["claude", "codex"])

    def test_similar_facts_remain_distinct(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(Path(directory))
            self.addCleanup(store.close)
            store.apply_round(
                1,
                (
                    (
                        "codex",
                        AgentUpdate(
                            nodes=(NodeProposal("cbc", "fact", "The cipher mode is AES-CBC"),),
                            edges=(),
                            goal_achieved=False,
                            final_answer=None,
                        ),
                    ),
                    (
                        "claude",
                        AgentUpdate(
                            nodes=(NodeProposal("ecb", "fact", "The cipher mode is AES-ECB"),),
                            edges=(),
                            goal_achieved=False,
                            final_answer=None,
                        ),
                    ),
                ),
            )

            facts = [node for node in store.snapshot()["nodes"] if node["type"] == "fact"]
            self.assertEqual(len(facts), 2)

    def test_new_intent_supersedes_same_agents_old_intent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(Path(directory))
            self.addCleanup(store.close)
            first = AgentUpdate(
                nodes=(NodeProposal("i1", "intent", "Inspect imports"),),
                edges=(),
                goal_achieved=False,
                final_answer=None,
            )
            second = AgentUpdate(
                nodes=(NodeProposal("i2", "intent", "Trace decryption"),),
                edges=(),
                goal_achieved=False,
                final_answer=None,
            )

            store.apply_round(1, (("codex", first),))
            store.apply_round(2, (("codex", second),))
            snapshot = store.snapshot()
            intents = [node for node in snapshot["nodes"] if node["type"] == "intent"]

            self.assertEqual([node["state"] for node in intents], ["superseded", "active"])
            self.assertIn("supersedes", [edge["type"] for edge in snapshot["edges"]])

    def test_invalid_edge_rolls_back_whole_round(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(Path(directory))
            self.addCleanup(store.close)
            update = AgentUpdate(
                nodes=(NodeProposal("f1", "fact", "A useful fact"),),
                edges=(EdgeProposal("supports", "f1", "missing-node"),),
                goal_achieved=False,
                final_answer=None,
            )

            with self.assertRaisesRegex(ValueError, "unknown node reference"):
                store.apply_round(1, (("codex", update),))

            self.assertEqual(
                [node for node in store.snapshot()["nodes"] if node["type"] == "fact"],
                [],
            )
```

- [ ] **Step 2: Run the merge tests and verify the missing method failure**

Run:

```powershell
python -m unittest tests.test_blackboard_store.BlackboardMergeTests -v
```

Expected: tests fail with `AttributeError: 'BlackboardStore' object has no attribute 'apply_round'`.

- [ ] **Step 3: Implement normalization, consolidation, edge resolution, and Intent replacement**

Add these imports and methods to `orchestrator/store.py`:

```python
import hashlib
import unicodedata
from difflib import SequenceMatcher

from orchestrator.models import AgentUpdate, EDGE_TYPES


def _normalize_fact(content: str) -> str:
    normalized = unicodedata.normalize("NFKC", content)
    return " ".join(normalized.strip().split())


def _fact_fingerprint(content: str) -> str:
    return hashlib.sha256(_normalize_fact(content).encode("utf-8")).hexdigest()
```

Add the following methods inside `BlackboardStore`:

```python
    def _add_provenance(
        self,
        node_id: str,
        source: str,
        round_number: int,
        original_content: str,
    ) -> None:
        self.connection.execute(
            """
            INSERT OR IGNORE INTO node_provenance(
                id, node_id, source, round_number, original_content
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (_id("provenance"), node_id, source, round_number, original_content),
        )
        self._event(round_number, "provenance_added", source, {"node_id": node_id})

    def _current_intent(self, source: str) -> str | None:
        row = self.connection.execute(
            """
            SELECT n.id
            FROM nodes AS n
            JOIN node_provenance AS p ON p.node_id = n.id
            WHERE n.run_id = ? AND n.type = 'intent' AND n.state = 'active'
              AND p.source = ?
            ORDER BY n.created_round DESC LIMIT 1
            """,
            (self.run_id, source),
        ).fetchone()
        return row[0] if row else None

    def _similar_facts(self, content: str, exclude_id: str) -> tuple[str, ...]:
        normalized = _normalize_fact(content)
        rows = self.connection.execute(
            "SELECT id, content FROM nodes WHERE run_id = ? AND type = 'fact' AND id != ?",
            (self.run_id, exclude_id),
        ).fetchall()
        return tuple(
            row["id"]
            for row in rows
            if SequenceMatcher(None, normalized, _normalize_fact(row["content"])).ratio() >= 0.92
        )

    def _merge_node(
        self,
        proposal_type: str,
        content: str,
        source: str,
        round_number: int,
    ) -> str:
        if proposal_type == "fact":
            fingerprint = _fact_fingerprint(content)
            row = self.connection.execute(
                """
                SELECT id FROM nodes
                WHERE run_id = ? AND type = 'fact' AND fingerprint = ?
                """,
                (self.run_id, fingerprint),
            ).fetchone()
            if row:
                self._add_provenance(row["id"], source, round_number, content)
                return row["id"]
            node_id = self._insert_node(
                "fact", _normalize_fact(content), source, round_number, fingerprint
            )
            for similar_id in self._similar_facts(content, node_id):
                self._insert_edge("similar_to", node_id, similar_id, "orchestrator", round_number)
            return node_id

        previous = self._current_intent(source)
        node_id = self._insert_node("intent", content, source, round_number)
        if previous:
            self.connection.execute(
                "UPDATE nodes SET state = 'superseded' WHERE id = ?",
                (previous,),
            )
            self._insert_edge("supersedes", node_id, previous, "orchestrator", round_number)
            self._event(round_number, "intent_superseded", source, {"old": previous, "new": node_id})
        return node_id

    def _existing_node_ids(self) -> set[str]:
        return {
            row[0]
            for row in self.connection.execute(
                "SELECT id FROM nodes WHERE run_id = ?",
                (self.run_id,),
            ).fetchall()
        }

    def apply_round(
        self,
        round_number: int,
        updates: tuple[tuple[str, AgentUpdate], ...],
    ) -> None:
        with self.connection:
            known = self._existing_node_ids()
            for source, update in sorted(updates, key=lambda item: item[0]):
                refs: dict[str, str] = {}
                for proposal in update.nodes:
                    if proposal.type not in {"fact", "intent"}:
                        raise ValueError(f"unsupported agent node type: {proposal.type}")
                    refs[proposal.ref] = self._merge_node(
                        proposal.type,
                        proposal.content,
                        source,
                        round_number,
                    )
                for edge in update.edges:
                    if edge.type not in EDGE_TYPES or edge.type == "supersedes":
                        raise ValueError(f"unsupported edge type: {edge.type}")
                    from_node = refs.get(edge.from_ref, edge.from_ref)
                    to_node = refs.get(edge.to_ref, edge.to_ref)
                    if from_node not in known | set(refs.values()) or to_node not in known | set(refs.values()):
                        raise ValueError("unknown node reference")
                    self._insert_edge(
                        edge.type,
                        from_node,
                        to_node,
                        source,
                        round_number,
                    )
                known.update(refs.values())
            self.connection.execute(
                """
                INSERT INTO rounds(run_id, round_number, status)
                VALUES (?, ?, 'merged')
                ON CONFLICT(run_id, round_number) DO UPDATE SET status = 'merged'
                """,
                (self.run_id, round_number),
            )
```

- [ ] **Step 4: Run all store tests and verify they pass**

Run:

```powershell
python -m unittest tests.test_blackboard_store -v
```

Expected: seven tests report `ok` and the command ends with `OK`.

- [ ] **Step 5: Commit graph merging when Git metadata is valid**

```powershell
git add orchestrator/store.py tests/test_blackboard_store.py
git commit -m "feat: merge sourced blackboard graph updates"
```

### Task 3: Structured Agent Protocol

**Files:**
- Create: `orchestrator/protocol.py`
- Create: `tests/test_protocol.py`

- [ ] **Step 1: Write failing prompt and parser tests**

Create `tests/test_protocol.py`:

```python
import json
import unittest

from orchestrator.protocol import parse_update, render_request


class ProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.snapshot = {
            "run": {"run_id": "run-1", "status": "running", "max_rounds": 5},
            "nodes": [
                {
                    "id": "goal-1",
                    "type": "goal",
                    "content": "Recover the flag",
                    "state": "active",
                    "created_round": 0,
                    "sources": ["human"],
                }
            ],
            "edges": [],
        }

    def test_render_request_contains_snapshot_and_protocol_contract(self) -> None:
        request = render_request(self.snapshot, round_number=1, challenge_dir="E:/ctf/task")

        self.assertIn('"id": "goal-1"', request)
        self.assertIn("BLACKBOARD_UPDATE:", request)
        self.assertIn("Only propose fact and intent nodes", request)
        self.assertIn("E:/ctf/task", request)

    def test_parse_update_uses_last_prefixed_record(self) -> None:
        stdout = "\n".join(
            [
                "analysis",
                'BLACKBOARD_UPDATE: {"nodes": [], "edges": [], "goal_achieved": false, "final_answer": null}',
                'BLACKBOARD_UPDATE: {"nodes": [{"ref":"f1","type":"fact","content":"flag is in config"}], "edges": [], "goal_achieved": true, "final_answer": "flag{ok}"}',
            ]
        )

        result = parse_update(stdout, existing_node_ids={"goal-1"})

        self.assertEqual(result.status, "valid")
        self.assertEqual(result.update.nodes[0].type, "fact")
        self.assertEqual(result.update.final_answer, "flag{ok}")

    def test_rejects_human_owned_node_type(self) -> None:
        stdout = (
            'BLACKBOARD_UPDATE: {"nodes": '
            '[{"ref":"h1","type":"hint","content":"fake hint"}], '
            '"edges": [], "goal_achieved": false, "final_answer": null}'
        )

        result = parse_update(stdout, existing_node_ids={"goal-1"})

        self.assertEqual(result.status, "protocol_error")
        self.assertIn("node type", result.error)

    def test_rejects_unknown_edge_reference(self) -> None:
        payload = {
            "nodes": [],
            "edges": [{"type": "supports", "from": "missing", "to": "goal-1"}],
            "goal_achieved": False,
            "final_answer": None,
        }

        result = parse_update(
            "BLACKBOARD_UPDATE: " + json.dumps(payload),
            existing_node_ids={"goal-1"},
        )

        self.assertEqual(result.status, "protocol_error")
        self.assertIn("unknown node reference", result.error)

    def test_rejects_achieved_without_answer(self) -> None:
        stdout = (
            'BLACKBOARD_UPDATE: {"nodes": [], "edges": [], '
            '"goal_achieved": true, "final_answer": null}'
        )

        result = parse_update(stdout, existing_node_ids={"goal-1"})

        self.assertEqual(result.status, "protocol_error")
        self.assertIn("final_answer", result.error)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run protocol tests and verify the import failure**

Run:

```powershell
python -m unittest tests.test_protocol -v
```

Expected: `ERROR` with `ModuleNotFoundError: No module named 'orchestrator.protocol'`.

- [ ] **Step 3: Implement request rendering and strict update parsing**

Create `orchestrator/protocol.py`:

```python
import json
from pathlib import Path

from orchestrator.models import (
    AGENT_NODE_TYPES,
    EDGE_TYPES,
    AgentUpdate,
    EdgeProposal,
    NodeProposal,
    ProtocolResult,
)


PREFIX = "BLACKBOARD_UPDATE:"


def render_request(
    snapshot: dict[str, object],
    round_number: int,
    challenge_dir: str | Path | None,
) -> str:
    challenge = str(challenge_dir) if challenge_dir else "not provided"
    return (
        f"You are participating in blackboard round {round_number}.\n"
        f"CTF challenge directory (read-only): {challenge}\n"
        "Read the graph snapshot below. Goal, origin, and hint nodes are read-only.\n"
        "Only propose fact and intent nodes. Use existing node IDs or temporary refs "
        "from your own nodes when proposing edges.\n"
        "Write any analysis normally, then finish with exactly one line beginning "
        f"{PREFIX} followed by one JSON object with keys nodes, edges, "
        "goal_achieved, and final_answer.\n\n"
        "Graph snapshot:\n"
        + json.dumps(snapshot, ensure_ascii=False, indent=2)
        + "\n"
    )


def _error(message: str) -> ProtocolResult:
    return ProtocolResult(status="protocol_error", update=None, error=message)


def parse_update(stdout: str, existing_node_ids: set[str]) -> ProtocolResult:
    candidates = [line[len(PREFIX):].strip() for line in stdout.splitlines() if line.startswith(PREFIX)]
    if not candidates:
        return _error("missing BLACKBOARD_UPDATE")

    for candidate in reversed(candidates):
        try:
            payload = json.loads(candidate)
            if not isinstance(payload, dict):
                raise ValueError("update must be an object")
            raw_nodes = payload.get("nodes")
            raw_edges = payload.get("edges")
            achieved = payload.get("goal_achieved")
            answer = payload.get("final_answer")
            if not isinstance(raw_nodes, list) or not isinstance(raw_edges, list):
                raise ValueError("nodes and edges must be lists")
            if not isinstance(achieved, bool):
                raise ValueError("goal_achieved must be a boolean")
            if answer is not None and (not isinstance(answer, str) or not answer.strip()):
                raise ValueError("final_answer must be null or a non-empty string")
            if achieved and answer is None:
                raise ValueError("goal_achieved requires final_answer")

            nodes = []
            refs: set[str] = set()
            for raw in raw_nodes:
                if not isinstance(raw, dict):
                    raise ValueError("node proposal must be an object")
                ref = raw.get("ref")
                node_type = raw.get("type")
                content = raw.get("content")
                if not isinstance(ref, str) or not ref or ref in refs:
                    raise ValueError("node ref must be unique and non-empty")
                if node_type not in AGENT_NODE_TYPES:
                    raise ValueError("unsupported agent node type")
                if not isinstance(content, str) or not content.strip():
                    raise ValueError("node content must be non-empty")
                refs.add(ref)
                nodes.append(NodeProposal(ref, node_type, content.strip()))

            allowed_refs = existing_node_ids | refs
            edges = []
            for raw in raw_edges:
                if not isinstance(raw, dict):
                    raise ValueError("edge proposal must be an object")
                edge_type = raw.get("type")
                from_ref = raw.get("from")
                to_ref = raw.get("to")
                if edge_type not in EDGE_TYPES or edge_type == "supersedes":
                    raise ValueError("unsupported edge type")
                if from_ref not in allowed_refs or to_ref not in allowed_refs:
                    raise ValueError("unknown node reference")
                edges.append(EdgeProposal(edge_type, from_ref, to_ref))

            return ProtocolResult(
                status="valid",
                update=AgentUpdate(tuple(nodes), tuple(edges), achieved, answer.strip() if answer else None),
                error=None,
            )
        except (json.JSONDecodeError, ValueError) as exc:
            last_error = str(exc)

    return _error(last_error)
```

- [ ] **Step 4: Run protocol tests and verify they pass**

Run:

```powershell
python -m unittest tests.test_protocol -v
```

Expected: five tests report `ok` and the command ends with `OK`.

- [ ] **Step 5: Commit the protocol when Git metadata is valid**

```powershell
git add orchestrator/protocol.py tests/test_protocol.py
git commit -m "feat: add blackboard agent protocol"
```

### Task 4: Make the Worker Executor Request-Neutral

**Files:**
- Modify: `worker/models.py`
- Modify: `worker/__init__.py`
- Modify: `worker/executor.py`
- Modify: `tests/test_executor.py`

- [ ] **Step 1: Change the executor test to require exact raw request delivery**

In `tests/test_executor.py`, replace `test_passes_prompt_through_stdin_and_uses_provider_cwd` with:

```python
    async def test_passes_rendered_request_unchanged_and_uses_provider_cwd(self) -> None:
        code = (
            "import os,sys;"
            "data=sys.stdin.read();"
            "print(os.getcwd());"
            "print(data)"
        )
        provider = ProviderConfig(
            name="fake",
            enabled=True,
            priority=10,
            command=(sys.executable, "-c", code),
            timeout_seconds=5,
        )
        request = "GRAPH SNAPSHOT\nBLACKBOARD_UPDATE contract | keep $text"

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            work_dir = root / "fake"
            challenge_dir = root / "challenge"
            challenge_dir.mkdir()

            result = await execute_provider(
                provider, request, challenge_dir, work_dir
            )

            self.assertEqual(result.status, "success")
            self.assertIn(str(work_dir.resolve()), result.output)
            self.assertIn(request, result.output)
            self.assertNotIn("Task:\n", result.output)
```

- [ ] **Step 2: Run the executor test and verify it fails on the old prompt wrapper**

Run:

```powershell
python -m unittest tests.test_executor.ExecutorTests.test_passes_rendered_request_unchanged_and_uses_provider_cwd -v
```

Expected: `FAIL` because output still contains the executor-generated `Task:` wrapper.

- [ ] **Step 3: Remove orchestration prompt construction from the executor**

Delete `_build_prompt` from `worker/executor.py`. Rename the `prompt` parameter to `request` and replace payload creation with:

```python
async def execute_provider(
    provider: ProviderConfig,
    request: str,
    challenge_dir: Path | None,
    work_dir: Path,
) -> WorkerResult:
    work_dir.mkdir(parents=True, exist_ok=True)
    command = _resolve_executable(
        _expand_command(provider, challenge_dir, work_dir)
    )
    payload = request.encode("utf-8")
```

Keep the remaining process, timeout, tree termination, logging, and result code unchanged.

Modify `worker/models.py` so it contains only:

```python
from dataclasses import asdict, dataclass


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    name: str
    enabled: bool
    priority: int
    command: tuple[str, ...]
    timeout_seconds: float


@dataclass(frozen=True, slots=True)
class WorkerResult:
    provider: str
    priority: int
    status: str
    exit_code: int | None
    duration_seconds: float
    output: str
    stdout_path: str
    stderr_path: str
    work_dir: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)
```

Modify `worker/__init__.py`:

```python
from worker.models import ProviderConfig, WorkerResult

__all__ = ["ProviderConfig", "WorkerResult"]
```

- [ ] **Step 4: Run executor and configuration tests**

Run:

```powershell
python -m unittest tests.test_executor tests.test_config -v
```

Expected: all tests report `ok` and the command ends with `OK`.

- [ ] **Step 5: Commit executor separation when Git metadata is valid**

```powershell
git add worker/__init__.py worker/models.py worker/executor.py tests/test_executor.py
git commit -m "refactor: keep worker executor protocol neutral"
```

### Task 5: Agent Result Persistence and Final-Answer Consensus

**Files:**
- Modify: `orchestrator/store.py`
- Modify: `tests/test_blackboard_store.py`

- [ ] **Step 1: Add failing result and consensus tests**

Append to `BlackboardStoreTests` in `tests/test_blackboard_store.py`:

```python
    def test_consensus_requires_two_agents_when_multiple_are_available(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = BlackboardStore.create(
                Path(directory) / "blackboard.db",
                run_id="run-1",
                max_rounds=5,
                goal="Goal",
                origin="Origin",
                hints=(),
            )
            self.addCleanup(store.close)
            store.record_agent_result(1, "codex", "success", "valid", 0, 1.0, "a", "b", "c", "flag{same}")

            self.assertIsNone(store.consensus(1, available_agents=3))

            store.record_agent_result(1, "claude", "success", "valid", 0, 1.0, "d", "e", "f", " flag{same} ")
            self.assertEqual(store.consensus(1, available_agents=3), "flag{same}")

    def test_conflicting_answers_are_returned_without_priority_tiebreak(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = BlackboardStore.create(
                Path(directory) / "blackboard.db",
                run_id="run-1",
                max_rounds=5,
                goal="Goal",
                origin="Origin",
                hints=(),
            )
            self.addCleanup(store.close)
            store.record_agent_result(1, "codex", "success", "valid", 0, 1.0, "a", "b", "c", "flag{a}")
            store.record_agent_result(1, "claude", "success", "valid", 0, 1.0, "d", "e", "f", "flag{b}")

            self.assertIsNone(store.consensus(1, available_agents=2))
            self.assertEqual(store.candidates(), ("flag{a}", "flag{b}"))
```

- [ ] **Step 2: Run the new tests and verify missing-method failures**

Run:

```powershell
python -m unittest tests.test_blackboard_store.BlackboardStoreTests.test_consensus_requires_two_agents_when_multiple_are_available tests.test_blackboard_store.BlackboardStoreTests.test_conflicting_answers_are_returned_without_priority_tiebreak -v
```

Expected: `ERROR` because `record_agent_result` is not defined.

- [ ] **Step 3: Implement result persistence, consensus, Hints, and run completion**

Add these methods to `BlackboardStore`:

```python
    def record_agent_result(
        self,
        round_number: int,
        provider: str,
        process_status: str,
        protocol_status: str,
        exit_code: int | None,
        duration_seconds: float,
        stdout_path: str,
        stderr_path: str,
        work_dir: str,
        final_answer: str | None,
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO agent_results(
                    run_id, round_number, provider, process_status,
                    protocol_status, exit_code, duration_seconds, stdout_path,
                    stderr_path, work_dir, final_answer
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self.run_id,
                    round_number,
                    provider,
                    process_status,
                    protocol_status,
                    exit_code,
                    duration_seconds,
                    stdout_path,
                    stderr_path,
                    work_dir,
                    final_answer.strip() if final_answer else None,
                ),
            )

    def consensus(self, round_number: int, available_agents: int) -> str | None:
        rows = self.connection.execute(
            """
            SELECT trim(final_answer) AS answer, COUNT(DISTINCT provider) AS supporters
            FROM agent_results
            WHERE run_id = ? AND round_number = ? AND protocol_status = 'valid'
              AND final_answer IS NOT NULL
            GROUP BY trim(final_answer)
            ORDER BY supporters DESC, answer
            """,
            (self.run_id, round_number),
        ).fetchall()
        threshold = 1 if available_agents == 1 else 2
        winners = [row["answer"] for row in rows if row["supporters"] >= threshold]
        return winners[0] if len(winners) == 1 else None

    def candidates(self) -> tuple[str, ...]:
        return tuple(
            row[0]
            for row in self.connection.execute(
                """
                SELECT DISTINCT trim(final_answer)
                FROM agent_results
                WHERE run_id = ? AND final_answer IS NOT NULL
                ORDER BY trim(final_answer)
                """,
                (self.run_id,),
            ).fetchall()
        )

    def add_hint(self, content: str, round_number: int) -> str:
        goal_id = self.connection.execute(
            "SELECT id FROM nodes WHERE run_id = ? AND type = 'goal'",
            (self.run_id,),
        ).fetchone()[0]
        with self.connection:
            hint_id = self._insert_node("hint", content, "human", round_number)
            self._insert_edge("addresses", hint_id, goal_id, "human", round_number)
        return hint_id

    def finish(
        self,
        status: str,
        final_answer: str | None,
        round_number: int,
    ) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE runs SET status = ?, final_answer = ? WHERE run_id = ?",
                (status, final_answer, self.run_id),
            )
            self._event(
                round_number,
                "run_finished",
                "orchestrator",
                {"status": status, "final_answer": final_answer},
            )
```

- [ ] **Step 4: Run all store tests**

Run:

```powershell
python -m unittest tests.test_blackboard_store -v
```

Expected: all store tests report `ok` and the command ends with `OK`.

- [ ] **Step 5: Commit consensus storage when Git metadata is valid**

```powershell
git add orchestrator/store.py tests/test_blackboard_store.py
git commit -m "feat: persist agent results and answer consensus"
```

### Task 6: Multi-Round Concurrent Scheduler

**Files:**
- Replace: `orchestrator/scheduler.py`
- Create: `tests/test_scheduler.py`

- [ ] **Step 1: Write failing scheduler tests with fake executors**

Create `tests/test_scheduler.py`:

```python
import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path

from orchestrator.scheduler import run_orchestration
from worker.models import ProviderConfig, WorkerResult


def provider(name: str) -> ProviderConfig:
    return ProviderConfig(name, True, 10, ("unused",), 5)


def worker_result(name: str, work_dir: Path, output: str) -> WorkerResult:
    work_dir.mkdir(parents=True, exist_ok=True)
    stdout = work_dir / "stdout.txt"
    stderr = work_dir / "stderr.txt"
    stdout.write_text(output, encoding="utf-8")
    stderr.write_text("", encoding="utf-8")
    return WorkerResult(
        provider=name,
        priority=10,
        status="success",
        exit_code=0,
        duration_seconds=0.1,
        output=output,
        stdout_path=str(stdout),
        stderr_path=str(stderr),
        work_dir=str(work_dir),
    )


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def test_runs_agents_concurrently_with_identical_snapshot(self) -> None:
        requests: list[str] = []

        async def execute(config, request, challenge_dir, work_dir):
            requests.append(request)
            await asyncio.sleep(0.2)
            output = 'BLACKBOARD_UPDATE: {"nodes": [], "edges": [], "goal_achieved": false, "final_answer": null}'
            return worker_result(config.name, work_dir, output)

        with tempfile.TemporaryDirectory() as directory:
            started = time.monotonic()
            result = await run_orchestration(
                providers=(provider("codex"), provider("claude")),
                goal="Goal",
                origin="Origin",
                hints=(),
                challenge_dir=None,
                runs_root=Path(directory),
                max_rounds=1,
                execute=execute,
                hint_reader=lambda _: "",
                interactive=False,
            )
            elapsed = time.monotonic() - started

            self.assertLess(elapsed, 0.35)
            self.assertEqual(requests[0], requests[1])
            self.assertEqual(result.status, "max_rounds_reached")

    async def test_reaches_consensus_without_provider_priority(self) -> None:
        async def execute(config, request, challenge_dir, work_dir):
            output = 'BLACKBOARD_UPDATE: {"nodes": [], "edges": [], "goal_achieved": true, "final_answer": "flag{same}"}'
            return worker_result(config.name, work_dir, output)

        with tempfile.TemporaryDirectory() as directory:
            result = await run_orchestration(
                providers=(
                    ProviderConfig("codex", True, 1, ("unused",), 5),
                    ProviderConfig("claude", True, 999, ("unused",), 5),
                ),
                goal="Goal",
                origin="Origin",
                hints=(),
                challenge_dir=None,
                runs_root=Path(directory),
                max_rounds=3,
                execute=execute,
                hint_reader=lambda _: "",
                interactive=False,
            )

            self.assertEqual(result.status, "achieved")
            self.assertEqual(result.final_answer, "flag{same}")
            self.assertEqual(result.rounds_completed, 1)

    async def test_conflict_continues_and_human_hint_reaches_next_snapshot(self) -> None:
        calls = 0
        second_round_requests: list[str] = []

        async def execute(config, request, challenge_dir, work_dir):
            nonlocal calls
            calls += 1
            round_number = int(work_dir.parent.name)
            if round_number == 2:
                second_round_requests.append(request)
                answer = "flag{resolved}"
            else:
                answer = "flag{a}" if config.name == "codex" else "flag{b}"
            output = (
                'BLACKBOARD_UPDATE: {"nodes": [], "edges": [], '
                f'"goal_achieved": true, "final_answer": "{answer}"}}'
            )
            return worker_result(config.name, work_dir, output)

        hints = iter(("Check the alternate key",))
        with tempfile.TemporaryDirectory() as directory:
            result = await run_orchestration(
                providers=(provider("codex"), provider("claude")),
                goal="Goal",
                origin="Origin",
                hints=(),
                challenge_dir=None,
                runs_root=Path(directory),
                max_rounds=3,
                execute=execute,
                hint_reader=lambda _: next(hints, ""),
                interactive=True,
            )

            self.assertEqual(calls, 4)
            self.assertEqual(result.status, "achieved")
            self.assertTrue(all("Check the alternate key" in request for request in second_round_requests))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run scheduler tests and verify the stub failure**

Run:

```powershell
python -m unittest tests.test_scheduler -v
```

Expected: `ERROR` with `ModuleNotFoundError: No module named 'orchestrator.scheduler'`.

- [ ] **Step 3: Implement the round scheduler**

Replace `orchestrator/scheduler.py` with:

```python
import asyncio
import sys
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from orchestrator.models import AgentRoundResult, OrchestrationResult, ProtocolResult
from orchestrator.protocol import parse_update, render_request
from orchestrator.store import BlackboardStore
from worker.executor import execute_provider
from worker.models import ProviderConfig, WorkerResult


Executor = Callable[
    [ProviderConfig, str, Path | None, Path],
    Awaitable[WorkerResult],
]


def _new_run_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{uuid4().hex[:8]}"


def _node_ids(snapshot: dict[str, object]) -> set[str]:
    return {node["id"] for node in snapshot["nodes"]}


async def _run_agent(
    config: ProviderConfig,
    request: str,
    challenge_dir: Path | None,
    work_dir: Path,
    existing_node_ids: set[str],
    execute: Executor,
) -> AgentRoundResult:
    result = await execute(config, request, challenge_dir, work_dir)
    protocol = (
        parse_update(result.output, existing_node_ids)
        if result.status == "success"
        else ProtocolResult(
            status="not_attempted",
            update=None,
            error=f"process status was {result.status}",
        )
    )
    return AgentRoundResult(
        provider=config.name,
        process_status=result.status,
        protocol_status=protocol.status,
        exit_code=result.exit_code,
        duration_seconds=result.duration_seconds,
        output=result.output,
        stdout_path=result.stdout_path,
        stderr_path=result.stderr_path,
        work_dir=result.work_dir,
        update=protocol.update,
        protocol_error=protocol.error,
    )


async def run_orchestration(
    providers: tuple[ProviderConfig, ...],
    goal: str,
    origin: str,
    hints: tuple[str, ...],
    challenge_dir: Path | None,
    runs_root: Path,
    max_rounds: int = 5,
    execute: Executor = execute_provider,
    hint_reader: Callable[[str], str] = input,
    interactive: bool | None = None,
) -> OrchestrationResult:
    run_id = _new_run_id()
    run_dir = runs_root / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    store = BlackboardStore.create(
        run_dir / "blackboard.db",
        run_id,
        max_rounds,
        goal,
        origin,
        hints,
    )
    enabled = tuple(provider for provider in providers if provider.enabled)
    is_interactive = sys.stdin.isatty() if interactive is None else interactive
    rounds_completed = 0
    final_answer: str | None = None
    status = "no_usable_providers" if not enabled else "running"

    try:
        store.export_snapshot(0, run_dir / "snapshots")
        for round_number in range(1, max_rounds + 1):
            if not enabled:
                break
            store.export_snapshot(round_number, run_dir / "snapshots")
            snapshot = store.snapshot()
            request = render_request(snapshot, round_number, challenge_dir)
            existing = _node_ids(snapshot)
            results = await asyncio.gather(
                *(
                    _run_agent(
                        config,
                        request,
                        challenge_dir,
                        run_dir / "rounds" / f"{round_number:03d}" / config.name,
                        existing,
                        execute,
                    )
                    for config in enabled
                )
            )
            rounds_completed = round_number
            valid_updates = []
            available_agents = sum(
                result.process_status != "unavailable" for result in results
            )
            for result in results:
                answer = result.update.final_answer if result.update and result.update.goal_achieved else None
                store.record_agent_result(
                    round_number,
                    result.provider,
                    result.process_status,
                    result.protocol_status,
                    result.exit_code,
                    result.duration_seconds,
                    result.stdout_path,
                    result.stderr_path,
                    result.work_dir,
                    answer,
                )
                if result.update is not None:
                    valid_updates.append((result.provider, result.update))
            store.apply_round(round_number, tuple(valid_updates))
            if available_agents == 0:
                status = "no_usable_providers"
                break
            final_answer = store.consensus(round_number, available_agents)
            if final_answer is not None:
                status = "achieved"
                break
            if round_number < max_rounds and is_interactive:
                hint = hint_reader("Hint (blank to continue): ").strip()
                if hint:
                    store.add_hint(hint, round_number)
        if status == "running":
            status = "unresolved" if len(store.candidates()) > 1 else "max_rounds_reached"
        store.finish(status, final_answer, rounds_completed)
        return OrchestrationResult(
            run_id=run_id,
            run_dir=str(run_dir.resolve()),
            status=status,
            rounds_completed=rounds_completed,
            final_answer=final_answer,
            candidates=store.candidates(),
        )
    finally:
        store.close()
```

- [ ] **Step 4: Run scheduler tests**

Run:

```powershell
python -m unittest tests.test_scheduler -v
```

Expected: three tests report `ok` and the command ends with `OK`.

- [ ] **Step 5: Add provider failure isolation test**

Append to `SchedulerTests`:

```python
    async def test_protocol_error_does_not_block_valid_agent(self) -> None:
        async def execute(config, request, challenge_dir, work_dir):
            output = (
                "not structured"
                if config.name == "codex"
                else 'BLACKBOARD_UPDATE: {"nodes": [{"ref":"f1","type":"fact","content":"valid"}], "edges": [], "goal_achieved": false, "final_answer": null}'
            )
            return worker_result(config.name, work_dir, output)

        with tempfile.TemporaryDirectory() as directory:
            result = await run_orchestration(
                providers=(provider("codex"), provider("claude")),
                goal="Goal",
                origin="Origin",
                hints=(),
                challenge_dir=None,
                runs_root=Path(directory),
                max_rounds=1,
                execute=execute,
                hint_reader=lambda _: "",
                interactive=False,
            )
            database = Path(result.run_dir) / "blackboard.db"
            with sqlite3.connect(database) as connection:
                count = connection.execute(
                    "SELECT COUNT(*) FROM nodes WHERE type = 'fact'"
                ).fetchone()[0]
            self.assertEqual(count, 1)

    async def test_one_available_agent_can_reach_consensus(self) -> None:
        async def execute(config, request, challenge_dir, work_dir):
            if config.name == "missing":
                result = worker_result(config.name, work_dir, "")
                return WorkerResult(
                    provider=result.provider,
                    priority=result.priority,
                    status="unavailable",
                    exit_code=None,
                    duration_seconds=result.duration_seconds,
                    output="",
                    stdout_path=result.stdout_path,
                    stderr_path=result.stderr_path,
                    work_dir=result.work_dir,
                )
            output = 'BLACKBOARD_UPDATE: {"nodes": [], "edges": [], "goal_achieved": true, "final_answer": "flag{solo}"}'
            return worker_result(config.name, work_dir, output)

        with tempfile.TemporaryDirectory() as directory:
            result = await run_orchestration(
                providers=(provider("missing"), provider("codex")),
                goal="Goal",
                origin="Origin",
                hints=(),
                challenge_dir=None,
                runs_root=Path(directory),
                max_rounds=2,
                execute=execute,
                hint_reader=lambda _: "",
                interactive=False,
            )
            self.assertEqual(result.status, "achieved")
            self.assertEqual(result.final_answer, "flag{solo}")
```

Also add `import sqlite3` to the test imports.

- [ ] **Step 6: Run scheduler tests again**

Run:

```powershell
python -m unittest tests.test_scheduler -v
```

Expected: five tests report `ok` and the command ends with `OK`.

- [ ] **Step 7: Commit the scheduler when Git metadata is valid**

```powershell
git add orchestrator/scheduler.py tests/test_scheduler.py
git commit -m "feat: orchestrate concurrent blackboard rounds"
```

### Task 7: Replace the CLI Contract

**Files:**
- Modify: `main.py`
- Modify: `tests/test_main.py`

- [ ] **Step 1: Replace main tests with the new explicit input contract**

Replace `tests/test_main.py` with:

```python
import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import main
from orchestrator.models import OrchestrationResult


class MainTests(unittest.TestCase):
    @patch("main.load_provider_configs", return_value=())
    @patch("main.run_orchestration", new_callable=AsyncMock)
    def test_passes_explicit_blackboard_inputs_and_prints_answer(
        self,
        run_orchestration: AsyncMock,
        load_provider_configs: Mock,
    ) -> None:
        run_orchestration.return_value = OrchestrationResult(
            run_id="run-1",
            run_dir=str(Path("worker/runs/run-1").resolve()),
            status="achieved",
            rounds_completed=2,
            final_answer="flag{ok}",
            candidates=("flag{ok}",),
        )
        output = io.StringIO()

        with redirect_stdout(output):
            exit_code = main.run_cli(
                [
                    "--goal", "Recover the flag",
                    "--origin", "Challenge statement",
                    "--hint", "Check config",
                    "--hint", "Inspect strings",
                    "--max-rounds", "7",
                ]
            )

        self.assertEqual(exit_code, 0)
        self.assertIn("flag{ok}", output.getvalue())
        kwargs = run_orchestration.await_args.kwargs
        self.assertEqual(kwargs["goal"], "Recover the flag")
        self.assertEqual(kwargs["origin"], "Challenge statement")
        self.assertEqual(kwargs["hints"], ("Check config", "Inspect strings"))
        self.assertEqual(kwargs["max_rounds"], 7)

    def test_rejects_empty_goal(self) -> None:
        errors = io.StringIO()
        with redirect_stderr(errors):
            exit_code = main.run_cli(["--goal", " ", "--origin", "Origin"])
        self.assertEqual(exit_code, 1)
        self.assertIn("goal must not be empty", errors.getvalue())

    def test_rejects_non_positive_max_rounds(self) -> None:
        with self.assertRaises(SystemExit):
            main.run_cli(["--goal", "Goal", "--origin", "Origin", "--max-rounds", "0"])

    @patch("main.load_provider_configs", return_value=())
    @patch("main.run_orchestration", new_callable=AsyncMock)
    def test_prints_unresolved_candidates_and_returns_one(
        self,
        run_orchestration: AsyncMock,
        load_provider_configs: Mock,
    ) -> None:
        run_orchestration.return_value = OrchestrationResult(
            run_id="run-1",
            run_dir="run-dir",
            status="unresolved",
            rounds_completed=5,
            final_answer=None,
            candidates=("flag{a}", "flag{b}"),
        )
        output = io.StringIO()

        with redirect_stdout(output):
            exit_code = main.run_cli(["--goal", "Goal", "--origin", "Origin"])

        self.assertEqual(exit_code, 1)
        self.assertIn("flag{a}", output.getvalue())
        self.assertIn("flag{b}", output.getvalue())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run main tests and verify they fail against the old CLI**

Run:

```powershell
python -m unittest tests.test_main -v
```

Expected: failures because `main.run_orchestration`, `--goal`, and `--origin` do not exist.

- [ ] **Step 3: Implement the new CLI entry point**

Replace `main.py` with:

```python
import argparse
import asyncio
import sys
from pathlib import Path
from typing import Callable, Sequence

from orchestrator.scheduler import run_orchestration
from worker.config import ConfigError, load_provider_configs


DEFAULT_CONFIG = Path(__file__).resolve().parent / "worker" / "providers.json"
DEFAULT_RUNS_ROOT = Path(__file__).resolve().parent / "worker" / "runs"


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run CLI agents through a shared CTF blackboard graph."
    )
    parser.add_argument("--challenge", type=Path, help="read-only CTF challenge directory")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--goal", help="outcome the agents must achieve")
    parser.add_argument("--origin", help="challenge statement and known conditions")
    parser.add_argument("--hint", action="append", default=[], help="initial human hint; repeatable")
    parser.add_argument("--max-rounds", type=_positive_int, default=5)
    return parser


def run_cli(
    argv: Sequence[str] | None = None,
    input_func: Callable[[str], str] = input,
) -> int:
    args = _build_parser().parse_args(argv)
    challenge_dir = args.challenge.resolve() if args.challenge else None
    if challenge_dir and not challenge_dir.is_dir():
        print(f"Error: challenge directory does not exist: {challenge_dir}", file=sys.stderr)
        return 1

    goal = args.goal if args.goal is not None else input_func("Goal: ")
    origin = args.origin if args.origin is not None else input_func("Origin: ")
    goal = goal.strip()
    origin = origin.strip()
    if not goal:
        print("Error: goal must not be empty", file=sys.stderr)
        return 1
    if not origin:
        print("Error: origin must not be empty", file=sys.stderr)
        return 1

    try:
        providers = load_provider_configs(args.config.resolve())
        result = asyncio.run(
            run_orchestration(
                providers=providers,
                goal=goal,
                origin=origin,
                hints=tuple(hint.strip() for hint in args.hint if hint.strip()),
                challenge_dir=challenge_dir,
                runs_root=DEFAULT_RUNS_ROOT,
                max_rounds=args.max_rounds,
                hint_reader=input_func,
            )
        )
    except ConfigError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"Error: cannot create orchestration run: {exc}", file=sys.stderr)
        return 1

    print(f"Status: {result.status}")
    print(f"Rounds: {result.rounds_completed}")
    if result.final_answer:
        print(f"Final answer: {result.final_answer}")
    elif result.candidates:
        print("Candidate answers:")
        for candidate in result.candidates:
            print(f"- {candidate}")
    print(f"Run directory: {result.run_dir}")
    print(f"Blackboard database: {Path(result.run_dir) / 'blackboard.db'}")
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(run_cli())
```

- [ ] **Step 4: Run main tests**

Run:

```powershell
python -m unittest tests.test_main -v
```

Expected: four tests report `ok` and the command ends with `OK`.

- [ ] **Step 5: Commit the CLI when Git metadata is valid**

```powershell
git add main.py tests/test_main.py
git commit -m "feat: expose blackboard orchestration CLI"
```

### Task 8: Remove the One-Shot Runner, Update Documentation, and Run Full Verification

**Files:**
- Delete: `worker/runner.py`
- Delete: `tests/test_runner.py`
- Create: `tests/fixtures/smoke_providers.json`
- Modify: `README.md`
- Verify: all source and tests

- [ ] **Step 1: Remove obsolete one-shot runner files**

Delete `worker/runner.py` and `tests/test_runner.py`. Search for stale imports:

```powershell
Get-ChildItem -Recurse -File -Include *.py | Select-String -Pattern 'worker\.runner|RunSummary|--prompt'
```

Expected: no application or test source matches. The historical design and plan documents may still mention the old runner and should not be edited.

- [ ] **Step 2: Rewrite README usage and architecture sections**

Replace `README.md` with documentation containing these exact sections and examples:

```markdown
# Multi-CLI CTF Blackboard

Run Codex CLI, Claude Code, Kimi CLI, and additional command-line agents in concurrent rounds against one shared CTF blackboard graph.

## Blackboard Concepts

- Goal: the outcome the run must achieve.
- Origin: the challenge statement and known starting conditions.
- Fact: an agent-discovered proposition with one or more sources.
- Intent: one agent's next planned action.
- Hint: human guidance supplied initially or between rounds.

Every concept is a sourced graph node. Relationships such as `supports`, `contradicts`, `derived_from`, and `plans_from` are stored as sourced edges.

## Requirements

- Python 3.10 or newer
- At least one configured CLI installed and authenticated

The orchestrator uses only the Python standard library. Each CLI owns its own authentication.

## Run

```powershell
python main.py --goal "Recover the flag" --origin "Analyze the supplied binary and config.dat" --challenge E:\ctf\challenge
```

Add initial human Hints and change the round limit:

```powershell
python main.py --goal "Recover the flag" --origin "Challenge statement" --hint "Inspect config.dat" --hint "Check for reused IVs" --max-rounds 7 --challenge E:\ctf\challenge
```

When Goal or Origin is omitted, an interactive terminal asks for it. Between unresolved rounds, an interactive run accepts another Hint; blank input continues without one.

## Agent Protocol

Every provider receives the same JSON graph snapshot at the start of a round. It may write ordinary analysis, but its final structured line must begin with:

```text
BLACKBOARD_UPDATE:
```

The JSON object may propose Fact and Intent nodes, sourced edges, a goal-completion claim, and a final answer. Agents cannot create Goal, Origin, or Hint nodes and cannot choose their stored source identity.

## Persistence

Each run creates:

```text
worker/runs/<run-id>/
  blackboard.db
  snapshots/
    round-000.json
    round-001.json
  rounds/
    001/
      codex/
        stdout.txt
        stderr.txt
```

`blackboard.db` is the source of truth. JSON files are reproducible snapshots used for agent input and inspection.

Exact normalized duplicate Facts share one graph node while retaining every independent source. Textually similar Facts are not automatically merged because small wording differences may be technically significant.

## Completion

Provider priority controls presentation only. With multiple available agents, two independent agents must submit the same final answer to reach consensus. Conflicting answers remain visible and trigger another round. A single available agent may complete the run alone.

The process exits with code `0` only when the Goal is achieved. Unresolved conflicts, exhausted rounds, or no usable providers exit with code `1`.

## Provider Setup

Provider commands, enablement, presentation priority, and timeouts are configured in `worker/providers.json`. Prompts travel through stdin with `shell=False`, and every provider runs in its own writable round directory.

## Test

Tests use fake local CLIs and do not consume model quota:

```powershell
python -m unittest discover -s tests -v
```
```

- [ ] **Step 3: Run the complete test suite**

Run:

```powershell
python -m unittest discover -s tests -v
```

Expected: every configuration, executor, store, protocol, scheduler, and main test reports `ok`; the command ends with `OK`.

- [ ] **Step 4: Run compile verification**

Run:

```powershell
python -m compileall main.py orchestrator worker tests
```

Expected: all Python files compile without syntax errors and the command exits with code 0.

- [ ] **Step 5: Add deterministic smoke providers and run the real CLI**

Create `tests/fixtures/smoke_providers.json`:

```json
{
  "providers": [
    {
      "name": "fake-a",
      "enabled": true,
      "priority": 10,
      "command": [
        "python",
        "-c",
        "import json,sys;sys.stdin.read();o=chr(123);c=chr(125);p=dict(nodes=[],edges=[],goal_achieved=True,final_answer='flag'+o+'smoke'+c);print('BLACKBOARD_UPDATE: '+json.dumps(p))"
      ],
      "timeout_seconds": 10
    },
    {
      "name": "fake-b",
      "enabled": true,
      "priority": 20,
      "command": [
        "python",
        "-c",
        "import json,sys;sys.stdin.read();o=chr(123);c=chr(125);p=dict(nodes=[],edges=[],goal_achieved=True,final_answer='flag'+o+'smoke'+c);print('BLACKBOARD_UPDATE: '+json.dumps(p))"
      ],
      "timeout_seconds": 10
    }
  ]
}
```

Run:

```powershell
python main.py --config tests\fixtures\smoke_providers.json --goal "Recover the smoke flag" --origin "Deterministic local smoke run" --max-rounds 2
```

Verify stdout includes:

```text
Status: achieved
Final answer: flag{smoke}
```

The generated run directory must contain `blackboard.db`, `snapshots/round-000.json`, `snapshots/round-001.json`, `rounds/001/fake-a/`, and `rounds/001/fake-b/`.

- [ ] **Step 6: Commit cleanup and documentation when Git metadata is valid**

```powershell
git add README.md tests/fixtures/smoke_providers.json worker/runner.py tests/test_runner.py
git commit -m "docs: document blackboard orchestration"
```

Because the two Python files are deleted, `git add` stages their deletion when repository metadata is valid.
