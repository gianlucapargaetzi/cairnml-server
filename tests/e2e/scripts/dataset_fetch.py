"""Gets one version of a dataset on another machine and prints the digest of each file it finds. With --task, the
fetch happens inside a run, and the run's id is printed so the test can read what the run recorded."""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import file_digest, report  # noqa: E402

from cairnml import Task  # noqa: E402
from cairnml.datasets import Dataset  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="get a dataset version and check its files")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--project")
    parser.add_argument("--name")
    parser.add_argument("--alias")
    args = parser.parse_args()

    task = Task.init(project_name=args.project, task_name=args.name, reuse_last_task_id=False) if args.project else None
    dataset = Dataset.get(args.repo, args.commit, alias=args.alias)
    files = {
        p.relative_to(dataset.path).as_posix(): file_digest(p) for p in sorted(dataset.path.rglob("*")) if p.is_file()
    }
    report({"commit": dataset.commit, "content_hash": dataset.content_hash, "files": files,
            "task_id": task.id if task else None})
    if task:
        task.close()


if __name__ == "__main__":
    main()
