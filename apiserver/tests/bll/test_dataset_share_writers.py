# apiserver/tests/bll/test_dataset_share_writers.py
"""Who may see a dataset or a registry, and the share records that the dataset writers keep in step with the old fields."""
from datetime import datetime
from uuid import uuid4

import pytest

from apiserver.bll import datasets
from apiserver.database.model.auth import User
from apiserver.database.model.dataset import Dataset
from apiserver.database.model.project import Project, ProjectMember, ProjectRole
from apiserver.database.model.share import Share
from apiserver.tests.db import ensure_db

ensure_db()

REPO_URL = "https://github.com/owner/cars"


@pytest.fixture
def company():
    return f"c-{uuid4().hex[:8]}"


@pytest.fixture
def public_github(monkeypatch):
    """The repository is public, so no credential or network is needed to add it"""
    monkeypatch.setattr(datasets, "_public", lambda repo: True)
    monkeypatch.setattr(datasets, "list_entries", lambda app, registry: [])


def make_user(company, user_id):
    User(id=user_id, name=user_id, company=company, created=datetime.utcnow()).save()


def make_root(company, id_, owner, members=()):
    Project(
        id=id_, company=company, user=owner, name=id_, basename=id_, created=datetime.utcnow(),
        members=[ProjectMember(user=u, role=r, added=datetime.utcnow(), added_by=owner) for u, r in members],
    ).save()


def sees(company, user, dataset_id):
    return any(row["id"] == dataset_id for row in datasets.list_visible(company, user, False))


@pytest.fixture
def setup(company):
    for user in ("owner1", "u2", "stranger"):
        make_user(company, user)
    make_root(company, "root1", "owner1", members=[("u2", ProjectRole.read)])
    make_root(company, "root2", "owner1")


# --- characterisation: who may see the dataset before any writer is changed


def test_a_public_dataset_is_seen_by_every_user(company, setup, public_github):
    dataset = datasets.add_repo(company, "owner1", REPO_URL, visibility="Public")
    assert sees(company, "stranger", dataset["id"])


def test_set_visibility_projects_shows_the_dataset_to_the_members_of_the_projects(company, setup, public_github):
    dataset = datasets.add_repo(company, "owner1", REPO_URL, visibility="user")
    assert not sees(company, "u2", dataset["id"])

    datasets.set_visibility(company, "owner1", dataset["id"], "projects", ["root1"], False)
    assert sees(company, "u2", dataset["id"])
    assert not sees(company, "stranger", dataset["id"])

    datasets.set_visibility(company, "owner1", dataset["id"], "user", [], False)
    assert not sees(company, "u2", dataset["id"])


def test_allocate_and_unallocate_show_and_hide_the_dataset_for_the_members_of_the_project(company, setup,
                                                                                          public_github):
    dataset = datasets.add_repo(company, "owner1", REPO_URL, visibility="user")
    datasets.allocate(company, "owner1", dataset["id"], "root1", False)
    assert sees(company, "u2", dataset["id"])

    datasets.unallocate(company, "owner1", dataset["id"], "root1", False)
    assert not sees(company, "u2", dataset["id"])


def test_delete_removes_the_dataset_for_everyone(company, setup, public_github):
    dataset = datasets.add_repo(company, "owner1", REPO_URL, visibility="Public")
    datasets.delete(company, "owner1", dataset["id"], False)
    assert not sees(company, "stranger", dataset["id"])
    assert Dataset.objects(company=company, id=dataset["id"]).first() is None


# --- consistency: after each writer, the old fields and the share records describe the same users


def grants_of(company, dataset_id):
    """(from the old fields, from the share records) for one dataset or registry: whether it is Public, and the
    projects it is allocated to"""
    record = Dataset.objects(company=company, id=dataset_id).first()
    old = {"public": record.visibility == "Public", "projects": set(record.projects or [])}
    shares = Share.objects(company=company, resource_id=dataset_id, resource_type__in=["dataset", "registry"])
    records = {
        "public": any(s.scope == "public" for s in shares),
        "projects": {s.project for s in shares if s.scope == "projects"},
    }
    return old, records


def assert_consistent(company, dataset_id):
    old, records = grants_of(company, dataset_id)
    assert old == records


def test_a_public_dataset_has_a_public_share_and_loses_it_when_it_is_no_longer_public(company, setup, public_github):
    dataset = datasets.add_repo(company, "owner1", REPO_URL, visibility="Public")
    assert_consistent(company, dataset["id"])

    datasets.set_visibility(company, "owner1", dataset["id"], "user", [], False)
    assert_consistent(company, dataset["id"])


def test_the_allocations_of_a_dataset_follow_set_visibility_and_allocate(company, setup, public_github):
    dataset = datasets.add_repo(company, "owner1", REPO_URL, visibility="user")
    datasets.set_visibility(company, "owner1", dataset["id"], "projects", ["root1", "root2"], False)
    assert_consistent(company, dataset["id"])

    datasets.set_visibility(company, "owner1", dataset["id"], "projects", ["root2"], False)
    assert_consistent(company, dataset["id"])

    datasets.allocate(company, "owner1", dataset["id"], "root1", False)
    assert_consistent(company, dataset["id"])

    datasets.unallocate(company, "owner1", dataset["id"], "root2", False)
    assert_consistent(company, dataset["id"])


def test_an_allocation_is_granted_by_the_owner_of_the_project(company, setup, public_github):
    # owner1 is a member of root3, which another user owns: the allocation is granted by that owner
    make_user(company, "owner2")
    make_root(company, "root3", "owner2", members=[("owner1", ProjectRole.read)])
    dataset = datasets.add_repo(company, "owner1", REPO_URL, visibility="user")
    datasets.allocate(company, "owner1", dataset["id"], "root3", False)

    share = Share.objects(company=company, resource_id=dataset["id"], scope="projects", project="root3").first()
    assert share.granted_by == "owner2"


def test_a_registry_follows_its_visibility(company, setup, public_github):
    registry = datasets.add_registry(company, "owner1", "https://github.com/owner/registry", visibility="Public")
    old, records = grants_of(company, registry["id"])
    assert old["public"] and records["public"]


def test_deleting_a_dataset_removes_its_shares(company, setup, public_github):
    dataset = datasets.add_repo(company, "owner1", REPO_URL, visibility="Public")
    datasets.allocate(company, "owner1", dataset["id"], "root1", False)
    datasets.delete(company, "owner1", dataset["id"], False)
    assert Share.objects(company=company, resource_id=dataset["id"]).count() == 0
