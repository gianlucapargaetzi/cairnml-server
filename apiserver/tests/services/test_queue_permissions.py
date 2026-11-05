# apiserver/tests/services/test_queue_permissions.py
from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from apiserver.apierrors import errors
from apiserver.apimodels.project_access import AddQueuesRequest, GetAssignableQueuesRequest, GetQueuesRequest, RemoveQueueRequest, TransferOwnershipRequest
from apiserver.apimodels.metadata import AddOrUpdateMetadata, DeleteMetadata, MetadataItem
from apiserver.apimodels.queues import (
    AddOrUpdateMetadataRequest,
    GetAllRequest,
    CreateRequest,
    DeleteMetadataRequest,
    DeleteRequest,
    GetByIdRequest,
    QueueRequest,
    UpdateRequest,
    UserRequest,
)
from apiserver.apimodels.tasks import DequeueRequest
from apiserver.bll.queue.queue_access import QueueAccessBLL
from apiserver.database.model.auth import Role, User
from apiserver.database.model.project import Project, ProjectMember, ProjectRole, ProjectVisibility
from apiserver.database.model.queue import Entry, Queue, QueueUser
from apiserver.database.model.share import Share
from apiserver.database.model.task.task import Execution, Task, TaskStatus
from apiserver.service_repo.auth import Identity
from apiserver.services import project_access as project_svc
from apiserver.services import queues as svc
from apiserver.services import tasks as tasks_svc
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


class UpdateCall(FakeCall):
    def __init__(self, ident, **data):
        super().__init__(ident)
        self.data = data
        self.data_model_for_partial_update = data


def share_of(company, resource_type, resource_id, scope, granted_by, project="", user=""):
    """ The share record the writers keep next to each old field """
    Share(
        id=uuid4().hex, company=company, resource_type=resource_type, resource_id=resource_id, scope=scope,
        project=project, user=user, level="read", granted_by=granted_by, created=datetime.utcnow(),
    ).save()


def make_queue(company, name=None, public=False, users=(), system_tags=None, created_by=None):
    queue = Queue(
        id=uuid4().hex, company=company, name=name or f"q-{uuid4().hex[:6]}", created=datetime.utcnow(),
        public=public, system_tags=system_tags or [], created_by=created_by,
        users=[QueueUser(user=u, added=datetime.utcnow(), added_by="admin") for u in users],
    )
    queue.save()
    if public:
        share_of(company, "queue", queue.id, "public", created_by or "")
    for user in users:
        share_of(company, "queue", queue.id, "user", "admin", user=user)
    return queue


def make_root(company, owner="owner", members=(), queues=(), id_=None):
    project = Project(
        id=id_ or uuid4().hex, company=company, user=owner, name=f"p-{uuid4().hex[:6]}", basename="p",
        created=datetime.utcnow(), visibility=ProjectVisibility.private,
        members=[ProjectMember(user=u, role=r, added=datetime.utcnow(), added_by=owner) for u, r in members],
        queues=list(queues),
    )
    project.save()
    for queue_id in queues:
        share_of(company, "queue", queue_id, "projects", owner, project=project.id)
    return project


def reload_root(project):
    return Project.objects(id=project.id).first()


def visible_ids(company, ident):
    return set(Queue.objects(company=company).filter(QueueAccessBLL.visible_query(ident)).scalar("id"))


# --- creation and admin rights --------------------------------------------

def test_a_non_admin_creates_a_queue_and_becomes_its_owner(company):
    call = FakeCall(identity(company, "u1"))
    svc.create(call, company, CreateRequest(name="user-queue"))
    queue = Queue.objects(company=company, name="user-queue").first()
    assert queue.created_by == "u1"
    assert queue.public is False
    assert call.result.data == {"id": queue.id}


def test_a_non_admin_creates_a_public_queue_as_its_owner(company):
    svc.create(FakeCall(identity(company, "u1")), company, CreateRequest(name="user-public", public=True))
    queue = Queue.objects(company=company, name="user-public").first()
    assert queue.public is True
    assert queue.created_by == "u1"


