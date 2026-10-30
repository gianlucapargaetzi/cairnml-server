# apiserver/tests/services/test_queue_permissions.py
from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from apiserver.apierrors import errors
from apiserver.apimodels.project_access import AddQueuesRequest, GetAssignableQueuesRequest, GetQueuesRequest, RemoveQueueRequest, TransferOwnershipRequest
from apiserver.apimodels.queues import CreateRequest, DeleteRequest, GetByIdRequest, QueueRequest, UserRequest
from apiserver.bll.queue.queue_access import QueueAccessBLL
from apiserver.database.model.auth import Role, User
from apiserver.database.model.project import Project, ProjectMember, ProjectRole, ProjectVisibility
from apiserver.database.model.queue import Queue, QueueUser
from apiserver.service_repo.auth import Identity
from apiserver.services import project_access as project_svc
from apiserver.services import queues as svc
from apiserver.tests.db import ensure_db
from apiserver.utilities.partial_version import PartialVersion

ensure_db()


@pytest.fixture
def company():
    return f"c-{uuid4().hex[:8]}"


def identity(company, user, role=Role.user):
    return Identity(user=user, company=company, role=role)


class FakeCall:
    def __init__(self, ident):
        self.identity = ident
        self.data = {}
        self.requested_endpoint_version = PartialVersion("2.38")
        self.result = SimpleNamespace(data=None, data_model=None)


def make_user(company, user_id):
    User(id=user_id, name=user_id, company=company, created=datetime.utcnow()).save()


def make_queue(company, name=None, public=False, users=(), system_tags=None):
    queue = Queue(
        id=uuid4().hex, company=company, name=name or f"q-{uuid4().hex[:6]}", created=datetime.utcnow(),
        public=public, system_tags=system_tags or [],
        users=[QueueUser(user=u, added=datetime.utcnow(), added_by="admin") for u in users],
    )
    queue.save()
    return queue


def make_root(company, owner="owner", members=(), queues=(), id_=None):
    project = Project(
        id=id_ or uuid4().hex, company=company, user=owner, name=f"p-{uuid4().hex[:6]}", basename="p",
        created=datetime.utcnow(), visibility=ProjectVisibility.private,
        members=[ProjectMember(user=u, role=r, added=datetime.utcnow(), added_by=owner) for u, r in members],
        queues=list(queues),
    )
    project.save()
    return project


def reload_root(project):
    return Project.objects(id=project.id).first()


# --- admin-only management -------------------------------------------------

def test_a_non_admin_cannot_create_a_queue(company):
    with pytest.raises(errors.forbidden.NoWritePermission):
        svc.create(FakeCall(identity(company, "u1")), company, CreateRequest(name="new-queue"))


def test_a_non_admin_cannot_delete_a_queue(company):
    queue = make_queue(company)
    with pytest.raises(errors.forbidden.NoWritePermission):
        svc.delete(FakeCall(identity(company, "u1")), company, DeleteRequest(queue=queue.id))


def test_an_admin_can_create_a_public_queue(company):
    call = FakeCall(identity(company, "a1", Role.admin))
    svc.create(call, company, CreateRequest(name="public-queue", public=True))
    queue = Queue.objects(company=company, name="public-queue").first()
    assert queue.public is True


def test_an_admin_is_allowed_to_manage_queues(company):
    assert QueueAccessBLL.is_admin(identity(company, "a1", Role.admin))
    assert QueueAccessBLL.is_admin(identity(company, "r1", Role.root))
    assert not QueueAccessBLL.is_admin(identity(company, "u1"))


# --- use rule --------------------------------------------------------------

def test_a_queue_without_grant_public_or_allocation_is_not_found_for_a_user(company):
    queue = make_queue(company)
    with pytest.raises(errors.bad_request.InvalidQueueId):
        svc.get_by_id(FakeCall(identity(company, "u1")), company, GetByIdRequest(queue=queue.id))


def test_a_grant_lets_the_user_use_the_queue(company):
    make_user(company, "u1")
    queue = make_queue(company, users=["u1"])
    assert QueueAccessBLL.usable_queue_ids(identity(company, "u1"), company) == [queue.id]


def test_a_public_queue_is_usable_by_every_user(company):
    queue = make_queue(company, public=True)
    assert queue.id in QueueAccessBLL.usable_queue_ids(identity(company, "stranger"), company)


