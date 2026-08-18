import argparse
import json
import sys
from pathlib import Path


RESULT_FIELDS = {"protocol_version", "intent_id", "facts", "edges", "goal_achieved", "goal_result_ref"}
LEGACY_RESULT_FIELDS = {"intent_id", "facts", "edges", "goal_achieved", "final_answer"}
FACT_FIELDS = {"ref", "content"}
EDGE_FIELDS = {"type", "from", "to"}
EDGE_TYPES = {"supports", "contradicts", "derived_from", "addresses", "similar_to", "equivalent_to"}


def _pair(value: str, label: str) -> tuple[str, str]:
    left, separator, right = value.partition(":")
    if not separator or not left or not right:
        raise ValueError(f"{label} must use <left>:<right>")
    return left, right


def validate_payload(payload: object, *, intent_id: str, goal_node_id: str, existing_nodes: dict[str, str], existing_addresses: set[tuple[str, str]]) -> None:
    if not isinstance(payload, dict):
        raise ValueError("result must be an object")
    legacy = set(payload) == LEGACY_RESULT_FIELDS
    if not legacy and set(payload) != RESULT_FIELDS:
        raise ValueError("result fields must be exactly: " + ", ".join(sorted(RESULT_FIELDS)))
    if not legacy and payload["protocol_version"] != 2:
        raise ValueError("protocol_version must be 2")
    if payload["intent_id"] != intent_id:
        raise ValueError("result must reference the assigned intent")
    if not isinstance(payload["facts"], list) or not isinstance(payload["edges"], list):
        raise ValueError("facts and edges must be lists")
    if not isinstance(payload["goal_achieved"], bool):
        raise ValueError("goal_achieved must be a boolean")
    refs: set[str] = set()
    for fact in payload["facts"]:
        if not isinstance(fact, dict):
            raise ValueError("fact proposal must be an object")
        if set(fact) != FACT_FIELDS:
            raise ValueError("fact fields must be exactly ref and content")
        ref, content = fact["ref"], fact["content"]
        if not isinstance(ref, str) or not ref.strip() or ref in refs:
            raise ValueError("fact ref must be a unique non-empty string")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("fact content must be a non-empty string")
        refs.add(ref)
    known = set(existing_nodes) | refs
    edges: set[tuple[str, str, str]] = set()
    for edge in payload["edges"]:
        if not isinstance(edge, dict) or set(edge) != EDGE_FIELDS:
            raise ValueError("edge fields must be exactly type, from, and to")
        edge_type, from_ref, to_ref = edge["type"], edge["from"], edge["to"]
        if edge_type not in EDGE_TYPES:
            raise ValueError("unsupported relation edge type")
        if from_ref not in known or to_ref not in known:
            raise ValueError("unknown node reference")
        edges.add((edge_type, from_ref, to_ref))
    achieved = payload["goal_achieved"]
    if legacy:
        answer = payload["final_answer"]
        if answer is not None and (not isinstance(answer, str) or not answer.strip()):
            raise ValueError("final_answer must be null or a non-empty string")
        if achieved and answer is None:
            raise ValueError("goal_achieved requires final_answer")
        return
    result_ref = payload["goal_result_ref"]
    if not achieved:
        if result_ref is not None:
            raise ValueError("goal_result_ref must be null when goal is not achieved")
        return
    if not isinstance(result_ref, str) or not result_ref.strip():
        raise ValueError("goal_achieved requires goal_result_ref")
    if result_ref not in refs and existing_nodes.get(result_ref) != "fact":
        raise ValueError("goal_result_ref must reference a Fact")
    if (result_ref, goal_node_id) not in existing_addresses and ("addresses", result_ref, goal_node_id) not in edges:
        raise ValueError("goal result must address the current Goal")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate BLACKBOARD_RESULT v2 JSON.")
    parser.add_argument("--intent-id", required=True)
    parser.add_argument("--goal-node-id")
    parser.add_argument("--existing-node", action="append", default=[])
    parser.add_argument("--existing-node-id", action="append", default=[])
    parser.add_argument("--existing-address", action="append", default=[])
    parser.add_argument("--file", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        text = args.file.read_text(encoding="utf-8") if args.file else sys.stdin.read()
        payload = json.loads(text.strip())
        nodes = dict(_pair(value, "existing node") for value in args.existing_node)
        for node_id in args.existing_node_id:
            nodes.setdefault(node_id, "unknown")
        addresses = {_pair(value, "existing address") for value in args.existing_address}
        goal_node_id = args.goal_node_id or (
            args.existing_node_id[0] if args.existing_node_id else "goal-1"
        )
        nodes[goal_node_id] = "goal"
        validate_payload(payload, intent_id=args.intent_id, goal_node_id=goal_node_id, existing_nodes=nodes, existing_addresses=addresses)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print("valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
