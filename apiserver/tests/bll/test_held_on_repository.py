# apiserver/tests/bll/test_held_on_repository.py
"""The level a person holds on a repository's dataset, as datasets.git_token resolves it: through the kept record, or
for a repository the company's App finds, as datasets.visible shows it. And the sequence that creates a dataset: it is
added, then its owner gets a write token."""
from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from apiserver.apierrors import errors
from apiserver.bll import datasets as bll
from apiserver.bll import user_access
from apiserver.idp import git_credentials as store
from apiserver.database.model.dataset import Dataset
from apiserver.database.model.user_access import UserAccess
from apiserver.services import datasets as service
from apiserver.tests.db import ensure_db

ensure_db()

APP_OWNER = "admin1"


@pytest.fixture
def company():
    """A company of its own, whose records are removed afterwards."""
    name = f"c-{uuid4().hex[:8]}"
    yield name
    Dataset.objects(company=name).delete()
    UserAccess.objects(company=name).delete()


@pytest.fixture
def app_listing(company, monkeypatch):
    """The company's App is installed with credential owner admin1, and the cache lists acme/found."""
    UserAccess(id=f"app-{company}", company=company, user=APP_OWNER, kind="app", label="App", scope="company",
               secret="x", created=datetime.utcnow()).save()
    monkeypatch.setattr(bll.dataset_cache, "cached_list", lambda: {"datasets": [{"repository": "acme/found"}]})
    yield
    UserAccess.objects(company=company, kind="app").delete()


@pytest.fixture
def u3_reads_on_github(company, monkeypatch):
    """u3 has a GitHub token of their own that reads acme/found (R70); nobody else's token is asked for."""
    monkeypatch.setattr(store, "key_material", lambda: "test-key-material")
    UserAccess(id=f"tok-{company}", company=company, user="u3", kind="token", label="mine", scope="user",
               host="github.com", path="", secret=store.encrypt("ghp_u3", store.key_material()),
               created=datetime.utcnow()).save()
    monkeypatch.setattr(user_access, "repository_readable_with", lambda api, token, repo, http=None: token == "ghp_u3")


class FakeApp:
    def __init__(self):
        self.requests = []

    def installation_token(self, repo, permissions=None):
        permissions = permissions or {"contents": "read"}
        self.requests.append((repo, permissions))
        return SimpleNamespace(token=f"tok-{permissions['contents']}", expires_at=datetime(2030, 1, 1))


def person(user, is_admin=False):
    return SimpleNamespace(
        data={}, identity=SimpleNamespace(user=user, role="admin" if is_admin else "user"),
        auth=SimpleNamespace(token_kind="client", job_task=None), real_ip="127.0.0.1",
        get_header=lambda header, default=None: default, HEADER_REAL_IP="X-Real-IP",
    )


@pytest.fixture
def github(monkeypatch):
    """The App the endpoint asks for tokens, with no audit and no network: the repositories are private."""
    app = FakeApp()
    monkeypatch.setattr(service, "build_from_config", lambda: app)
    monkeypatch.setattr(service, "public_repo", lambda repo: False)
    monkeypatch.setattr(service, "audit", lambda *args, **kwargs: None)
    monkeypatch.setattr(service, "client_address", lambda call: "127.0.0.1")
    return app


def ask(company, call, repository, level):
    call.data = {"repository": repository, "level": level}
    return service.git_token(call, company, None)


def test_an_app_discovered_repository_with_no_record_is_read_by_a_user_who_reads_it_on_github_and_collaborated_by_the_app_owner(
        company, app_listing, u3_reads_on_github):
    assert bll.held_on_repository(company, "u3", False, "acme/found") == ("read", "dataset")
    assert bll.held_on_repository(company, APP_OWNER, False, "acme/found") == ("collaborator", "dataset")
    assert bll.held_on_repository(company, "u3", True, "acme/found") == ("collaborator", "dataset")


def test_a_repository_the_app_does_not_list_and_no_record_gives_no_level(company, app_listing):
    assert bll.held_on_repository(company, "u3", False, "acme/elsewhere") == (None, "dataset")


def test_a_person_gets_the_read_token_for_an_app_discovered_repository_with_no_record(company, app_listing, github,
                                                                                    u3_reads_on_github):
    answer = ask(company, person("u3"), "https://github.com/acme/found", "read")

    assert answer["token"] == "tok-read"
    assert github.requests == [("acme/found", {"contents": "read"})]


def test_a_person_gets_no_write_token_for_an_app_discovered_repository_with_no_record(company, app_listing, github,
                                                                                     u3_reads_on_github):
    with pytest.raises(errors.forbidden.NoWritePermission):
        ask(company, person("u3"), "https://github.com/acme/found", "read_write")
    assert github.requests == []


def test_the_owner_of_a_new_dataset_gets_a_write_token_after_it_is_added(company, monkeypatch, github):
    """The create sequence: the repository is added (owner = the caller, visibility user) with the owner's GitHub App
    as its credential, then the owner asks for a read_write token, which the App makes. GitHub is faked: the App
    reads the repository."""
    monkeypatch.setattr(store, "key_material", lambda: "test-key-material")
    credential = f"app-{company}"
    UserAccess(id=credential, company=company, user="owner1", kind="app", label="Owner App", scope="user",
               app_id="7", api_url=None, secret=store.encrypt("pem", store.key_material()),
               created=datetime.utcnow()).save()
    monkeypatch.setattr(bll, "GitHubApp", lambda app_id, key, api: github)
    monkeypatch.setattr(bll, "_can_read", lambda reader, repo: True)
    monkeypatch.setattr(bll, "_public", lambda repo: False)
    added = bll.add_repo(company, "owner1", "https://github.com/owner/cars.git", visibility="user", credential=credential)
    assert added["owner"] == "owner1" and added["visibility"] == "user"
    answer = ask(company, person("owner1"), "https://github.com/owner/cars.git", "read_write")

    assert answer["token"] == "tok-write"
    assert github.requests == [("owner/cars", {"contents": "write", "workflows": "write"})]
    assert bll.held_on_repository(company, "owner1", False, "owner/cars") == ("collaborator", "dataset")
