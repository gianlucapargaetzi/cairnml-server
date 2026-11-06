"""A dataset is read with the credential it was added with, and no other. No credential means public; a private
dataset with none cannot be read. A personal token reads only. Only the owner or an admin changes the credential, and the
credential is the user's own or the company's."""
from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from apiserver.apierrors import errors
from apiserver.bll import datasets as bll
from apiserver.database.model.dataset import Dataset
from apiserver.database.model.user_access import UserAccess
from apiserver.idp import git_credentials as store
from apiserver.idp.github import RepoNotAccessible
from apiserver.services import datasets as service
from apiserver.tests.db import ensure_db

ensure_db()
READ = {"contents": "read"}
WRITE = {"contents": "write", "workflows": "write"}
REPO = "acme/cars"


class FakeApp:
    def __init__(self, installed=True):
        self.installed = installed
        self.requests = []

    def installation_token(self, repo, permissions=None):
        permissions = permissions or READ
        self.requests.append((repo, permissions))
        if not self.installed:
            raise RepoNotAccessible(repo)
        return SimpleNamespace(token=f"app-{permissions['contents']}", expires_at=datetime(2030, 1, 1))


@pytest.fixture
def company():
    name = f"c-choice-{uuid4().hex[:8]}"
    yield name
    Dataset.objects(company=name).delete()
    UserAccess.objects(company=name).delete()


@pytest.fixture
def github(monkeypatch):
    monkeypatch.setattr(store, "key_material", lambda: "test-key-material")
    monkeypatch.setattr(service, "public_repo", lambda repo: False)
    monkeypatch.setattr(service, "audit", lambda *args, **kwargs: None)
    monkeypatch.setattr(service, "client_address", lambda call: "127.0.0.1")
    monkeypatch.setattr(service, "build_from_config", lambda: None)  # the company's App is not the one used here
    app = FakeApp()
    monkeypatch.setattr(bll, "GitHubApp", lambda app_id, key, api: app)
    monkeypatch.setattr(bll, "_can_read", lambda reader, repo: True)
    monkeypatch.setattr(bll, "_public", lambda repo: False)
    monkeypatch.setattr(bll, "held_on_repository", lambda company, user, is_admin, repo: ("collaborator", "dataset"))
    return app


def credential(company, entry_id, user="u1", kind="token", scope="user", secret="ghp_token"):
    UserAccess(id=entry_id, company=company, user=user, kind=kind, label=f"label {entry_id}", scope=scope,
               host="github.com", path="", app_id="7" if kind == "app" else None,
               secret=store.encrypt(secret, store.key_material()), created=datetime.utcnow()).save()
    return entry_id


def dataset(company, owner, credential_id, repo=REPO):
    record = Dataset(id=uuid4().hex, company=company, kind="repo", ref=repo, name=repo.split("/")[1], owner=owner,
                     visibility="user", credential=credential_id, state="ok", created=datetime.utcnow())
    record.save()
    return record


def person(user, is_admin=False):
    return SimpleNamespace(
        data={}, identity=SimpleNamespace(user=user, role="admin" if is_admin else "user"),
        auth=SimpleNamespace(token_kind="client", job_task=None), real_ip="127.0.0.1",
        get_header=lambda header, default=None: default, HEADER_REAL_IP="X-Real-IP",
    )


def ask(company, call, level="read"):
    call.data = {"repository": f"https://github.com/{REPO}", "level": level}
    return service.git_token(call, company, None)


def test_a_dataset_with_no_credential_cannot_be_read_by_a_person(company, github):
    dataset(company, "u1", None)
    with pytest.raises(errors.bad_request.ValidationError, match="has no credential"):
        ask(company, person("u1"))
    assert github.requests == []


def test_the_dataset_is_read_with_its_own_github_app_for_read_and_write(company, github):
    app_id = credential(company, "app-u1", kind="app", secret="pem")
    dataset(company, "u1", app_id)
    assert ask(company, person("u1"), "read")["token"] == "app-read"
    assert ask(company, person("u1"), "read_write")["token"] == "app-write"
    assert github.requests == [(REPO, READ), (REPO, WRITE)]


