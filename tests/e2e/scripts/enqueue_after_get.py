"""Gets a task, then enqueues it, in one process. Used to show the order-dependence of the enqueue check (KNOWN
DEFECT D6): enqueueing as the first SDK call works, after a service call it is refused."""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import report  # noqa: E402

from cairnml import Task  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="get a task, then enqueue it")
    parser.add_argument("--task-id", required=True)
    args = parser.parse_args()

    Task.get_task(task_id=args.task_id)
    result = Task.enqueue(args.task_id, queue_name="default", force=True)
    report({"queued": result.queued})


if __name__ == "__main__":
    main()
