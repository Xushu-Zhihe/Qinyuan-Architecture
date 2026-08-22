import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

from orchestrator.fact_compaction_schema import (
    FACT_COMPACTION_REQUIRED_SCHEMA,
    FACT_COMPACTION_SCHEMA,
    migrate_fact_compaction_v6,
)
from orchestrator.health import HEALTH_STATUSES, redact_error
from orchestrator.migrations import apply_migrations, stamp_new_schema
from orchestrator.models import (
    GoalResult,
    GoalResultFact,
    MAX_SUPPORTING_FACT_CONTENT_LENGTH,
    IntentProposal,
    RELATION_EDGE_TYPES,
)
from orchestrator.persistence_utils import (
    content_fingerprint,
    new_id,
    normalize_text,
    raw_fingerprint,
    utc_now,
)


PROVIDER_HEALTH_SCHEMA = """
CREATE TABLE IF NOT EXISTS provider_health (
    provider TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK(
        status IN (
            'healthy', 'unhealthy', 'auth_failed',
            'missing', 'cooldown', 'unknown'
        )
    ),
    consecutive_failures INTEGER NOT NULL DEFAULT 0
        CHECK(consecutive_failures >= 0),
    last_error TEXT,
    checked_at TEXT NOT NULL,
    cooldown_until TEXT,
    updated_at TEXT NOT NULL,
    cli_version TEXT NOT NULL DEFAULT 'unknown'
);
"""

AUDIT_SCHEMA = """
CREATE TABLE IF NOT EXISTS planning_attempts (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    status TEXT NOT NULL,
    snapshot_sequence INTEGER NOT NULL CHECK(snapshot_sequence >= 0),
    queued_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    queue_seconds REAL NOT NULL DEFAULT 0 CHECK(queue_seconds >= 0),
    duration_seconds REAL,
    stdout_path TEXT NOT NULL,
    stderr_path TEXT NOT NULL,
    work_dir TEXT NOT NULL,
    decision_reason TEXT,
    intent_edge_id TEXT REFERENCES intents(edge_id) ON DELETE SET NULL,
    retry_count INTEGER NOT NULL DEFAULT 0 CHECK(retry_count >= 0),
    wait_reasons_json TEXT NOT NULL DEFAULT '[]',
    model TEXT NOT NULL DEFAULT 'unknown',
    cli_version TEXT NOT NULL DEFAULT 'unknown',
    token_usage_json TEXT NOT NULL DEFAULT '"unknown"',
    logs_retained INTEGER NOT NULL DEFAULT 1 CHECK(logs_retained IN (0, 1)),
    execution_backend TEXT NOT NULL DEFAULT 'unknown',
    isolation TEXT NOT NULL DEFAULT 'unknown',
    backend_run_id TEXT,
    cleanup_policy TEXT,
    cleanup_status TEXT NOT NULL DEFAULT 'not_applicable',
    cleanup_detail TEXT,
    backend_metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS planning_attempts_by_project
ON planning_attempts(project_id, queued_at, id);
"""


SCHEMA = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 5000;

CREATE TABLE projects (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    goal_version INTEGER NOT NULL DEFAULT 1,
    goal_result_fact_id TEXT REFERENCES nodes(id),
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
    queued_at TEXT,
    started_at TEXT,
    finished_at TEXT,
    exit_code INTEGER,
    stdout_path TEXT NOT NULL,
    stderr_path TEXT NOT NULL,
    work_dir TEXT NOT NULL,
    error_message TEXT,
    queue_seconds REAL NOT NULL DEFAULT 0,
    duration_seconds REAL,
    retry_count INTEGER NOT NULL DEFAULT 0,
    cancel_reason TEXT,
    wait_reasons_json TEXT NOT NULL DEFAULT '[]',
    model TEXT NOT NULL DEFAULT 'unknown',
    cli_version TEXT NOT NULL DEFAULT 'unknown',
    token_usage_json TEXT NOT NULL DEFAULT '"unknown"',
    logs_retained INTEGER NOT NULL DEFAULT 1,
    retry_of_execution_id TEXT REFERENCES intent_executions(id),
    execution_backend TEXT NOT NULL DEFAULT 'unknown',
    isolation TEXT NOT NULL DEFAULT 'unknown',
    backend_run_id TEXT,
    cleanup_policy TEXT,
    cleanup_status TEXT NOT NULL DEFAULT 'not_applicable',
    cleanup_detail TEXT,
    backend_metadata_json TEXT NOT NULL DEFAULT '{}'
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
    answer_key TEXT NOT NULL,
    answer_fingerprint TEXT NOT NULL,
    goal_version INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE goal_completion_claims (
    id TEXT PRIMARY KEY,
    goal_version INTEGER NOT NULL,
    provider TEXT NOT NULL,
    execution_id TEXT NOT NULL REFERENCES intent_executions(id),
    fact_node_id TEXT NOT NULL REFERENCES nodes(id),
    created_at TEXT NOT NULL
);

CREATE INDEX final_answers_by_consensus
ON final_answers(goal_version, answer_fingerprint, provider);

CREATE TABLE events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    source TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
""" + PROVIDER_HEALTH_SCHEMA + AUDIT_SCHEMA + FACT_COMPACTION_SCHEMA


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
    return new_id(prefix)


def _now() -> str:
    return utc_now()


def _timestamp_after(value: str, seconds: float) -> str:
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        moment = datetime.now(timezone.utc)
    return datetime.fromtimestamp(
        moment.timestamp() + max(0.0, seconds),
        timezone.utc,
    ).isoformat()


BLACKBOARD_SCHEMA_VERSION = 6


def _add_columns(
    connection: sqlite3.Connection,
    table: str,
    additions: dict[str, str],
) -> None:
    columns = {
        str(row[1])
        for row in connection.execute(f"PRAGMA table_info({table})")
    }
    for name, definition in additions.items():
        if name not in columns:
            connection.execute(
                f"ALTER TABLE {table} ADD COLUMN {name} {definition}"
            )


def _migrate_blackboard_v1(connection: sqlite3.Connection) -> None:
    connection.executescript(PROVIDER_HEALTH_SCHEMA)
    _add_columns(
        connection,
        "provider_health",
        {"cli_version": "TEXT NOT NULL DEFAULT 'unknown'"},
    )


def _migrate_blackboard_v2(connection: sqlite3.Connection) -> None:
    connection.executescript(AUDIT_SCHEMA)
    _add_columns(
        connection,
        "intent_executions",
        {
            "queued_at": "TEXT",
            "queue_seconds": "REAL NOT NULL DEFAULT 0",
            "duration_seconds": "REAL",
            "retry_count": "INTEGER NOT NULL DEFAULT 0",
            "cancel_reason": "TEXT",
            "wait_reasons_json": "TEXT NOT NULL DEFAULT '[]'",
            "model": "TEXT NOT NULL DEFAULT 'unknown'",
            "cli_version": "TEXT NOT NULL DEFAULT 'unknown'",
            "token_usage_json": "TEXT NOT NULL DEFAULT '\"unknown\"'",
            "logs_retained": "INTEGER NOT NULL DEFAULT 1",
            "retry_of_execution_id": "TEXT REFERENCES intent_executions(id)",
        },
    )
    connection.execute(
        """
        UPDATE intent_executions
        SET queued_at = COALESCE(queued_at, started_at)
        WHERE queued_at IS NULL
        """
    )


def _migrate_blackboard_v3(connection: sqlite3.Connection) -> None:
    backend_columns = {
        "execution_backend": "TEXT NOT NULL DEFAULT 'unknown'",
        "isolation": "TEXT NOT NULL DEFAULT 'unknown'",
        "backend_run_id": "TEXT",
        "cleanup_policy": "TEXT",
        "cleanup_status": "TEXT NOT NULL DEFAULT 'not_applicable'",
        "cleanup_detail": "TEXT",
        "backend_metadata_json": "TEXT NOT NULL DEFAULT '{}'",
    }
    _add_columns(connection, "intent_executions", backend_columns)
    _add_columns(connection, "planning_attempts", backend_columns)


def _migrate_blackboard_v4(connection: sqlite3.Connection) -> None:
    _add_columns(
        connection,
        "final_answers",
        {
            "answer_key": "TEXT NOT NULL DEFAULT ''",
            "answer_fingerprint": "TEXT NOT NULL DEFAULT ''",
        },
    )
    rows = connection.execute(
        "SELECT id, answer FROM final_answers"
    ).fetchall()
    for row in rows:
        answer_key = _normalize(str(row["answer"]))
        connection.execute(
            """
            UPDATE final_answers
            SET answer_key = ?, answer_fingerprint = ?
            WHERE id = ?
            """,
            (answer_key, _fingerprint(answer_key), str(row["id"])),
        )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS final_answers_by_consensus
        ON final_answers(goal_version, answer_fingerprint, provider)
        """
    )


