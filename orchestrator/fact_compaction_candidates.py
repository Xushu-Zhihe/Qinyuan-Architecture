import sqlite3
from dataclasses import dataclass
from difflib import SequenceMatcher


@dataclass(frozen=True, slots=True)
class FactCandidate:
    id: str
    content: str
    generation: int


def find_fact_compaction_candidates(
    connection: sqlite3.Connection,
    project_id: str,
    *,
    min_facts: int = 3,
    max_facts: int = 32,
    scan_limit: int = 256,
) -> tuple[tuple[FactCandidate, ...], ...]:
    if min_facts < 2:
        raise ValueError("min_facts must be at least 2")
    if max_facts < min_facts:
        raise ValueError("max_facts must be at least min_facts")
    if scan_limit < max_facts:
        raise ValueError("scan_limit must be at least max_facts")

    rows = connection.execute(
        """
        SELECT n.id, n.content, n.generation
        FROM nodes AS n
        WHERE n.project_id = ? AND n.type = 'fact' AND n.state = 'active'
          AND NOT EXISTS (
              SELECT 1 FROM node_provenance AS p
              WHERE p.node_id = n.id
                AND p.source IN ('fact-summarizer', 'human')
          )
          AND NOT EXISTS (
              SELECT 1 FROM goal_completion_claims AS c
              JOIN projects AS project ON project.id = n.project_id
              WHERE c.fact_node_id = n.id
                AND c.goal_version = project.goal_version
          )
        ORDER BY n.created_at DESC, n.id DESC
        LIMIT ?
        """,
        (project_id, scan_limit),
    ).fetchall()
    facts = {
        str(row["id"]): FactCandidate(
            id=str(row["id"]),
            content=str(row["content"]),
            generation=int(row["generation"]),
        )
        for row in rows
    }
    if len(facts) < min_facts:
        return ()

    adjacency = {fact_id: set() for fact_id in facts}
    for row in connection.execute(
        """
        SELECT from_node, to_node FROM edges
        WHERE project_id = ? AND type = 'similar_to'
        """,
        (project_id,),
    ).fetchall():
        left, right = str(row["from_node"]), str(row["to_node"])
        if left in adjacency and right in adjacency:
            adjacency[left].add(right)
            adjacency[right].add(left)

    fact_items = tuple(facts.items())
    for index, (left_id, left) in enumerate(fact_items):
        for right_id, right in fact_items[index + 1 :]:
            if right_id in adjacency[left_id]:
                continue
            length_ratio = min(len(left.content), len(right.content)) / max(
                len(left.content), len(right.content)
            )
            if length_ratio < 0.6:
                continue
            if SequenceMatcher(None, left.content, right.content).ratio() >= 0.78:
                adjacency[left_id].add(right_id)
                adjacency[right_id].add(left_id)

    groups: list[tuple[FactCandidate, ...]] = []
    unseen = set(facts)
    while unseen:
        root = min(unseen)
        stack = [root]
        unseen.remove(root)
        component: list[str] = []
        while stack:
            current = stack.pop()
            component.append(current)
            for neighbour in adjacency[current]:
                if neighbour in unseen:
                    unseen.remove(neighbour)
                    stack.append(neighbour)
        if len(component) < min_facts:
            continue
        component.sort()
        for offset in range(0, len(component), max_facts):
            batch = component[offset : offset + max_facts]
            if len(batch) >= min_facts:
                groups.append(tuple(facts[item] for item in batch))
    return tuple(groups)
