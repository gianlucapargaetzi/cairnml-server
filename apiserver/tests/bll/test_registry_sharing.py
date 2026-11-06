# apiserver/tests/bll/test_registry_sharing.py
"""A registry is shared as a whole, at the registry level. Its entries have no share of their own and inherit from it: a
project the registry is shared with gets its entries, a read share cannot remove the registry, and the owner removes it."""
from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from apiserver.apierrors import errors
from apiserver.bll import access
from apiserver.bll import datasets
from apiserver.database.model.auth import Role, User
from apiserver.database.model.dataset import Dataset
from apiserver.database.model.project import Project, ProjectMember, ProjectRole
from apiserver.database.model.share import Share
from apiserver.database.model.user_access import UserAccess
from apiserver.services import devices
from apiserver.tests.db import ensure_db

ensure_db()

REGISTRY_URL = "https://github.com/owner/registry"
REGISTRY_REF = "owner/registry"


@pytest.fixture
def company():
    return f"c-{uuid4().hex[:8]}"


@pytest.fixture
def github(monkeypatch):
    """GitHub is replaced: every repository is public, and the registry lists the entries the test sets."""
    state = SimpleNamespace(entries=[])
    monkeypatch.setattr(datasets, "_public", lambda repo: True)
    monkeypatch.setattr(datasets, "list_entries", lambda app, registry: state.entries)
    monkeypatch.setattr(datasets, "read_metadata", lambda app, registry, name: None)
    return state


def make_user(company, user_id, role=Role.user, disabled=False):
    User(id=user_id, name=user_id, company=company, role=role, created=datetime.utcnow(),
         idp_disabled=disabled).save()


def make_root(company, id_, owner, members=()):
    Project(
        id=id_, company=company, user=owner, name=id_, basename=id_, created=datetime.utcnow(),
        members=[ProjectMember(user=u, role=r, added=datetime.utcnow(), added_by=owner) for u, r in members],
    ).save()


@pytest.fixture
def setup(company):
    make_user(company, "owner1")
    make_user(company, "u2")
    make_user(company, "stranger")
    make_root(company, "root1", "owner1", members=[("u2", ProjectRole.read)])
    make_root(company, "root2", "owner1")
    yield
    for model in (Share, Dataset, Project, UserAccess, User):
        model.objects(company=company).delete()


def registry_of(company) -> Dataset:
    return Dataset.objects(company=company, kind="registry", ref__iexact=REGISTRY_REF).first()


def entry_of(company, name) -> Dataset:
    return Dataset.objects(company=company, kind="registry_entry", ref=f"{REGISTRY_REF}/{name}").first()


def sees(company, user, dataset_id, is_admin=False):
    return any(row["id"] == dataset_id for row in datasets.list_visible(company, user, is_admin))


def add_registry_with_entries(company, github, visibility="user"):
    github.entries = [{"name": "cars", "source": "web", "link": "x"},
                      {"name": "bikes", "source": "web", "link": "y"}]
    datasets.add_registry(company, "owner1", REGISTRY_URL, visibility=visibility)
    return registry_of(company), entry_of(company, "cars")


# --- a registry shared with a project gives its entries to that project


def test_a_registry_shared_with_a_project_gives_its_entries_to_the_project(company, setup, github):
    registry, entry = add_registry_with_entries(company, github)
    assert not sees(company, "u2", entry.id)

    datasets.set_visibility(company, "owner1", registry.id, "projects", ["root1"], False)

    assert sees(company, "u2", entry.id)
    assert not sees(company, "stranger", entry.id)


def test_an_entry_of_a_registry_shared_with_no_project_is_not_seen_by_a_project_member(company, setup, github):
    registry, entry = add_registry_with_entries(company, github)
    datasets.set_visibility(company, "owner1", registry.id, "projects", ["root2"], False)
    assert not sees(company, "u2", entry.id)


# --- entries are not shared one by one


def test_adding_a_public_registry_creates_no_share_record_for_its_entries(company, setup, github):
    _, entry = add_registry_with_entries(company, github, visibility="Public")
    assert Share.objects(company=company, resource_id=entry.id).count() == 0


def test_an_entry_cannot_be_allocated_to_a_project_on_its_own(company, setup, github):
    _, entry = add_registry_with_entries(company, github)
    with pytest.raises(errors.bad_request.ValidationError):
        datasets.allocate(company, "owner1", entry.id, "root1", False)
    assert Share.objects(company=company, resource_id=entry.id).count() == 0
    assert not sees(company, "u2", entry.id)


def test_an_entry_cannot_change_its_visibility_on_its_own(company, setup, github):
    _, entry = add_registry_with_entries(company, github)
    with pytest.raises(errors.bad_request.ValidationError):
        datasets.set_visibility(company, "owner1", entry.id, "Public", [], False)
    assert Share.objects(company=company, resource_id=entry.id).count() == 0
    assert not sees(company, "stranger", entry.id)


# --- removing a registry: manage_sharing on the registry


def test_a_read_share_of_a_registry_cannot_remove_it(company, setup, github):
    registry, _ = add_registry_with_entries(company, github)
    datasets.set_visibility(company, "owner1", registry.id, "projects", ["root1"], False)
    with pytest.raises(errors.bad_request.ValidationError):
        datasets.delete(company, "u2", registry.id, False)
    assert registry_of(company) is not None


def test_a_collaborator_share_of_a_registry_removes_it(company, setup, github):
    registry, _ = add_registry_with_entries(company, github)
    access.record_grant(company, "owner1", "registry", registry.id, "projects", "root1", level="collaborator",
                        actor="owner1")
    datasets.delete(company, "u2", registry.id, False)
    assert registry_of(company) is None


def test_the_owner_removes_a_registry_and_its_entries_go_with_it(company, setup, github):
    registry, _ = add_registry_with_entries(company, github)
    datasets.delete(company, "owner1", registry.id, False)
    assert registry_of(company) is None
    assert Dataset.objects(company=company, kind="registry_entry").count() == 0


# --- a deactivated owner or grantee gets no access (R25)


def test_a_deactivated_grantee_does_not_see_the_entries_of_a_shared_registry(company, setup, github):
    registry, entry = add_registry_with_entries(company, github)
    datasets.set_visibility(company, "owner1", registry.id, "projects", ["root1"], False)
    User.objects(id="u2").update(set__idp_disabled=True)
    assert not sees(company, "u2", entry.id)


# --- regression: a job of the project reads the registry with the registry's credential


class FakeApp:
    def __init__(self, app_id, secret, api_url):
        self.secret = secret

    def installation_token(self, repo):
        return SimpleNamespace(token=f"tok-{self.secret}", expires_at=datetime(2030, 1, 1))


