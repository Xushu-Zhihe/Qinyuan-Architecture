import argparse
import json
from pathlib import Path

from experiments.metrics import summarize_results


def analyze_results(directory: Path) -> dict[str, object]:
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    results = [
        json.loads(line)
        for line in (directory / "raw_results.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    summary = summarize_results(
        results,
        concurrent_projects=int(manifest["config"]["budget"]["concurrent_projects"]),
        mode=str(manifest["config"]["mode"]),
    )
    (directory / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Rebuild an experiment summary")
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    summary = analyze_results(args.directory.resolve())
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
