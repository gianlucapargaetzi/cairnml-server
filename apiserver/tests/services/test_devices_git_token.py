from types import SimpleNamespace

import pytest

from apiserver.apierrors import errors
from apiserver.apimodels.devices import GitTokenRequest
from apiserver.services import devices
from apiserver.tests.datachunks.dataset_fakes import DVC, FakeGitHub, good_files


class FakeTasks:
    """Stands in for Task.objects(...): returns the given task, or None when the task was deleted."""

    def __init__(self, task):
        self.task = task

    def __call__(self, **_):
        return SimpleNamespace(first=lambda: self.task)


class FakeApp:
    def installation_token(self, repo):
        return SimpleNamespace(token="t", expires_at=SimpleNamespace(isoformat=lambda: "later"))


@pytest.fixture
def job_token(monkeypatch):
    """Makes the caller a job token for task 't1' and skips the audit log. No repository is public unless a test says so."""
    monkeypatch.setattr(devices.Token, "decode", lambda _token: {"kind": "job", "task": "t1", "device": "dev_1"})
    monkeypatch.setattr(devices, "audit", lambda *args, **kwargs: None)
    monkeypatch.setattr(devices, "build_from_config", lambda: FakeApp())
    monkeypatch.setattr(devices, "public_repo", lambda repo: False)
    # these tests cover the layout check and the App; that the job's user reads the repository on GitHub (R70) is
    # covered by bll/test_app_row_github_access
    monkeypatch.setattr(devices.datasets_bll, "require_app_read_for_job", lambda company, user, repo: None)
    return SimpleNamespace(authorization="Bearer x", get_header=lambda header, default=None: default,
                           HEADER_REAL_IP="X-Real-IP", real_ip="127.0.0.1")


def task(repository="https://github.com/acme/proj.git"):
    return SimpleNamespace(id="t1", user="owner", project=None, script=SimpleNamespace(repository=repository))


def use_github(monkeypatch, github, public=()):
    """The App is ``github``; the repositories in ``public`` are public (no token for them)."""
    monkeypatch.setattr(devices, "build_from_config", lambda: github)
    monkeypatch.setattr(devices, "public_repo", lambda repo: repo in public)


def test_a_job_token_for_a_deleted_task_gets_a_clear_bad_request(monkeypatch, job_token):
    monkeypatch.setattr(devices, "Task", SimpleNamespace(objects=FakeTasks(None)))

    with pytest.raises(errors.bad_request.InvalidId) as ctx:
        devices.git_token(job_token, None, GitTokenRequest(task="t1"))

    assert "task not found" in str(ctx.value)


def test_a_job_token_for_an_existing_task_gets_the_task_repository_token(monkeypatch, job_token):
    """A private repository is read with the GitHub App attached to its record (made by this first use)."""
    monkeypatch.setattr(devices, "Task", SimpleNamespace(objects=FakeTasks(task())))
    monkeypatch.setattr(devices, "repo_of", lambda url: "acme/proj")
    monkeypatch.setattr(devices.repositories_bll, "ensure", lambda company, repo, owner, now: SimpleNamespace(
        credential="app-1"))
    monkeypatch.setattr(devices.repositories_bll, "token_for_job", lambda company, record, repo: {
        "token": "t", "expires_at": None, "username": "x-access-token"})
    use_github(monkeypatch, FakeGitHub({}), public=())

    response = devices.git_token(job_token, None, GitTokenRequest(task="t1"))

    assert response["token"] == "t"
    assert response["username"] == "x-access-token"


def test_a_private_repository_with_no_app_attached_is_refused_with_the_reason(monkeypatch, job_token):
    monkeypatch.setattr(devices, "Task", SimpleNamespace(objects=FakeTasks(task())))
    monkeypatch.setattr(devices, "repo_of", lambda url: "acme/proj")
    monkeypatch.setattr(devices.repositories_bll, "ensure", lambda company, repo, owner, now: SimpleNamespace(
        credential=None))
    use_github(monkeypatch, FakeGitHub({}), public=())

    with pytest.raises(errors.bad_request.ValidationError, match="no GitHub App attached"):
        devices.git_token(job_token, None, GitTokenRequest(task="t1"))


def test_a_public_task_repository_gets_a_successful_answer_with_no_token(monkeypatch, job_token):
    monkeypatch.setattr(devices, "Task", SimpleNamespace(objects=FakeTasks(task())))
    github = FakeGitHub({"acme/proj": good_files()})
    use_github(monkeypatch, github, public={"acme/proj"})

    response = devices.git_token(job_token, None, GitTokenRequest(task="t1"))

    assert response == {"token": None, "expires_at": None, "username": None, "url": "https://github.com/acme/proj"}
    assert github.token_requests == []


# A dataset: a repository the job asks for by its URL, in the request.

def dataset_request(repository="https://github.com/acme/cars"):
    return GitTokenRequest(task="t1", repository=repository)


