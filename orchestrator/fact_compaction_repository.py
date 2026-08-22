import json
import sqlite3

from orchestrator.fact_compaction_candidates import (
    FactCandidate,
    find_fact_compaction_candidates,
)
from orchestrator.persistence_utils import (
    content_fingerprint,
    new_id,
    normalize_text,
    utc_now,
)


class FactCompactionRepository:
    def __init__(
        self,
        connection: sqlite3.Connection,
        project_id: str,
    ) -> None:
        self.connection = connection
        self.project_id = project_id

    def candidates(
        self,
        *,
        min_facts: int = 3,
        max_facts: int = 32,
    ) -> tuple[tuple[FactCandidate, ...], ...]:
        return find_fact_compaction_candidates(
            self.connection,
            self.project_id,
            min_facts=min_facts,
            max_facts=max_facts,
        )

    def apply(
        self,
        source_fact_ids: tuple[str, ...],
        summary: str,
        *,
        generation: int | None = None,
        source: str = "fact-summarizer",
    ) -> str:
        summary_ids = self.apply_groups(
            ((source_fact_ids, summary),),
            generation=generation,
            source=source,
        )
        return summary_ids[0]

    def apply_groups(
        self,
        groups: tuple[tuple[tuple[str, ...], str], ...],
        *,
        generation: int | None = None,
        source: str = "fact-summarizer",
    ) -> tuple[str, ...]:
        if not groups:
            raise ValueError("fact compaction requires at least one group")
        normalized_groups = tuple(
            (
                tuple(dict.fromkeys(str(item).strip() for item in ids)),
                normalize_text(summary),
            )
            for ids, summary in groups
        )
        if any(len(ids) < 2 for ids, _ in normalized_groups):
            raise ValueError("each compaction group requires at least two Facts")
        if any(not summary for _, summary in normalized_groups):
            raise ValueError("each compaction group requires a summary")
        if any(len(summary) > 20_000 for _, summary in normalized_groups):
            raise ValueError("fact compaction summary is too long")

        connection = self.connection
        project_id = self.project_id
        with connection:
            summary_ids: list[str] = []
            archive_ids: set[str] = set()
            for source_fact_ids, summary in normalized_groups:
                summary_id, selected_ids = self._apply_group(
                    source_fact_ids,
                    summary,
                    generation=generation,
                    source=source,
                )
                summary_ids.append(summary_id)
                archive_ids.update(selected_ids)
            for source_id in sorted(archive_ids):
                if self._is_protected(source_id):
                    continue
                connection.execute(
                    """
                    UPDATE nodes SET state = 'archived'
                    WHERE id = ? AND project_id = ? AND type = 'fact'
                    """,
                    (source_id, project_id),
                )
                self._event(
                    "graph.node_updated",
                    source,
                    {
                        "node_id": source_id,
                        "state": "archived",
                        "reason": "fact_compaction",
                    },
                )
            return tuple(summary_ids)

    def _apply_group(
        self,
        source_fact_ids: tuple[str, ...],
        summary: str,
        *,
        generation: int | None,
        source: str,
    ) -> tuple[str, tuple[str, ...]]:
        ids = tuple(dict.fromkeys(str(item).strip() for item in source_fact_ids))
        if len(ids) < 2:
            raise ValueError("fact compaction requires at least two source Facts")
        normalized = normalize_text(summary)
        if not normalized:
            raise ValueError("fact compaction summary must be non-empty")
        connection = self.connection
        project_id = self.project_id
        batch_fingerprint = content_fingerprint(
            "\0".join(sorted(ids)) + "\0" + normalized
        )
        existing = connection.execute(
            """
            SELECT summary_node_id FROM fact_compactions
            WHERE project_id = ? AND batch_fingerprint = ?
            """,
            (project_id, batch_fingerprint),
        ).fetchone()
        if existing is not None:
            return str(existing["summary_node_id"]), ids

        rows = connection.execute(
            """
            SELECT id, generation FROM nodes
            WHERE project_id = ? AND type = 'fact' AND state = 'active'
              AND id IN (%s)
            """ % ",".join("?" for _ in ids),
            (project_id, *ids),
        ).fetchall()
        if len(rows) != len(ids):
            raise ValueError("fact compaction source Fact is not active")
        summary_generation = generation
        if summary_generation is None:
            summary_generation = max(int(row["generation"]) for row in rows) + 1
        summary_id = self._merge_summary(
            source,
            normalized,
            max(0, summary_generation),
        )
        self._link_sources(
            ids,
            summary_id,
            source,
            summary_generation,
        )
        self._project_external_relations(
                ids,
                summary_id,
                source,
                summary_generation,
            )
        connection.execute(
            """
            INSERT INTO fact_compactions(
                id, project_id, batch_fingerprint, summary_node_id,
                source_node_ids_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                new_id("compaction"),
                project_id,
                batch_fingerprint,
                summary_id,
                json.dumps(ids, ensure_ascii=False),
                utc_now(),
            ),
        )
        self._event(
            "graph.fact_compacted",
            source,
            {
                "summary_node_id": summary_id,
                "source_node_ids": list(ids),
                "source_count": len(ids),
            },
        )
        return summary_id, ids

    def _project_external_relations(
        self,
        ids: tuple[str, ...],
        summary_id: str,
        source: str,
        generation: int,
    ) -> None:
        connection = self.connection
        project_id = self.project_id
        placeholders = ",".join("?" for _ in ids)
        relations = connection.execute(
            """
            SELECT type, from_node, to_node FROM edges
            WHERE project_id = ?
              AND type IN ('addresses', 'supports', 'contradicts')
              AND (from_node IN (%s) OR to_node IN (%s))
            """ % (placeholders, placeholders),
            (project_id, *ids, *ids),
        ).fetchall()
        source_ids = set(ids)
        for relation in relations:
            left, right = str(relation["from_node"]), str(relation["to_node"])
            if left in source_ids and right in source_ids:
                continue
            new_left = summary_id if left in source_ids else left
            new_right = summary_id if right in source_ids else right
            duplicate = connection.execute(
                """
                SELECT 1 FROM edges
                WHERE project_id = ? AND type = ?
                  AND from_node = ? AND to_node = ? LIMIT 1
                """,
                (project_id, str(relation["type"]), new_left, new_right),
            ).fetchone()
            if duplicate is None:
                self._insert_relation(
                    str(relation["type"]),
                    new_left,
                    new_right,
                    source,
                    max(0, generation),
                )

    def _link_sources(
        self,
        ids: tuple[str, ...],
        summary_id: str,
        source: str,
        generation: int,
    ) -> None:
        connection = self.connection
        project_id = self.project_id
        for source_id in ids:
            if source_id == summary_id:
                continue
            duplicate = connection.execute(
                """
                SELECT 1 FROM edges
                WHERE project_id = ? AND type = 'derived_from'
                  AND from_node = ? AND to_node = ? LIMIT 1
                """,
                (project_id, summary_id, source_id),
            ).fetchone()
            if duplicate is None:
                self._insert_relation(
                    "derived_from",
                    summary_id,
                    source_id,
                    source,
                    max(0, generation),
                )

    def _is_protected(self, fact_id: str) -> bool:
        row = self.connection.execute(
            """
            SELECT 1
            FROM projects AS project
            WHERE project.id = ? AND (
                project.goal_result_fact_id = ? OR EXISTS (
                    SELECT 1 FROM goal_completion_claims AS claim
                    WHERE claim.fact_node_id = ?
                      AND claim.goal_version = project.goal_version
                )
            )
            """,
            (self.project_id, fact_id, fact_id),
        ).fetchone()
        return row is not None

    def _merge_summary(
        self,
        source: str,
        content: str,
        generation: int,
    ) -> str:
        fingerprint = content_fingerprint(content)
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

        node_id = new_id("fact")
        self.connection.execute(
            """
            INSERT INTO nodes(
                id, project_id, type, content, fingerprint,
                state, generation, created_at
            ) VALUES (?, ?, 'fact', ?, ?, 'active', ?, ?)
            """,
            (
                node_id,
                self.project_id,
                content,
                fingerprint,
                generation,
                utc_now(),
            ),
        )
        self._add_node_provenance(node_id, source, content)
        self._event(
            "graph.node_added",
            source,
            {"node_id": node_id, "type": "fact"},
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
                new_id("provenance"),
                node_id,
                source,
                original_content,
                utc_now(),
            ),
        )

    def _insert_relation(
        self,
        edge_type: str,
        from_node: str,
        to_node: str,
        source: str,
        generation: int,
    ) -> str:
        edge_id = new_id("edge")
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
                utc_now(),
            ),
        )
        self.connection.execute(
            """
            INSERT OR IGNORE INTO edge_provenance(
                id, edge_id, source, created_at
            ) VALUES (?, ?, ?, ?)
            """,
            (new_id("provenance"), edge_id, source, utc_now()),
        )
        self._event(
            "graph.edge_added",
            source,
            {"edge_id": edge_id, "type": edge_type},
        )
        return edge_id

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
                utc_now(),
            ),
        )
        return int(cursor.lastrowid)
