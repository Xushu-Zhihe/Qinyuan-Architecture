import hashlib
import json
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from uuid import uuid4

from orchestrator.models import IntentProposal, RELATION_EDGE_TYPES


SCHEMA = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 5000;

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
    type TEXT NOT NULL CHECK(type IN ('goal', 'origin', 'fact', 'hint')),
    content TEXT NOT NULL CHECK(length(trim(content)) > 0),
    fingerprint TEXT,
    state TEXT NOT NULL DEFAULT 'active',
    generation INTEGER NOT NULL CHECK(generation >= 0),
    created_at TEXT NOT NULL
);

CREATE UNIQUE INDEX one_goal_per_project
ON nodes(project_id) WHERE type = 'goal' AND state = 'active';
CREATE UNIQUE INDEX one_origin_per_project
ON nodes(project_id) WHERE type = 'origin' AND state = 'active';
CREATE UNIQUE INDEX fact_fingerprint
ON nodes(project_id, fingerprint) WHERE type = 'fact';
CREATE INDEX nodes_by_project_type
ON nodes(project_id, type, state, generation);

CREATE TABLE node_provenance (
    id TEXT PRIMARY KEY,
    node_id TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    original_content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(node_id, source, original_content)
);

CREATE TABLE node_versions (
    id TEXT PRIMARY KEY,
    node_id TEXT NOT NULL REFERENCES nodes(id),
    old_content TEXT NOT NULL,
    new_content TEXT NOT NULL,
    source TEXT NOT NULL,
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

CREATE INDEX edges_by_project_generation
ON edges(project_id, generation, type);

CREATE TABLE edge_provenance (
    id TEXT PRIMARY KEY,
    edge_id TEXT NOT NULL REFERENCES edges(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(edge_id, source)
);

CREATE TABLE intents (
    edge_id TEXT PRIMARY KEY REFERENCES edges(id) ON DELETE CASCADE,
    content TEXT NOT NULL,
    fingerprint TEXT NOT NULL UNIQUE
);

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

CREATE INDEX executions_by_intent
ON intent_executions(intent_edge_id, status, started_at);

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
"""


@dataclass(frozen=True, slots=True)
class IntentRecord:
    id: str
    source_node_id: str
    target_node_id: str
    content: str
    generation: int


@dataclass(frozen=True, slots=True)
class ExecutionRecord:
    id: str
    intent_id: str
    provider: str
    status: str


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize(content: str) -> str:
    normalized = unicodedata.normalize("NFKC", content)
    return " ".join(normalized.strip().split())


def _fingerprint(content: str) -> str:
    return hashlib.sha256(_normalize(content).encode("utf-8")).hexdigest()


def _intent_fingerprint(proposal: IntentProposal) -> str:
    material = "\0".join(
        (
            proposal.source_node_id,
            proposal.target_node_id,
            _normalize(proposal.content),
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class BlackboardStore:
    def __init__(
        self,
        path: Path,
        connection: sqlite3.Connection,
        project_id: str,
    ) -> None:
        self.path = path
        self.connection = connection
        self.project_id = project_id

    @classmethod
    def open(cls, path: Path, project_id: str) -> "BlackboardStore":
        connection = sqlite3.connect(path, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        exists = connection.execute(
            "SELECT 1 FROM projects WHERE id = ?",
            (project_id,),
        ).fetchone()
        if exists is None:
            connection.close()
            raise KeyError(project_id)
        return cls(path, connection, project_id)

    @classmethod
    def create(
        cls,
        path: Path,
        project_id: str,
        goal: str,
        origin: str,
        hints: tuple[str, ...],
    ) -> "BlackboardStore":
        if not goal.strip() or not origin.strip():
            raise ValueError("goal and origin must be non-empty")
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.executescript(SCHEMA)
        store = cls(path, connection, project_id)
        now = _now()
        with connection:
            connection.execute(
                """
                INSERT INTO projects(
                    id, status, goal_version, created_at, updated_at
                ) VALUES (?, 'idle', 1, ?, ?)
                """,
                (project_id, now, now),
            )
            goal_id = store._insert_node(
                "goal", goal, "human", generation=0
            )
            store._insert_node(
                "origin", origin, "human", generation=0
            )
            for hint in hints:
                if not hint.strip():
                    continue
                hint_id = store._insert_node(
                    "hint", hint, "human", generation=0
                )
                store._insert_relation(
                    "addresses",
                    hint_id,
                    goal_id,
                    "human",
                    generation=0,
                )
        return store

    def close(self) -> None:
        self.connection.close()

    def _event(
        self,
        event_type: str,
        source: str,
        payload: dict[str, object],
    ) -> int:
        cursor = self.connection.execute(
            """
            INSERT INTO events(type, source, payload_json, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                event_type,
                source,
                json.dumps(payload, ensure_ascii=False),
                _now(),
            ),
        )
        return int(cursor.lastrowid)

    def _insert_node(
        self,
        node_type: str,
        content: str,
        source: str,
        generation: int,
        fingerprint: str | None = None,
    ) -> str:
        node_id = _id(node_type)
        normalized = _normalize(content)
        created_at = _now()
        self.connection.execute(
            """
            INSERT INTO nodes(
                id, project_id, type, content, fingerprint,
                state, generation, created_at
            ) VALUES (?, ?, ?, ?, ?, 'active', ?, ?)
            """,
            (
                node_id,
                self.project_id,
                node_type,
                normalized,
                fingerprint,
                generation,
                created_at,
            ),
        )
        self._add_node_provenance(node_id, source, content)
        self._event(
            "graph.node_added",
            source,
            {"node_id": node_id, "type": node_type},
        )
        return node_id

    def _add_node_provenance(
        self,
        node_id: str,
        source: str,
        original_content: str,
    ) -> None:
        self.connection.execute(
            """
            INSERT OR IGNORE INTO node_provenance(
                id, node_id, source, original_content, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                _id("provenance"),
                node_id,
                source,
                original_content,
                _now(),
            ),
        )

    def _add_edge_provenance(self, edge_id: str, source: str) -> None:
        self.connection.execute(
            """
            INSERT OR IGNORE INTO edge_provenance(
                id, edge_id, source, created_at
            ) VALUES (?, ?, ?, ?)
            """,
            (_id("provenance"), edge_id, source, _now()),
        )

    def _insert_relation(
        self,
        edge_type: str,
        from_node: str,
        to_node: str,
        source: str,
        generation: int,
    ) -> str:
        if edge_type not in RELATION_EDGE_TYPES:
            raise ValueError(f"unsupported relation edge type: {edge_type}")
        edge_id = _id("edge")
        self.connection.execute(
            """
            INSERT INTO edges(
                id, project_id, type, from_node, to_node,
                generation, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                edge_id,
                self.project_id,
                edge_type,
                from_node,
                to_node,
                generation,
                _now(),
            ),
        )
        self._add_edge_provenance(edge_id, source)
        self._event(
            "graph.edge_added",
            source,
            {"edge_id": edge_id, "type": edge_type},
        )
        return edge_id

    def node_id(self, node_type: str) -> str:
        row = self.connection.execute(
            """
            SELECT id FROM nodes
            WHERE project_id = ? AND type = ? AND state = 'active'
            ORDER BY created_at LIMIT 1
            """,
            (self.project_id, node_type),
        ).fetchone()
        if row is None:
            raise KeyError(node_type)
        return str(row["id"])

    def node_types(self) -> dict[str, str]:
        return {
            str(row["id"]): str(row["type"])
            for row in self.connection.execute(
                """
                SELECT id, type FROM nodes
                WHERE project_id = ? AND state = 'active'
                """,
                (self.project_id,),
            ).fetchall()
        }

    def add_human_node(self, node_type: str, content: str) -> str:
        if node_type == "fact":
            return self.add_fact("human", content, generation=0)
        if node_type != "hint":
            raise ValueError("human graph nodes must be fact or hint")
        with self.connection:
            return self._insert_node(
                "hint", content, "human", generation=0
            )

    def add_fact(
        self,
        source: str,
        content: str,
        generation: int,
    ) -> str:
        with self.connection:
            return self._merge_fact(source, content, generation)

    def _merge_fact(
        self,
        source: str,
        content: str,
        generation: int,
    ) -> str:
        normalized = _normalize(content)
        if not normalized:
            raise ValueError("fact content must be non-empty")
        fingerprint = _fingerprint(normalized)
        existing = self.connection.execute(
            """
            SELECT id FROM nodes
            WHERE project_id = ? AND type = 'fact' AND fingerprint = ?
            """,
            (self.project_id, fingerprint),
        ).fetchone()
        if existing is not None:
            node_id = str(existing["id"])
            self._add_node_provenance(node_id, source, content)
            self._event(
                "graph.node_updated",
                source,
                {"node_id": node_id, "change": "provenance"},
            )
            return node_id

        node_id = self._insert_node(
            "fact",
            normalized,
            source,
            generation,
            fingerprint,
        )
        for similar_id in self._similar_facts(normalized, node_id):
            self._insert_relation(
                "similar_to",
                node_id,
                similar_id,
                "orchestrator",
                generation,
            )
        return node_id

    def _similar_facts(
        self,
        content: str,
        exclude_id: str,
    ) -> tuple[str, ...]:
        rows = self.connection.execute(
            """
            SELECT id, content FROM nodes
            WHERE project_id = ? AND type = 'fact' AND id != ?
            """,
            (self.project_id, exclude_id),
        ).fetchall()
        return tuple(
            str(row["id"])
            for row in rows
            if SequenceMatcher(
                None,
                content,
                _normalize(str(row["content"])),
            ).ratio()
            >= 0.92
        )

    def add_relation(
        self,
        edge_type: str,
        from_node: str,
        to_node: str,
        source: str = "human",
    ) -> str:
        generation = max(
            self._node_generation(from_node),
            self._node_generation(to_node),
        )
        with self.connection:
            return self._insert_relation(
                edge_type,
                from_node,
                to_node,
                source,
                generation,
            )

    def _node_generation(self, node_id: str) -> int:
        row = self.connection.execute(
            "SELECT generation FROM nodes WHERE id = ? AND project_id = ?",
            (node_id, self.project_id),
        ).fetchone()
        if row is None:
            raise ValueError("unknown node reference")
        return int(row["generation"])

    def create_or_merge_intent(
        self,
        source: str,
        proposal: IntentProposal,
    ) -> IntentRecord:
        node_types = self.node_types()
        source_type = node_types.get(proposal.source_node_id)
        target_type = node_types.get(proposal.target_node_id)
        facts_exist = "fact" in node_types.values()
        if source_type != "fact" and not (
            source_type == "origin" and not facts_exist
        ):
            raise ValueError("origin is only allowed as bootstrap source")
        if target_type not in {"goal", "fact", "hint"}:
            raise ValueError("unsupported intent target")
        normalized = _normalize(proposal.content)
        if not normalized:
            raise ValueError("intent content must be non-empty")
        fingerprint = _intent_fingerprint(
            IntentProposal(
                proposal.source_node_id,
                proposal.target_node_id,
                normalized,
            )
        )
        generation = self._node_generation(proposal.source_node_id) + 1

        with self.connection:
            existing = self.connection.execute(
                """
                SELECT e.id, e.from_node, e.to_node, i.content, e.generation
                FROM intents AS i
                JOIN edges AS e ON e.id = i.edge_id
                WHERE i.fingerprint = ?
                """,
                (fingerprint,),
            ).fetchone()
            if existing is not None:
                edge_id = str(existing["id"])
                self._add_edge_provenance(edge_id, source)
                self._event(
                    "graph.edge_updated",
                    source,
                    {"edge_id": edge_id, "change": "provenance"},
                )
                return IntentRecord(
                    edge_id,
                    str(existing["from_node"]),
                    str(existing["to_node"]),
                    str(existing["content"]),
                    int(existing["generation"]),
                )

            edge_id = _id("intent")
            self.connection.execute(
                """
                INSERT INTO edges(
                    id, project_id, type, from_node, to_node,
                    generation, created_at
                ) VALUES (?, ?, 'intent', ?, ?, ?, ?)
                """,
                (
                    edge_id,
                    self.project_id,
                    proposal.source_node_id,
                    proposal.target_node_id,
                    generation,
                    _now(),
                ),
            )
            self.connection.execute(
                """
                INSERT INTO intents(edge_id, content, fingerprint)
                VALUES (?, ?, ?)
                """,
                (edge_id, normalized, fingerprint),
            )
            self._add_edge_provenance(edge_id, source)
            self._event(
                "graph.edge_added",
                source,
                {"edge_id": edge_id, "type": "intent"},
            )
            return IntentRecord(
                edge_id,
                proposal.source_node_id,
                proposal.target_node_id,
                normalized,
                generation,
            )

    def intent_sources(self, intent_id: str) -> tuple[str, ...]:
        return tuple(
            str(row["source"])
            for row in self.connection.execute(
                """
                SELECT source FROM edge_provenance
                WHERE edge_id = ? ORDER BY source
                """,
                (intent_id,),
            ).fetchall()
        )

    def reconnect_edge(
        self,
        edge_id: str,
        from_node: str,
        to_node: str,
        source: str = "human",
    ) -> None:
        row = self.connection.execute(
            """
            SELECT e.type, i.content
            FROM edges AS e
            LEFT JOIN intents AS i ON i.edge_id = e.id
            WHERE e.id = ? AND e.project_id = ?
            """,
            (edge_id, self.project_id),
        ).fetchone()
        if row is None:
            raise KeyError(edge_id)
        node_types = self.node_types()
        if from_node not in node_types or to_node not in node_types:
            raise ValueError("unknown node reference")
        generation = max(
            self._node_generation(from_node),
            self._node_generation(to_node),
        )
        with self.connection:
            if row["type"] == "intent":
                facts_exist = "fact" in node_types.values()
                if node_types[from_node] != "fact" and not (
                    node_types[from_node] == "origin" and not facts_exist
                ):
                    raise ValueError("origin is only allowed as bootstrap source")
                if node_types[to_node] not in {"goal", "fact", "hint"}:
                    raise ValueError("unsupported intent target")
                generation = self._node_generation(from_node) + 1
                proposal = IntentProposal(
                    from_node,
                    to_node,
                    str(row["content"]),
                )
                self.connection.execute(
                    "UPDATE intents SET fingerprint = ? WHERE edge_id = ?",
                    (_intent_fingerprint(proposal), edge_id),
                )
            self.connection.execute(
                """
                UPDATE edges
                SET from_node = ?, to_node = ?, generation = ?
                WHERE id = ?
                """,
                (from_node, to_node, generation, edge_id),
            )
            self._add_edge_provenance(edge_id, source)
            self._event(
                "graph.edge_updated",
                source,
                {
                    "edge_id": edge_id,
                    "from_node": from_node,
                    "to_node": to_node,
                },
            )

    def delete_edge(self, edge_id: str) -> None:
        with self.connection:
            row = self.connection.execute(
                "SELECT type FROM edges WHERE id = ? AND project_id = ?",
                (edge_id, self.project_id),
            ).fetchone()
            if row is None:
                raise KeyError(edge_id)
            executions = int(
                self.connection.execute(
                    """
                    SELECT COUNT(*) FROM intent_executions
                    WHERE intent_edge_id = ?
                    """,
                    (edge_id,),
                ).fetchone()[0]
            )
            if executions:
                raise ValueError("edge with execution history cannot be deleted")
            self.connection.execute("DELETE FROM edges WHERE id = ?", (edge_id,))
            self._event(
                "graph.edge_updated",
                "human",
                {"edge_id": edge_id, "state": "deleted"},
            )

    def start_execution(
        self,
        intent_id: str,
        provider: str,
        stdout_path: str,
        stderr_path: str,
        work_dir: str,
    ) -> ExecutionRecord:
        execution_id = _id("execution")
        with self.connection:
            exists = self.connection.execute(
                "SELECT 1 FROM intents WHERE edge_id = ?",
                (intent_id,),
            ).fetchone()
            if exists is None:
                raise KeyError(intent_id)
            self.connection.execute(
                """
                INSERT INTO intent_executions(
                    id, intent_edge_id, provider, status, started_at,
                    stdout_path, stderr_path, work_dir
                ) VALUES (?, ?, ?, 'running', ?, ?, ?, ?)
                """,
                (
                    execution_id,
                    intent_id,
                    provider,
                    _now(),
                    stdout_path,
                    stderr_path,
                    work_dir,
                ),
            )
            self._event(
                "intent.execution_started",
                provider,
                {
                    "intent_id": intent_id,
                    "execution_id": execution_id,
                },
            )
        return ExecutionRecord(
            execution_id,
            intent_id,
            provider,
            "running",
        )

    def execution(self, execution_id: str) -> dict[str, object]:
        row = self.connection.execute(
            "SELECT * FROM intent_executions WHERE id = ?",
            (execution_id,),
        ).fetchone()
        if row is None:
            raise KeyError(execution_id)
        return dict(row)

    def finish_execution(
        self,
        execution_id: str,
        update,
        exit_code: int | None,
        status: str,
    ) -> tuple[str, ...]:
        with self.connection:
            execution = self.connection.execute(
                "SELECT * FROM intent_executions WHERE id = ?",
                (execution_id,),
            ).fetchone()
            if execution is None:
                raise KeyError(execution_id)
            intent_id = str(execution["intent_edge_id"])
            if update.intent_id != intent_id:
                raise ValueError("result references the wrong intent")
            intent = self.connection.execute(
                """
                SELECT e.generation
                FROM edges AS e
                JOIN intents AS i ON i.edge_id = e.id
                WHERE e.id = ?
                """,
                (intent_id,),
            ).fetchone()
            if intent is None:
                raise KeyError(intent_id)

            refs: dict[str, str] = {}
            for fact in update.facts:
                refs[fact.ref] = self._merge_fact(
                    str(execution["provider"]),
                    fact.content,
                    int(intent["generation"]),
                )

            known = {
                str(row["id"])
                for row in self.connection.execute(
                    "SELECT id FROM nodes WHERE project_id = ?",
                    (self.project_id,),
                ).fetchall()
            }
            for edge in update.edges:
                from_node = refs.get(edge.from_ref, edge.from_ref)
                to_node = refs.get(edge.to_ref, edge.to_ref)
                if from_node not in known or to_node not in known:
                    raise ValueError("unknown node reference")
                self._insert_relation(
                    edge.type,
                    from_node,
                    to_node,
                    str(execution["provider"]),
                    int(intent["generation"]),
                )

            for node_id in dict.fromkeys(refs.values()):
                self.connection.execute(
                    """
                    INSERT OR IGNORE INTO intent_outputs(
                        intent_edge_id, execution_id, node_id
                    ) VALUES (?, ?, ?)
                    """,
                    (intent_id, execution_id, node_id),
                )

            if update.goal_achieved:
                if update.final_answer is None:
                    raise ValueError("goal completion requires final answer")
                self._record_answer(
                    str(execution["provider"]),
                    update.final_answer,
                )

            self.connection.execute(
                """
                UPDATE intent_executions
                SET status = ?, finished_at = ?, exit_code = ?
                WHERE id = ?
                """,
                (status, _now(), exit_code, execution_id),
            )
            self._event(
                "intent.execution_finished",
                str(execution["provider"]),
                {
                    "intent_id": intent_id,
                    "execution_id": execution_id,
                    "status": status,
                },
            )
            return tuple(dict.fromkeys(refs.values()))

    def fail_execution(
        self,
        execution_id: str,
        status: str,
        error_message: str,
        exit_code: int | None = None,
    ) -> None:
        with self.connection:
            execution = self.execution(execution_id)
            self.connection.execute(
                """
                UPDATE intent_executions
                SET status = ?, finished_at = ?, exit_code = ?,
                    error_message = ?
                WHERE id = ?
                """,
                (status, _now(), exit_code, error_message, execution_id),
            )
            self._event(
                "intent.execution_finished",
                str(execution["provider"]),
                {
                    "intent_id": execution["intent_edge_id"],
                    "execution_id": execution_id,
                    "status": status,
                },
            )

    def intent_status(self, intent_id: str) -> str:
        statuses = [
            str(row["status"])
            for row in self.connection.execute(
                """
                SELECT status FROM intent_executions
                WHERE intent_edge_id = ?
                """,
                (intent_id,),
            ).fetchall()
        ]
        if not statuses:
            return "planned"
        if any(status in {"planned", "running"} for status in statuses):
            return "running"
        if "completed" in statuses:
            return "completed"
        if all(status == "cancelled" for status in statuses):
            return "cancelled"
        return "failed"

    def record_answer(self, provider: str, answer: str) -> None:
        with self.connection:
            self._record_answer(provider, answer)

    def _record_answer(self, provider: str, answer: str) -> None:
        normalized = answer.strip()
        if not normalized:
            raise ValueError("answer must be non-empty")
        goal_version = int(
            self.connection.execute(
                "SELECT goal_version FROM projects WHERE id = ?",
                (self.project_id,),
            ).fetchone()[0]
        )
        self.connection.execute(
            """
            INSERT INTO final_answers(
                id, provider, answer, goal_version, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (_id("answer"), provider, normalized, goal_version, _now()),
        )
        self._event(
            "answer.proposed",
            provider,
            {"answer": normalized, "goal_version": goal_version},
        )

    def consensus(self, available_providers: int) -> str | None:
        goal_version = int(
            self.connection.execute(
                "SELECT goal_version FROM projects WHERE id = ?",
                (self.project_id,),
            ).fetchone()[0]
        )
        rows = self.connection.execute(
            """
            SELECT trim(answer) AS answer, COUNT(DISTINCT provider) AS supporters
            FROM final_answers
            WHERE goal_version = ?
            GROUP BY trim(answer)
            ORDER BY supporters DESC, answer
            """,
            (goal_version,),
        ).fetchall()
        threshold = 1 if available_providers == 1 else 2
        winners = [
            str(row["answer"])
            for row in rows
            if int(row["supporters"]) >= threshold
        ]
        return winners[0] if len(winners) == 1 else None

    def candidates(self) -> tuple[str, ...]:
        goal_version = int(
            self.connection.execute(
                "SELECT goal_version FROM projects WHERE id = ?",
                (self.project_id,),
            ).fetchone()[0]
        )
        return tuple(
            str(row["answer"])
            for row in self.connection.execute(
                """
                SELECT DISTINCT trim(answer) AS answer
                FROM final_answers
                WHERE goal_version = ? ORDER BY answer
                """,
                (goal_version,),
            ).fetchall()
        )

    def events_after(self, sequence: int) -> tuple[dict[str, object], ...]:
        return tuple(
            {
                "sequence": int(row["sequence"]),
                "type": str(row["type"]),
                "source": str(row["source"]),
                "payload": json.loads(str(row["payload_json"])),
                "created_at": str(row["created_at"]),
            }
            for row in self.connection.execute(
                """
                SELECT sequence, type, source, payload_json, created_at
                FROM events WHERE sequence > ? ORDER BY sequence
                """,
                (sequence,),
            ).fetchall()
        )

    def append_event(
        self,
        event_type: str,
        source: str,
        payload: dict[str, object],
    ) -> dict[str, object]:
        with self.connection:
            sequence = self._event(event_type, source, payload)
        return self.events_after(sequence - 1)[0]

    def mark_interrupted(self) -> int:
        with self.connection:
            cursor = self.connection.execute(
                """
                UPDATE intent_executions
                SET status = 'interrupted', finished_at = ?,
                    error_message = 'server restarted'
                WHERE status IN ('planned', 'running')
                """,
                (_now(),),
            )
            return int(cursor.rowcount)

    def set_project_status(
        self,
        status: str,
        final_answer: str | None = None,
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                UPDATE projects
                SET status = ?, final_answer = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, final_answer, _now(), self.project_id),
            )
            self._event(
                "project.status_changed",
                "orchestrator",
                {"status": status, "final_answer": final_answer},
            )

    def archive_node(self, node_id: str, source: str = "human") -> None:
        with self.connection:
            cursor = self.connection.execute(
                """
                UPDATE nodes SET state = 'archived'
                WHERE id = ? AND project_id = ?
                """,
                (node_id, self.project_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(node_id)
            self._event(
                "graph.node_updated",
                source,
                {"node_id": node_id, "state": "archived"},
            )

    def snapshot(self) -> dict[str, object]:
        project_row = self.connection.execute(
            "SELECT * FROM projects WHERE id = ?",
            (self.project_id,),
        ).fetchone()
        if project_row is None:
            raise KeyError(self.project_id)

        node_rows = self.connection.execute(
            """
            SELECT id, type, content, state, generation, created_at
            FROM nodes WHERE project_id = ?
            ORDER BY generation,
                CASE type
                    WHEN 'goal' THEN 0
                    WHEN 'origin' THEN 1
                    WHEN 'hint' THEN 2
                    ELSE 3
                END,
                created_at,
                id
            """,
            (self.project_id,),
        ).fetchall()
        nodes = []
        for row in node_rows:
            sources = [
                str(item["source"])
                for item in self.connection.execute(
                    """
                    SELECT DISTINCT source FROM node_provenance
                    WHERE node_id = ? ORDER BY source
                    """,
                    (row["id"],),
                ).fetchall()
            ]
            output = self.connection.execute(
                """
                SELECT intent_edge_id, execution_id
                FROM intent_outputs
                WHERE node_id = ?
                ORDER BY rowid DESC LIMIT 1
                """,
                (row["id"],),
            ).fetchone()
            produced_by = (
                {
                    "intent_id": str(output["intent_edge_id"]),
                    "execution_id": str(output["execution_id"]),
                }
                if output is not None
                else None
            )
            nodes.append(
                {
                    **dict(row),
                    "sources": sources,
                    "produced_by": produced_by,
                }
            )

        edge_rows = self.connection.execute(
            """
            SELECT
                e.id, e.type, e.from_node, e.to_node,
                e.generation, e.created_at, i.content
            FROM edges AS e
            LEFT JOIN intents AS i ON i.edge_id = e.id
            WHERE e.project_id = ?
            ORDER BY e.generation, e.created_at, e.id
            """,
            (self.project_id,),
        ).fetchall()
        edges = []
        for row in edge_rows:
            sources = [
                str(item["source"])
                for item in self.connection.execute(
                    """
                    SELECT DISTINCT source FROM edge_provenance
                    WHERE edge_id = ? ORDER BY source
                    """,
                    (row["id"],),
                ).fetchall()
            ]
            edge = {**dict(row), "sources": sources}
            if edge["type"] == "intent":
                edge["status"] = self.intent_status(str(edge["id"]))
                edge["executions"] = [
                    dict(item)
                    for item in self.connection.execute(
                        """
                        SELECT * FROM intent_executions
                        WHERE intent_edge_id = ?
                        ORDER BY started_at, id
                        """,
                        (edge["id"],),
                    ).fetchall()
                ]
            edges.append(edge)

        highest_generation = max(
            [0]
            + [int(node["generation"]) for node in nodes]
            + [int(edge["generation"]) for edge in edges]
        )
        sequence_row = self.connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) AS sequence FROM events"
        ).fetchone()
        return {
            "project": dict(project_row),
            "nodes": nodes,
            "edges": edges,
            "candidates": list(self.candidates()),
            "highest_generation": highest_generation,
            "sequence": int(sequence_row["sequence"]),
        }

    def export_snapshot(self, directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        generation = int(self.snapshot()["highest_generation"])
        path = directory / f"generation-{generation:03d}.json"
        path.write_text(
            json.dumps(self.snapshot(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return path
