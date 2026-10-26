"""Puts a recorded task in a queue. Must be the first SDK call of its process: the SDK learns the server's API level
from its first session, and enqueue depends on it."""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import report  # noqa: E402

from cairnml import Task  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="enqueue a task")
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--queue", default="default")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    result = Task.enqueue(args.task_id, queue_name=args.queue, force=args.force)
    report({"queued": result.queued, "status": (result.fields or {}).get("status")})


if __name__ == "__main__":
    main()
