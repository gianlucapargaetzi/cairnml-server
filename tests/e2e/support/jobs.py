"""Queueing jobs and following them. Status is read over REST, so the reading does not depend on the SDK's
API level (see scripts/enqueue.py for why enqueueing must be the first SDK call of its process)."""

import time
from pathlib import Path

from . import workspace

FINAL = ("completed", "failed", "stopped", "aborted", "closed")

# What a job's console says when its container cannot reach the server. The SDK retries these for hours (the agent
# sets api.http.retries.total to 240), so a job that never starts would otherwise wait out the whole timeout.
UNREACHABLE = ("Max retries exceeded", "Failed to establish a new connection", "Connection refused",
               "Name or service not known", "Temporary failure in name resolution")


class JobEnded(AssertionError):
    """The job did not get where the test waited for it: it ended in another status, it cannot reach the server, or it
    made no progress before the timeout. The message names the status, the agent's reason and the step it reached."""


def enqueue(home: Path, task_id: str, *, queue="default", force=True) -> dict:
    args = ["--task-id", task_id, "--queue", queue]
    if force:
        args.append("--force")
    return workspace.run_json(home, "enqueue.py", args, timeout=300)


def task_record(api, task_id: str) -> dict:
    return api.authed("tasks.get_by_id", {"task": task_id})["task"]


def console(api, task_id: str, size: int = 50) -> list:
    """The job's last console lines, oldest first."""
    events = api.authed("events.get_task_log", {"task": task_id, "order": "desc", "size": size})["events"]
    return [event["msg"] for event in reversed(events)]


ERROR_MARKERS = ("ERROR", "Error", "Failed", "failed", "Traceback")


def explain(api, task_id: str, record: dict = None) -> str:
    """The status, the reason the agent gave, and the last error line the job wrote (or its last line, if none):
    the step it reached. Console messages can hold several lines, so they are split first."""
    record = record or task_record(api, task_id)
    lines = [line.strip() for msg in console(api, task_id) for line in msg.splitlines() if line.strip()]
    errors = [line for line in lines if any(marker in line for marker in ERROR_MARKERS)]
    last = (errors or lines or ["no console output"])[-1]
    return "status '{}', reason {!r}, message {!r}, last console line {!r}".format(
        record.get("status"), record.get("status_reason"), record.get("status_message"), last)


def _unreachable(api, task_id: str):
    """The console line that says the container cannot reach the server, or None."""
    for line in console(api, task_id):
        if any(marker in line for marker in UNREACHABLE):
            return line
    return None


def _follow(api, task_id: str, done, what: str, timeout: float, poll: float = 1.0) -> dict:
    """Polls the task until done(record) holds, and returns the record. Fails at once, with the explanation, when the
    job ends first or cannot reach the server before it has started. Fails after the timeout with the same explanation."""
    deadline = time.monotonic() + timeout
    record = {}
    while time.monotonic() < deadline:
        record = task_record(api, task_id)
        if done(record):
            return record
        if record.get("status") in FINAL:
            raise JobEnded(f"{what}: the job ended first, {explain(api, task_id, record)}")
        if not record.get("last_metrics"):
            unreachable = _unreachable(api, task_id)
            if unreachable:
                raise JobEnded(f"{what}: the job's container cannot reach the server, {explain(api, task_id, record)}, "
                               f"console {unreachable!r}")
        time.sleep(poll)
    raise JobEnded(f"{what}: still '{record.get('status')}' after {timeout:.0f} s, {explain(api, task_id, record)}")


def wait_for(api, task_id: str, states, timeout: float = 60) -> dict:
    """Waits until the status is one of states, and returns the record (see _follow for the failures)."""
    return _follow(api, task_id, lambda r: r.get("status") in states,
                   f"waiting for {' or '.join(states)}", timeout)


def wait_for_start(api, task_id: str, timeout: float = 60) -> dict:
    """Waits until the job body has started (its first scalar is logged). A job that never starts fails here with the
    step it reached, instead of timing out in the test that needed it (docs/e2e_testing/E2E.md, speed section, item 8)."""
    return _follow(api, task_id, lambda r: r.get("last_metrics"), "waiting for the job to start", timeout)
