import argparse
import asyncio
import sys
from pathlib import Path
from typing import Callable, Sequence

from orchestrator.scheduler import run_orchestration
from worker.backends import build_execution_backend
from worker.config import (
    ConfigError,
    load_audit_config,
    load_coordinator_config,
    load_execution_config,
    load_fact_compaction_config,
    load_provider_configs,
)


DEFAULT_CONFIG = Path(__file__).resolve().parent / "worker" / "providers.json"
DEFAULT_RUNS_ROOT = Path(__file__).resolve().parent / "worker" / "runs"


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run CLI agents through a shared CTF blackboard graph."
    )
    parser.add_argument(
        "--challenge",
        type=Path,
        help="read-only CTF challenge directory",
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--goal", help="outcome the agents must achieve")
    parser.add_argument(
        "--origin",
        help="challenge statement and known conditions",
    )
    parser.add_argument(
        "--hint",
        action="append",
        default=[],
        help="initial human hint; repeatable",
    )
    parser.add_argument("--max-iterations", type=_positive_int, default=5)
    return parser


def run_cli(
    argv: Sequence[str] | None = None,
    input_func: Callable[[str], str] = input,
) -> int:
    args = _build_parser().parse_args(argv)
    challenge_dir = args.challenge.resolve() if args.challenge else None
    if challenge_dir and not challenge_dir.is_dir():
        print(
            f"Error: challenge directory does not exist: {challenge_dir}",
            file=sys.stderr,
        )
        return 1

    goal = args.goal if args.goal is not None else input_func("Goal: ")
    origin = args.origin if args.origin is not None else input_func("Origin: ")
    goal = goal.strip()
    origin = origin.strip()
    if not goal:
        print("Error: goal must not be empty", file=sys.stderr)
        return 1
    if not origin:
        print("Error: origin must not be empty", file=sys.stderr)
        return 1

    try:
        providers = load_provider_configs(args.config.resolve())
        coordinator_config = load_coordinator_config(args.config.resolve())
        audit_config = load_audit_config(args.config.resolve())
        execution_backend = build_execution_backend(
            load_execution_config(args.config.resolve())
        )
        result = asyncio.run(
            run_orchestration(
                providers=providers,
                goal=goal,
                origin=origin,
                hints=tuple(
                    hint.strip() for hint in args.hint if hint.strip()
                ),
                challenge_dir=challenge_dir,
                runs_root=DEFAULT_RUNS_ROOT,
                max_iterations=args.max_iterations,
                coordinator_config=coordinator_config,
                audit_config=audit_config,
                fact_compaction_config=load_fact_compaction_config(
                    args.config.resolve()
                ),
                execute=execution_backend.execute,
            )
        )
    except ConfigError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"Error: cannot create orchestration run: {exc}", file=sys.stderr)
        return 1

    print(f"Status: {result.status}")
    print(f"Generation: {result.highest_generation}")
    if result.final_answer:
        print(f"Final answer: {result.final_answer}")
    elif result.candidates:
        print("Candidate answers:")
        for candidate in result.candidates:
            print(f"- {candidate}")
    print(f"Run directory: {result.run_dir}")
    print(
        f"Blackboard database: {Path(result.run_dir) / 'blackboard.db'}"
    )
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(run_cli())
