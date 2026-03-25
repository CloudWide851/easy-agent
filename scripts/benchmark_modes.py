from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from rich.console import Console
from rich.table import Table

from agent_runtime.benchmark import run_default_suite

console = Console()


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark single-agent, sub-agent, graph, and team modes.")
    parser.add_argument("--config", default="easy-agent.yml", help="Base config path")
    parser.add_argument("--repeat", type=int, default=2, help="Runs per mode")
    parser.add_argument("--output", default=".easy-agent/benchmark-report.json", help="Output JSON report path")
    args = parser.parse_args()

    if not os.environ.get("DEEPSEEK_API_KEY"):
        raise SystemExit("DEEPSEEK_API_KEY is required in the environment.")

    report = run_default_suite(args.config, args.repeat)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    table = Table(title="easy-agent benchmark")
    table.add_column("Mode", style="cyan")
    table.add_column("Runs", style="green")
    table.add_column("Successes", style="green")
    table.add_column("Failures", style="red")
    table.add_column("Avg Seconds", style="yellow")
    table.add_column("Avg Tool Calls", style="magenta")
    table.add_column("Avg SubAgent Calls", style="blue")
    for mode, summary in report["summary"].items():
        table.add_row(
            mode,
            str(summary["runs"]),
            str(summary["successes"]),
            str(summary["failures"]),
            str(summary["average_duration_seconds"]),
            str(summary["average_tool_calls"]),
            str(summary["average_subagent_calls"]),
        )
    console.print(table)
    console.print(f"[bold green]Report written to:[/bold green] {output_path}")


if __name__ == "__main__":
    main()


