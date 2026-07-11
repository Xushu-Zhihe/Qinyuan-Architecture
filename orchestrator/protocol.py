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
UPDATE_FIELDS = {"nodes", "edges", "goal_achieved", "final_answer"}
NODE_FIELDS = {"ref", "type", "content"}
EDGE_FIELDS = {"type", "from", "to"}


def render_request(
    snapshot: dict[str, object],
    round_number: int,
    challenge_dir: str | Path | None,
) -> str:
    challenge = str(challenge_dir) if challenge_dir else "not provided"
    return (
        f"You are participating in blackboard round {round_number}.\n"
        f"CTF challenge directory (read-only): {challenge}\n"
        "Read the graph snapshot below. Goal, origin, and hint nodes are "
        "read-only.\n"
        "Only propose fact and intent nodes. Use existing node IDs or "
        "temporary refs from your own nodes when proposing edges.\n"
        "Do not include source, round, or persistent IDs; the orchestrator "
        "assigns them.\n"
        "Write any analysis normally, then finish with one line beginning "
        f"{PREFIX} followed by a JSON object with exactly the keys nodes, "
        "edges, goal_achieved, and final_answer.\n\n"
        "Graph snapshot:\n"
        + json.dumps(snapshot, ensure_ascii=False, indent=2)
        + "\n"
    )


def _error(message: str) -> ProtocolResult:
    return ProtocolResult(
        status="protocol_error",
        update=None,
        error=message,
    )


def _require_fields(
    value: dict[str, object],
    expected: set[str],
    label: str,
) -> None:
    extra = set(value) - expected
    missing = expected - set(value)
    if extra:
        raise ValueError(f"unexpected {label} fields: {sorted(extra)}")
    if missing:
        raise ValueError(f"missing {label} fields: {sorted(missing)}")


def _parse_candidate(
    candidate: str,
    existing_node_ids: set[str],
) -> AgentUpdate:
    payload = json.loads(candidate)
    if not isinstance(payload, dict):
        raise ValueError("update must be an object")
    _require_fields(payload, UPDATE_FIELDS, "update")

    raw_nodes = payload["nodes"]
    raw_edges = payload["edges"]
    achieved = payload["goal_achieved"]
    answer = payload["final_answer"]
    if not isinstance(raw_nodes, list) or not isinstance(raw_edges, list):
        raise ValueError("nodes and edges must be lists")
    if not isinstance(achieved, bool):
        raise ValueError("goal_achieved must be a boolean")
    if answer is not None and (
        not isinstance(answer, str) or not answer.strip()
    ):
        raise ValueError("final_answer must be null or a non-empty string")
    if achieved and answer is None:
        raise ValueError("goal_achieved requires final_answer")

    nodes: list[NodeProposal] = []
    refs: set[str] = set()
    for raw in raw_nodes:
        if not isinstance(raw, dict):
            raise ValueError("node proposal must be an object")
        _require_fields(raw, NODE_FIELDS, "node")
        ref = raw["ref"]
        node_type = raw["type"]
        content = raw["content"]
        if not isinstance(ref, str) or not ref or ref in refs:
            raise ValueError("node ref must be unique and non-empty")
        if node_type not in AGENT_NODE_TYPES:
            raise ValueError("unsupported agent node type")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("node content must be non-empty")
        refs.add(ref)
        nodes.append(NodeProposal(ref, node_type, content.strip()))

    allowed_refs = existing_node_ids | refs
    edges: list[EdgeProposal] = []
    for raw in raw_edges:
        if not isinstance(raw, dict):
            raise ValueError("edge proposal must be an object")
        _require_fields(raw, EDGE_FIELDS, "edge")
        edge_type = raw["type"]
        from_ref = raw["from"]
        to_ref = raw["to"]
        if edge_type not in EDGE_TYPES or edge_type == "supersedes":
            raise ValueError("unsupported edge type")
        if from_ref not in allowed_refs or to_ref not in allowed_refs:
            raise ValueError("unknown node reference")
        edges.append(EdgeProposal(edge_type, from_ref, to_ref))

    return AgentUpdate(
        nodes=tuple(nodes),
        edges=tuple(edges),
        goal_achieved=achieved,
        final_answer=answer.strip() if answer else None,
    )


def parse_update(
    stdout: str,
    existing_node_ids: set[str],
) -> ProtocolResult:
    candidates = [
        line[len(PREFIX) :].strip()
        for line in stdout.splitlines()
        if line.startswith(PREFIX)
    ]
    if not candidates:
        return _error("missing BLACKBOARD_UPDATE")

    last_error = "invalid BLACKBOARD_UPDATE"
    for candidate in reversed(candidates):
        try:
            return ProtocolResult(
                status="valid",
                update=_parse_candidate(candidate, existing_node_ids),
                error=None,
            )
        except (json.JSONDecodeError, ValueError) as exc:
            last_error = str(exc)
    return _error(last_error)
