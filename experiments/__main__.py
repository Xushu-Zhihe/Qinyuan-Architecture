import argparse
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from experiments.config import ExperimentConfigError, load_experiment_config
from experiments.runner import run_experiment_suite
from worker.config import ConfigError, load_provider_configs


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "experiments" / "configs" / "deterministic_smoke.json"
DEFAULT_RESULTS = ROOT / "experiments" / "results"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run reproducible blackboard comparison experiments"
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--provider-config", type=Path)
    parser.add_argument(
        "--allow-real-providers",
        action="store_true",
        help="explicitly authorize real CLI/model calls and their cost",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        config = load_experiment_config(args.config.resolve())
        if config.mode == "real":
            if not args.allow_real_providers:
                raise PermissionError(
                    "real mode requires --allow-real-providers"
                )
            if args.provider_config is None:
                raise ValueError("real mode requires --provider-config")
            providers = load_provider_configs(args.provider_config.resolve())
        else:
            providers = ()
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        output = args.output or DEFAULT_RESULTS / f"{config.name}-{stamp}"
        summary = asyncio.run(
            run_experiment_suite(
                config,
                output,
                provider_configs=providers,
                allow_real_providers=args.allow_real_providers,
            )
        )
    except (ConfigError, ExperimentConfigError, OSError, ValueError, PermissionError) as exc:
        print(f"Error: {exc}")
        return 2
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"Artifacts: {output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
