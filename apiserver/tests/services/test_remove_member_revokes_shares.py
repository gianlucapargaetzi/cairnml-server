# apiserver/tests/services/test_remove_member_revokes_shares.py
"""Removing a member of a project also revokes what that member shared with the project: the shares they granted, of
every resource type, inherited or not. Shares granted by other people stay."""

from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from apiserver.apimodels.project_access import RemoveMemberRequest
from apiserver.bll.access import record_grant
from apiserver.database.model.auth import Role, User
from apiserver.database.model.dataset import Dataset
from apiserver.database.model.project import Project, ProjectMember, ProjectRole, ProjectVisibility
from apiserver.database.model.queue import Queue
from apiserver.database.model.share import Share
from apiserver.database.model.user_access import UserAccess
from apiserver.service_repo.auth import Identity
from apiserver.services import project_access as svc
from apiserver.tests.db import ensure_db

ensure_db()

OWNER, COLLABORATOR, OTHER_MEMBER = "owner", "collaborator", "other-member"


class FakeCall:
    def __init__(self, ident):
        self.identity = ident
        self.result = SimpleNamespace(data=None, data_model=None)


@pytest.fixture
def company():
    return f"c-{uuid4().hex[:8]}"


@pytest.fixture
def world(company):
    for user in (OWNER, COLLABORATOR, OTHER_MEMBER):
        User(id=user, name=user, company=company, created=datetime.utcnow()).save()
    root = Project(
        id=uuid4().hex, company=company, user=OWNER, name="root", basename="root", created=datetime.utcnow(),
        visibility=ProjectVisibility.private,
        members=[
            ProjectMember(user=COLLABORATOR, role=ProjectRole.collaborator, added=datetime.utcnow(), added_by=OWNER),
            ProjectMember(user=OTHER_MEMBER, role=ProjectRole.collaborator, added=datetime.utcnow(), added_by=OWNER),
        ],
    )
    root.save()
    queue = Queue(id=uuid4().hex, company=company, name="gpu", created_by=COLLABORATOR, created=datetime.utcnow())
    queue.save()
    dataset = Dataset(id=uuid4().hex, company=company, kind="repo", ref="acme/cars", name="cars",
                      owner=COLLABORATOR, created=datetime.utcnow())
    dataset.save()
    credential = UserAccess(id=uuid4().hex, company=company, user=COLLABORATOR, kind="app", label="token",
                            host="github.com", path="", secret="s3cr3t", created=datetime.utcnow())
    credential.save()
    own_dataset = Dataset(id=uuid4().hex, company=company, kind="repo", ref="acme/trucks", name="trucks",
                          owner=OWNER, created=datetime.utcnow())
    own_dataset.save()
    return SimpleNamespace(
        root=root.id, queue=queue.id, dataset=dataset.id, credential=credential.id, own_dataset=own_dataset.id,
    )


def test_removing_a_member_revokes_the_shares_they_granted_to_the_project(company, world):
    queue_share = record_grant(company, COLLABORATOR, "queue", world.queue, "projects", world.root,
                               level="read", actor=COLLABORATOR)
    dataset_share = record_grant(company, COLLABORATOR, "dataset", world.dataset, "projects", world.root,
                                 level="read", actor=COLLABORATOR)
    credential_share = record_grant(company, COLLABORATOR, "credential", world.credential, "projects", world.root,
                                    level="read", actor=COLLABORATOR)
    unrelated = record_grant(company, OWNER, "dataset", world.own_dataset, "projects", world.root,
                             level="read", actor=OWNER)

    svc.remove_member(FakeCall(Identity(user=OWNER, company=company, role=Role.user)), company,
                      RemoveMemberRequest(project=world.root, user=COLLABORATOR))

    assert Share.objects(company=company, id__in=[queue_share.id, dataset_share.id, credential_share.id]).count() == 0
    assert Share.objects(company=company, id=unrelated.id).count() == 1
    assert [m.user for m in Project.objects(id=world.root).first().members] == [OTHER_MEMBER]


def test_removing_a_member_keeps_the_shares_granted_by_other_people(company, world):
    shared_by_other = record_grant(company, OTHER_MEMBER, "dataset", world.dataset, "projects", world.root,
                                   level="read", actor=OTHER_MEMBER)

    svc.remove_member(FakeCall(Identity(user=OWNER, company=company, role=Role.user)), company,
                      RemoveMemberRequest(project=world.root, user=COLLABORATOR))

    assert Share.objects(company=company, id=shared_by_other.id).count() == 1


def test_removing_a_member_revokes_their_inherited_shares(company, world):
    inherited = record_grant(company, COLLABORATOR, "dataset", world.dataset, "projects", world.root,
                             level="read", actor=COLLABORATOR, inherited=True, run_id="run-1")

    svc.remove_member(FakeCall(Identity(user=OWNER, company=company, role=Role.user)), company,
                      RemoveMemberRequest(project=world.root, user=COLLABORATOR))

    assert Share.objects(company=company, id=inherited.id).count() == 0
