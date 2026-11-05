# apiserver/tests/bll/test_credential_share_writers.py
"""Who may use a credential, and the share records that the credential grant and revoke keep in step with the old field."""
from datetime import datetime
from uuid import uuid4

import pytest

from apiserver.bll import user_access
from apiserver.database.model.auth import User
from apiserver.database.model.project import Project, ProjectMember, ProjectRole
from apiserver.database.model.share import Share
from apiserver.database.model.user_access import UserAccess
from apiserver.idp import git_credentials as store
from apiserver.tests.db import ensure_db

ensure_db()


@pytest.fixture
def company():
    return f"c-{uuid4().hex[:8]}"


def make_user(company, user_id):
    User(id=user_id, name=user_id, company=company, created=datetime.utcnow()).save()


def make_root(company, id_, owner, members=()):
    Project(
        id=id_, company=company, user=owner, name=id_, basename=id_, created=datetime.utcnow(),
        members=[ProjectMember(user=u, role=r, added=datetime.utcnow(), added_by=owner) for u, r in members],
    ).save()


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setattr(store, "key_material", lambda: "test-key-material")


@pytest.fixture
def setup(company):
    """u1 owns a token and is a member of root1 (owned by owner1, with u2 as a member); root2 is owned by owner1"""
    for user in ("owner1", "u1", "u2", "stranger"):
        make_user(company, user)
    make_root(company, "root1", "owner1", members=[("u1", ProjectRole.read), ("u2", ProjectRole.read)])
    make_root(company, "root2", "owner1")
    entry = UserAccess(
        id="acc1", company=company, user="u1", kind="token", label="token", host="github.com", path="",
        secret=store.encrypt("ghp_secret", store.key_material()), created=datetime.utcnow(),
    )
    entry.save()
    return entry


def usable_by(company, user, project):
    return [entry.id for entry, _ in user_access.usable_for(company, user, "github.com", "owner/repo", project)]


# --- characterisation: who may use the credential before any writer is changed


def test_a_grant_lets_the_members_of_the_project_use_the_credential_until_it_is_revoked(company, setup):
    assert usable_by(company, "u2", "root1") == []

    user_access.grant(company, "u1", "acc1", "root1")
    assert usable_by(company, "u2", "root1") == ["acc1"]
    assert usable_by(company, "stranger", "root1") == []
    assert usable_by(company, "u2", "root2") == []

    user_access.revoke(company, "owner1", "acc1", "root1", is_admin=False)
    assert usable_by(company, "u2", "root1") == []


# --- the share records follow every grant and revoke


def grants_of(company, access_id):
    """The projects the credential is shared with, from its share records"""
    shares = Share.objects(company=company, resource_type="credential", resource_id=access_id, scope="projects")
    return {s.project for s in shares}


def test_the_credential_shares_follow_every_grant_and_revoke(company, setup):
    user_access.grant(company, "u1", "acc1", "root1")
    assert grants_of(company, "acc1") == {"root1"}

    user_access.revoke(company, "owner1", "acc1", "root1", is_admin=False)
    assert grants_of(company, "acc1") == set()


def test_a_grant_is_granted_by_the_owner_of_the_credential(company, setup):
    user_access.grant(company, "u1", "acc1", "root1")
    share = Share.objects(company=company, resource_type="credential", resource_id="acc1").first()
    assert share.granted_by == "u1"
    assert share.level == "read"


def test_deleting_a_credential_removes_its_shares(company, setup):
    user_access.grant(company, "u1", "acc1", "root1")
    user_access.delete(company, "u1", "acc1")
    assert Share.objects(company=company, resource_type="credential", resource_id="acc1").count() == 0
