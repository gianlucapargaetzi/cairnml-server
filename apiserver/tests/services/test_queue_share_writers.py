# apiserver/tests/services/test_queue_share_writers.py
"""Who may use a queue, and the share records that the queue writers keep in step with the old fields."""
from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from apiserver.apierrors import errors
from apiserver.apimodels.project_access import AddQueuesRequest, RemoveQueueRequest, TransferOwnershipRequest
from apiserver.apimodels.queues import DeleteRequest, QueueRequest, UpdateRequest, UserRequest
from apiserver.bll.queue.queue_access import QueueAccessBLL
from apiserver.bll.queue.queue_bll import QueueBLL
from apiserver.database.model.auth import Role, User
from apiserver.database.model.project import Project, ProjectMember, ProjectRole
from apiserver.database.model.queue import Queue
from apiserver.database.model.share import Share
from apiserver.service_repo.auth import Identity
from apiserver.services import project_access as project_svc
from apiserver.services import queues as svc
from apiserver.utilities.partial_version import PartialVersion
from apiserver.tests.db import ensure_db

ensure_db()


@pytest.fixture
def company():
    return f"c-{uuid4().hex[:8]}"


def identity(company, user, role=Role.user):
    return Identity(user=user, company=company, role=role)


class FakeCall:
    def __init__(self, ident, data_model=None):
        self.identity = ident
        self.requested_endpoint_version = PartialVersion("2.40")
        self.result = SimpleNamespace(data=None, data_model=None)
        self.data_model_for_partial_update = data_model or {}


def make_user(company, user_id):
    User(id=user_id, name=user_id, company=company, created=datetime.utcnow()).save()


def make_root(company, id_, owner, members=()):
    project = Project(
        id=id_, company=company, user=owner, name=id_, basename=id_, created=datetime.utcnow(),
        members=[ProjectMember(user=u, role=r, added=datetime.utcnow(), added_by=owner) for u, r in members],
    )
    project.save()
    return project


def _save_queue(company, owner, id_, public):
    queue = Queue(id=id_, company=company, name=f"queue {id_}", created=datetime.utcnow(), created_by=owner, public=public)
    queue.save()
    return queue


def can_use(company, user, queue_id):
    try:
        QueueAccessBLL.assert_can_use(identity(company, user), company, queue_id)
    except errors.bad_request.InvalidQueueId:
        return False
    return True


@pytest.fixture
def setup(company):
    """owner1 owns queue q1; u2 and u3 are users; root1 is owned by owner1 with member u2; root2 by u2"""
    for user in ("owner1", "u2", "u3", "stranger"):
        make_user(company, user)
    make_root(company, "root1", "owner1", members=[("u2", ProjectRole.read)])
    make_root(company, "root2", "u2")
    queue = _save_queue(company, "owner1", "q1", public=False)
    return queue


# --- characterisation: who may use the queue before any writer is changed


def test_a_user_grant_lets_that_user_use_the_queue_until_it_is_removed(company, setup):
    assert not can_use(company, "u2", "q1")
    svc.add_user(FakeCall(identity(company, "owner1")), company, UserRequest(queue="q1", user="u2"))
    assert can_use(company, "u2", "q1")
    assert not can_use(company, "u3", "q1")
    assert not can_use(company, "stranger", "q1")

    svc.remove_user(FakeCall(identity(company, "owner1")), company, UserRequest(queue="q1", user="u2"))
    assert not can_use(company, "u2", "q1")


def test_a_public_queue_is_usable_by_every_user_until_it_is_made_private(company, setup):
    assert not can_use(company, "stranger", "q1")
    svc.update(FakeCall(identity(company, "owner1"), {"public": True}), company, UpdateRequest(queue="q1"))
    assert can_use(company, "stranger", "q1")

    svc.update(FakeCall(identity(company, "owner1"), {"public": False}), company, UpdateRequest(queue="q1"))
    assert not can_use(company, "stranger", "q1")


def test_an_allocation_lets_the_members_of_the_project_use_the_queue_until_it_is_removed(company, setup):
    project_svc.add_queues(FakeCall(identity(company, "owner1")), company,
                           AddQueuesRequest(project="root1", queues=["q1"]))
    assert can_use(company, "u2", "q1")
    assert not can_use(company, "stranger", "q1")

    project_svc.remove_queue(FakeCall(identity(company, "owner1")), company,
                             RemoveQueueRequest(project="root1", queue="q1"))
    assert not can_use(company, "u2", "q1")


def test_removing_a_grant_takes_back_the_allocations_the_user_made(company, setup):
    svc.add_user(FakeCall(identity(company, "owner1")), company, UserRequest(queue="q1", user="u2"))
    project_svc.add_queues(FakeCall(identity(company, "u2")), company,
                           AddQueuesRequest(project="root2", queues=["q1"]))
    assert can_use(company, "u2", "q1")

    svc.remove_user(FakeCall(identity(company, "owner1")), company, UserRequest(queue="q1", user="u2"))
    assert Share.objects(company=company, resource_type="queue", resource_id="q1", scope="projects",
                         project="root2").count() == 0