def test_an_admin_creating_a_queue_becomes_its_owner_too(company):
    svc.create(FakeCall(identity(company, "a1", Role.admin)), company, CreateRequest(name="admin-queue"))
    assert Queue.objects(company=company, name="admin-queue").first().created_by == "a1"


def test_an_admin_is_allowed_to_manage_queues(company):
    assert QueueAccessBLL.is_admin(identity(company, "a1", Role.admin))
    assert QueueAccessBLL.is_admin(identity(company, "r1", Role.root))
    assert not QueueAccessBLL.is_admin(identity(company, "u1"))


def test_an_admin_creates_a_public_queue(company):
    call = FakeCall(identity(company, "a1", Role.admin))
    svc.create(call, company, CreateRequest(name="public-queue", public=True))
    queue = Queue.objects(company=company, name="public-queue").first()
    assert queue.public is True


# --- ownership -------------------------------------------------------------

def test_the_owner_sees_their_queue_with_its_owner(company):
    svc.create(FakeCall(identity(company, "owner")), company, CreateRequest(name="owned-queue"))
    queue = Queue.objects(company=company, name="owned-queue").first()
    call = FakeCall(identity(company, "owner"))
    svc.get_by_id(call, company, GetByIdRequest(queue=queue.id))
    assert call.result.data["queue"]["created_by"] == "owner"


def test_the_queue_output_reports_its_owner_or_null_before_ownership(company):
    legacy = make_queue(company, name="legacy-queue", public=True)
    svc.create(FakeCall(identity(company, "owner")), company, CreateRequest(name="owned-queue"))
    owned = Queue.objects(company=company, name="owned-queue").first()

    listing = FakeCall(identity(company, "a1", Role.admin))
    svc.get_all(listing, company, GetAllRequest())
    by_id = {q["id"]: q for q in listing.result.data["queues"]}
    assert by_id[legacy.id]["created_by"] is None
    assert by_id[owned.id]["created_by"] == "owner"

    single = FakeCall(identity(company, "a1", Role.admin))
    svc.get_by_id(single, company, GetByIdRequest(queue=legacy.id))
    assert single.result.data["queue"]["created_by"] is None


def test_the_owner_updates_and_deletes_their_queue(company):
    queue = make_queue(company, created_by="owner")
    svc.update(UpdateCall(identity(company, "owner"), tags=["mine"]), company, UpdateRequest(queue=queue.id, tags=["mine"]))
    assert Queue.objects(id=queue.id).first().tags == ["mine"]
    svc.delete(FakeCall(identity(company, "owner")), company, DeleteRequest(queue=queue.id))
    assert Queue.objects(id=queue.id).first() is None


def test_the_owner_manages_grants_and_metadata(company):
    make_user(company, "u2")
    queue = make_queue(company, created_by="owner")
    owner = identity(company, "owner")

    svc.add_user(FakeCall(owner), company, UserRequest(queue=queue.id, user="u2"))
    listing = FakeCall(owner)
    svc.get_users(listing, company, QueueRequest(queue=queue.id))
    assert [u["user"] for u in listing.result.data["users"]] == ["u2"]

    svc.add_or_update_metadata(
        FakeCall(owner), company,
        AddOrUpdateMetadataRequest(queue=queue.id, metadata=[MetadataItem(key="k", type="str", value="v")]),
    )
    assert "k" in Queue.objects(id=queue.id).first().metadata
    svc.delete_metadata(FakeCall(owner), company, DeleteMetadataRequest(queue=queue.id, keys=["k"]))
    assert "k" not in (Queue.objects(id=queue.id).first().metadata or {})

    svc.remove_user(FakeCall(owner), company, UserRequest(queue=queue.id, user="u2"))
    assert Queue.objects(id=queue.id).first().users == []


