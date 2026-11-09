# apiserver/tests/services/test_unshare_by_project_collaborator.py
"""A collaborator of a project may revoke, through access.unshare, a dataset share and a credential share of that
project. The owners of the resources and a plain member of the project are not covered by this rule."""

from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from apiserver.apierrors import errors
from apiserver.bll.access import record_grant
from apiserver.database.model.auth import Role, User
from apiserver.database.model.dataset import Dataset
from apiserver.database.model.project import Project, ProjectMember, ProjectRole, ProjectVisibility
from apiserver.database.model.share import Share
from apiserver.database.model.user_access import UserAccess
from apiserver.service_repo.auth import Identity
from apiserver.services import access as svc
from apiserver.tests.db import ensure_db
from apiserver.utilities.partial_version import PartialVersion

ensure_db()

OWNER, COLLABORATOR, MEMBER, DATASET_OWNER, CREDENTIAL_OWNER = (
    "owner", "collaborator", "member", "dataset-owner", "credential-owner")


class FakeCall:
    def __init__(self, ident, data):
        self.identity = ident
        self.data = data
        self.requested_endpoint_version = PartialVersion("2.40")
        self.result = SimpleNamespace(data=None, data_model=None)


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
        ],
    )
    root.save()
    dataset = Dataset(id=uuid4().hex, company=company, kind="repo", ref="acme/cars", name="cars",
                      owner=DATASET_OWNER, created=datetime.utcnow())
    dataset.save()
    credential = UserAccess(id=uuid4().hex, company=company, user=CREDENTIAL_OWNER, kind="app", label="token",
                            host="github.com", path="", secret="s3cr3t", created=datetime.utcnow())
    credential.save()
    dataset_share = record_grant(company, DATASET_OWNER, "dataset", dataset.id, "projects", root.id,
                                 level="read", actor=DATASET_OWNER)
    credential_share = record_grant(company, CREDENTIAL_OWNER, "credential", credential.id, "projects", root.id,
                                    level="read", actor=CREDENTIAL_OWNER)
    return SimpleNamespace(root=root.id, dataset_share=dataset_share.id, credential_share=credential_share.id)


def unshare(company, user, share_id):
    svc.unshare(FakeCall(Identity(user=user, company=company, role=Role.user), {"share_id": share_id}), company, None)


def test_a_project_collaborator_revokes_a_dataset_share_of_the_project(company, world):
    unshare(company, COLLABORATOR, world.dataset_share)

    assert Share.objects(company=company, id=world.dataset_share).count() == 0


def test_a_project_collaborator_revokes_a_credential_share_of_the_project(company, world):
    unshare(company, COLLABORATOR, world.credential_share)

    assert Share.objects(company=company, id=world.credential_share).count() == 0


def test_a_plain_member_of_the_project_may_not_revoke_a_dataset_share(company, world):
    with pytest.raises(errors.bad_request.ValidationError):  # no level on the dataset: "no such dataset"
        unshare(company, MEMBER, world.dataset_share)

    assert Share.objects(company=company, id=world.dataset_share).count() == 1
