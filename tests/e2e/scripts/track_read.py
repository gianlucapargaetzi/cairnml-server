"""Reads what the server holds for a run, from another process: scalar series, parameters, single values,
console lines, text lines and debug samples. Prints one JSON line. Used by the tracking tests."""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import report  # noqa: E402

from cairnml import Task  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="read a run back")
    parser.add_argument("--task-id", required=True)
    args = parser.parse_args()

    task = Task.get_task(task_id=args.task_id)
    scalars = task.get_reported_scalars()
    series = {}
    for title, by_series in scalars.items():
        for name, data in by_series.items():
            series[f"{title}/{name}"] = {
                "points": len(data["x"]),
                "first_iteration": data["x"][0],
                "last_iteration": data["x"][-1],
                "first_value": data["y"][0],
                "last_value": data["y"][-1],
            }
    params = task.get_parameters()
    summary = task.get_last_scalar_metrics()
    console = task.get_reported_console_output(number_of_reports=5000)
    samples = task.get_debug_samples(title="debug", series="gradient")
    report({
        "task_id": args.task_id,
        "status": str(task.get_status()),
        "series": series,
        "params": {k: v for k, v in params.items() if k.startswith("General/")},
        "single_values": summary.get("Summary", {}),
        "console_has_example_line": any("console line from the example run" in line for line in console),
        "console_lines": len(console),
        "debug_samples": len(samples),
    })


if __name__ == "__main__":
    main()
