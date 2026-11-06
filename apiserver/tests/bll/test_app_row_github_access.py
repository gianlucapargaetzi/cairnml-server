# apiserver/tests/bll/test_app_row_github_access.py
"""A repository the company's GitHub App finds, with no dataset record, is read only by a user who can read it on GitHub
(R70): the user's own GitHub token must read the repository. Admins and the owner of the App keep full access (R71).
The list shows such a row only to a user who passes the same check, and the person path and the job path refuse the
same way, with a reason that names the repository."""
from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from apiserver.apierrors import errors
from apiserver.bll import datasets as bll
from apiserver.bll import user_access
from apiserver.database.model.dataset import Dataset
from apiserver.database.model.user_access import UserAccess
from apiserver.idp import git_credentials as store
from apiserver.idp.github import GitHubError
from apiserver.services import datasets as service
from apiserver.services import devices
from apiserver.tests.db import ensure_db

ensure_db()

APP_OWNER = "admin1"
REPO = "acme/found"
REFUSAL = "no GitHub read access to acme/found: link a GitHub token in your settings"


@pytest.fixture
def company(monkeypatch):
    name = f"c-{uuid4().hex[:8]}"
    monkeypatch.setattr(store, "key_material", lambda: "test-key-material")
    UserAccess(id=f"app-{name}", company=name, user=APP_OWNER, kind="app", label="App", scope="company",
               secret="x", created=datetime.utcnow()).save()
    monkeypatch.setattr(bll.dataset_cache, "cached_list", lambda: {"datasets": [{"repository": REPO}]})
    yield name
    Dataset.objects(company=name).delete()
    UserAccess.objects(company=name).delete()


@pytest.fixture
def github(monkeypatch):
    """GitHub, as a fake: a token reads the repository when it is in READABLE; a token in ERRORS fails to be asked."""
    fake = SimpleNamespace(asked=[], readable=set(), errors=set())

    def reads(api_url, token, repo, http=None):
        fake.asked.append((token, repo))
        if token in fake.errors:
            raise GitHubError("GitHub transport error")
        return token in fake.readable

    monkeypatch.setattr(user_access, "repository_readable_with", reads)
    return fake


def link_token(company, user, token, path=""):
    """The user's own GitHub token, as the user's settings save it."""
    UserAccess(id=f"tok-{uuid4().hex[:8]}", company=company, user=user, kind="token", label="mine", scope="user",
               host="github.com", path=path, secret=store.encrypt(token, store.key_material()),
               created=datetime.utcnow(), updated=datetime.utcnow()).save()


class FakeApp:
    def __init__(self):
        self.requests = []

    def installation_token(self, repo, permissions=None):
        permissions = permissions or {"contents": "read"}
        self.requests.append((repo, permissions))
        return SimpleNamespace(token=f"tok-{permissions['contents']}", expires_at=datetime(2030, 1, 1))


@pytest.fixture
def app(monkeypatch):
    fake = FakeApp()
    monkeypatch.setattr(service, "build_from_config", lambda: fake)
    monkeypatch.setattr(service, "public_repo", lambda repo: False)
    monkeypatch.setattr(service, "audit", lambda *args, **kwargs: None)
    monkeypatch.setattr(service, "client_address", lambda call: "127.0.0.1")
    return fake


def person(user, is_admin=False):
    return SimpleNamespace(
        data={}, identity=SimpleNamespace(user=user, role="admin" if is_admin else "user"),
        auth=SimpleNamespace(token_kind="client", job_task=None), real_ip="127.0.0.1",
        get_header=lambda header, default=None: default, HEADER_REAL_IP="X-Real-IP",
    )


def ask(company, user, is_admin=False):
    call = person(user, is_admin)
    call.data = {"repository": f"https://github.com/{REPO}", "level": "read"}
    return service.git_token(call, company, None)


def rows_for(company, user, is_admin=False):
    return [row["ref"] for row in bll.list_visible(company, user, is_admin)]


# --- A company user

def test_a_user_who_reads_the_repository_on_github_gets_the_row_and_a_read_token(company, github, app):
    link_token(company, "u3", "ghp_u3")
    github.readable.add("ghp_u3")

    assert REPO in rows_for(company, "u3")
    assert bll.held_on_repository(company, "u3", False, REPO) == ("read", "dataset")
    assert ask(company, "u3")["token"] == "tok-read"
    assert app.requests == [(REPO, {"contents": "read"})]
    assert {token for token, _repo in github.asked} == {"ghp_u3"}