def test_a_stranger_does_not_see_or_manage_a_queue_they_do_not_own(company):
    make_user(company, "u2")
    queue = make_queue(company, created_by="owner")
    stranger = identity(company, "u2")
    assert queue.id not in visible_ids(company, stranger)

    with pytest.raises(errors.bad_request.InvalidQueueId):
        svc.get_by_id(FakeCall(stranger), company, GetByIdRequest(queue=queue.id))
    with pytest.raises(errors.bad_request.InvalidQueueId):
        svc.update(UpdateCall(stranger, tags=["x"]), company, UpdateRequest(queue=queue.id, tags=["x"]))
    with pytest.raises(errors.bad_request.InvalidQueueId):
        svc.delete(FakeCall(stranger), company, DeleteRequest(queue=queue.id))
    with pytest.raises(errors.bad_request.InvalidQueueId):
        svc.get_users(FakeCall(stranger), company, QueueRequest(queue=queue.id))
    with pytest.raises(errors.bad_request.InvalidQueueId):
        svc.add_user(FakeCall(stranger), company, UserRequest(queue=queue.id, user="u2"))


def test_a_grantee_uses_the_queue_but_cannot_manage_it(company):
    queue = make_queue(company, created_by="owner", users=["u2"])
    grantee = identity(company, "u2")
    assert queue.id in visible_ids(company, grantee)

    call = FakeCall(grantee)
    svc.get_by_id(call, company, GetByIdRequest(queue=queue.id))
    assert call.result.data["queue"]["id"] == queue.id

    with pytest.raises(errors.forbidden.NoWritePermission):
        svc.update(UpdateCall(grantee, tags=["x"]), company, UpdateRequest(queue=queue.id, tags=["x"]))
    with pytest.raises(errors.forbidden.NoWritePermission):
        svc.delete(FakeCall(grantee), company, DeleteRequest(queue=queue.id))
    with pytest.raises(errors.forbidden.NoWritePermission):
        svc.get_users(FakeCall(grantee), company, QueueRequest(queue=queue.id))
    with pytest.raises(errors.forbidden.NoWritePermission):
        svc.add_user(FakeCall(grantee), company, UserRequest(queue=queue.id, user="u3"))
    with pytest.raises(errors.forbidden.NoWritePermission):
        svc.remove_user(FakeCall(grantee), company, UserRequest(queue=queue.id, user="u2"))
    with pytest.raises(errors.forbidden.NoWritePermission):
        svc.add_or_update_metadata(
            FakeCall(grantee), company,
            AddOrUpdateMetadataRequest(queue=queue.id, metadata=[MetadataItem(key="k", type="str", value="v")]),
        )
    with pytest.raises(errors.forbidden.NoWritePermission):
        svc.delete_metadata(FakeCall(grantee), company, DeleteMetadataRequest(queue=queue.id, keys=["k"]))


def test_a_member_of_an_allocated_project_sees_and_uses_the_queue_but_cannot_manage_it(company):
    queue = make_queue(company, created_by="owner")
    make_root(company, owner="owner", members=[("reader", ProjectRole.read)], queues=[queue.id])
    member = identity(company, "reader")
    assert queue.id in visible_ids(company, member)
    assert queue.id in QueueAccessBLL.usable_queue_ids(member, company)
    with pytest.raises(errors.forbidden.NoWritePermission):
        svc.delete(FakeCall(member), company, DeleteRequest(queue=queue.id))


def test_admins_see_and_manage_every_queue(company):
    queue = make_queue(company, created_by="owner")
    admin = identity(company, "a1", Role.admin)
    assert queue.id in visible_ids(company, admin)
    svc.update(UpdateCall(admin, tags=["by-admin"]), company, UpdateRequest(queue=queue.id, tags=["by-admin"]))
    assert Queue.objects(id=queue.id).first().tags == ["by-admin"]


def test_public_queues_stay_visible_to_everyone(company):
    queue = make_queue(company, public=True)
    assert queue.id in visible_ids(company, identity(company, "stranger"))


def test_the_owner_sets_the_public_flag(company):
    queue = make_queue(company, created_by="owner")
    svc.update(UpdateCall(identity(company, "owner"), public=True), company, UpdateRequest(queue=queue.id, public=True))
    assert Queue.objects(id=queue.id).first().public is True


