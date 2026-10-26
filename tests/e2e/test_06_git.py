"""Section 4 of docs/e2e_testing/E2E.md: git-managed and unmanaged code, as the run records it, and as a job checks it out.

Repositories here are local: a bare remote on this machine, and a checkout of it. The GitHub-only rules (tokens
for a repository on github.com, a private repository refused) need a GitHub App and a reachable repository; they are
the github-marked tests, which need CAIRNML_E2E_GITHUB_REPO (and CAIRNML_E2E_GITHUB_PRIVATE_REPO for one of them).
"""

import os
import shutil
from pathlib import Path

import pytest

from support import git, jobs, workspace
from support.api import ApiError
from support.workspace import new_name, run_json

SCRIPTS = Path(__file__).parent / "scripts"

# the job tests below need an agent: they share one with the other agent modules (xdist group)
pytestmark = pytest.mark.xdist_group("agent")


@pytest.fixture(scope="module")
def checkout(tmp_path_factory):
    root = tmp_path_factory.mktemp("repo")
    remote, work = git.make_remote_and_checkout(root)
    for name in ("common.py", "job_script.py"):
        shutil.copy(SCRIPTS / name, work / name)
    commit = git.commit_all(work, "add the e2e script")
    git.git(work, "push", "origin", "main")
    return {"remote": remote, "work": work, "commit": commit}


def record_of(client_home, task_id):
    api = workspace.device_api(client_home)
    return jobs.task_record(api, task_id)["script"]


def test_run_from_a_checkout_records_repository_branch_and_commit(client_home, project, checkout):
    """A run from a checkout records its repository, branch and commit.

    Expect: The repository is the remote, the branch is main, and the commit is the checkout's HEAD; the script sees that commit too.
    """
    run = run_json(client_home, str(checkout["work"] / "job_script.py"),
                   ["--project", project, "--name", new_name("git"), "--git", "--scalars", "2"])
    script = record_of(client_home, run["task_id"])
    assert checkout["remote"].as_posix() in script["repository"]
    assert script["branch"] == "main"
    assert script["version_num"] == checkout["commit"]
    assert run["commit"] == checkout["commit"], "the script must see the commit it was run from"


def test_uncommitted_changes_are_recorded_as_a_diff(client_home, project, checkout):
    """Uncommitted changes are recorded as a diff.

    Expect: The diff contains the change, and the recorded commit is still the last one.
    """
    work = checkout["work"]
    marker = "# e2e uncommitted change\n"
    (work / "common.py").write_text((work / "common.py").read_text() + marker)
    try:
        run = run_json(client_home, str(work / "job_script.py"),
                       ["--project", project, "--name", new_name("diff"), "--scalars", "1"])
        script = record_of(client_home, run["task_id"])
        assert "e2e uncommitted change" in script["diff"]
        assert script["version_num"] == checkout["commit"], "the commit is still the last one, not the change"
    finally:
        git.git(work, "checkout", "--", "common.py")


def test_script_outside_git_is_recorded_without_a_repository(client_home, project, tmp_path):
    """A script outside git is recorded without a repository.

    Expect: No repository is recorded, and the script's own code is kept with the run.
    """
    folder = tmp_path / "plain"
    folder.mkdir()
    for name in ("common.py", "job_script.py"):
        shutil.copy(SCRIPTS / name, folder / name)
    run = run_json(client_home, str(folder / "job_script.py"),
                   ["--project", project, "--name", new_name("plain"), "--scalars", "1"])
    script = record_of(client_home, run["task_id"])
    assert not script.get("repository"), "a folder outside git has no repository to record"
    assert "def main" in script["diff"], "the script's own code must be kept with the run"


def test_checkout_without_a_remote_records_commit_but_no_url(client_home, project, tmp_path):
    """A checkout without a remote records its commit, but no URL.

    Expect: No repository URL is recorded, and the commit is recorded.
    """
    local = tmp_path / "local-only"
    local.mkdir()
    git.git(local, "init", "-b", "main")
    for name in ("common.py", "job_script.py"):
        shutil.copy(SCRIPTS / name, local / name)
    commit = git.commit_all(local, "local commit")
    run = run_json(client_home, str(local / "job_script.py"),
                   ["--project", project, "--name", new_name("no-remote"), "--git", "--scalars", "1"])
    script = record_of(client_home, run["task_id"])
    assert not script.get("repository")
    assert script["version_num"] == commit


