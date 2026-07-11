import hashlib
import json
import sqlite3
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path
from uuid import uuid4

from orchestrator.models import AGENT_NODE_TYPES, EDGE_TYPES, AgentUpdate


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


def _normalize_fact(content: str) -> str:
    normalized = unicodedata.normalize("NFKC", content)
    return " ".join(normalized.strip().split())


def _fact_fingerprint(content: str) -> str:
    return hashlib.sha256(_normalize_fact(content).encode("utf-8")).hexdigest()


class BlackboardStore:
    def __init__(
        self,
        path: Path,
        connection: sqlite3.Connection,
        run_id: str,
    ) -> None:
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
                """
                INSERT INTO runs(run_id, status, max_rounds)
                VALUES (?, 'running', ?)
                """,
                (run_id, max_rounds),
            )
            connection.execute(
                """
                INSERT INTO rounds(run_id, round_number, status)
                VALUES (?, 0, 'initialized')
                """,
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
            INSERT INTO nodes(
                id, run_id, type, content, fingerprint, created_round
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                node_id,
                self.run_id,
                node_type,
                content.strip(),
                fingerprint,
                round_number,
            ),
        )
        self.connection.execute(
            """
            INSERT INTO node_provenance(
                id, node_id, source, round_number, original_content
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (_id("provenance"), node_id, source, round_number, content),
        )
        self._event(
            round_number,
            "node_created",
            source,
            {"node_id": node_id, "type": node_type},
        )
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
            INSERT INTO edges(
                id, run_id, type, from_node, to_node, source, round_number
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                edge_id,
                self.run_id,
                edge_type,
                from_node,
                to_node,
                source,
                round_number,
            ),
        )
        self._event(
            round_number,
            "edge_created",
            source,
            {"edge_id": edge_id, "type": edge_type},
        )
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
            INSERT INTO events(
                run_id, round_number, type, source, payload_json
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                self.run_id,
                round_number,
                event_type,
                source,
                json.dumps(payload, ensure_ascii=False),
            ),
        )

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
            (
                _id("provenance"),
                node_id,
                source,
                round_number,
                original_content,
            ),
        )
        self._event(
            round_number,
            "provenance_added",
            source,
            {"node_id": node_id},
        )

    def _current_intent(self, source: str) -> str | None:
        row = self.connection.execute(
            """
            SELECT n.id
            FROM nodes AS n
            JOIN node_provenance AS p ON p.node_id = n.id
            WHERE n.run_id = ?
              AND n.type = 'intent'
              AND n.state = 'active'
              AND p.source = ?
            ORDER BY n.created_round DESC
            LIMIT 1
            """,
            (self.run_id, source),
        ).fetchone()
        return row[0] if row else None

    def _similar_facts(
        self,
        content: str,
        exclude_id: str,
    ) -> tuple[str, ...]:
        normalized = _normalize_fact(content)
        rows = self.connection.execute(
            """
            SELECT id, content
            FROM nodes
            WHERE run_id = ? AND type = 'fact' AND id != ?
            """,
            (self.run_id, exclude_id),
        ).fetchall()
        return tuple(
            row["id"]
            for row in rows
            if SequenceMatcher(
                None,
                normalized,
                _normalize_fact(row["content"]),
            ).ratio()
            >= 0.92
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
                SELECT id
                FROM nodes
                WHERE run_id = ? AND type = 'fact' AND fingerprint = ?
                """,
                (self.run_id, fingerprint),
            ).fetchone()
            if row:
                self._add_provenance(
                    row["id"],
                    source,
                    round_number,
                    content,
                )
                return row["id"]

            node_id = self._insert_node(
                "fact",
                _normalize_fact(content),
                source,
                round_number,
                fingerprint,
            )
            for similar_id in self._similar_facts(content, node_id):
                self._insert_edge(
                    "similar_to",
                    node_id,
                    similar_id,
                    "orchestrator",
                    round_number,
                )
            return node_id

        previous = self._current_intent(source)
        node_id = self._insert_node(
            "intent",
            content,
            source,
            round_number,
        )
        if previous:
            self.connection.execute(
                "UPDATE nodes SET state = 'superseded' WHERE id = ?",
                (previous,),
            )
            self._insert_edge(
                "supersedes",
                node_id,
                previous,
                "orchestrator",
                round_number,
            )
            self._event(
                round_number,
                "intent_superseded",
                source,
                {"old": previous, "new": node_id},
            )
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
                    if proposal.type not in AGENT_NODE_TYPES:
                        raise ValueError(
                            f"unsupported agent node type: {proposal.type}"
                        )
                    refs[proposal.ref] = self._merge_node(
                        proposal.type,
                        proposal.content,
                        source,
                        round_number,
                    )

                resolved = known | set(refs.values())
                for edge in update.edges:
                    if edge.type not in EDGE_TYPES or edge.type == "supersedes":
                        raise ValueError(f"unsupported edge type: {edge.type}")
                    from_node = refs.get(edge.from_ref, edge.from_ref)
                    to_node = refs.get(edge.to_ref, edge.to_ref)
                    if from_node not in resolved or to_node not in resolved:
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
                ON CONFLICT(run_id, round_number) DO UPDATE SET
                    status = 'merged'
                """,
                (self.run_id, round_number),
            )

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
                    run_id,
                    round_number,
                    provider,
                    process_status,
                    protocol_status,
                    exit_code,
                    duration_seconds,
                    stdout_path,
                    stderr_path,
                    work_dir,
                    final_answer
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

    def consensus(
        self,
        round_number: int,
        available_agents: int,
    ) -> str | None:
        rows = self.connection.execute(
            """
            SELECT
                trim(final_answer) AS answer,
                COUNT(DISTINCT provider) AS supporters
            FROM agent_results
            WHERE run_id = ?
              AND round_number = ?
              AND protocol_status = 'valid'
              AND final_answer IS NOT NULL
            GROUP BY trim(final_answer)
            ORDER BY supporters DESC, answer
            """,
            (self.run_id, round_number),
        ).fetchall()
        threshold = 1 if available_agents == 1 else 2
        winners = [
            row["answer"]
            for row in rows
            if row["supporters"] >= threshold
        ]
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
            """
            SELECT id
            FROM nodes
            WHERE run_id = ? AND type = 'goal'
            """,
            (self.run_id,),
        ).fetchone()[0]
        with self.connection:
            hint_id = self._insert_node(
                "hint",
                content,
                "human",
                round_number,
            )
            self._insert_edge(
                "addresses",
                hint_id,
                goal_id,
                "human",
                round_number,
            )
        return hint_id

    def finish(
        self,
        status: str,
        final_answer: str | None,
        round_number: int,
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                UPDATE runs
                SET status = ?, final_answer = ?
                WHERE run_id = ?
                """,
                (status, final_answer, self.run_id),
            )
            self._event(
                round_number,
                "run_finished",
                "orchestrator",
                {"status": status, "final_answer": final_answer},
            )

    def snapshot(self) -> dict[str, object]:
        run = dict(
            self.connection.execute(
                """
                SELECT run_id, status, max_rounds, final_answer
                FROM runs WHERE run_id = ?
                """,
                (self.run_id,),
            ).fetchone()
        )
        node_rows = self.connection.execute(
            """
            SELECT id, type, content, state, created_round
            FROM nodes
            WHERE run_id = ?
            ORDER BY created_round,
                CASE type
                    WHEN 'goal' THEN 0
                    WHEN 'origin' THEN 1
                    WHEN 'hint' THEN 2
                    WHEN 'fact' THEN 3
                    ELSE 4
                END,
                id
            """,
            (self.run_id,),
        ).fetchall()
        nodes: list[dict[str, object]] = []
        for row in node_rows:
            sources = [
                item[0]
                for item in self.connection.execute(
                    """
                    SELECT DISTINCT source
                    FROM node_provenance
                    WHERE node_id = ?
                    ORDER BY source
                    """,
                    (row["id"],),
                ).fetchall()
            ]
            nodes.append({**dict(row), "sources": sources})
        edges = [
            dict(row)
            for row in self.connection.execute(
                """
                SELECT id, type, from_node, to_node, source, round_number
                FROM edges
                WHERE run_id = ?
                ORDER BY round_number, id
                """,
                (self.run_id,),
            ).fetchall()
        ]
        return {
            "run": run,
            "nodes": nodes,
            "edges": edges,
            "candidates": list(self.candidates()),
        }

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
                INSERT INTO rounds(
                    run_id, round_number, status, snapshot_path
                ) VALUES (?, ?, 'snapshot_ready', ?)
                ON CONFLICT(run_id, round_number) DO UPDATE SET
                    status = excluded.status,
                    snapshot_path = excluded.snapshot_path
                """,
                (self.run_id, round_number, str(path.resolve())),
            )
        return path