def test_members_of_a_project_may_use_its_allocated_queues_in_any_role(company):
    queue = make_queue(company)
    make_root(company, owner="owner", members=[("reader", ProjectRole.read)], queues=[queue.id])
    assert queue.id in QueueAccessBLL.usable_queue_ids(identity(company, "reader"), company)
    assert queue.id not in QueueAccessBLL.usable_queue_ids(identity(company, "stranger"), company)


def test_admins_may_use_every_queue(company):
    queue = make_queue(company)
    assert queue.id in QueueAccessBLL.usable_queue_ids(identity(company, "a1", Role.admin), company)


# --- allocation ------------------------------------------------------------

def test_an_owner_with_a_grant_can_allocate_the_granted_queue(company):
    queue = make_queue(company, users=["owner"])
    root = make_root(company, owner="owner")
    project_svc.add_queues(FakeCall(identity(company, "owner")), company, AddQueuesRequest(project=root.id, queues=[queue.id]))
    assert reload_root(root).queues == [queue.id]


def test_an_owner_without_a_grant_cannot_allocate_a_private_queue(company):
    queue = make_queue(company)
    root = make_root(company, owner="owner")
    with pytest.raises(errors.bad_request.InvalidQueueId):
        project_svc.add_queues(FakeCall(identity(company, "owner")), company, AddQueuesRequest(project=root.id, queues=[queue.id]))
    assert reload_root(root).queues == []


def test_an_admin_who_is_not_the_owner_cannot_allocate(company):
    queue = make_queue(company, public=True)
    root = make_root(company, owner="owner")
    with pytest.raises(errors.forbidden.NoWritePermission):
        project_svc.add_queues(FakeCall(identity(company, "admin", Role.admin)), company, AddQueuesRequest(project=root.id, queues=[queue.id]))


def test_a_member_cannot_allocate_even_with_the_right_role(company):
    queue = make_queue(company, public=True)
    root = make_root(company, owner="owner", members=[("collab", ProjectRole.collaborator)])
    with pytest.raises(errors.forbidden.NoWritePermission):
        project_svc.add_queues(FakeCall(identity(company, "collab")), company, AddQueuesRequest(project=root.id, queues=[queue.id]))


def test_a_queue_is_refused_twice_and_in_one_request_twice(company):
    queue = make_queue(company, public=True)
    root = make_root(company, owner="owner")
    with pytest.raises(errors.bad_request.ValidationError):
        project_svc.add_queues(FakeCall(identity(company, "owner")), company, AddQueuesRequest(project=root.id, queues=[queue.id, queue.id]))
    project_svc.add_queues(FakeCall(identity(company, "owner")), company, AddQueuesRequest(project=root.id, queues=[queue.id]))
    with pytest.raises(errors.bad_request.ValidationError):
        project_svc.add_queues(FakeCall(identity(company, "owner")), company, AddQueuesRequest(project=root.id, queues=[queue.id]))


def test_several_queues_can_be_added_in_one_action(company):
    first = make_queue(company, public=True)
    second = make_queue(company, public=True)
    root = make_root(company, owner="owner")
    project_svc.add_queues(FakeCall(identity(company, "owner")), company, AddQueuesRequest(project=root.id, queues=[first.id, second.id]))
    assert set(reload_root(root).queues) == {first.id, second.id}


def test_the_owner_sees_only_the_queues_they_may_allocate(company):
    public = make_queue(company, public=True)
    granted = make_queue(company, users=["owner"])
    make_queue(company)
    root = make_root(company, owner="owner")
    result = FakeCall(identity(company, "owner"))
    project_svc.get_assignable_queues(result, company, GetAssignableQueuesRequest(project=root.id))
    assert {q["id"] for q in result.result.data["queues"]} == {public.id, granted.id}


def test_a_member_sees_the_allocated_queues_without_a_grant(company):
    queue = make_queue(company)
    root = make_root(company, owner="owner", members=[("reader", ProjectRole.read)], queues=[queue.id])
    result = FakeCall(identity(company, "reader"))
    project_svc.get_queues(result, company, GetQueuesRequest(project=root.id))
    assert [q["id"] for q in result.result.data["queues"]] == [queue.id]