def _migrate_blackboard_v5(connection: sqlite3.Connection) -> None:
    _add_columns(connection, "projects", {"goal_result_fact_id": "TEXT"})
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS goal_completion_claims (
            id TEXT PRIMARY KEY,
            goal_version INTEGER NOT NULL,
            provider TEXT NOT NULL,
            execution_id TEXT NOT NULL REFERENCES intent_executions(id),
            fact_node_id TEXT NOT NULL REFERENCES nodes(id),
            created_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS goal_completion_claims_lookup "
        "ON goal_completion_claims(goal_version, fact_node_id, provider)"
    )


BLACKBOARD_MIGRATIONS = {
    1: ("provider health state", _migrate_blackboard_v1),
    2: ("planning and execution audit", _migrate_blackboard_v2),
    3: ("execution backend audit", _migrate_blackboard_v3),
    4: ("canonical final answer consensus", _migrate_blackboard_v4),
    5: ("goal result completion claims", _migrate_blackboard_v5),
    6: ("asynchronous fact compaction", migrate_fact_compaction_v6),
}


def _ensure_runtime_schema(
    connection: sqlite3.Connection,
    path: Path,
) -> Path | None:
    backup_path = apply_migrations(
        connection,
        path,
        latest_version=BLACKBOARD_SCHEMA_VERSION,
        migrations=BLACKBOARD_MIGRATIONS,
    )
    _validate_blackboard_schema(connection)
    return backup_path


BLACKBOARD_REQUIRED_SCHEMA = {
    "projects": {"id", "status", "goal_version", "goal_result_fact_id"},
    "provider_health": {"provider", "status", "cli_version"},
    "planning_attempts": {
        "id",
        "queue_seconds",
        "token_usage_json",
        "execution_backend",
        "isolation",
        "cleanup_status",
        "backend_metadata_json",
    },
    "intent_executions": {
        "id",
        "queue_seconds",
        "token_usage_json",
        "retry_of_execution_id",
        "execution_backend",
        "isolation",
        "cleanup_status",
        "backend_metadata_json",
    },
    "final_answers": {"answer_key", "answer_fingerprint"},
    "goal_completion_claims": {
        "id", "goal_version", "provider", "execution_id", "fact_node_id"
    },
    "fact_compactions": FACT_COMPACTION_REQUIRED_SCHEMA,
}


def _validate_blackboard_schema(connection: sqlite3.Connection) -> None:
    missing_tables: list[str] = []
    missing_columns: list[str] = []
    for table, required_columns in BLACKBOARD_REQUIRED_SCHEMA.items():
        columns = {
            str(row[1])
            for row in connection.execute(f"PRAGMA table_info({table})")
        }
        if not columns:
            missing_tables.append(table)
            continue
        missing_columns.extend(
            f"{table}.{column}"
            for column in sorted(required_columns - columns)
        )
    if missing_tables or missing_columns:
        details: list[str] = []
        if missing_tables:
            details.append("missing tables: " + ", ".join(missing_tables))
        if missing_columns:
            details.append("missing columns: " + ", ".join(missing_columns))
        raise RuntimeError("blackboard schema drift detected (" + "; ".join(details) + ")")


