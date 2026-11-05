"""A job that reads a dataset uses the dataset's credential, and no other access, when it is shared with the job's project."""
from datetime import datetime
from types import SimpleNamespace

from apiserver.services import devices

REPO = "https://github.com/acme/cars"


class FakeApp:
    def __init__(self, app_id, secret, api_url):
        self.secret = secret

    def installation_token(self, repo):
        return SimpleNamespace(token=f"tok-{self.secret}", expires_at=datetime(2030, 1, 1))


def granted(*entries):
    return [(SimpleNamespace(id=entry_id, kind="app", app_id="1", api_url=None, user="u1"), f"s{entry_id}")
            for entry_id in entries]


def run(monkeypatch, pairs, credential):
    monkeypatch.setattr(devices, "GitHubApp", FakeApp)
    monkeypatch.setattr(devices, "audit", lambda *args, **kwargs: None)
    monkeypatch.setattr(devices.user_access, "root_project", lambda company, project: "p-team")
    monkeypatch.setattr(devices.user_access, "granted_for_job", lambda *args: pairs)
    task = SimpleNamespace(project="p-team", execution=SimpleNamespace(enqueued_by="u2"))
    return devices._granted_token("c1", task, "agent", "task1", None, "acme/cars", credential=credential)


def test_the_dataset_credential_is_used_even_when_another_grant_comes_first(monkeypatch):
    result = run(monkeypatch, granted("A", "B"), credential="B")
    assert result["token"] == "tok-sB"


def test_a_dataset_credential_that_is_not_shared_with_the_project_gives_no_token(monkeypatch):
    assert run(monkeypatch, granted("A"), credential="C") is None


def test_without_a_dataset_credential_the_first_shared_access_is_used(monkeypatch):
    result = run(monkeypatch, granted("A", "B"), credential=None)
    assert result["token"] == "tok-sA"
