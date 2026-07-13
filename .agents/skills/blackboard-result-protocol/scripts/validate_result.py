import argparse
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from orchestrator.protocol import RESULT_PREFIX, parse_result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate a BLACKBOARD_RESULT JSON payload."
    )
    parser.add_argument("--intent-id", required=True)
    parser.add_argument(
        "--existing-node-id",
        action="append",
        default=[],
    )
    parser.add_argument("--file", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        payload = args.file.read_text(encoding="utf-8").strip()
    except OSError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    result = parse_result(
        f"{RESULT_PREFIX} {payload}",
        assigned_intent_id=args.intent_id,
        existing_node_ids=set(args.existing_node_id),
    )
    if result.status != "valid":
        print(result.error or "invalid result protocol", file=sys.stderr)
        return 1
    print("valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