def test_a_grantee_cannot_set_the_public_flag(company):
    queue = make_queue(company, created_by="owner", users=["u1"])
    with pytest.raises(errors.forbidden.NoWritePermission):
        svc.update(UpdateCall(identity(company, "u1"), public=True), company, UpdateRequest(queue=queue.id, public=True))
    assert Queue.objects(id=queue.id).first().public is False


# --- allocation of owned queues --------------------------------------------

def test_the_project_owner_allocates_a_queue_they_own(company):
    queue = make_queue(company, created_by="owner")
    root = make_root(company, owner="owner")
    assert queue.id in QueueAccessBLL.assignable_queue_ids(company, "owner")
    project_svc.add_queues(FakeCall(identity(company, "owner")), company, AddQueuesRequest(project=root.id, queues=[queue.id]))
    assert reload_root(root).queues == [queue.id]


def test_the_owner_of_a_queue_who_does_not_own_the_project_cannot_allocate_it(company):
    queue = make_queue(company, created_by="u1")
    root = make_root(company, owner="owner")
    with pytest.raises(errors.forbidden.NoWritePermission):
        project_svc.add_queues(FakeCall(identity(company, "u1")), company, AddQueuesRequest(project=root.id, queues=[queue.id]))


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


# --- enqueueing --------------------------------------------------------------

def test_enqueueing_into_a_queue_the_user_may_not_see_is_refused(company, monkeypatch):
    from apiserver.bll.task.task_operations import enqueue_task
    from apiserver.database.model.task.task import Task
    from apiserver.idp import github_check

    monkeypatch.setattr(github_check, "build_from_config", lambda: None)
    monkeypatch.setattr(github_check, "check_submission", lambda task, config: None)
    make_user(company, "u2")
    queue = make_queue(company, created_by="owner")
    task = Task(id=uuid4().hex, company=company, user="u2", name="task-one", type="training", created=datetime.utcnow())
    task.save()

    with pytest.raises(errors.bad_request.InvalidQueueId):
        enqueue_task(
            task_id=task.id, company_id=company, identity=identity(company, "u2"), queue_id=queue.id,
            status_message="", status_reason="",
        )
    assert Task.objects(id=task.id).first().status != "queued"


# --- agents serving a queue ------------------------------------------------

def test_an_agent_may_serve_only_the_queues_its_user_owns_or_is_granted(company):
    make_user(company, "u1")
    owned = make_queue(company, created_by="u1")
    granted = make_queue(company, created_by="owner", users=["u1"])
    other = make_queue(company, created_by="owner")
    public = make_queue(company, public=True)
    user = identity(company, "u1")

    QueueAccessBLL.assert_can_serve(user, company, [owned.id, granted.id])
    QueueAccessBLL.assert_can_serve(user, company, [owned.id, public.id])
    with pytest.raises(errors.bad_request.InvalidQueueId):
        QueueAccessBLL.assert_can_serve(user, company, [owned.id, other.id])


def test_an_admin_may_have_an_agent_serve_any_queue(company):
    queue = make_queue(company, created_by="owner")
    QueueAccessBLL.assert_can_serve(identity(company, "a1", Role.admin), company, [queue.id])


@pytest.mark.needs_redis
def test_an_agent_registration_for_a_queue_the_user_may_not_serve_is_refused(company):
    from apiserver.bll.workers import WorkerBLL
    from apiserver.database.model.company import Company

    Company(id=company, name=company).save()
    make_user(company, "u1")
    other = make_queue(company, created_by="owner")
    worker_bll = WorkerBLL()

    with pytest.raises(errors.bad_request.InvalidQueueId):
        worker_bll.register_worker(
            company_id=company, user_id="u1", worker=f"w-{uuid4().hex[:6]}", queues=[other.id],
            identity=identity(company, "u1"),
        )


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


# --- projects of a queue ------------------------------------------------------

def projects_of(company, ident, queue_id):
    call = FakeCall(ident)
    svc.get_projects(call, company, QueueRequest(queue=queue_id))
    return {p["id"]: p["name"] for p in call.result.data["projects"]}


