"""The evidence of a failing test, kept in the report folder of the run (cairnml-server#310).

A failure of a test that runs a job is explained only by the logs of the moment it happened, and the stack, the agent's
working folder and its background log are removed when the session ends. So the moment a test phase fails, this module
copies out of the running stack and the agent machines: the API server's log, every agent's daemon log and worker
log, and a note with the failure and the machine's load. They go to `<report folder>/failures/<time>-<test>-<phase>/`.
The junit file of the run is written to the same report folder (conftest.py).

Collecting evidence must never become a second failure: a source that cannot be read is named in the note instead.
"""

import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from . import report as e2e_report

# How long the copy of the server's log may take.
LOG_TIMEOUT_SEC = 120

_stack = None
_started = datetime.now(timezone.utc)
_machines = []  # every agent machine this session started (support/agent.py)


def watch_stack(stack) -> None:
    """The session's stack: its API server's log is copied on a failure."""
    global _stack
    _stack = stack


def watch_machine(machine) -> None:
    """An agent machine: its daemon log and worker log are copied on a failure."""
    _machines.append(machine)


def active() -> bool:
    """Whether this process runs a stack. With pytest-xdist the controller does not: its workers do, and keep their own."""
    return _stack is not None


def folder_for(nodeid: str, phase: str, now: datetime = None) -> Path:
    """The folder of one failure: the time makes a repeat of the same test its own folder."""
    stamp = (now or datetime.now()).strftime("%Y%m%dT%H%M%S")
    name = re.sub(r"[^A-Za-z0-9_.-]+", "_", nodeid).strip("_")
    return e2e_report.REPORT_DIR / "failures" / f"{stamp}-{name[-150:]}-{phase}"


def _load() -> str:
    one, five, fifteen = os.getloadavg()
    return f"load average {one:.2f} {five:.2f} {fifteen:.2f}, {os.cpu_count()} cpus"


def _server_log(folder: Path, notes: list) -> None:
    try:
        result = _stack.compose(
            "logs",
            "--no-color",
            "--timestamps",
            "--since",
            _started.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "apiserver",
            timeout=LOG_TIMEOUT_SEC,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        notes.append(f"the API server's log could not be read: {exc}")
        return
    (folder / "apiserver.log").write_text(result.stdout)
    if result.returncode != 0:
        notes.append(f"docker compose logs exited with {result.returncode}: {result.stderr[-500:]}")


def _machine_logs(folder: Path, notes: list) -> None:
    for machine in _machines:
        try:
            (folder / f"agent-{machine.worker_name}-daemon.log").write_text(machine.log())
            worker = machine.worker_log()
            (folder / f"agent-{machine.worker_name}-worker.log").write_text(
                worker or "(no worker log: it is empty so far, or the daemon never named one)\n"
            )
        except OSError as exc:
            notes.append(f"the log of agent {machine.worker_name} could not be read: {exc}")


def keep(report) -> Path:
    """Copies the evidence of the failed phase `report` (a pytest TestReport) and returns its folder."""
    folder = folder_for(report.nodeid, report.when)
    folder.mkdir(parents=True, exist_ok=True)
    notes = []
    _server_log(folder, notes)
    _machine_logs(folder, notes)
    text = [
        f"test: {report.nodeid}",
        f"phase: {report.when}",
        f"time: {datetime.now().astimezone().isoformat(timespec='seconds')}",
        f"machine: {_load()}",
        f"stack project: {_stack.project}",
        f"agents: {', '.join(m.worker_name for m in _machines) or 'none'}",
        *notes,
        "",
        str(report.longrepr),
    ]
    (folder / "failure.txt").write_text("\n".join(text) + "\n")
    return folder