def test_job_clones_the_recorded_commit_and_reports_it(client_home, project, checkout, job_agent):
    """A job clones the recorded commit and reports it. It needs an agent: the job runs on the session's agent.

    Expect: The job completes, and reports the commit it was recorded with.
    """
    work = checkout["work"]
    # the job's container clones the remote, so the remote is served on an address the container can reach
    with git.served(checkout["remote"].parent) as base:
        git.git(work, "remote", "set-url", "origin", f"{base}/{checkout['remote'].name}")
        try:
            run = run_json(client_home, str(work / "job_script.py"),
                           ["--project", project, "--name", new_name("clone-job"), "--git", "--scalars", "2"])
            jobs.enqueue(client_home, run["task_id"])
            api = workspace.device_api(client_home)
            record = jobs.wait_for(api, run["task_id"], jobs.FINAL)
        finally:
            git.git(work, "remote", "set-url", "origin", checkout["remote"].as_posix())
    assert record["status"] == "completed", jobs.explain(api, run["task_id"], record)
    assert record["script"]["version_num"] == checkout["commit"], "the job must report the commit it cloned"


def test_repository_on_github_is_refused_at_enqueue_without_the_app(client_home, project, checkout):
    """docs/e2e_testing/E2E.md and README: a job whose repository is on GitHub is refused when enqueued unless the GitHub App is set up.

    Expect: The enqueue is refused, and its message names GitHub.
    """
    work = checkout["work"]
    git.git(work, "remote", "set-url", "origin", "https://github.com/e2e-owner/e2e-repo.git")
    try:
        run = run_json(client_home, str(work / "job_script.py"),
                       ["--project", project, "--name", new_name("github-refused"), "--scalars", "1"])
        result = workspace.run_raw(client_home, "enqueue.py", ["--task-id", run["task_id"], "--force"])
        assert result.returncode != 0, "the enqueue must be refused"
        assert "GitHub" in result.output or "github" in result.output
    finally:
        git.git(work, "remote", "set-url", "origin", checkout["remote"].as_posix())


def _github_checkout(root: Path, repo: str) -> Path:
    """A local checkout whose origin is the given github.com repository. Nothing is pushed: the job's own clone
    is the one the test is about."""
    checkout = root / "github-checkout"
    checkout.mkdir()
    git.git(checkout, "init", "-b", "main")
    for name in ("common.py", "job_script.py"):
        shutil.copy(SCRIPTS / name, checkout / name)
    git.commit_all(checkout, "e2e script")
    git.git(checkout, "remote", "add", "origin", f"https://github.com/{repo}.git")
    return checkout


@pytest.mark.github
def test_private_repository_is_refused_at_clone_time(client_home, project, tmp_path):
    """A repository the GitHub App cannot read: the job fails at its clone, with a message, and no token is logged.

    Expect: The job fails at its clone with a message, and no token appears in the message.
    """
    repo = os.environ.get("CAIRNML_E2E_GITHUB_PRIVATE_REPO")
    if not repo:
        pytest.skip("set CAIRNML_E2E_GITHUB_PRIVATE_REPO to a repository this App cannot read")
    checkout = _github_checkout(tmp_path, repo)
    run = run_json(client_home, str(checkout / "job_script.py"),
                   ["--project", project, "--name", new_name("private-repo"), "--git", "--scalars", "1"])
    jobs.enqueue(client_home, run["task_id"])
    api = workspace.device_api(client_home)
    record = jobs.wait_for(api, run["task_id"], jobs.FINAL)
    assert record["status"] == "failed"
    assert "x-access-token" not in str(record.get("status_message", ""))


@pytest.mark.github
def test_a_client_device_cannot_get_a_repository_token(client_home, project, tmp_path):
    """Repository tokens go to a job or to its agent, never to a person's machine.

    Expect: The request for a repository token is refused with 401 or 403.
    """
    repo = os.environ.get("CAIRNML_E2E_GITHUB_REPO")
    if not repo:
        pytest.skip("set CAIRNML_E2E_GITHUB_REPO to a repository the App can read")
    checkout = _github_checkout(tmp_path, repo)
    run = run_json(client_home, str(checkout / "job_script.py"),
                   ["--project", project, "--name", new_name("token-refused"), "--scalars", "1"])
    api = workspace.device_api(client_home)
    with pytest.raises(ApiError) as refused:
        api.authed("devices.git_token", {"task": run["task_id"]})
    assert refused.value.status in (401, 403)