def test_a_job_token_is_issued_for_any_installed_repository_with_a_dataset_yaml(monkeypatch, job_token):
    monkeypatch.setattr(devices, "Task", SimpleNamespace(objects=FakeTasks(task())))
    github = FakeGitHub({"acme/cars": good_files()})
    use_github(monkeypatch, github)

    response = devices.git_token(job_token, None, dataset_request())

    assert response["token"] == "token-for-acme/cars"
    assert response["username"] == "x-access-token"
    assert github.token_requests == ["acme/cars"]


def test_a_job_token_is_refused_for_an_installed_repository_without_dataset_yaml_with_a_clear_message(
        monkeypatch, job_token):
    monkeypatch.setattr(devices, "Task", SimpleNamespace(objects=FakeTasks(task())))
    files = good_files()
    files["dataset.yaml"] = None
    github = FakeGitHub({"acme/notes": files})
    use_github(monkeypatch, github)

    with pytest.raises(errors.bad_request.ValidationError) as ctx:
        devices.git_token(job_token, None, dataset_request("https://github.com/acme/notes"))

    assert "acme/notes is not a CairnML dataset: dataset.yaml is missing on its default branch" in ctx.value.msg
    assert github.token_requests == []


def test_a_job_token_is_refused_for_an_installed_repository_whose_footprint_does_not_hold(monkeypatch, job_token):
    monkeypatch.setattr(devices, "Task", SimpleNamespace(objects=FakeTasks(task())))
    github = FakeGitHub({"acme/cars": good_files(dvc=DVC.replace("2048", "4096"))})
    use_github(monkeypatch, github)

    with pytest.raises(errors.bad_request.ValidationError) as ctx:
        devices.git_token(job_token, None, dataset_request())

    assert "footprint" in str(ctx.value)
    assert github.token_requests == []


def test_a_repository_the_app_is_not_installed_on_gets_no_token(monkeypatch, job_token):
    monkeypatch.setattr(devices, "Task", SimpleNamespace(objects=FakeTasks(task())))
    use_github(monkeypatch, FakeGitHub({}))

    with pytest.raises(errors.bad_request.ValidationError) as ctx:
        devices.git_token(job_token, None, dataset_request())

    assert "the GitHub App is not installed on acme/cars" in str(ctx.value)


def test_a_public_repository_as_a_dataset_gets_no_token_and_the_app_is_not_asked(monkeypatch, job_token):
    monkeypatch.setattr(devices, "Task", SimpleNamespace(objects=FakeTasks(task())))
    github = FakeGitHub({"acme/open-data": good_files()})
    use_github(monkeypatch, github, public={"acme/open-data"})

    response = devices.git_token(job_token, None, dataset_request("https://github.com/acme/open-data"))

    assert response["token"] is None
    assert github.token_requests == []
    assert github.reads == []


def test_a_public_dataset_without_the_app_needs_no_app_and_gets_no_token(monkeypatch, job_token):
    monkeypatch.setattr(devices, "Task", SimpleNamespace(objects=FakeTasks(task())))
    use_github(monkeypatch, None, public={"acme/open-data"})

    response = devices.git_token(job_token, None, dataset_request("https://github.com/acme/open-data"))

    assert response["token"] is None


def test_without_the_app_a_private_dataset_is_refused_as_not_configured(monkeypatch, job_token):
    monkeypatch.setattr(devices, "Task", SimpleNamespace(objects=FakeTasks(task())))
    use_github(monkeypatch, None)

    with pytest.raises(errors.bad_request.ValidationError) as ctx:
        devices.git_token(job_token, None, dataset_request())

    assert "GitHub is not configured" in str(ctx.value)


def test_a_dataset_repository_that_is_not_on_github_is_refused(monkeypatch, job_token):
    monkeypatch.setattr(devices, "Task", SimpleNamespace(objects=FakeTasks(task())))
    use_github(monkeypatch, FakeGitHub({}))

    with pytest.raises(errors.bad_request.ValidationError) as ctx:
        devices.git_token(job_token, None, GitTokenRequest(task="t1", repository="https://gitlab.example.org/a/b"))

    assert "the repository is not on GitHub" in str(ctx.value)


def test_a_job_token_for_a_dataset_refreshes_its_entry_in_the_dataset_list(monkeypatch, job_token):
    monkeypatch.setattr(devices, "Task", SimpleNamespace(objects=FakeTasks(task())))
    use_github(monkeypatch, FakeGitHub({"acme/cars": good_files()}))
    refreshed = []
    monkeypatch.setattr(devices.dataset_cache, "refresh_repository", lambda app, repo: refreshed.append(repo))

    response = devices.git_token(job_token, None, dataset_request())

    assert response["token"] == "token-for-acme/cars"
    assert refreshed == ["acme/cars"]


def test_a_failed_refresh_of_the_dataset_list_does_not_change_the_token_answer(monkeypatch, job_token):
    monkeypatch.setattr(devices, "Task", SimpleNamespace(objects=FakeTasks(task())))
    use_github(monkeypatch, FakeGitHub({"acme/cars": good_files()}))

    def broken(app, repo):
        raise RuntimeError("the cache is unreachable")

    monkeypatch.setattr(devices.dataset_cache, "refresh_repository", broken)

    response = devices.git_token(job_token, None, dataset_request())

    assert response["token"] == "token-for-acme/cars"