def _sanitize_backend_metadata(value: object) -> object:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return redact_error(value, limit=1000)
    if isinstance(value, dict):
        return {
            redact_error(str(key), limit=200): _sanitize_backend_metadata(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_sanitize_backend_metadata(item) for item in value]
    return redact_error(str(value), limit=1000)


def _backend_metadata_json(metadata: dict[str, object] | None) -> str:
    safe = _sanitize_backend_metadata(metadata or {})
    return json.dumps(safe, ensure_ascii=False, sort_keys=True)


def _normalize(content: str) -> str:
    return normalize_text(content)


def _fingerprint(content: str) -> str:
    return content_fingerprint(content)


def _intent_fingerprint(proposal: IntentProposal) -> str:
    material = "\0".join(
        (
            proposal.source_node_id,
            proposal.target_node_id,
            _normalize(proposal.content),
        )
    )
    return raw_fingerprint(material)


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
        try:
            _ensure_runtime_schema(connection, path)
        except Exception:
            connection.close()
            raise
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
        stamp_new_schema(
            connection,
            latest_version=BLACKBOARD_SCHEMA_VERSION,
            migrations=BLACKBOARD_MIGRATIONS,
        )
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

    def current_goal_node_id(self) -> str:
        row = self.connection.execute(
            """
            SELECT id FROM nodes
            WHERE project_id = ? AND type = 'goal' AND state = 'active'
            ORDER BY generation DESC, created_at DESC, id DESC LIMIT 1
            """,
            (self.project_id,),
        ).fetchone()
        if row is None:
            raise ValueError("current Goal is missing")
        return str(row["id"])

    def existing_addresses(self) -> set[tuple[str, str]]:
        return {
            (str(row["from_node"]), str(row["to_node"]))
            for row in self.connection.execute(
                """
                SELECT from_node, to_node FROM edges
                WHERE project_id = ? AND type = 'addresses'
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
            """
            SELECT generation FROM nodes
            WHERE id = ? AND project_id = ? AND state = 'active'
            """,
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

    def _record_backend_cleanup_event(
        self,
        kind: str,
        record_id: str,
        provider: str,
        execution_backend: str,
        cleanup_policy: str | None,
        cleanup_status: str,
        cleanup_detail: str | None,
    ) -> None:
        if execution_backend == "unknown" and cleanup_status == "not_applicable":
            return
        payload: dict[str, object] = {
            "kind": kind,
            "record_id": record_id,
            "execution_backend": redact_error(
                execution_backend or "unknown", limit=100
            ),
            "cleanup_policy": (
                redact_error(cleanup_policy, limit=100)
                if cleanup_policy
                else None
            ),
            "cleanup_status": redact_error(
                cleanup_status or "not_applicable", limit=100
            ),
        }
        if cleanup_detail:
            payload["cleanup_detail"] = redact_error(cleanup_detail, limit=1000)
        self._event("execution_backend.cleanup_finished", provider, payload)

    @staticmethod
    def _decode_audit_row(row: sqlite3.Row) -> dict[str, object]:
        record = dict(row)
        for field in (
            "wait_reasons_json",
            "token_usage_json",
            "backend_metadata_json",
        ):
            if field not in record:
                continue
            value = record.pop(field)
            try:
                record[field.removesuffix("_json")] = json.loads(str(value))
            except (json.JSONDecodeError, TypeError):
                record[field.removesuffix("_json")] = (
                    {} if field == "backend_metadata_json" else "unknown"
                )
        if "logs_retained" in record:
            record["logs_retained"] = bool(record["logs_retained"])
        return record

    def start_planning_attempt(
        self,
        provider: str,
        snapshot_sequence: int,
        stdout_path: str,
        stderr_path: str,
        work_dir: str,
    ) -> str:
        attempt_id = _id("planning")
        queued_at = _now()
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO planning_attempts(
                    id, project_id, provider, status, snapshot_sequence,
                    queued_at, stdout_path, stderr_path, work_dir
                ) VALUES (?, ?, ?, 'queued', ?, ?, ?, ?, ?)
                """,
                (
                    attempt_id,
                    self.project_id,
                    provider,
                    snapshot_sequence,
                    queued_at,
                    stdout_path,
                    stderr_path,
                    work_dir,
                ),
            )
            self._event(
                "planning.queued",
                provider,
                {
                    "planning_attempt_id": attempt_id,
                    "snapshot_sequence": snapshot_sequence,
                },
            )
        return attempt_id

    def mark_planning_started(
        self,
        attempt_id: str,
        queue_seconds: float,
        wait_reasons: tuple[str, ...],
    ) -> None:
        attempt = self.planning_attempt(attempt_id)
        started_at = _timestamp_after(str(attempt["queued_at"]), queue_seconds)
        with self.connection:
            self.connection.execute(
                """
                UPDATE planning_attempts
                SET status = 'running', started_at = ?, queue_seconds = ?,
                    wait_reasons_json = ?
                WHERE id = ?
                """,
                (
                    started_at,
                    max(0.0, queue_seconds),
                    json.dumps(wait_reasons),
                    attempt_id,
                ),
            )
            self._event(
                "scheduler.permit_acquired",
                str(attempt["provider"]),
                {
                    "kind": "planning",
                    "planning_attempt_id": attempt_id,
                    "queue_seconds": round(max(0.0, queue_seconds), 6),
                    "wait_reasons": list(wait_reasons),
                },
            )

    def finish_planning_attempt(
        self,
        attempt_id: str,
        status: str,
        decision_reason: str,
        *,
        intent_id: str | None = None,
        queue_seconds: float = 0.0,
        duration_seconds: float | None = None,
        wait_reasons: tuple[str, ...] = (),
        model: str = "unknown",
        cli_version: str = "unknown",
        token_usage: object = "unknown",
        execution_backend: str = "unknown",
        isolation: str = "unknown",
        backend_run_id: str | None = None,
        cleanup_policy: str | None = None,
        cleanup_status: str = "not_applicable",
        cleanup_detail: str | None = None,
        backend_metadata: dict[str, object] | None = None,
    ) -> None:
        attempt = self.planning_attempt(attempt_id)
        safe_reason = redact_error(decision_reason, limit=1000)
        started_at = attempt.get("started_at") or _timestamp_after(
            str(attempt["queued_at"]),
            queue_seconds,
        )
        with self.connection:
            self.connection.execute(
                """
                UPDATE planning_attempts
                SET status = ?, started_at = ?, finished_at = ?,
                    queue_seconds = ?, duration_seconds = ?,
                    decision_reason = ?, intent_edge_id = ?,
                    wait_reasons_json = ?, model = ?, cli_version = ?,
                    token_usage_json = ?, execution_backend = ?, isolation = ?,
                    backend_run_id = ?, cleanup_policy = ?, cleanup_status = ?,
                    cleanup_detail = ?, backend_metadata_json = ?
                WHERE id = ?
                """,
                (
                    status,
                    started_at,
                    _now(),
                    max(0.0, queue_seconds),
                    duration_seconds,
                    safe_reason,
                    intent_id,
                    json.dumps(wait_reasons),
                    model or "unknown",
                    cli_version or "unknown",
                    json.dumps(token_usage, ensure_ascii=False),
                    redact_error(execution_backend or "unknown", limit=100),
                    redact_error(isolation or "unknown", limit=100),
                    (
                        redact_error(backend_run_id, limit=500)
                        if backend_run_id
                        else None
                    ),
                    (
                        redact_error(cleanup_policy, limit=100)
                        if cleanup_policy
                        else None
                    ),
                    redact_error(cleanup_status or "not_applicable", limit=100),
                    (
                        redact_error(cleanup_detail, limit=1000)
                        if cleanup_detail
                        else None
                    ),
                    _backend_metadata_json(backend_metadata),
                    attempt_id,
                ),
            )
            if status == "cancelled" and wait_reasons:
                self._event(
                    "scheduler.wait_cancelled",
                    str(attempt["provider"]),
                    {
                        "kind": "planning",
                        "planning_attempt_id": attempt_id,
                        "queue_seconds": round(max(0.0, queue_seconds), 6),
                        "wait_reasons": list(wait_reasons),
                        "reason": safe_reason,
                    },
                )
            self._event(
                "planning.finished",
                str(attempt["provider"]),
                {
                    "planning_attempt_id": attempt_id,
                    "status": status,
                    "decision_reason": safe_reason,
                    "intent_id": intent_id,
                    "execution_backend": execution_backend,
                    "isolation": isolation,
                    "cleanup_policy": cleanup_policy,
                    "cleanup_status": cleanup_status,
                },
            )
            self._record_backend_cleanup_event(
                "planning",
                attempt_id,
                str(attempt["provider"]),
                execution_backend,
                cleanup_policy,
                cleanup_status,
                cleanup_detail,
            )

    def planning_attempt(self, attempt_id: str) -> dict[str, object]:
        row = self.connection.execute(
            "SELECT * FROM planning_attempts WHERE id = ? AND project_id = ?",
            (attempt_id, self.project_id),
        ).fetchone()
        if row is None:
            raise KeyError(attempt_id)
        return self._decode_audit_row(row)

    def planning_attempts(self) -> tuple[dict[str, object], ...]:
        return tuple(
            self._decode_audit_row(row)
            for row in self.connection.execute(
                """
                SELECT * FROM planning_attempts
                WHERE project_id = ? ORDER BY queued_at, id
                """,
                (self.project_id,),
            ).fetchall()
        )

    def start_execution(
        self,
        intent_id: str,
        provider: str,
        stdout_path: str,
        stderr_path: str,
        work_dir: str,
        *,
        retry_count: int = 0,
        retry_of_execution_id: str | None = None,
        defer_start: bool = False,
    ) -> ExecutionRecord:
        execution_id = _id("execution")
        queued_at = _now()
        initial_status = "planned" if defer_start else "running"
        started_at = None if defer_start else queued_at
        with self.connection:
            exists = self.connection.execute(
                "SELECT 1 FROM intents WHERE edge_id = ?",
                (intent_id,),
            ).fetchone()
            if exists is None:
                raise KeyError(intent_id)
            if retry_of_execution_id is not None:
                retry_source = self.connection.execute(
                    """
                    SELECT intent_edge_id FROM intent_executions
                    WHERE id = ?
                    """,
                    (retry_of_execution_id,),
                ).fetchone()
                if retry_source is None:
                    raise KeyError(retry_of_execution_id)
                if retry_source["intent_edge_id"] != intent_id:
                    raise ValueError("retry source belongs to another intent")
            self.connection.execute(
                """
                INSERT INTO intent_executions(
                    id, intent_edge_id, provider, status, started_at,
                    stdout_path, stderr_path, work_dir, queued_at, retry_count,
                    retry_of_execution_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    execution_id,
                    intent_id,
                    provider,
                    initial_status,
                    started_at,
                    stdout_path,
                    stderr_path,
                    work_dir,
                    queued_at,
                    retry_count,
                    retry_of_execution_id,
                ),
            )
            self._event(
                "scheduler.wait_started",
                provider,
                {
                    "kind": "execution",
                    "intent_id": intent_id,
                    "execution_id": execution_id,
                    "retry_count": retry_count,
                    "retry_of_execution_id": retry_of_execution_id,
                },
            )
            if retry_of_execution_id is not None:
                self._event(
                    "intent.execution_retried",
                    provider,
                    {
                        "intent_id": intent_id,
                        "execution_id": execution_id,
                        "retry_of_execution_id": retry_of_execution_id,
                        "retry_count": retry_count,
                    },
                )
            if not defer_start:
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
            initial_status,
        )

    def mark_execution_started(
        self,
        execution_id: str,
        queue_seconds: float,
        wait_reasons: tuple[str, ...],
    ) -> None:
        execution = self.execution(execution_id)
        queued_at = str(execution.get("queued_at") or execution["started_at"])
        with self.connection:
            self.connection.execute(
                """
                UPDATE intent_executions
                SET status = 'running', started_at = ?, queue_seconds = ?,
                    wait_reasons_json = ?
                WHERE id = ?
                """,
                (
                    _timestamp_after(queued_at, queue_seconds),
                    max(0.0, queue_seconds),
                    json.dumps(wait_reasons),
                    execution_id,
                ),
            )
            self._event(
                "scheduler.permit_acquired",
                str(execution["provider"]),
                {
                    "kind": "execution",
                    "intent_id": execution["intent_edge_id"],
                    "execution_id": execution_id,
                    "queue_seconds": round(max(0.0, queue_seconds), 6),
                    "wait_reasons": list(wait_reasons),
                },
            )
            self._event(
                "intent.execution_started",
                str(execution["provider"]),
                {
                    "intent_id": execution["intent_edge_id"],
                    "execution_id": execution_id,
                },
            )

    def execution(self, execution_id: str) -> dict[str, object]:
        row = self.connection.execute(
            "SELECT * FROM intent_executions WHERE id = ?",
            (execution_id,),
        ).fetchone()
        if row is None:
            raise KeyError(execution_id)
        return self._decode_audit_row(row)

    def intent(self, intent_id: str) -> IntentRecord:
        row = self.connection.execute(
            """
            SELECT e.id, e.from_node, e.to_node, e.generation, i.content
            FROM edges AS e
            JOIN intents AS i ON i.edge_id = e.id
            WHERE e.id = ? AND e.project_id = ?
            """,
            (intent_id, self.project_id),
        ).fetchone()
        if row is None:
            raise KeyError(intent_id)
        return IntentRecord(
            id=str(row["id"]),
            source_node_id=str(row["from_node"]),
            target_node_id=str(row["to_node"]),
            content=str(row["content"]),
            generation=int(row["generation"]),
        )

    def executions(self) -> tuple[dict[str, object], ...]:
        return tuple(
            self._decode_audit_row(row)
            for row in self.connection.execute(
                """
                SELECT execution.*
                FROM intent_executions AS execution
                JOIN intents ON intents.edge_id = execution.intent_edge_id
                JOIN edges ON edges.id = intents.edge_id
                WHERE edges.project_id = ?
                ORDER BY COALESCE(execution.queued_at, execution.started_at),
                    execution.id
                """,
                (self.project_id,),
            ).fetchall()
        )

    def execution_count(self, intent_id: str) -> int:
        row = self.connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM intent_executions
            WHERE intent_edge_id = ?
            """,
            (intent_id,),
        ).fetchone()
        return int(row["count"])

    def finish_execution(
        self,
        execution_id: str,
        update,
        exit_code: int | None,
        status: str,
        *,
        queue_seconds: float = 0.0,
        duration_seconds: float | None = None,
        wait_reasons: tuple[str, ...] = (),
        model: str = "unknown",
        cli_version: str = "unknown",
        token_usage: object = "unknown",
        execution_backend: str = "unknown",
        isolation: str = "unknown",
        backend_run_id: str | None = None,
        cleanup_policy: str | None = None,
        cleanup_status: str = "not_applicable",
        cleanup_detail: str | None = None,
        backend_metadata: dict[str, object] | None = None,
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
                if getattr(update, "protocol_version", 1) == 2:
                    result_ref = getattr(update, "goal_result_ref", None)
                    if not isinstance(result_ref, str) or not result_ref.strip():
                        raise ValueError("goal completion requires one goal_result_ref")
                    result_node_id = refs.get(result_ref or "", result_ref or "")
                    result = self.connection.execute(
                        "SELECT type FROM nodes WHERE id = ? AND project_id = ?",
                        (result_node_id, self.project_id),
                    ).fetchone()
                    if result is None or result["type"] != "fact":
                        raise ValueError("goal result must resolve to a Fact")
                    goal = self.connection.execute(
                        """
                        SELECT id FROM nodes
                        WHERE project_id = ? AND type = 'goal' AND state = 'active'
                        ORDER BY generation DESC, created_at DESC, id DESC LIMIT 1
                        """,
                        (self.project_id,),
                    ).fetchone()
                    if goal is None:
                        raise ValueError("current Goal is missing")
                    address = self.connection.execute(
                        """
                        SELECT 1 FROM edges
                        WHERE project_id = ? AND type = 'addresses'
                          AND from_node = ? AND to_node = ?
                        LIMIT 1
                        """,
                        (self.project_id, result_node_id, str(goal["id"])),
                    ).fetchone()
                    if address is None:
                        raise ValueError("goal result must address the current Goal")
                    goal_version = int(
                        self.connection.execute(
                            "SELECT goal_version FROM projects WHERE id = ?",
                            (self.project_id,),
                        ).fetchone()[0]
                    )
                    existing_claim = self.connection.execute(
                        "SELECT 1 FROM goal_completion_claims WHERE execution_id = ? LIMIT 1",
                        (execution_id,),
                    ).fetchone()
                    if existing_claim is not None:
                        raise ValueError("an execution may declare only one Goal result")
                    self.connection.execute(
                        """
                        INSERT INTO goal_completion_claims(
                            id, goal_version, provider, execution_id,
                            fact_node_id, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            _id("claim"), goal_version, str(execution["provider"]),
                            execution_id, result_node_id, _now(),
                        ),
                    )
                    self._event(
                        "goal.completion_claimed",
                        str(execution["provider"]),
                        {
                            "execution_id": execution_id,
                            "fact_node_id": result_node_id,
                            "goal_version": goal_version,
                        },
                    )
                else:
                    if update.final_answer is None:
                        raise ValueError("goal completion requires final answer")
                    self._record_answer(
                        str(execution["provider"]),
                        update.final_answer,
                        update.answer_key,
                    )

            self.connection.execute(
                """
                UPDATE intent_executions
                SET status = ?, finished_at = ?, exit_code = ?,
                    queue_seconds = ?, duration_seconds = ?,
                    wait_reasons_json = ?, model = ?, cli_version = ?,
                    token_usage_json = ?, execution_backend = ?, isolation = ?,
                    backend_run_id = ?, cleanup_policy = ?, cleanup_status = ?,
                    cleanup_detail = ?, backend_metadata_json = ?
                WHERE id = ?
                """,
                (
                    status,
                    _now(),
                    exit_code,
                    max(0.0, queue_seconds),
                    duration_seconds,
                    json.dumps(wait_reasons),
                    model or "unknown",
                    cli_version or "unknown",
                    json.dumps(token_usage, ensure_ascii=False),
                    redact_error(execution_backend or "unknown", limit=100),
                    redact_error(isolation or "unknown", limit=100),
                    (
                        redact_error(backend_run_id, limit=500)
                        if backend_run_id
                        else None
                    ),
                    (
                        redact_error(cleanup_policy, limit=100)
                        if cleanup_policy
                        else None
                    ),
                    redact_error(cleanup_status or "not_applicable", limit=100),
                    (
                        redact_error(cleanup_detail, limit=1000)
                        if cleanup_detail
                        else None
                    ),
                    _backend_metadata_json(backend_metadata),
                    execution_id,
                ),
            )
            self._event(
                "intent.execution_finished",
                str(execution["provider"]),
                {
                    "intent_id": intent_id,
                    "execution_id": execution_id,
                    "status": status,
                    "execution_backend": execution_backend,
                    "isolation": isolation,
                    "cleanup_policy": cleanup_policy,
                    "cleanup_status": cleanup_status,
                },
            )
            self._record_backend_cleanup_event(
                "execution",
                execution_id,
                str(execution["provider"]),
                execution_backend,
                cleanup_policy,
                cleanup_status,
                cleanup_detail,
            )
            return tuple(dict.fromkeys(refs.values()))

    def fail_execution(
        self,
        execution_id: str,
        status: str,
        error_message: str,
        exit_code: int | None = None,
        *,
        queue_seconds: float = 0.0,
        duration_seconds: float | None = None,
        cancel_reason: str | None = None,
        wait_reasons: tuple[str, ...] = (),
        model: str = "unknown",
        cli_version: str = "unknown",
        token_usage: object = "unknown",
        execution_backend: str = "unknown",
        isolation: str = "unknown",
        backend_run_id: str | None = None,
        cleanup_policy: str | None = None,
        cleanup_status: str = "not_applicable",
        cleanup_detail: str | None = None,
        backend_metadata: dict[str, object] | None = None,
    ) -> None:
        safe_error = redact_error(error_message, limit=1000)
        safe_cancel = (
            redact_error(cancel_reason, limit=1000)
            if cancel_reason
            else None
        )
        with self.connection:
            execution = self.execution(execution_id)
            self.connection.execute(
                """
                UPDATE intent_executions
                SET status = ?, finished_at = ?, exit_code = ?,
                    error_message = ?, queue_seconds = ?, duration_seconds = ?,
                    cancel_reason = ?, wait_reasons_json = ?, model = ?,
                    cli_version = ?, token_usage_json = ?,
                    execution_backend = ?, isolation = ?, backend_run_id = ?,
                    cleanup_policy = ?, cleanup_status = ?, cleanup_detail = ?,
                    backend_metadata_json = ?
                WHERE id = ?
                """,
                (
                    status,
                    _now(),
                    exit_code,
                    safe_error,
                    max(0.0, queue_seconds),
                    duration_seconds,
                    safe_cancel,
                    json.dumps(wait_reasons),
                    model or "unknown",
                    cli_version or "unknown",
                    json.dumps(token_usage, ensure_ascii=False),
                    redact_error(execution_backend or "unknown", limit=100),
                    redact_error(isolation or "unknown", limit=100),
                    (
                        redact_error(backend_run_id, limit=500)
                        if backend_run_id
                        else None
                    ),
                    (
                        redact_error(cleanup_policy, limit=100)
                        if cleanup_policy
                        else None
                    ),
                    redact_error(cleanup_status or "not_applicable", limit=100),
                    (
                        redact_error(cleanup_detail, limit=1000)
                        if cleanup_detail
                        else None
                    ),
                    _backend_metadata_json(backend_metadata),
                    execution_id,
                ),
            )
            if status == "cancelled" and wait_reasons:
                self._event(
                    "scheduler.wait_cancelled",
                    str(execution["provider"]),
                    {
                        "kind": "execution",
                        "intent_id": execution["intent_edge_id"],
                        "execution_id": execution_id,
                        "queue_seconds": round(max(0.0, queue_seconds), 6),
                        "wait_reasons": list(wait_reasons),
                        "reason": safe_cancel or safe_error,
                    },
                )
            self._event(
                "intent.execution_finished",
                str(execution["provider"]),
                {
                    "intent_id": execution["intent_edge_id"],
                    "execution_id": execution_id,
                    "status": status,
                    "error_message": safe_error,
                    "execution_backend": execution_backend,
                    "isolation": isolation,
                    "cleanup_policy": cleanup_policy,
                    "cleanup_status": cleanup_status,
                },
            )
            self._record_backend_cleanup_event(
                "execution",
                execution_id,
                str(execution["provider"]),
                execution_backend,
                cleanup_policy,
                cleanup_status,
                cleanup_detail,
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

    def record_answer(
        self,
        provider: str,
        answer: str,
        answer_key: str | None = None,
    ) -> None:
        with self.connection:
            self._record_answer(provider, answer, answer_key)

    def _record_answer(
        self,
        provider: str,
        answer: str,
        answer_key: str | None = None,
    ) -> None:
        normalized = answer.strip()
        if not normalized:
            raise ValueError("answer must be non-empty")
        canonical = _normalize(answer_key if answer_key is not None else answer)
        if not canonical:
            raise ValueError("answer_key must be non-empty")
        fingerprint = _fingerprint(canonical)
        goal_version = int(
            self.connection.execute(
                "SELECT goal_version FROM projects WHERE id = ?",
                (self.project_id,),
            ).fetchone()[0]
        )
        self.connection.execute(
            """
            INSERT INTO final_answers(
                id, provider, answer, answer_key, answer_fingerprint,
                goal_version, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _id("answer"),
                provider,
                normalized,
                canonical,
                fingerprint,
                goal_version,
                _now(),
            ),
        )
        self._event(
            "answer.proposed",
            provider,
            {
                "answer": normalized,
                "answer_key": canonical,
                "answer_fingerprint": fingerprint,
                "goal_version": goal_version,
            },
        )

    def consensus_details(
        self,
        available_providers: int,
    ) -> dict[str, object] | None:
        goal_version = int(
            self.connection.execute(
                "SELECT goal_version FROM projects WHERE id = ?",
                (self.project_id,),
            ).fetchone()[0]
        )
        rows = self.connection.execute(
            """
            SELECT answer_fingerprint, MIN(answer_key) AS answer_key,
                   COUNT(DISTINCT provider) AS supporters
            FROM final_answers
            WHERE goal_version = ?
            GROUP BY answer_fingerprint
            ORDER BY supporters DESC, answer_key
            """,
            (goal_version,),
        ).fetchall()
        threshold = 1 if available_providers == 1 else 2
        winners = [
            row
            for row in rows
            if int(row["supporters"]) >= threshold
        ]
        if len(winners) != 1:
            return None
        winner = winners[0]
        display = self.connection.execute(
            """
            SELECT answer FROM final_answers
            WHERE goal_version = ? AND answer_fingerprint = ?
            ORDER BY length(answer) DESC, created_at DESC, id DESC
            LIMIT 1
            """,
            (goal_version, str(winner["answer_fingerprint"])),
        ).fetchone()
        if display is None:
            return None
        return {
            "answer": str(display["answer"]),
            "answer_key": str(winner["answer_key"]),
            "answer_fingerprint": str(winner["answer_fingerprint"]),
            "supporters": int(winner["supporters"]),
            "threshold": threshold,
        }

    def consensus(self, available_providers: int) -> str | None:
        details = self.consensus_details(available_providers)
        return str(details["answer"]) if details is not None else None

    def submission_result(self, fact_id: str) -> GoalResult:
        """Project one active Fact into a submission result without changing consensus."""
        fact_id = fact_id.strip()
        if not fact_id:
            raise ValueError("submission Fact id must be non-empty")
        project = self.connection.execute(
            "SELECT goal_version FROM projects WHERE id = ?",
            (self.project_id,),
        ).fetchone()
        if project is None:
            raise KeyError(self.project_id)
        goal = self.connection.execute(
            """
            SELECT id FROM nodes
            WHERE project_id = ? AND type = 'goal' AND state = 'active'
            ORDER BY generation DESC, created_at DESC, id DESC LIMIT 1
            """,
            (self.project_id,),
        ).fetchone()
        if goal is None:
            raise ValueError("current Goal is missing")
        fact = self.connection.execute(
            """
            SELECT id, content FROM nodes
            WHERE id = ? AND project_id = ? AND type = 'fact' AND state = 'active'
            """,
            (fact_id, self.project_id),
        ).fetchone()
        if fact is None:
            raise ValueError("selected Fact is not active in the current project")
        provenance = tuple(
            str(row["source"])
            for row in self.connection.execute(
                "SELECT DISTINCT source FROM node_provenance WHERE node_id = ? ORDER BY source",
                (fact_id,),
            ).fetchall()
        )
        produced = self.connection.execute(
            """
            SELECT intent_edge_id, execution_id FROM intent_outputs
            WHERE node_id = ? ORDER BY rowid DESC LIMIT 1
            """,
            (fact_id,),
        ).fetchone()
        goal_version = int(project["goal_version"])
        result_fact = GoalResultFact(
            id=fact_id,
            content=str(fact["content"]),
            sources=provenance,
            produced_by_intent_id=(str(produced["intent_edge_id"]) if produced else None),
            produced_by_execution_id=(str(produced["execution_id"]) if produced else None),
        )
        return GoalResult(
            project_id=self.project_id,
            goal_version=goal_version,
            goal_node_id=str(goal["id"]),
            result_ref=f"{self.project_id}:{goal_version}:{fact_id}",
            fact=result_fact,
        )

    def goal_result(
        self,
        available_providers: int | None = None,
    ) -> GoalResult | None:
        consensus = self._goal_result_consensus(available_providers)
        winners = consensus["winners"]
        if len(winners) != 1:
            return None
        winner = winners[0]
        project = consensus["project"]
        goal = consensus["goal"]
        goal_version = int(project["goal_version"])
        threshold = int(consensus["threshold"])
        stored_fact_id = project["goal_result_fact_id"]
        claimed_fact_ids = set(winner["claims_by_fact"])
        if stored_fact_id is not None and str(stored_fact_id) in claimed_fact_ids:
            fact_id = str(stored_fact_id)
        else:
            fact_id = min(
                claimed_fact_ids,
                key=lambda candidate: (
                    -len(winner["claims_by_fact"][candidate]),
                    candidate,
                ),
            )
        fact = self.connection.execute(
            "SELECT id, content FROM nodes WHERE id = ? AND project_id = ? AND type = 'fact' AND state = 'active'",
            (fact_id, self.project_id),
        ).fetchone()
        if fact is None:
            return None
        provenance = tuple(
            str(row["source"])
            for row in self.connection.execute(
                "SELECT DISTINCT source FROM node_provenance WHERE node_id = ? ORDER BY source",
                (fact_id,),
            ).fetchall()
        )
        produced = self.connection.execute(
            """
            SELECT intent_edge_id, execution_id FROM intent_outputs
            WHERE node_id = ? ORDER BY rowid DESC LIMIT 1
            """,
            (fact_id,),
        ).fetchone()
        result_fact = GoalResultFact(
            id=fact_id,
            content=str(fact["content"]),
            sources=provenance,
            produced_by_intent_id=(str(produced["intent_edge_id"]) if produced else None),
            produced_by_execution_id=(str(produced["execution_id"]) if produced else None),
        )
        supporting_rows = self.connection.execute(
            """
            SELECT DISTINCT n.id, n.content
            FROM edges e JOIN nodes n ON n.id = e.from_node
            WHERE e.project_id = ? AND e.to_node = ?
              AND e.type IN ('supports', 'derived_from')
              AND n.type = 'fact' AND n.state = 'active'
            ORDER BY n.created_at DESC, n.id LIMIT 20
            """,
            (self.project_id, fact_id),
        ).fetchall()
        supporting_by_id = {
            str(row["id"]): GoalResultFact(
                id=str(row["id"]),
                content=str(row["content"])[:MAX_SUPPORTING_FACT_CONTENT_LENGTH],
            )
            for row in supporting_rows
            if str(row["id"]) != fact_id
        }
        equivalent_ids = sorted(set(winner["fact_ids"]) - {fact_id})
        if equivalent_ids:
            placeholders = ",".join("?" for _ in equivalent_ids)
            for row in self.connection.execute(
                f"SELECT id, content FROM nodes WHERE id IN ({placeholders}) AND state = 'active' ORDER BY id",
                equivalent_ids,
            ).fetchall():
                supporting_by_id.setdefault(
                    str(row["id"]),
                    GoalResultFact(
                        id=str(row["id"]),
                        content=str(row["content"])[:MAX_SUPPORTING_FACT_CONTENT_LENGTH],
                    ),
                )
        with self.connection:
            self.connection.execute(
                "UPDATE projects SET goal_result_fact_id = ?, updated_at = ? WHERE id = ?",
                (fact_id, _now(), self.project_id),
            )
        return GoalResult(
            project_id=self.project_id,
            goal_version=goal_version,
            goal_node_id=str(goal["id"]),
            result_ref=f"{self.project_id}:{goal_version}:{fact_id}",
            fact=result_fact,
            supporting_facts=tuple(list(supporting_by_id.values())[:20]),
            supporting_providers=tuple(sorted(winner["supporters"])),
            completion_threshold=threshold,
        )

    def goal_result_conflict(
        self,
        available_providers: int | None = None,
    ) -> bool:
        return len(self._goal_result_consensus(available_providers)["winners"]) > 1

    def _goal_result_consensus(
        self,
        available_providers: int | None,
    ) -> dict[str, object]:
        project = self.connection.execute(
            "SELECT goal_version, goal_result_fact_id FROM projects WHERE id = ?",
            (self.project_id,),
        ).fetchone()
        if project is None:
            raise KeyError(self.project_id)
        goal_version = int(project["goal_version"])
        goal = self.connection.execute(
            """
            SELECT id FROM nodes
            WHERE project_id = ? AND type = 'goal' AND state = 'active'
            ORDER BY generation DESC, created_at DESC, id DESC LIMIT 1
            """,
            (self.project_id,),
        ).fetchone()
        if goal is None:
            return {"project": project, "goal": None, "threshold": 1, "winners": []}
        fact_rows = self.connection.execute(
            """
            SELECT id FROM nodes
            WHERE project_id = ? AND type = 'fact' AND state = 'active'
            """,
            (self.project_id,),
        ).fetchall()
        active_facts = {str(row["id"]) for row in fact_rows}
        parents = {fact_id: fact_id for fact_id in active_facts}

        def find(fact_id: str) -> str:
            while parents[fact_id] != fact_id:
                parents[fact_id] = parents[parents[fact_id]]
                fact_id = parents[fact_id]
            return fact_id

        def union(left: str, right: str) -> None:
            left_root = find(left)
            right_root = find(right)
            if left_root != right_root:
                first, second = sorted((left_root, right_root))
                parents[second] = first

        equivalent_rows = self.connection.execute(
            """
            SELECT e.from_node, e.to_node
            FROM edges e
            JOIN nodes source ON source.id = e.from_node
            JOIN nodes target ON target.id = e.to_node
            WHERE e.project_id = ? AND e.type = 'equivalent_to'
              AND source.type = 'fact' AND source.state = 'active'
              AND target.type = 'fact' AND target.state = 'active'
            """,
            (self.project_id,),
        ).fetchall()
        for row in equivalent_rows:
            left = str(row["from_node"])
            right = str(row["to_node"])
            if left in parents and right in parents:
                union(left, right)

        claim_rows = self.connection.execute(
            """
            SELECT fact_node_id, provider FROM goal_completion_claims
            WHERE goal_version = ? ORDER BY fact_node_id, provider
            """,
            (goal_version,),
        ).fetchall()
        components: dict[str, dict[str, object]] = {}
        providers: set[str] = set()
        for row in claim_rows:
            fact_id = str(row["fact_node_id"])
            if fact_id not in parents:
                continue
            provider = str(row["provider"])
            providers.add(provider)
            root = find(fact_id)
            component = components.setdefault(
                root,
                {"fact_ids": set(), "supporters": set(), "claims_by_fact": {}},
            )
            component["fact_ids"].add(fact_id)
            component["supporters"].add(provider)
            component["claims_by_fact"].setdefault(fact_id, set()).add(provider)
        for fact_id in active_facts:
            root = find(fact_id)
            if root in components:
                components[root]["fact_ids"].add(fact_id)
        provider_count = available_providers if available_providers is not None else max(1, len(providers))
        threshold = 1 if provider_count == 1 else 2
        winners = [
            component
            for component in components.values()
            if len(component["supporters"]) >= threshold
        ]
        winners.sort(key=lambda component: min(component["fact_ids"]))
        return {
            "project": project,
            "goal": goal,
            "threshold": threshold,
            "winners": winners,
        }

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

    def graph_sequence(self) -> int:
        row = self.connection.execute(
            """
            SELECT COALESCE(MAX(sequence), 0) AS sequence
            FROM events
            WHERE type LIKE 'graph.node_%'
               OR type LIKE 'graph.edge_%'
            """
        ).fetchone()
        return int(row["sequence"])

    def append_event(
        self,
        event_type: str,
        source: str,
        payload: dict[str, object],
    ) -> dict[str, object]:
        with self.connection:
            sequence = self._event(event_type, source, payload)
        return self.events_after(sequence - 1)[0]

    def ensure_provider_health(self, providers: tuple[str, ...]) -> None:
        now = _now()
        with self.connection:
            self.connection.executemany(
                """
                INSERT OR IGNORE INTO provider_health(
                    provider, status, consecutive_failures,
                    checked_at, updated_at
                ) VALUES (?, 'unknown', 0, ?, ?)
                """,
                ((provider, now, now) for provider in providers),
            )

    def set_provider_health(
        self,
        provider: str,
        status: str,
        *,
        consecutive_failures: int,
        last_error: str | None = None,
        cooldown_until: str | None = None,
        checked_at: str | None = None,
        cli_version: str = "unknown",
    ) -> dict[str, object]:
        if status not in HEALTH_STATUSES:
            raise ValueError(f"invalid provider health status: {status}")
        if consecutive_failures < 0:
            raise ValueError("consecutive_failures must be non-negative")
        safe_error = redact_error(last_error) if last_error else None
        checked = checked_at or _now()
        updated = _now()
        payload: dict[str, object] = {
            "provider": provider,
            "status": status,
            "consecutive_failures": consecutive_failures,
            "last_error": safe_error,
            "checked_at": checked,
            "cooldown_until": cooldown_until,
            "cli_version": redact_error(cli_version, limit=200) or "unknown",
        }
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO provider_health(
                    provider, status, consecutive_failures, last_error,
                    checked_at, cooldown_until, updated_at, cli_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider) DO UPDATE SET
                    status = excluded.status,
                    consecutive_failures = excluded.consecutive_failures,
                    last_error = excluded.last_error,
                    checked_at = excluded.checked_at,
                    cooldown_until = excluded.cooldown_until,
                    updated_at = excluded.updated_at,
                    cli_version = excluded.cli_version
                """,
                (
                    provider,
                    status,
                    consecutive_failures,
                    safe_error,
                    checked,
                    cooldown_until,
                    updated,
                    payload["cli_version"],
                ),
            )
            self._event("provider.health_changed", provider, payload)
        return self.provider_health_for(provider)

    def provider_health_for(self, provider: str) -> dict[str, object]:
        row = self.connection.execute(
            "SELECT * FROM provider_health WHERE provider = ?",
            (provider,),
        ).fetchone()
        if row is None:
            raise KeyError(provider)
        return dict(row)

    def provider_health(self) -> tuple[dict[str, object], ...]:
        return tuple(
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM provider_health ORDER BY provider"
            ).fetchall()
        )

    def active_execution_count(self) -> int:
        row = self.connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM intent_executions
            WHERE status IN ('planned', 'running')
            """
        ).fetchone()
        return int(row["count"])

    def active_work_count(self) -> int:
        planning = self.connection.execute(
            """
            SELECT COUNT(*) AS count FROM planning_attempts
            WHERE project_id = ? AND status IN ('queued', 'running')
            """,
            (self.project_id,),
        ).fetchone()
        return self.active_execution_count() + int(planning["count"])

    def mark_interrupted(self) -> int:
        now = _now()
        recovered = 0
        with self.connection:
            planning_attempts = self.connection.execute(
                """
                SELECT id, provider FROM planning_attempts
                WHERE project_id = ? AND status IN ('queued', 'running')
                ORDER BY queued_at, id
                """,
                (self.project_id,),
            ).fetchall()
            for attempt in planning_attempts:
                cursor = self.connection.execute(
                    """
                    UPDATE planning_attempts
                    SET status = 'interrupted', finished_at = ?,
                        decision_reason = 'server restarted'
                    WHERE id = ? AND status IN ('queued', 'running')
                    """,
                    (now, attempt["id"]),
                )
                if cursor.rowcount == 1:
                    self._event(
                        "planning.finished",
                        str(attempt["provider"]),
                        {
                            "planning_attempt_id": attempt["id"],
                            "status": "interrupted",
                            "decision_reason": "server restarted",
                            "intent_id": None,
                        },
                    )
            executions = self.connection.execute(
                """
                SELECT id, intent_edge_id, provider
                FROM intent_executions
                WHERE status IN ('planned', 'running')
                ORDER BY started_at, id
                """
            ).fetchall()
            for execution in executions:
                cursor = self.connection.execute(
                    """
                    UPDATE intent_executions
                    SET status = 'interrupted', finished_at = ?,
                        error_message = 'server restarted',
                        cancel_reason = 'server restarted'
                    WHERE id = ? AND status IN ('planned', 'running')
                    """,
                    (now, execution["id"]),
                )
                if cursor.rowcount != 1:
                    continue
                recovered += 1
                self._event(
                    "intent.execution_finished",
                    str(execution["provider"]),
                    {
                        "intent_id": execution["intent_edge_id"],
                        "execution_id": execution["id"],
                        "status": "interrupted",
                        "error_message": "server restarted",
                    },
                )

            project = self.connection.execute(
                """
                SELECT status, final_answer FROM projects
                WHERE id = ?
                """,
                (self.project_id,),
            ).fetchone()
            if project is None:
                raise KeyError(self.project_id)
            if project["status"] == "running":
                self.connection.execute(
                    """
                    UPDATE projects
                    SET status = 'idle', updated_at = ?
                    WHERE id = ? AND status = 'running'
                    """,
                    (now, self.project_id),
                )
                self._event(
                    "project.status_changed",
                    "orchestrator",
                    {
                        "status": "idle",
                        "final_answer": project["final_answer"],
                        "reason": "server restarted",
                    },
                )
        return recovered

    def recovery_summary(self) -> dict[str, object]:
        planning = self.connection.execute(
            """
            SELECT COUNT(*) AS count, MAX(finished_at) AS latest
            FROM planning_attempts
            WHERE project_id = ? AND status = 'interrupted'
                AND decision_reason = 'server restarted'
            """,
            (self.project_id,),
        ).fetchone()
        executions = self.connection.execute(
            """
            SELECT COUNT(*) AS count, MAX(finished_at) AS latest
            FROM intent_executions AS execution
            JOIN intents ON intents.edge_id = execution.intent_edge_id
            JOIN edges ON edges.id = intents.edge_id
            WHERE edges.project_id = ? AND execution.status = 'interrupted'
                AND execution.cancel_reason = 'server restarted'
            """,
            (self.project_id,),
        ).fetchone()
        latest_values = [
            str(value)
            for value in (planning["latest"], executions["latest"])
            if value is not None
        ]
        planning_count = int(planning["count"])
        execution_count = int(executions["count"])
        return {
            "planning_attempts": planning_count,
            "executions": execution_count,
            "total": planning_count + execution_count,
            "latest_at": max(latest_values) if latest_values else None,
        }

    def audit_log_records(self) -> tuple[dict[str, object], ...]:
        records: list[dict[str, object]] = []
        for attempt in self.planning_attempts():
            records.append(
                {
                    "kind": "planning",
                    "id": attempt["id"],
                    "status": attempt["status"],
                    "queued_at": attempt["queued_at"],
                    "stdout_path": attempt["stdout_path"],
                    "stderr_path": attempt["stderr_path"],
                    "logs_retained": attempt["logs_retained"],
                }
            )
        for execution in self.executions():
            records.append(
                {
                    "kind": "execution",
                    "id": execution["id"],
                    "status": execution["status"],
                    "queued_at": execution.get("queued_at") or execution["started_at"],
                    "stdout_path": execution["stdout_path"],
                    "stderr_path": execution["stderr_path"],
                    "logs_retained": execution["logs_retained"],
                }
            )
        return tuple(
            sorted(
                records,
                key=lambda record: (str(record["queued_at"]), str(record["id"])),
            )
        )

    def mark_logs_pruned(self, kind: str, record_id: str) -> None:
        table = (
            "planning_attempts" if kind == "planning" else "intent_executions"
        )
        if kind not in {"planning", "execution"}:
            raise ValueError(f"unsupported audit log kind: {kind}")
        with self.connection:
            self.connection.execute(
                f"UPDATE {table} SET logs_retained = 0 WHERE id = ?",
                (record_id,),
            )

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

    def reopen_project(self) -> int:
        with self.connection:
            project = self.connection.execute(
                """
                SELECT status, goal_version FROM projects WHERE id = ?
                """,
                (self.project_id,),
            ).fetchone()
            if project is None:
                raise KeyError(self.project_id)
            if project["status"] != "achieved":
                raise ValueError("only an achieved project can be reopened")
            if self.active_execution_count() != 0:
                raise ValueError("project still has active executions")
            previous_version = int(project["goal_version"])
            next_version = previous_version + 1
            self.connection.execute(
                """
                    UPDATE projects
                    SET status = 'idle', goal_version = ?, final_answer = NULL,
                        goal_result_fact_id = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (next_version, _now(), self.project_id),
            )
            self._event(
                "project.reopened",
                "human",
                {
                    "previous_goal_version": previous_version,
                    "goal_version": next_version,
                },
            )
            self._event(
                "project.status_changed",
                "orchestrator",
                {
                    "status": "idle",
                    "final_answer": None,
                    "goal_version": next_version,
                },
            )
        return next_version

    def archive_node(self, node_id: str, source: str = "human") -> None:
        with self.connection:
            row = self.connection.execute(
                """
                SELECT n.type, n.state,
                    EXISTS(
                        SELECT 1 FROM node_provenance AS p
                        WHERE p.node_id = n.id AND p.source = 'human'
                    ) AS is_human,
                    EXISTS(
                        SELECT 1 FROM intent_outputs AS output
                        WHERE output.node_id = n.id
                    ) AS is_execution_output
                FROM nodes AS n
                WHERE n.id = ? AND n.project_id = ?
                """,
                (node_id, self.project_id),
            ).fetchone()
            if row is None:
                raise KeyError(node_id)
            if row["state"] == "archived":
                raise ValueError("node is already archived")
            if row["type"] not in {"fact", "hint"} or not row["is_human"]:
                raise ValueError("only human Fact or Hint nodes can be archived")
            if row["is_execution_output"]:
                raise ValueError("node produced by an execution cannot be archived")
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

    def restore_node(self, node_id: str, source: str = "human") -> None:
        with self.connection:
            row = self.connection.execute(
                """
                SELECT type, state FROM nodes
                WHERE id = ? AND project_id = ?
                """,
                (node_id, self.project_id),
            ).fetchone()
            if row is None:
                raise KeyError(node_id)
            if row["state"] != "archived":
                raise ValueError("node is not archived")
            if row["type"] not in {"fact", "hint"}:
                raise ValueError("only Fact or Hint nodes can be restored")
            self.connection.execute(
                "UPDATE nodes SET state = 'active' WHERE id = ?",
                (node_id,),
            )
            self._event(
                "graph.node_updated",
                source,
                {"node_id": node_id, "state": "active"},
            )

    def snapshot(self) -> dict[str, object]:
        connection = sqlite3.connect(self.path, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA query_only = ON")
        reader = BlackboardStore(self.path, connection, self.project_id)
        try:
            connection.execute("BEGIN")
            return reader._snapshot_from_connection()
        finally:
            connection.rollback()
            connection.close()

    def _snapshot_from_connection(self) -> dict[str, object]:
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
                    self._decode_audit_row(item)
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
            "planning_attempts": list(self.planning_attempts()),
            "provider_health": list(self.provider_health()),
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
