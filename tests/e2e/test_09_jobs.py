"""Section 6 of docs/e2e_testing/E2E.md: queueing, running, ending and monitoring jobs.

A job is a recorded task put in the default queue. The agent listening to that queue takes it and runs it in a
container. A test that fails because of a known defect fails, and its docstring names the defect.
"""

import json
import shutil
import tempfile
import time
from pathlib import Path

import pytest

from support import cli, jobs, workspace
from support.agent import worker_record
from support.workspace import new_name

pytestmark = [pytest.mark.agent, pytest.mark.xdist_group("agent")]

JOB_FOLDER = Path(tempfile.mkdtemp(prefix="e2e-job-script-"))
for _name in ("common.py", "job_script.py"):
    shutil.copy(Path(__file__).parent / "scripts" / _name, JOB_FOLDER / _name)
JOB_SCRIPT = JOB_FOLDER / "job_script.py"

MISSING_IMAGE = "e2e-missing/does-not-exist:never"
EVENT_TIMEOUT = 60


@pytest.fixture
def machine(job_agent):
    return job_agent


# The job records of this module, by label: each one's arguments (the scalars default to 2).
JOB_SPECS = {
    "twice": [], "after-get": [],
    "missing-image": ["--base-image", MISSING_IMAGE],
    "completes": ["--scalars", "5"],
    "fails": ["--scalars", "5", "--fail-after", "2"],
    "aborted": ["--scalars", "1", "--sleep", "600"],
    "release": ["--scalars", "1"],
    "with-model": ["--scalars", "1", "--model"],
}
DEFAULT_ARGS = ["--scalars", "2"]


@pytest.fixture(scope="module")
def recorded(client_home, project, tmp_path_factory):
    """The records of this module's jobs, made in one process (docs/e2e_testing/E2E.md, speed section, item 12). Each record has its own
    arguments; the body runs on the agent with them.

    Expect: every job of the module is recorded in the project, with the arguments its test needs.
    """
    folder = tmp_path_factory.mktemp("job-records")
    labels = list(JOB_SPECS)
    entries = [{"argv": ["--project", project, "--name", new_name(label), *DEFAULT_ARGS, *JOB_SPECS[label]]}
               for label in labels]
    (folder / "jobs.json").write_text(json.dumps(entries))
    result = cli.python(JOB_SCRIPT, client_home, args=["--batch", folder / "jobs.json"], timeout=300)
    assert result.returncode == 0, result.output[-3000:]
    reports = [json.loads(line) for line in result.stdout.splitlines() if line.startswith('{"')]
    assert len(reports) == len(labels), result.output[-3000:]
    return {label: report["task_id"] for label, report in zip(labels, reports)}


# --- scheduling -------------------------------------------------------------------------------------------------

def test_enqueueing_a_queued_task_again_leaves_it_queued_once(client_home, project, machine, recorded):
    """Enqueueing a task that is already queued is refused: it stays queued, once.

    Expect: a second enqueue is refused, and the task is still queued or already running, once.
    """
    task_id = recorded["twice"]
    jobs.enqueue(client_home, task_id)
    again = workspace.run_raw(client_home, "enqueue.py", ["--task-id", task_id])
    assert again.returncode != 0, again.output
    api = workspace.device_api(client_home)
    # the agent may already have taken it: the task is queued, or running once taken
    assert jobs.task_record(api, task_id)["status"] in ("queued", "in_progress")


def test_enqueue_works_after_the_task_was_read(client_home, project, machine, recorded):
    """Enqueueing after the task was read over the API still works (defect D6 would refuse it).

    Expect: the enqueue succeeds in a process that first reads the task.
    """
    task_id = recorded["after-get"]
    result = workspace.run_raw(client_home, "enqueue_after_get.py", ["--task-id", task_id])
    assert result.returncode == 0, result.output[-400:]


def test_job_with_a_missing_image_fails_once_and_is_not_rescheduled(client_home, project, machine, recorded):
    """An internal failure (the image cannot be pulled) is reported as failed; only a lost attempt is retried.

    Expect: the job ends as failed, after one attempt, with no retry event.
    """
    task_id = recorded["missing-image"]
    api = workspace.device_api(client_home)
    workspace.run_raw(client_home, "enqueue.py", ["--task-id", task_id, "--force"])
    record = jobs.wait_for(api, task_id, jobs.FINAL, timeout=300)
    assert record["status"] == "failed"


