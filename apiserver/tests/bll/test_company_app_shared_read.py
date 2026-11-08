# apiserver/tests/bll/test_company_app_shared_read.py
"""A repository the company's GitHub App finds, with no dataset record, is the company's shared read access: every
active user of the company may read it, and nothing more is asked of the user (the App is installed by an admin only on
what the company means to share). Admins and the owner of the App hold a collaborator level; any other user holds read.
A deactivated user is given nothing. The list, the person path and the job path agree."""
from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from apiserver.bll import datasets as bll
from apiserver.bll import user_access
from apiserver.database.model.dataset import Dataset
from apiserver.database.model.user_access import UserAccess
from apiserver.idp import git_credentials as store
from apiserver.services import datasets as service
from apiserver.services import devices
from apiserver.tests.db import ensure_db

ensure_db()

APP_OWNER = "admin1"
REPO = "acme/found"


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


# --- Any user of the company

def test_any_user_gets_the_row_and_a_read_token_with_nothing_more_asked(company, app):
    assert REPO in rows_for(company, "u3")
    assert bll.held_on_repository(company, "u3", False, REPO) == ("read", "dataset")
    assert ask(company, "u3")["token"] == "tok-read"
    assert app.requests == [(REPO, {"contents": "read"})]


def test_a_user_gets_no_write_token_for_what_the_app_finds(company, app):
    from apiserver.apierrors import errors

    call = person("u3")
    call.data = {"repository": f"https://github.com/{REPO}", "level": "read_write"}
    with pytest.raises(errors.forbidden.NoWritePermission):
        service.git_token(call, company, None)
    assert app.requests == []


def test_a_repository_the_app_does_not_list_gives_no_level(company):
    assert bll.held_on_repository(company, "u3", False, "acme/elsewhere") == (None, "dataset")


def test_a_deactivated_user_is_given_nothing(company, monkeypatch):
    monkeypatch.setattr(user_access, "is_active", lambda user: False)

    assert REPO not in rows_for(company, "u3")
    assert bll.held_on_repository(company, "u3", False, REPO) == (None, "dataset")


# --- Admins and the owner of the App

def test_an_admin_holds_a_collaborator_level(company, app):
    assert REPO in rows_for(company, "u5", is_admin=True)
    assert bll.held_on_repository(company, "u5", True, REPO) == ("collaborator", "dataset")
    assert ask(company, "u5", is_admin=True)["token"] == "tok-read"


def test_the_owner_of_the_app_holds_a_collaborator_level(company, app):
    assert REPO in rows_for(company, APP_OWNER)
    assert bll.held_on_repository(company, APP_OWNER, False, REPO) == ("collaborator", "dataset")
    assert ask(company, APP_OWNER)["token"] == "tok-read"


# --- The job path

def job_token(monkeypatch, company, enqueuer):
    """A job of the task run by `enqueuer`, asking for the App-discovered repository."""
    task = SimpleNamespace(project=None, user=enqueuer, execution=SimpleNamespace(enqueued_by=enqueuer))
    monkeypatch.setattr(devices.Token, "decode", staticmethod(lambda _token: {"kind": "job", "task": "task1", "device": "dev1"}))
    monkeypatch.setattr(devices, "_bearer", lambda call: "bearer")
    monkeypatch.setattr(devices, "Task", SimpleNamespace(objects=lambda **_kw: SimpleNamespace(first=lambda: task)))
    monkeypatch.setattr(devices, "public_repo", lambda repo: False)
    monkeypatch.setattr(devices, "verify_dataset", lambda app, repo: None)
    monkeypatch.setattr(devices, "_refresh_dataset_entry", lambda app, repo: None)
    monkeypatch.setattr(devices, "audit", lambda *args, **kwargs: None)
    fake = FakeApp()
    monkeypatch.setattr(devices, "build_from_config", lambda: fake)
    # the repository is read as the binding says: nothing is assigned and it is private, so the company App reads it
    monkeypatch.setattr(devices.repositories_bll, "is_private", lambda address: True)
    monkeypatch.setattr(devices.repositories_bll, "_github_app", lambda: fake)
    call = SimpleNamespace(data={}, real_ip="127.0.0.1", get_header=lambda h, d=None: d, HEADER_REAL_IP="X-Real-IP")
    request = SimpleNamespace(task="task1", repository=f"https://github.com/{REPO}")
    return lambda: devices.git_token(call, company, request), fake


@pytest.mark.parametrize("user", ["u3", APP_OWNER])
def test_the_job_of_any_user_gets_a_read_token(company, monkeypatch, user):
    run, fake = job_token(monkeypatch, company, user)

    try:
        assert run()["token"] == "tok-read"
        assert fake.requests and all(request == (REPO, {"contents": "read"}) for request in fake.requests)
    finally:
        from apiserver.database.model.code_repository import CodeRepository, RepositoryBinding

        CodeRepository.objects(company=company).delete()
        RepositoryBinding.objects(company=company).delete()
