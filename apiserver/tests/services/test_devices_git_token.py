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


def bind(monkeypatch, answer=None, refuse=None):
    """The task's repository is recorded and read as bll/repositories says; the binding and its answer are faked."""
    source = SimpleNamespace(host="github.com", path="acme/proj")
    binding = SimpleNamespace(credential="app-1")
    monkeypatch.setattr(devices.repositories_bll, "discover", lambda company, repository, user, project: binding)
    monkeypatch.setattr(devices.repositories_bll, "source_of", lambda b: source)

    def token_for_job(src, bnd):
        if refuse:
            raise errors.bad_request.ValidationError(refuse)
        return answer

    monkeypatch.setattr(devices.repositories_bll, "token_for_job", token_for_job)


def test_a_job_token_for_an_existing_task_gets_the_task_repository_token(monkeypatch, job_token):
    monkeypatch.setattr(devices, "Task", SimpleNamespace(objects=FakeTasks(task())))
    bind(monkeypatch, {"token": "t", "expires_at": None, "username": "x-access-token",
                       "url": "https://github.com/acme/proj"})

    response = devices.git_token(job_token, None, GitTokenRequest(task="t1"))

    assert response["token"] == "t"
    assert response["username"] == "x-access-token"


def test_a_repository_that_cannot_be_read_is_refused_with_the_reason(monkeypatch, job_token):
    monkeypatch.setattr(devices, "Task", SimpleNamespace(objects=FakeTasks(task())))
    bind(monkeypatch, refuse="github.com/acme/proj: The repository is private and no credential is assigned")

    with pytest.raises(errors.bad_request.ValidationError, match="no credential is assigned"):
        devices.git_token(job_token, None, GitTokenRequest(task="t1"))


def test_a_repository_cloned_without_a_credential_gets_a_successful_answer_with_no_token(monkeypatch, job_token):
    monkeypatch.setattr(devices, "Task", SimpleNamespace(objects=FakeTasks(task())))
    answer = {"token": None, "expires_at": None, "username": None, "url": "https://github.com/acme/proj"}
    bind(monkeypatch, answer)

    assert devices.git_token(job_token, None, GitTokenRequest(task="t1")) == answer


# A dataset: a repository the job asks for by its URL, in the request. It is read as the project's binding says, like code.

def dataset_request(repository="https://github.com/acme/cars"):
    return GitTokenRequest(task="t1", repository=repository)


class Client:
    """The App client of a resolution: verify_dataset is faked, so only its use is recorded."""


def bind_dataset(monkeypatch, answer=None, refuse=None, client=None, verify=None, refreshed=None):
    source = SimpleNamespace(host="github.com", path="acme/cars")
    binding = SimpleNamespace(credential=None)
    monkeypatch.setattr(devices, "Task", SimpleNamespace(objects=FakeTasks(task())))
    monkeypatch.setattr(devices.repositories_bll, "discover", lambda company, repository, user, project: binding)
    monkeypatch.setattr(devices.repositories_bll, "source_of", lambda b: source)
    monkeypatch.setattr(devices.repositories_bll, "resolve", lambda src, b: SimpleNamespace(state="assigned"))

    def token_for_job(src, b, resolution=None):
        if refuse:
            raise errors.bad_request.ValidationError(refuse)
        return answer

    monkeypatch.setattr(devices.repositories_bll, "token_for_job", token_for_job)
    monkeypatch.setattr(devices.repositories_bll, "github_client", lambda resolution, src: client)
    monkeypatch.setattr(devices, "verify_dataset", verify or (lambda app, repo: None))
    monkeypatch.setattr(devices, "_refresh_dataset_entry", lambda app, repo: (refreshed if refreshed is not None else []).append(repo))


TOKEN = {"token": "t", "expires_at": "later", "username": "x-access-token", "url": "https://github.com/acme/cars"}


def test_a_dataset_is_read_with_the_projects_binding_and_its_layout_is_checked(monkeypatch, job_token):
    checked, refreshed = [], []
    bind_dataset(monkeypatch, TOKEN, client=Client(), verify=lambda app, repo: checked.append(repo), refreshed=refreshed)

    assert devices.git_token(job_token, None, dataset_request())["token"] == "t"
    assert checked == ["acme/cars"] and refreshed == ["acme/cars"]


def test_a_dataset_that_is_not_a_cairnml_dataset_is_refused_with_the_reason(monkeypatch, job_token):
    def verify(app, repo):
        raise errors.bad_request.ValidationError("acme/cars is not a CairnML dataset: dataset.yaml is missing")

    bind_dataset(monkeypatch, TOKEN, client=Client(), verify=verify)
    with pytest.raises(errors.bad_request.ValidationError, match="not a CairnML dataset"):
        devices.git_token(job_token, None, dataset_request())


def test_a_dataset_the_binding_cannot_read_is_refused_with_the_reason(monkeypatch, job_token):
    bind_dataset(monkeypatch, refuse="github.com/acme/cars: The repository is private and no credential is assigned")
    with pytest.raises(errors.bad_request.ValidationError, match="no credential is assigned"):
        devices.git_token(job_token, None, dataset_request())


def test_a_dataset_cloned_without_a_credential_gets_no_token_and_no_layout_check(monkeypatch, job_token):
    checked = []
    answer = {"token": None, "expires_at": None, "username": None, "url": "https://github.com/acme/cars"}
    bind_dataset(monkeypatch, answer, client=None, verify=lambda app, repo: checked.append(repo))

    assert devices.git_token(job_token, None, dataset_request()) == answer
    assert checked == []


def test_an_address_that_is_not_a_hosted_repository_is_refused(monkeypatch, job_token):
    bind_dataset(monkeypatch, TOKEN)
    monkeypatch.setattr(devices.repositories_bll, "discover", lambda company, repository, user, project: None)
    with pytest.raises(errors.bad_request.ValidationError, match="not a hosted git repository"):
        devices.git_token(job_token, None, dataset_request("/home/me/data"))
