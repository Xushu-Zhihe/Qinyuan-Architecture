import sqlite3


FACT_COMPACTION_SCHEMA = """
CREATE TABLE fact_compactions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    batch_fingerprint TEXT NOT NULL,
    summary_node_id TEXT NOT NULL REFERENCES nodes(id),
    source_node_ids_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(project_id, batch_fingerprint)
);

CREATE INDEX fact_compactions_by_project
ON fact_compactions(project_id, created_at);
"""


FACT_COMPACTION_REQUIRED_SCHEMA = {
    "id",
    "project_id",
    "batch_fingerprint",
    "summary_node_id",
    "source_node_ids_json",
}


def migrate_fact_compaction_v6(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS fact_compactions (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            batch_fingerprint TEXT NOT NULL,
            summary_node_id TEXT NOT NULL REFERENCES nodes(id),
            source_node_ids_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(project_id, batch_fingerprint)
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS fact_compactions_by_project "
        "ON fact_compactions(project_id, created_at)"
    )
