from types import SimpleNamespace

import pytest

from apiserver.apierrors import errors
from apiserver.apimodels.devices import GitTokenRequest
from apiserver.services import devices


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
    """Makes the caller a job token for task 't1' and skips the audit log."""
    monkeypatch.setattr(devices.Token, "decode", lambda _token: {"kind": "job", "task": "t1", "device": "dev_1"})
    monkeypatch.setattr(devices, "audit", lambda *args, **kwargs: None)
    monkeypatch.setattr(devices, "build_from_config", lambda: FakeApp())
    return SimpleNamespace(authorization="Bearer x")


def task(repository="https://github.com/acme/proj.git"):
    return SimpleNamespace(id="t1", script=SimpleNamespace(repository=repository))


def test_a_job_token_for_a_deleted_task_gets_a_clear_bad_request(monkeypatch, job_token):
    monkeypatch.setattr(devices, "Task", SimpleNamespace(objects=FakeTasks(None)))

    with pytest.raises(errors.bad_request.InvalidId) as ctx:
        devices.git_token(job_token, None, GitTokenRequest(task="t1"))

    assert "task not found" in str(ctx.value)


def test_a_job_token_for_an_existing_task_gets_the_task_repository_token(monkeypatch, job_token):
    monkeypatch.setattr(devices, "Task", SimpleNamespace(objects=FakeTasks(task())))
    monkeypatch.setattr(devices, "repo_of", lambda url: "acme/proj")

    response = devices.git_token(job_token, None, GitTokenRequest(task="t1"))

    assert response["token"] == "t"
    assert response["username"] == "x-access-token"
