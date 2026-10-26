"""The body of a job: what runs on an agent when a task is enqueued.

Options let the tests shape a run: how many scalars it logs, how long it sleeps, whether it fails after a set
number of scalars, and whether it reports the git commit of the checkout it runs in. It always writes one small
artifact, and prints a JSON line at the end.

Recording: run from a client (no agent around it), the script only records the job: its task, its arguments, its base
image. The body runs on the agent, with the same arguments, which are kept on the task in its "Args" section.
`--batch <file>` records several jobs in one process: the file lists {"argv": [...]} entries, one task each
(docs/e2e_testing/E2E.md, speed section, item 12). Each job's arguments are connected to its own task, so no job sees another's.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from cairnml import Task

# A small image that is on the host already, so a job does not depend on a registry pull.
BASE_IMAGE = "python:3.12-slim"
# Arguments that name the task rather than shape the run: they are not kept in the job's Args section.
NAMING = ("project", "name", "batch")


def report(result: dict) -> None:
    """The last line of the job: what it did, as JSON, for the test that ran it."""
    print(json.dumps(result, sort_keys=True))


def git_commit(folder: Path):
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=folder, capture_output=True, text=True, timeout=30)
    except OSError:
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="a job body")
    parser.add_argument("--scalars", type=int, default=10)
    parser.add_argument("--sleep", type=float, default=0.0)
    parser.add_argument("--fail-after", type=int, default=0, help="raise after this many scalars (0: never)")
    parser.add_argument("--git", action="store_true", help="report the commit of the checkout")
    parser.add_argument("--base-image", default=BASE_IMAGE, help="the container image the job runs in")
    parser.add_argument("--project", default="e2e-jobs")
    parser.add_argument("--name", default="job")
    parser.add_argument("--batch", default="", help="record the jobs listed in this file, one task each")
    return parser


def open_task(args: argparse.Namespace) -> Task:
    """The task of one job, with its arguments connected to it. On an agent the connection gives back the values the
    job was recorded with, so the body runs with them."""
    task = Task.init(project_name=args.project, task_name=args.name, reuse_last_task_id=False,
                     auto_connect_arg_parser=False)
    task.set_base_docker(args.base_image)
    shaped = {key: value for key, value in vars(args).items() if key not in NAMING}
    shaped = task.connect(shaped, name="Args")
    for key, value in shaped.items():
        setattr(args, key, value)
    return task


def record(argv, parser: argparse.ArgumentParser) -> None:
    """Records one job from its argument list, in this process: one task, closed before the next one starts."""
    args = parser.parse_args(argv)
    task = open_task(args)
    commit = git_commit(Path(__file__).resolve().parent) if args.git else None
    report({"task_id": task.id, "commit": commit})
    task.close()


def run_body(args: argparse.Namespace) -> None:
    task = open_task(args)
    logger = task.get_logger()
    for step in range(args.scalars):
        logger.report_scalar(title="job", series="progress", value=step, iteration=step)
        # the deliberate failure happens in the job, not when the record is made on the client
        if args.fail_after and step + 1 == args.fail_after:
            raise RuntimeError("deliberate failure in the job script")
        time.sleep(args.sleep / max(args.scalars, 1))
    print("job body finished")

    commit = git_commit(Path(__file__).resolve().parent) if args.git else None
    marker = Path(tempfile.mkstemp(prefix="job-result-", suffix=".txt")[1])
    marker.write_text("result of the job\n")
    task.upload_artifact("result", artifact_object=str(marker), wait_on_upload=True)
    report({"task_id": task.id, "commit": commit})
    task.close()


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.batch:
        # the batch file's entries are argument lists; the batch flag itself is not part of any job
        for entry in json.loads(Path(args.batch).read_text()):
            record(entry["argv"], parser)
        return
    if not os.environ.get("CAIRNML_WORKER_ID"):
        # on the client the script only records the job (item 14 of docs/e2e_testing/E2E.md, speed section)
        record(sys.argv[1:], parser)
        return
    run_body(args)


if __name__ == "__main__":
    main()
