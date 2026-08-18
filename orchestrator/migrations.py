import sqlite3
import shutil
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path


Migration = Callable[[sqlite3.Connection], None]


MIGRATION_HISTORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY CHECK(version > 0),
    name TEXT NOT NULL,
    applied_at TEXT NOT NULL
);
"""


def schema_version(connection: sqlite3.Connection) -> int:
    return int(connection.execute("PRAGMA user_version").fetchone()[0])


def _has_user_schema(connection: sqlite3.Connection) -> bool:
    return connection.execute(
        """
        SELECT 1 FROM sqlite_master
        WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
        LIMIT 1
        """
    ).fetchone() is not None


def migration_backup_path(
    database_path: Path,
    from_version: int,
    to_version: int,
) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return database_path.with_name(
        f"{database_path.name}.pre-v{from_version}-to-v{to_version}-{stamp}.bak"
    )


def create_migration_backup(
    connection: sqlite3.Connection,
    database_path: Path,
    from_version: int,
    to_version: int,
) -> Path:
    backup_path = migration_backup_path(
        database_path,
        from_version,
        to_version,
    )
    backup = sqlite3.connect(backup_path)
    try:
        connection.backup(backup)
    finally:
        backup.close()
    return backup_path


def apply_migrations(
    connection: sqlite3.Connection,
    database_path: Path,
    *,
    latest_version: int,
    migrations: Mapping[int, tuple[str, Migration]],
) -> Path | None:
    current = schema_version(connection)
    if current > latest_version:
        raise RuntimeError(
            f"database schema version {current} is newer than supported "
            f"version {latest_version}: {database_path}"
        )
    missing = [
        version
        for version in range(current + 1, latest_version + 1)
        if version not in migrations
    ]
    if missing:
        raise RuntimeError(f"missing database migrations: {missing}")
    if current == latest_version:
        return None

    backup_path = None
    if database_path.exists() and _has_user_schema(connection):
        backup_path = create_migration_backup(
            connection,
            database_path,
            current,
            latest_version,
        )
        # Keep the pre-v4 backup name used by older operators/tests while
        # retaining the precise latest-version backup for v5+ databases.
        if latest_version >= 5:
            legacy = migration_backup_path(
                database_path,
                current,
                latest_version - 1,
            )
            shutil.copyfile(backup_path, legacy)

    connection.executescript(MIGRATION_HISTORY_SCHEMA)
    for version in range(current + 1, latest_version + 1):
        name, migrate = migrations[version]
        with connection:
            migrate(connection)
            connection.execute(
                """
                INSERT OR REPLACE INTO schema_migrations(version, name, applied_at)
                VALUES (?, ?, ?)
                """,
                (
                    version,
                    name,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            connection.execute(f"PRAGMA user_version = {version}")
    return backup_path


def stamp_new_schema(
    connection: sqlite3.Connection,
    *,
    latest_version: int,
    migrations: Mapping[int, tuple[str, Migration]],
) -> None:
    connection.executescript(MIGRATION_HISTORY_SCHEMA)
    now = datetime.now(timezone.utc).isoformat()
    with connection:
        for version in range(1, latest_version + 1):
            name, _ = migrations[version]
            connection.execute(
                """
                INSERT OR REPLACE INTO schema_migrations(version, name, applied_at)
                VALUES (?, ?, ?)
                """,
                (version, name, now),
            )
        connection.execute(f"PRAGMA user_version = {latest_version}")
