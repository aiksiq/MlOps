import argparse
import csv
from pathlib import Path
from types import SimpleNamespace

from loadgen import _write_results


def main() -> None:
    parser = argparse.ArgumentParser(description="Aggregate per-client CSV files from one HW11 run.")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--scenario", choices=("balanced", "hot"), required=True)
    parser.add_argument("--policy", choices=("normal", "affinity"), required=True)
    parser.add_argument("--duration", type=float, required=True)
    parser.add_argument("--total-rps", type=float, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with args.input.open(encoding="utf-8-sig", newline="") as file:
        rows = list(csv.DictReader(file))
    for row in rows:
        row["scenario"] = args.scenario
        row["policy"] = args.policy
    _write_results(SimpleNamespace(
        run_id=args.run_id,
        scenario=args.scenario,
        policy=args.policy,
        duration=args.duration,
        total_rps=args.total_rps,
        output=args.output,
    ), rows)


if __name__ == "__main__":
    main()
