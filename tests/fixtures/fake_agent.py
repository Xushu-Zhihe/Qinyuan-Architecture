import json
import sys
import time


def section(text: str, start: str, end: str | None = None) -> str:
    value = text.split(start, 1)[1]
    return value.split(end, 1)[0] if end else value


def main() -> int:
    provider = sys.argv[1]
    request = sys.stdin.read()
    fact_content = f"{provider} output"

    if "Assigned intent:\n" not in request:
        snapshot = json.loads(section(request, "Graph snapshot:\n"))
        if any(
            node.get("type") == "fact" and node.get("content") == fact_content
            for node in snapshot["nodes"]
        ):
            print(
                "BLACKBOARD_INTENT: "
                + json.dumps(
                    {"action": "idle", "reason": "deterministic result exists"}
                ),
                flush=True,
            )
            return 0
        source = next(
            (
                node
                for node in snapshot["nodes"]
                if node["type"] == "fact"
            ),
            next(node for node in snapshot["nodes"] if node["type"] == "origin"),
        )
        target = next(node for node in snapshot["nodes"] if node["type"] == "goal")
        print(f"{provider} planning", flush=True)
        print(
            "BLACKBOARD_INTENT: "
            + json.dumps(
                {
                    "action": "plan",
                    "source_node_id": source["id"],
                    "target_node_id": target["id"],
                    "content": f"Produce {provider} evidence",
                }
            ),
            flush=True,
        )
        return 0

    intent = json.loads(
        section(request, "Assigned intent:\n", "\n\nGraph snapshot:\n")
    )
    print(fact_content, flush=True)
    print(f"{provider} diagnostic", file=sys.stderr, flush=True)
    time.sleep(0.15)
    print(
        "BLACKBOARD_RESULT: "
        + json.dumps(
            {
                "intent_id": intent["id"],
                "facts": [{"ref": "result", "content": fact_content}],
                "edges": [],
                "goal_achieved": False,
                "final_answer": None,
            }
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
