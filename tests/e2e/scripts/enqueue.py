"""Puts a recorded task in a queue. Must be the first SDK call of its process: the SDK learns the server's API level
from its first session, and enqueue depends on it."""

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import report  # noqa: E402

from cairnml import Task  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="enqueue a task")
    parser.add_argument("--task-id", required=True)
    # the queue of the suite's job agent (support/jobs.py QUEUE_ENV), the public default queue without one
    parser.add_argument("--queue", default=os.environ.get("CAIRNML_E2E_AGENT_QUEUE", "default"))
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    result = Task.enqueue(args.task_id, queue_name=args.queue, force=args.force)
    report({"queued": result.queued, "status": (result.fields or {}).get("status")})


if __name__ == "__main__":
    main()
