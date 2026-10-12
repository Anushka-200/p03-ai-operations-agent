"""Turn saved eval runs into one comparison table (markdown) for docs/benchmarks.md.

Usage: uv run python -m evals.summarize baseline-4 E3-lean E2-caching ...   (latest file per label)
Only comparisons on the SAME scenario set are fair: the table prints the scenario count and runs.
"""

import json
import statistics
import sys
from pathlib import Path

RESULTS = Path(__file__).resolve().parent / "results"


def latest(label: str) -> dict:
    files = sorted(RESULTS.glob(f"*-{label}.json"))
    if not files:
        raise SystemExit(f"no results for label {label!r} in {RESULTS}")
    return json.loads(files[-1].read_text())


def row(label: str) -> str:
    d = latest(label)
    s, rs = d["summary"], d["results"]
    llm = statistics.mean(r["llm_calls"] for r in rs)
    return (
        f"| {label} | {s['scenarios']}x{s['runs'] // s['scenarios']} | {s['pass_rate']:.1%} | {s['pass_all_runs']}/{s['scenarios']} "
        f"| {s['forbidden_executed']} | {s['avg_tool_calls_per_booking'] or 0:.2f} | {llm:.1f} | ${s['cost_per_run_usd']:.4f} "
        f"| {s['p50_turn_s']:.1f} / {s['p95_turn_s']:.1f} |"
    )


def main(labels: list[str]) -> None:
    print("| arm | scenarios x runs | per-run pass | scenarios passing all runs | forbidden executed | tool calls / booking | model calls / run | $ / conversation | turn p50 / p95 (s) |")
    print("|---|---|---|---|---|---|---|---|---|")
    for label in labels:
        print(row(label))


if __name__ == "__main__":
    main(sys.argv[1:])
