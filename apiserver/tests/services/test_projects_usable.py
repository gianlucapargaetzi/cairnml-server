# apiserver/tests/services/test_projects_usable.py
"""projects.usable lists what a project can use: the datasets that are Public or shared with the project, and the
credentials shared with it. Only the owner and the collaborators of the project may ask, and no secret is ever returned."""

import json
import re
from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from apiserver.apierrors import errors
from apiserver.apimodels.project_access import UsableRequest
from apiserver.bll import access
from apiserver.bll.access import record_grant
from apiserver.database.model.auth import Role, User
from apiserver.database.model.dataset import Dataset
from apiserver.database.model.project import Project, ProjectMember, ProjectRole, ProjectVisibility
from apiserver.database.model.user_access import UserAccess
from apiserver.service_repo.auth import Identity
from apiserver.services import project_access as svc
from apiserver.tests.db import ensure_db

ensure_db()

OWNER, COLLABORATOR, MEMBER, DATASET_OWNER, CREDENTIAL_OWNER = (
    "owner", "collaborator", "member", "dataset-owner", "credential-owner")
SECRET = "ghp_s3cr3t-token-value"
SECRET_KEY = re.compile(r"token|secret|password|key", re.IGNORECASE)


class FakeCall:
    def __init__(self, ident):
        self.identity = ident
        self.result = SimpleNamespace(data=None, data_model=None)


def _identity(company, user):
    return Identity(user=user, company=company, role=Role.user)


@pytest.fixture
def company():
    return f"c-{uuid4().hex[:8]}"


@pytest.fixture
def world(company):
    for user in (OWNER, COLLABORATOR, MEMBER, DATASET_OWNER, CREDENTIAL_OWNER):
        User(id=user, name=user, company=company, created=datetime.utcnow()).save()
    root = Project(
        id=uuid4().hex, company=company, user=OWNER, name="root", basename="root", created=datetime.utcnow(),
        visibility=ProjectVisibility.private,
        members=[
            ProjectMember(user=COLLABORATOR, role=ProjectRole.collaborator, added=datetime.utcnow(), added_by=OWNER),
            ProjectMember(user=MEMBER, role=ProjectRole.read, added=datetime.utcnow(), added_by=OWNER),
            ProjectMember(user=CREDENTIAL_OWNER, role=ProjectRole.read, added=datetime.utcnow(), added_by=OWNER),
        ],
    )
    root.save()
    sub = Project(id=uuid4().hex, company=company, user=OWNER, name="root/sub", basename="sub",
                  created=datetime.utcnow(), parent=root.id, path=[root.id])
    sub.save()

    shared = Dataset(id=uuid4().hex, company=company, kind="repo", ref="acme/cars", name="cars",
                     owner=DATASET_OWNER, visibility="projects", created=datetime.utcnow())
    shared.save()
    public = Dataset(id=uuid4().hex, company=company, kind="repo", ref="acme/open", name="open",
                     owner=DATASET_OWNER, visibility="Public", created=datetime.utcnow())
    public.save()
    Dataset(id=uuid4().hex, company=company, kind="repo", ref="acme/private", name="private",
            owner=DATASET_OWNER, visibility="user", created=datetime.utcnow()).save()
    registry = Dataset(id=uuid4().hex, company=company, kind="registry", ref="acme/registry", name="registry",
                       owner=DATASET_OWNER, visibility="projects", created=datetime.utcnow())
    registry.save()

    credential = UserAccess(id=uuid4().hex, company=company, user=CREDENTIAL_OWNER, kind="token", label="team token",
                            host="github.com", path="acme", secret=SECRET, created=datetime.utcnow())
    credential.save()
    UserAccess(id=uuid4().hex, company=company, user=CREDENTIAL_OWNER, kind="token", label="not shared",
               host="github.com", path="", secret="another-secret", created=datetime.utcnow()).save()

    record_grant(company, OWNER, "dataset", shared.id, "projects", root.id, level="read", actor=OWNER)
    record_grant(company, DATASET_OWNER, "dataset", public.id, "public", actor=DATASET_OWNER)
    record_grant(company, OWNER, "registry", registry.id, "projects", root.id, level="read", actor=OWNER)
    record_grant(company, CREDENTIAL_OWNER, "credential", credential.id, "projects", root.id, level="read",
                 actor=CREDENTIAL_OWNER)
    return SimpleNamespace(root=root.id, sub=sub.id, shared=shared.id, public=public.id, credential=credential.id)


def usable(company, user, project):
    call = FakeCall(_identity(company, user))
    svc.usable(call, company, UsableRequest(project=project))
    return call.result.data


def _keys(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _keys(item)


def test_the_owner_sees_every_usable_dataset_and_credential(company, world):
    data = usable(company, OWNER, world.root)

    assert {d["id"] for d in data["datasets"]} == {world.shared, world.public}
    assert [c["id"] for c in data["credentials"]] == [world.credential]


def test_a_sub_project_resolves_to_its_root(company, world):
    assert usable(company, OWNER, world.sub) == usable(company, OWNER, world.root)


def test_a_plain_member_is_refused(company, world):
    with pytest.raises(errors.forbidden.NoWritePermission):
        usable(company, MEMBER, world.root)


def test_a_collaborator_sees_the_usable_items_and_can_remove_a_project_share(company, world):
    data = usable(company, COLLABORATOR, world.root)
    shared = next(d for d in data["datasets"] if d["id"] == world.shared)
    assert shared["scope"] == "project" and shared["removable"] is True and shared["share_id"]

    access.unshare(company, COLLABORATOR, shared["share_id"])

    after = usable(company, COLLABORATOR, world.root)
    assert world.shared not in {d["id"] for d in after["datasets"]}


def test_a_public_dataset_is_listed_and_not_removable(company, world):
    data = usable(company, COLLABORATOR, world.root)

    public = next(d for d in data["datasets"] if d["id"] == world.public)
    assert public["scope"] == "public"
    assert public["removable"] is False
    assert public["share_id"] is None
    assert public["level"] == "read"


def test_a_credential_share_is_listed_with_its_fields_and_no_secret(company, world):
    data = usable(company, COLLABORATOR, world.root)

    credential = data["credentials"][0]
    assert credential["name"] == "team token"
    assert credential["host"] == "github.com"
    assert credential["path"] == "acme"
    assert credential["owner"] == CREDENTIAL_OWNER
    assert credential["scope"] == "project"
    assert credential["removable"] is True


def test_no_secret_is_ever_returned(company, world):
    data = usable(company, OWNER, world.root)

    assert not [key for key in _keys(data) if SECRET_KEY.search(str(key))]
    assert SECRET not in json.dumps(data)