def test_only_the_owner_removes_an_allocation(company):
    queue = make_queue(company, public=True)
    root = make_root(company, owner="owner", members=[("collab", ProjectRole.collaborator)], queues=[queue.id])
    with pytest.raises(errors.forbidden.NoWritePermission):
        project_svc.remove_queue(FakeCall(identity(company, "collab")), company, RemoveQueueRequest(project=root.id, queue=queue.id))
    project_svc.remove_queue(FakeCall(identity(company, "owner")), company, RemoveQueueRequest(project=root.id, queue=queue.id))
    assert reload_root(root).queues == []


# --- cascades --------------------------------------------------------------

def test_removing_a_grant_removes_the_allocations_of_projects_the_user_owns(company):
    make_user(company, "owner")
    make_user(company, "other")
    queue = make_queue(company, users=["owner"])
    root = make_root(company, owner="owner", queues=[queue.id])
    other_root = make_root(company, owner="other", queues=[queue.id])
    svc.remove_user(FakeCall(identity(company, "admin", Role.admin)), company, UserRequest(queue=queue.id, user="owner"))
    assert reload_root(root).queues == []
    assert reload_root(other_root).queues == [queue.id]


def test_deleting_a_queue_removes_it_from_every_allocation(company):
    queue = make_queue(company, public=True)
    root = make_root(company, owner="owner", queues=[queue.id])
    svc.delete(FakeCall(identity(company, "admin", Role.admin)), company, DeleteRequest(queue=queue.id))
    assert reload_root(root).queues == []


def test_transfer_drops_allocations_the_new_owner_may_not_allocate(company):
    make_user(company, "new-owner")
    public = make_queue(company, public=True)
    private = make_queue(company)
    root = make_root(company, owner="owner", members=[("new-owner", ProjectRole.read)], queues=[public.id, private.id])
    project_svc.transfer_ownership(FakeCall(identity(company, "owner")), company, TransferOwnershipRequest(project=root.id, new_owner="new-owner"))
    assert reload_root(root).queues == [public.id]


# --- defaults --------------------------------------------------------------

def test_the_default_queue_is_public_on_a_new_install(company):
    from apiserver.mongo.initialize.util import _ensure_default_queue

    _ensure_default_queue(company)
    default = Queue.objects(company=company, system_tags="default").first()
    assert default.public is True


# --- grants endpoints -------------------------------------------------------

def test_an_admin_grants_and_removes_a_user_through_the_endpoints(company):
    make_user(company, "u1")
    queue = make_queue(company)
    admin = FakeCall(identity(company, "a1", Role.admin))
    svc.add_user(admin, company, UserRequest(queue=queue.id, user="u1"))
    assert Queue.objects(id=queue.id).first().users[0].user == "u1"

    listing = FakeCall(identity(company, "a1", Role.admin))
    svc.get_users(listing, company, QueueRequest(queue=queue.id))
    assert [u["user"] for u in listing.result.data["users"]] == ["u1"]

    svc.remove_user(admin, company, UserRequest(queue=queue.id, user="u1"))
    assert Queue.objects(id=queue.id).first().users == []


def test_a_non_admin_cannot_grant_access(company):
    make_user(company, "u1")
    queue = make_queue(company)
    with pytest.raises(errors.forbidden.NoWritePermission):
        svc.add_user(FakeCall(identity(company, "u2")), company, UserRequest(queue=queue.id, user="u1"))


# --- agent tokens ------------------------------------------------------------

class AgentCall:
    def __init__(self, name, token_kind):
        self.identity = identity("c-agent", "device-user", Role.admin)
        self.auth = SimpleNamespace(token_kind=token_kind)


def _endpoint(name):
    return SimpleNamespace(name=name, authorize=False, allows=lambda role: True)


def test_an_agent_token_cannot_administer_people_devices_or_projects():
    from apiserver.service_repo.validators import validate_role

    for name in ["users.get_all", "devices.revoke", "projects.add_member", "projects.transfer_ownership", "queues.create", "queues.add_user"]:
        with pytest.raises(errors.forbidden.NoWritePermission):
            validate_role(_endpoint(name), AgentCall(name, "agent"))


def test_an_agent_token_keeps_what_the_agent_does():
    from apiserver.service_repo.validators import validate_role

    for name in ["queues.get_next_task", "tasks.started", "workers.get_all", "devices.exchange"]:
        validate_role(_endpoint(name), AgentCall(name, "agent"))


def test_a_user_token_is_not_limited_by_the_agent_rule():
    from apiserver.service_repo.validators import validate_role

    validate_role(_endpoint("projects.add_member"), AgentCall("projects.add_member", "web"))