# --- consistency: after each writer, the public flag and the users listed by the queue match its share records


def grants_of(company, queue_id):
    """(from the public flag and the users the queue lists, from the share records) for one queue"""
    queue = Queue.objects(company=company, id=queue_id).first()
    listing = FakeCall(identity(company, "owner1"))
    svc.get_users(listing, company, QueueRequest(queue=queue_id))
    old = {
        "public": bool(queue.public),
        "users": {user["user"] for user in listing.result.data["users"]},
    }
    shares = Share.objects(company=company, resource_type="queue", resource_id=queue_id)
    records = {
        "public": any(s.scope == "public" for s in shares),
        "users": {s.user for s in shares if s.scope == "user"},
    }
    return old, records


def assert_consistent(company, queue_id):
    old, records = grants_of(company, queue_id)
    assert old == records


def test_the_queue_shares_follow_every_queue_writer(company, setup):
    assert_consistent(company, "q1")

    svc.add_user(FakeCall(identity(company, "owner1")), company, UserRequest(queue="q1", user="u2"))
    svc.add_user(FakeCall(identity(company, "owner1")), company, UserRequest(queue="q1", user="u3"))
    assert_consistent(company, "q1")

    svc.update(FakeCall(identity(company, "owner1"), {"public": True}), company, UpdateRequest(queue="q1"))
    assert_consistent(company, "q1")

    project_svc.add_queues(FakeCall(identity(company, "owner1")), company,
                           AddQueuesRequest(project="root1", queues=["q1"]))
    assert_consistent(company, "q1")

    svc.remove_user(FakeCall(identity(company, "owner1")), company, UserRequest(queue="q1", user="u3"))
    assert_consistent(company, "q1")

    project_svc.remove_queue(FakeCall(identity(company, "owner1")), company,
                             RemoveQueueRequest(project="root1", queue="q1"))
    svc.update(FakeCall(identity(company, "owner1"), {"public": False}), company, UpdateRequest(queue="q1"))
    assert_consistent(company, "q1")


def test_the_grant_and_the_allocation_are_granted_by_the_right_user(company, setup):
    svc.add_user(FakeCall(identity(company, "owner1")), company, UserRequest(queue="q1", user="u2"))
    project_svc.add_queues(FakeCall(identity(company, "owner1")), company,
                           AddQueuesRequest(project="root1", queues=["q1"]))

    granted = {s.scope: s.granted_by for s in Share.objects(company=company, resource_type="queue", resource_id="q1")}
    assert granted == {"user": "owner1", "projects": "owner1"}


def test_a_public_queue_created_public_has_a_public_share(company):
    make_user(company, "owner1")
    queue = QueueBLL.create(company, name="queue public", created_by="owner1", public=True)
    assert_consistent(company, queue.id)


def test_deleting_a_queue_removes_its_shares(company, setup):
    svc.add_user(FakeCall(identity(company, "owner1")), company, UserRequest(queue="q1", user="u2"))
    project_svc.add_queues(FakeCall(identity(company, "owner1")), company,
                           AddQueuesRequest(project="root1", queues=["q1"]))
    svc.delete(FakeCall(identity(company, "owner1")), company, DeleteRequest(queue="q1", force=True))
    assert Share.objects(company=company, resource_type="queue", resource_id="q1").count() == 0


def test_transferring_a_project_regrants_its_queues_from_the_new_owner(company, setup):
    make_user(company, "u9")
    project_svc.add_queues(FakeCall(identity(company, "owner1")), company,
                           AddQueuesRequest(project="root1", queues=["q1"]))
    svc.add_user(FakeCall(identity(company, "owner1")), company, UserRequest(queue="q1", user="u9"))
    Project.objects(id="root1").update_one(push__members=ProjectMember(
        user="u9", role=ProjectRole.collaborator, added=datetime.utcnow(), added_by="owner1"))

    project_svc.transfer_ownership(FakeCall(identity(company, "owner1")), company,
                                   TransferOwnershipRequest(project="root1", new_owner="u9"))
    assert_consistent(company, "q1")
    granted = Share.objects(company=company, resource_type="queue", resource_id="q1", scope="projects").first()
    assert granted.granted_by == "u9"


# --- a failed share write fails the request


def test_a_failed_share_write_fails_the_request_and_leaves_no_grant(company, setup, monkeypatch):
    from apiserver.services import queues as queues_module

    def fail(*args, **kwargs):
        raise RuntimeError("the share write failed")

    monkeypatch.setattr(queues_module, "record_grant", fail)
    with pytest.raises(RuntimeError, match="the share write failed"):
        svc.add_user(FakeCall(identity(company, "owner1")), company, UserRequest(queue="q1", user="u2"))
    assert Share.objects(company=company, resource_type="queue", resource_id="q1", scope="user", user="u2").count() == 0
