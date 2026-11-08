# apiserver/tests/services/test_unshare_queue_by_project_steward.py
"""The owner and the collaborators of a project may revoke, through access.unshare, a queue share with that project, as
they may a dataset or a credential share. A plain member of the project may not."""

from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from apiserver.apierrors import errors
from apiserver.bll.access import record_grant
from apiserver.database.model.auth import Role, User
from apiserver.database.model.project import Project, ProjectMember, ProjectRole, ProjectVisibility
from apiserver.database.model.queue import Queue
from apiserver.database.model.share import Share
from apiserver.service_repo.auth import Identity
from apiserver.services import access as svc
from apiserver.tests.db import ensure_db
from apiserver.utilities.partial_version import PartialVersion

ensure_db()

OWNER, COLLABORATOR, MEMBER, QUEUE_OWNER = "owner", "collaborator", "member", "queue-owner"


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
    for user in (OWNER, COLLABORATOR, MEMBER, QUEUE_OWNER):
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
    queue = Queue(id=uuid4().hex, company=company, name="gpu-queue", created=datetime.utcnow())
    queue.save()
    queue_share = record_grant(company, QUEUE_OWNER, "queue", queue.id, "projects", root.id, level="read",
                               actor=QUEUE_OWNER)
    return SimpleNamespace(queue_share=queue_share.id)


def unshare(company, user, share_id):
    svc.unshare(FakeCall(Identity(user=user, company=company, role=Role.user), {"share_id": share_id}), company, None)


def test_the_project_owner_revokes_a_queue_share_of_the_project(company, world):
    unshare(company, OWNER, world.queue_share)

    assert Share.objects(company=company, id=world.queue_share).count() == 0


def test_a_project_collaborator_revokes_a_queue_share_of_the_project(company, world):
    unshare(company, COLLABORATOR, world.queue_share)

    assert Share.objects(company=company, id=world.queue_share).count() == 0


def test_a_plain_member_of_the_project_may_not_revoke_a_queue_share(company, world):
    with pytest.raises(errors.bad_request.InvalidQueueId):  # no level on the queue: refused as not found
        unshare(company, MEMBER, world.queue_share)

    assert Share.objects(company=company, id=world.queue_share).count() == 1
