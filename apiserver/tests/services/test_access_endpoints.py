# apiserver/tests/services/test_access_endpoints.py
"""The share endpoints: access.list, access.share and access.unshare, and who may call them."""
from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from apiserver.apierrors import errors
from apiserver.bll.queue.queue_bll import QueueBLL
from apiserver.database.model.auth import Role, User
from apiserver.database.model.project import Project, ProjectMember, ProjectRole
from apiserver.database.model.share import Share
from apiserver.service_repo.auth import Identity
from apiserver.services import access as svc
from apiserver.utilities.partial_version import PartialVersion
from apiserver.tests.db import ensure_db

ensure_db()


@pytest.fixture
def company():
    return f"c-{uuid4().hex[:8]}"


def identity(company, user, role=Role.user):
    return Identity(user=user, company=company, role=role)


class FakeCall:
    def __init__(self, ident, data):
        self.identity = ident
        self.data = data
        self.requested_endpoint_version = PartialVersion("2.40")
        self.result = SimpleNamespace(data=None, data_model=None)


@pytest.fixture
def setup(company):
    for user in ("owner1", "u2", "stranger"):
        User(id=user, name=user, company=company, created=datetime.utcnow()).save()
    Project(id="root1", company=company, user="owner1", name="root1", basename="root1", created=datetime.utcnow(),
            members=[ProjectMember(user="u2", role=ProjectRole.read, added=datetime.utcnow(), added_by="owner1")]).save()
    return QueueBLL.create(company, name="queue endpoints", created_by="owner1")


def test_the_owner_shares_a_queue_with_a_project_lists_it_and_removes_it(company, setup):
    queue_id = setup.id
    svc.share(FakeCall(identity(company, "owner1"), {
        "resource_type": "queue", "resource_id": queue_id, "scope": "projects", "project": "root1", "level": "read",
    }), company, None)
    listed = FakeCall(identity(company, "owner1"), {"resource_type": "queue", "resource_id": queue_id})
    svc.list_shares(listed, company, None)
    assert [s["project"] for s in listed.result.data["shares"]] == ["root1"]
    assert listed.result.data["shares"][0]["granted_by"] == "owner1"

    svc.unshare(FakeCall(identity(company, "owner1"), {"share_id": listed.result.data["shares"][0]["id"]}),
                company, None)
    assert Share.objects(company=company, resource_type="queue", resource_id=queue_id).count() == 0


def test_a_user_who_may_not_manage_the_sharing_cannot_list_share_or_unshare(company, setup):
    queue_id = setup.id
    svc.share(FakeCall(identity(company, "owner1"), {
        "resource_type": "queue", "resource_id": queue_id, "scope": "public", "level": "read",
    }), company, None)
    share_id = Share.objects(company=company, resource_type="queue", resource_id=queue_id).first().id

    # the stranger sees the queue (it is public), so the refusal is for the level, not for the existence
    with pytest.raises(errors.forbidden.NoWritePermission):
        svc.list_shares(FakeCall(identity(company, "stranger"), {"resource_type": "queue", "resource_id": queue_id}),
                        company, None)
    with pytest.raises(errors.forbidden.NoWritePermission):
        svc.share(FakeCall(identity(company, "u2"), {
            "resource_type": "queue", "resource_id": queue_id, "scope": "public", "level": "read",
        }), company, None)
    with pytest.raises(errors.forbidden.NoWritePermission):
        svc.unshare(FakeCall(identity(company, "u2"), {"share_id": share_id}), company, None)
    assert Share.objects(company=company, id=share_id).count() == 1


def test_an_unknown_resource_type_and_a_repeated_share_are_refused(company, setup):
    with pytest.raises(errors.bad_request.ValidationError):
        svc.list_shares(FakeCall(identity(company, "owner1"), {"resource_type": "project", "resource_id": "x"}),
                        company, None)
    data = {"resource_type": "queue", "resource_id": setup.id, "scope": "public", "level": "read"}
    svc.share(FakeCall(identity(company, "owner1"), dict(data)), company, None)
    with pytest.raises(errors.bad_request.ValidationError):
        svc.share(FakeCall(identity(company, "owner1"), dict(data)), company, None)
