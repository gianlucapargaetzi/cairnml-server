# apiserver/tests/bll/test_deactivated_caller_writers.py
"""A deactivated caller changes nothing: neither the old field nor the share, whichever writer they call. And a
deactivated owner or grantee gets no queue access: the readers in bll/queue/queue_access.py see what bll/access.py
gives an active user only."""
from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from apiserver.apierrors import errors
from apiserver.apimodels.queues import UpdateRequest, UserRequest
from apiserver.bll import user_access
from apiserver.bll.queue.queue_access import QueueAccessBLL
from apiserver.bll.queue.queue_bll import QueueBLL
from apiserver.database.model.auth import Role, User
from apiserver.database.model.dataset import Dataset
from apiserver.database.model.project import Project, ProjectMember, ProjectRole
from apiserver.database.model.queue import Queue
from apiserver.database.model.share import Share
from apiserver.database.model.user_access import UserAccess
from apiserver.idp import git_credentials as store
from apiserver.service_repo.auth import Identity
from apiserver.services import queues as svc
from apiserver.tests.db import ensure_db
from apiserver.utilities.partial_version import PartialVersion

ensure_db()


@pytest.fixture
def company():
    return f"c-{uuid4().hex[:8]}"


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setattr(store, "key_material", lambda: "test-key-material")


@pytest.fixture
def gone(company):
    """A user who owns things while active, and is deactivated afterwards"""
    for user in ("gone", "u2"):
        User(id=user, name=user, company=company, created=datetime.utcnow()).save()
    Project(id="root1", company=company, user="gone", name="root1", basename="root1", created=datetime.utcnow(),
            members=[ProjectMember(user="u2", role=ProjectRole.read, added=datetime.utcnow(), added_by="gone")]).save()
    return company


def deactivate(company, user="gone"):
    User.objects(id=user, company=company).update(set__idp_disabled=True)


def call(company, data=None):
    return SimpleNamespace(identity=Identity(user="gone", company=company, role=Role.user),
                           data_model_for_partial_update=data or {},
                           requested_endpoint_version=PartialVersion("2.40"),
                           result=SimpleNamespace(data=None, data_model=None))


def share_count(company, resource_type, resource_id, scope):
    return Share.objects(company=company, resource_type=resource_type, resource_id=resource_id, scope=scope).count()


def test_a_deactivated_owner_cannot_grant_a_queue_to_a_user(gone):
    queue = QueueBLL.create(gone, name="queue gone", created_by="gone")
    deactivate(gone)
    with pytest.raises(errors.bad_request.InvalidQueueId):
        svc.add_user(call(gone), gone, UserRequest(queue=queue.id, user="u2"))
    assert share_count(gone, "queue", queue.id, "user") == 0


def test_a_deactivated_owner_cannot_take_back_a_queue_grant(gone):
    queue = QueueBLL.create(gone, name="queue gone", created_by="gone")
    svc.add_user(call(gone), gone, UserRequest(queue=queue.id, user="u2"))
    deactivate(gone)
    with pytest.raises(errors.bad_request.InvalidQueueId):
        svc.remove_user(call(gone), gone, UserRequest(queue=queue.id, user="u2"))
    assert share_count(gone, "queue", queue.id, "user") == 1


def test_a_deactivated_owner_cannot_make_a_queue_public(gone):
    queue = QueueBLL.create(gone, name="queue gone", created_by="gone")
    deactivate(gone)
    with pytest.raises(errors.bad_request.InvalidQueueId):
        svc.update(call(gone, {"public": True}), gone, UpdateRequest(queue=queue.id))
    assert Queue.objects(company=gone, id=queue.id).get().public is False
    assert share_count(gone, "queue", queue.id, "public") == 0


def test_a_deactivated_owner_cannot_change_the_visibility_of_a_dataset(gone):
    from apiserver.bll import datasets

    Dataset(id="d1", company=gone, kind="repo", ref="o/d1", name="d1", owner="gone", visibility="user").save()
    deactivate(gone)
    with pytest.raises(errors.forbidden.NoWritePermission):
        datasets.set_visibility(gone, "gone", "d1", "projects", ["root1"], False)
    dataset = Dataset.objects(company=gone, id="d1").get()
    assert dataset.visibility == "user"
    assert share_count(gone, "dataset", "d1", "projects") == 0


def test_a_deactivated_owner_cannot_grant_a_credential_to_a_project(gone):
    UserAccess(id="acc1", company=gone, user="gone", kind="token", label="t", host="github.com", path="",
               secret=store.encrypt("ghp", store.key_material())).save()
    deactivate(gone)
    with pytest.raises(errors.forbidden.NoWritePermission):
        user_access.grant(gone, "gone", "acc1", "root1")
    assert share_count(gone, "credential", "acc1", "projects") == 0


def ident(company, user):
    return Identity(user=user, company=company, role=Role.user)


def test_a_deactivated_owner_has_no_queue_access(gone):
    queue = QueueBLL.create(gone, name="queue gone", created_by="gone")
    assert queue.id in QueueAccessBLL.usable_queue_ids(ident(gone, "gone"), gone)
    deactivate(gone)
    assert queue.id not in QueueAccessBLL.usable_queue_ids(ident(gone, "gone"), gone)
    with pytest.raises(errors.bad_request.InvalidQueueId):
        QueueAccessBLL.assert_can_use(ident(gone, "gone"), gone, queue.id)
    with pytest.raises(errors.bad_request.InvalidQueueId):
        QueueAccessBLL.assert_can_manage(ident(gone, "gone"), gone, queue.id)
    with pytest.raises(errors.bad_request.InvalidQueueId):
        QueueAccessBLL.assert_can_serve(ident(gone, "gone"), gone, [queue.id])


def test_a_deactivated_grantee_has_no_queue_access(gone):
    queue = QueueBLL.create(gone, name="queue gone", created_by="gone")
    svc.add_user(call(gone), gone, UserRequest(queue=queue.id, user="u2"))
    QueueAccessBLL.assert_can_use(ident(gone, "u2"), gone, queue.id)
    deactivate(gone, user="u2")
    try:
        assert queue.id not in QueueAccessBLL.usable_queue_ids(ident(gone, "u2"), gone)
        with pytest.raises(errors.bad_request.InvalidQueueId):
            QueueAccessBLL.assert_can_use(ident(gone, "u2"), gone, queue.id)
    finally:
        # is_active looks the user up by id alone, so the flag must not outlive this test
        User.objects(id="u2", company=gone).update(set__idp_disabled=False)