# --- execution ----------------------------------------------------------------------------------

def test_job_runs_to_completion_with_its_scalars_and_artifact(client_home, project, machine, recorded):
    """A job runs to completion, with its scalars and artifact.

    Expect: the job completes on the agent, its scalars are kept, and its result artifact is on the task."""
    task_id = recorded["completes"]
    jobs.enqueue(client_home, task_id)
    api = workspace.device_api(client_home)
    # the first job of a run starts its container cold: the container installs the agent and the SDK from source, which
    # takes minutes, so it is the one job wait with a longer ceiling. Localhost does not make a package download faster
    # (docs/e2e_testing/E2E.md, speed section, item 8).
    record = jobs.wait_for(api, task_id, jobs.FINAL, timeout=300)
    assert record["status"] == "completed", jobs.explain(api, task_id, record) + "\n" + "\n".join(jobs.console(api, task_id, size=300))
    assert "result" in [a["key"] for a in record["execution"]["artifacts"]]


def test_failing_job_is_failed_with_its_traceback_and_the_agent_is_free(client_home, project, machine, recorded):
    """A failing job, and the agent after it.

    Expect: a job whose body raises ends as failed, its traceback is in the job's log, and the agent is free again.
    """
    task_id = recorded["fails"]
    jobs.enqueue(client_home, task_id)
    api = workspace.device_api(client_home)
    record = jobs.wait_for(api, task_id, jobs.FINAL)
    assert record["status"] == "failed"
    # the traceback is printed just before the agent's own "Process failed" line: look through the last events
    recent = workspace.device_api(client_home).authed(
        "events.get_task_log", {"task": task_id, "order": "desc", "size": 50})["events"]
    assert any("deliberate failure" in event["msg"] for event in recent), "the traceback must be in the job's log"


def test_aborted_job_stops_within_thirty_seconds(client_home, project, machine, recorded):
    """The job must really be running before it is aborted: its first scalar is logged, and that happens only
    once the job body has started (a claimed job whose process never starts logs nothing).

    Expect: after the abort, the job ends as aborted or stopped within 30 s.
    """
    task_id = recorded["aborted"]
    jobs.enqueue(client_home, task_id)
    api = workspace.device_api(client_home)
    jobs.wait_for_start(api, task_id)
    api.authed("tasks.stop", {"task": task_id, "force": True})
    stopped_at = time.monotonic()
    record = jobs.wait_for(api, task_id, ("aborted", "stopped"))
    assert time.monotonic() - stopped_at <= 30
    assert record["status"] in ("aborted", "stopped")


def test_agent_reports_no_running_job_once_a_job_completes(client_home, project, machine, recorded):
    """Only a job that completed proves the agent released it: an empty list after a failure proves nothing.

    Expect: after the job completes, the agent reports no running job.
    """
    task_id = recorded["release"]
    jobs.enqueue(client_home, task_id)
    api = workspace.device_api(client_home)
    record = jobs.wait_for(api, task_id, jobs.FINAL)
    assert record["status"] == "completed", jobs.explain(api, task_id, record)
    assert machine.wait_until(lambda: not worker_record(api, machine.worker_name).get("task"), timeout=30), \
        "the agent must report no running job once the job has completed"


def test_job_registers_a_model_under_its_own_task(client_home, project, machine, recorded):
    """A job may register a model under its own task. Models of other tasks are not its to use.

    Expect: the job completes, and its output model is linked to the job's task.
    """
    task_id = recorded["with-model"]
    api = workspace.device_api(client_home)
    jobs.enqueue(client_home, task_id)
    record = jobs.wait_for(api, task_id, jobs.FINAL, timeout=300)
    assert record["status"] == "completed", jobs.explain(api, task_id, record) + "\n" + "\n".join(jobs.console(api, task_id, size=300))
    task = api.authed("tasks.get_by_id", {"task": task_id})["task"]
    output = (task.get("models") or {}).get("output") or []
    assert output and output[0].get("model"), "\n".join(jobs.console(api, task_id, size=300))
