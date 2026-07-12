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
        normalized = _normalize(content)
        if not normalized:
            raise ValueError("fact content must be non-empty")
        fingerprint = _fingerprint(normalized)
        with self.connection:
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
            nodes.append({**dict(row), "sources": sources, "produced_by": None})

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
                edge["status"] = "planned"
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
            "candidates": [],
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
