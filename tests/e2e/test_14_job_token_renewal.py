"""Job tokens: a job that runs longer than its token lasts keeps its access to the server, because the agent renews the
token while the job runs (cairnml-agent issue #3).

The token lifetime is lowered for this module only: the API is restarted with a short lifetime, and back to the default
when the module ends. The data and the other services are kept.
"""

import json
import shutil
import tempfile
from pathlib import Path

import pytest

from support import cli, jobs, workspace
from support.workspace import new_name

pytestmark = [pytest.mark.agent, pytest.mark.docker, pytest.mark.slow]

TOKEN_LIFETIME_SEC = 180
# Longer than the lifetime, so the token the job started with expires while the job still runs.
JOB_SLEEP_SEC = 240

JOB_FOLDER = Path(tempfile.mkdtemp(prefix="e2e-job-token-script-"))
for _name in ("common.py", "job_script.py"):
    shutil.copy(Path(__file__).parent / "scripts" / _name, JOB_FOLDER / _name)
JOB_SCRIPT = JOB_FOLDER / "job_script.py"


@pytest.fixture(scope="module")
def short_token_lifetime(isolated_stack):
    isolated_stack.set_job_token_ttl(TOKEN_LIFETIME_SEC)
    try:
        yield
    finally:
        isolated_stack.set_job_token_ttl(3600)


@pytest.fixture(scope="module")
def long_job(client_home, project, short_token_lifetime, tmp_path_factory):
    """The record of one job that sleeps longer than the token lifetime. It is made after the lifetime is set."""
    folder = tmp_path_factory.mktemp("job-token-record")
    entries = [{"argv": ["--project", project, "--name", new_name("long-job"),
                         "--scalars", "40", "--sleep", str(JOB_SLEEP_SEC)]}]
    (folder / "jobs.json").write_text(json.dumps(entries))
    result = cli.python(JOB_SCRIPT, client_home, args=["--batch", folder / "jobs.json"], timeout=300)
    assert result.returncode == 0, result.output[-3000:]
    reports = [json.loads(line) for line in result.stdout.splitlines() if line.startswith('{"')]
    assert len(reports) == 1, result.output[-3000:]
    return reports[0]["task_id"]


def test_a_job_longer_than_its_token_lifetime_completes_and_its_token_is_renewed(client_home, job_agent, long_job):
    """A job that runs past its first token's expiry completes, and the agent renews its token while it runs.

    Expect: the job completes with its result artifact, which is uploaded after the first token would have expired, and
    the agent's log records a renewal for this task.
    """
    api = workspace.device_api(client_home)
    jobs.enqueue(client_home, long_job)
    record = jobs.wait_for(api, long_job, ("completed", "failed"), timeout=JOB_SLEEP_SEC + 300)
    assert record["status"] == "completed", (
        jobs.explain(api, long_job, record) + "\n" + "\n".join(jobs.console(api, long_job, size=200)[-40:])
    )
    assert "result" in [a["key"] for a in record["execution"]["artifacts"]]
    # the agent writes its own log of the job next to the job's temporary files; the renewals are logged there
    agent_logs = list(Path(tempfile.gettempdir()).glob(f"cairnml-jobs-*/{long_job}.log"))
    agent_log = agent_logs[0].read_text(errors="replace") if agent_logs else ""
    assert f"Job token renewed for task {long_job}" in agent_log, agent_log[-3000:]
