import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile

from orchestrator.store import BlackboardStore
from worker.redaction import redact_text


ACTIVE_AUDIT_STATUSES = frozenset({"queued", "planned", "running"})


def _project_file(project_dir: Path, value: object) -> Path | None:
    try:
        path = Path(str(value)).resolve()
        root = project_dir.resolve()
        path.relative_to(root)
    except (OSError, ValueError):
        return None
    return path


def _redact_value(value: object) -> object:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {str(key): _redact_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_value(item) for item in value]
    return value


def read_log_page(
    project_dir: Path,
    path_value: object,
    *,
    offset: int,
    limit: int,
    retained: bool,
) -> dict[str, object]:
    if offset < 0:
        raise ValueError("log offset must be non-negative")
    if limit <= 0:
        raise ValueError("log limit must be positive")
    path = _project_file(project_dir, path_value)
    if not retained or path is None or not path.is_file():
        return {
            "text": "",
            "offset": offset,
            "next_offset": offset,
            "total_bytes": 0,
            "has_more": False,
            "retained": False,
        }
    try:
        total = path.stat().st_size
        page_offset = min(offset, total)
        with path.open("rb") as handle:
            handle.seek(page_offset)
            raw = handle.read(limit)
    except OSError:
        return {
            "text": "",
            "offset": offset,
            "next_offset": offset,
            "total_bytes": 0,
            "has_more": False,
            "retained": False,
        }
    next_offset = min(total, page_offset + len(raw))
    return {
        "text": redact_text(raw.decode("utf-8", errors="replace")),
        "offset": page_offset,
        "next_offset": next_offset,
        "total_bytes": total,
        "has_more": next_offset < total,
        "retained": True,
    }


def prune_audit_logs(
    store: BlackboardStore,
    project_dir: Path,
    max_retained_log_sets: int,
) -> tuple[str, ...]:
    retained = [
        record
        for record in store.audit_log_records()
        if record["logs_retained"]
        and str(record["status"]) not in ACTIVE_AUDIT_STATUSES
    ]
    overflow = max(0, len(retained) - max_retained_log_sets)
    pruned: list[str] = []
    for record in retained[:overflow]:
        paths = (
            _project_file(project_dir, record["stdout_path"]),
            _project_file(project_dir, record["stderr_path"]),
        )
        if any(path is None for path in paths):
            continue
        deleted = True
        for path in paths:
            assert path is not None
            try:
                path.unlink(missing_ok=True)
            except OSError:
                deleted = False
        if not deleted:
            continue
        store.mark_logs_pruned(str(record["kind"]), str(record["id"]))
        pruned.append(str(record["id"]))
    return tuple(pruned)


def _json_bytes(value: object) -> bytes:
    safe = _redact_value(value)
    return (
        json.dumps(safe, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _audit_state(store: BlackboardStore) -> dict[str, object]:
    reader = BlackboardStore.open(store.path, store.project_id)
    try:
        reader.connection.execute("BEGIN")
        return {
            "snapshot": reader.snapshot(),
            "planning_attempts": reader.planning_attempts(),
            "executions": reader.executions(),
            "provider_health": reader.provider_health(),
            "events": reader.events_after(0),
            "log_records": reader.audit_log_records(),
        }
    finally:
        reader.connection.rollback()
        reader.close()


def export_audit_bundle(
    store: BlackboardStore,
    project_dir: Path,
    max_retained_exports: int,
) -> Path:
    export_dir = project_dir / "audit-exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output = export_dir / f"audit-{stamp}-{uuid4().hex[:8]}.zip"
    state = _audit_state(store)
    records = state["log_records"]
    assert isinstance(records, tuple)
    log_index: list[dict[str, object]] = []

    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr(
            "manifest.json",
            _json_bytes(
                {
                    "format": "qinyuan-audit-v1",
                    "project_id": store.project_id,
                    "exported_at": datetime.now(timezone.utc).isoformat(),
                    "log_redaction": "applied",
                }
            ),
        )
        archive.writestr("snapshot.json", _json_bytes(state["snapshot"]))
        archive.writestr(
            "planning_attempts.json",
            _json_bytes(state["planning_attempts"]),
        )
        archive.writestr("executions.json", _json_bytes(state["executions"]))
        archive.writestr(
            "provider_health.json",
            _json_bytes(state["provider_health"]),
        )
        archive.writestr("events.json", _json_bytes(state["events"]))

        for record in records:
            entry: dict[str, object] = {
                "kind": record["kind"],
                "id": record["id"],
                "status": record["status"],
                "logs_retained": bool(record["logs_retained"]),
                "files": [],
            }
            if record["logs_retained"]:
                for stream in ("stdout", "stderr"):
                    path = _project_file(
                        project_dir,
                        record[f"{stream}_path"],
                    )
                    if path is None or not path.is_file():
                        continue
                    try:
                        content = path.read_text(
                            encoding="utf-8",
                            errors="replace",
                        )
                    except OSError:
                        continue
                    archive_name = (
                        f"logs/{record['kind']}/{record['id']}/{stream}.txt"
                    )
                    archive.writestr(
                        archive_name,
                        redact_text(content).encode("utf-8"),
                    )
                    entry["files"].append(archive_name)
            log_index.append(entry)
        archive.writestr("logs/index.json", _json_bytes(log_index))

    exports = sorted(
        export_dir.glob("audit-*.zip"),
        key=lambda path: (path.stat().st_mtime_ns, path.name),
        reverse=True,
    )
    for stale in exports[max_retained_exports:]:
        try:
            stale.unlink()
        except OSError:
            pass
    return output