def test_a_user_who_cannot_read_the_repository_on_github_gets_no_row_and_a_clear_refusal(company, github, app):
    link_token(company, "u3", "ghp_u3")

    assert REPO not in rows_for(company, "u3")
    with pytest.raises(errors.bad_request.ValidationError) as refused:
        ask(company, "u3")
    assert REFUSAL in refused.value.msg
    assert app.requests == []


def test_a_user_with_no_stored_github_token_gets_no_row_and_the_refusal(company, github, app):
    assert REPO not in rows_for(company, "u3")
    with pytest.raises(errors.bad_request.ValidationError) as refused:
        ask(company, "u3")
    assert REFUSAL in refused.value.msg
    assert github.asked == []
    assert app.requests == []


def test_only_the_users_own_token_counts(company, github, app):
    """A token of another user who reads the repository does not give this user access."""
    link_token(company, "u3", "ghp_u3")
    github.readable.add("ghp_u3")

    assert REPO not in rows_for(company, "u4")
    with pytest.raises(errors.bad_request.ValidationError) as refused:
        ask(company, "u4")
    assert REFUSAL in refused.value.msg


def test_a_github_failure_is_a_refusal_not_a_row(company, github, app):
    link_token(company, "u3", "ghp_u3")
    github.errors.add("ghp_u3")

    assert REPO not in rows_for(company, "u3")
    with pytest.raises(errors.bad_request.ValidationError) as refused:
        ask(company, "u3")
    assert REFUSAL in refused.value.msg
    assert app.requests == []


# --- Admins and the owner of the App

def test_an_admin_keeps_full_access_without_a_github_token(company, github, app):
    assert REPO in rows_for(company, "u5", is_admin=True)
    assert bll.held_on_repository(company, "u5", True, REPO) == ("collaborator", "dataset")
    assert ask(company, "u5", is_admin=True)["token"] == "tok-read"
    assert github.asked == []


def test_the_owner_of_the_app_keeps_full_access_without_a_github_token(company, github, app):
    assert REPO in rows_for(company, APP_OWNER)
    assert bll.held_on_repository(company, APP_OWNER, False, REPO) == ("collaborator", "dataset")
    assert ask(company, APP_OWNER)["token"] == "tok-read"
    assert github.asked == []


# --- The job path

def job_claims(user_task="task1"):
    return {"kind": "job", "task": user_task, "device": "dev1"}


def job_token(monkeypatch, company, enqueuer):
    """A job of the task run by `enqueuer`, asking for the App-discovered repository."""
    task = SimpleNamespace(project=None, execution=SimpleNamespace(enqueued_by=enqueuer))
    monkeypatch.setattr(devices.Token, "decode", staticmethod(lambda _token: job_claims()))
    monkeypatch.setattr(devices, "_bearer", lambda call: "bearer")
    monkeypatch.setattr(devices, "Task", SimpleNamespace(objects=lambda **_kw: SimpleNamespace(first=lambda: task)))
    monkeypatch.setattr(devices, "public_repo", lambda repo: False)
    monkeypatch.setattr(devices, "verify_dataset", lambda app, repo: None)
    monkeypatch.setattr(devices, "_refresh_dataset_entry", lambda app, repo: None)
    monkeypatch.setattr(devices, "audit", lambda *args, **kwargs: None)
    fake = FakeApp()
    monkeypatch.setattr(devices, "build_from_config", lambda: fake)
    call = SimpleNamespace(data={}, real_ip="127.0.0.1", get_header=lambda h, d=None: d, HEADER_REAL_IP="X-Real-IP")
    request = SimpleNamespace(task="task1", repository=f"https://github.com/{REPO}")
    return lambda: devices.git_token(call, company, request), fake


def test_a_job_of_a_user_who_reads_the_repository_on_github_gets_a_read_token(company, github, monkeypatch):
    link_token(company, "u3", "ghp_u3")
    github.readable.add("ghp_u3")
    run, fake = job_token(monkeypatch, company, "u3")

    assert run()["token"] == "tok-read"
    assert fake.requests == [(REPO, {"contents": "read"})]


def test_a_job_of_a_user_who_cannot_read_the_repository_on_github_is_refused(company, github, monkeypatch):
    run, fake = job_token(monkeypatch, company, "u3")

    with pytest.raises(errors.bad_request.ValidationError) as refused:
        run()
    assert REFUSAL in refused.value.msg
    assert fake.requests == []


def test_a_job_of_the_app_owner_gets_a_read_token_without_a_github_token(company, github, monkeypatch):
    run, fake = job_token(monkeypatch, company, APP_OWNER)

    assert run()["token"] == "tok-read"
    assert github.asked == []