def test_a_personal_token_reads_but_cannot_write(company, github):
    token_id = credential(company, "tok-u1")
    dataset(company, "u1", token_id)
    assert ask(company, person("u1"), "read")["token"] == "ghp_token"
    with pytest.raises(errors.bad_request.ValidationError, match="cannot write"):
        ask(company, person("u1"), "read_write")


def test_the_companys_app_is_used_when_it_is_the_dataset_credential(company, github):
    app_id = credential(company, "company-app", user="admin1", kind="app", scope="company", secret="pem")
    dataset(company, "u1", app_id)
    assert ask(company, person("u2"), "read")["token"] == "app-read"


def test_another_users_credential_is_refused_for_a_reader(company, github):
    token_id = credential(company, "tok-u1", user="u1")
    dataset(company, "u1", token_id)
    with pytest.raises(errors.bad_request.ValidationError, match="belongs to someone else"):
        ask(company, person("u2"), "read")


def test_an_app_that_is_not_installed_refuses_with_its_name(company, github):
    github.installed = False
    app_id = credential(company, "app-u1", kind="app", secret="pem")
    dataset(company, "u1", app_id)
    with pytest.raises(errors.bad_request.ValidationError, match="not installed on acme/cars"):
        ask(company, person("u1"), "read_write")


def test_only_the_owner_or_an_admin_changes_the_credential(company, github):
    token_id = credential(company, "tok-u1")
    dataset(company, "u1", None)
    with pytest.raises(errors.forbidden.NoWritePermission):
        bll.set_credential(company, "u2", f"https://github.com/{REPO}", token_id, False)
    changed = bll.set_credential(company, "u1", f"https://github.com/{REPO}", token_id, False)
    assert changed["credential"] == token_id and changed["state"] == "ok"
    cleared = bll.set_credential(company, "u1", f"https://github.com/{REPO}", None, False)
    assert cleared["credential"] is None


def test_adding_a_private_dataset_without_a_credential_says_how_to_add_one(company, github, monkeypatch):
    monkeypatch.setattr(bll, "_public", lambda repo: False)
    added = bll.add_repo(company, "u1", f"https://github.com/{REPO}")
    assert added["state"] == "no_access" and "add-credential" in added["state_message"]


def test_a_public_dataset_needs_no_credential(company, github, monkeypatch):
    monkeypatch.setattr(bll, "_public", lambda repo: True)
    added = bll.add_repo(company, "u1", f"https://github.com/{REPO}")
    assert added["credential"] is None and added["state"] == "ok"


def test_the_owner_registering_their_repository_again_with_a_credential_sets_it(company, github):
    token_id = credential(company, "tok-u1")
    first = bll.add_repo(company, "u1", f"https://github.com/{REPO}")
    assert first["credential"] is None and first["state"] == "no_access"
    again = bll.add_repo(company, "u1", f"https://github.com/{REPO}", credential=token_id)
    assert again["credential"] == token_id and again["state"] == "ok"
    assert Dataset.objects(company=company, ref=REPO).count() == 1


def test_registering_again_without_a_credential_is_refused(company, github, monkeypatch):
    monkeypatch.setattr(bll, "_public", lambda repo: False)
    bll.add_repo(company, "u1", f"https://github.com/{REPO}")
    with pytest.raises(errors.bad_request.ValidationError, match="already added"):
        bll.add_repo(company, "u1", f"https://github.com/{REPO}")


def test_another_owner_cannot_register_an_added_repository(company, github, monkeypatch):
    token_id = credential(company, "tok-u2", user="u2")
    monkeypatch.setattr(bll, "_public", lambda repo: False)
    bll.add_repo(company, "u1", f"https://github.com/{REPO}")
    with pytest.raises(errors.bad_request.ValidationError, match="already added by u1"):
        bll.add_repo(company, "u2", f"https://github.com/{REPO}", credential=token_id)
