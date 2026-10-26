"""Shows how parameter writes combine: a group write (set_parameters) replaces what was there, and a single write
(set_parameter) adds one parameter. Prints what the run holds after each step."""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import report  # noqa: E402

from cairnml import Task  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="parameter writes")
    parser.add_argument("--project", required=True)
    parser.add_argument("--name", required=True)
    args = parser.parse_args()

    task = Task.init(project_name=args.project, task_name=args.name, reuse_last_task_id=False)
    task.set_parameters({"General/epochs": 3})
    task.set_parameter("General/lr", 0.01)
    kept = {k: v for k, v in task.get_parameters().items() if k.startswith("General/")}
    task.set_parameters({"General/epochs": 3})
    after_group = {k: v for k, v in task.get_parameters().items() if k.startswith("General/")}
    report({"task_id": task.id, "kept": kept, "after_group": after_group})
    task.close()


if __name__ == "__main__":
    main()