def test_the_owner_sees_the_projects_of_their_queue_that_they_own(company):
    queue = make_queue(company, created_by="owner")
    mine = make_root(company, owner="owner", queues=[queue.id])
    make_root(company, owner="other", queues=[queue.id])
    assert set(projects_of(company, identity(company, "owner"), queue.id)) == {mine.id}


def test_a_root_project_member_sees_only_the_projects_they_are_in(company):
    queue = make_queue(company, created_by="queue-owner")
    mine = make_root(company, owner="owner1", members=[("member", ProjectRole.read)], queues=[queue.id])
    other = make_root(company, owner="owner2", members=[("someone", ProjectRole.read_write)], queues=[queue.id])
    result = projects_of(company, identity(company, "member"), queue.id)
    assert set(result) == {mine.id}
    assert other.name not in result.values()


def test_an_admin_sees_every_root_project_allocated_to_the_queue(company):
    queue = make_queue(company, created_by="queue-owner")
    first = make_root(company, owner="owner1", queues=[queue.id])
    second = make_root(company, owner="owner2", queues=[queue.id])
    result = projects_of(company, identity(company, "a1", Role.admin), queue.id)
    assert set(result) == {first.id, second.id}


def test_projects_of_a_queue_the_user_cannot_use_are_refused(company):
    queue = make_queue(company, created_by="owner")
    make_root(company, owner="owner", queues=[queue.id])
    with pytest.raises(errors.bad_request.InvalidQueueId):
        svc.get_projects(FakeCall(identity(company, "stranger")), company, QueueRequest(queue=queue.id))


def test_child_projects_are_not_returned_as_queue_projects(company):
    queue = make_queue(company, created_by="owner")
    root = make_root(company, owner="owner", queues=[queue.id])
    Project(
        id=uuid4().hex, company=company, user="owner", name="child", basename="child",
        created=datetime.utcnow(), visibility=ProjectVisibility.private, parent=root.id, queues=[queue.id],
    ).save()
    assert set(projects_of(company, identity(company, "owner"), queue.id)) == {root.id}
    assert set(projects_of(company, identity(company, "a1", Role.admin), queue.id)) == {root.id}


# --- dequeue -----------------------------------------------------------------

def make_queued_task(company, queue, owner, task_id):
    now = datetime.utcnow()
    task = Task(
        id=task_id, company=company, user=owner, name=task_id, type="training", created=now,
        status=TaskStatus.queued, execution=Execution(queue=queue.id),
    )
    task.save()
    Queue.objects(id=queue.id).update_one(push__entries=Entry(task=task_id, added=now))
    return task


def test_dequeue_of_a_task_in_a_queue_the_caller_can_no_longer_use_is_refused(company):
    queue = make_queue(company, created_by="owner", users=["u2"])
    Share.objects(company=company, resource_type="queue", resource_id=queue.id, scope="user", user="u2").delete()
    make_queued_task(company, queue, "u2", "task-refused")
    with pytest.raises(errors.bad_request.InvalidQueueId):
        tasks_svc.dequeue(FakeCall(identity(company, "u2")), company, DequeueRequest(task="task-refused"))
    assert Task.objects(id="task-refused").first().status == TaskStatus.queued
    assert [e.task for e in Queue.objects(id=queue.id).first().entries] == ["task-refused"]


@pytest.mark.needs_redis
def test_dequeue_of_a_task_in_a_queue_the_caller_can_use_works(company, monkeypatch):
    from apiserver.bll.queue.queue_metrics import QueueMetrics

    # the queue metrics go to Elasticsearch, which this check does not need
    monkeypatch.setattr(QueueMetrics, "log_queue_metrics_to_es", lambda self, company_id, queues: 0)
    queue = make_queue(company, created_by="owner", users=["u2"])
    make_queued_task(company, queue, "u2", "task-usable")
    call = FakeCall(identity(company, "u2"))
    tasks_svc.dequeue(call, company, DequeueRequest(task="task-usable"))
    assert call.result.data_model.dequeued == 1
    assert Queue.objects(id=queue.id).first().entries == []
