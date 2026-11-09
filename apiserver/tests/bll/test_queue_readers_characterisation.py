# apiserver/tests/bll/test_queue_readers_characterisation.py
"""Pins what the queue readers decide today: who sees, uses, manages and serves a queue, which queues a project may
allocate, and the messages of every refusal. The state is built through the endpoints and writers, so the old fields
and the share records agree. These tests must pass before and after the readers move to bll/access.py."""

from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from apiserver.apimodels.project_access import AddQueuesRequest, RemoveQueueRequest
from apiserver.apimodels.queues import DeleteRequest, QueueRequest, UpdateRequest, UserRequest
from apiserver.bll.access import resource_ids_shared_with
from apiserver.bll.queue.queue_access import QueueAccessBLL
from apiserver.database.model.share import Share
from apiserver.apierrors import errors
from apiserver.database.model.auth import Role, User
from apiserver.database.model.project import Project, ProjectMember, ProjectRole, ProjectVisibility
from apiserver.database.model.queue import Queue
from apiserver.service_repo.auth import Identity
from apiserver.services import project_access as project_svc
from apiserver.services import queues as svc
from apiserver.tests.db import ensure_db
from apiserver.utilities.partial_version import PartialVersion

ensure_db()

OWNER, GRANTEE, MEMBER, STRANGER, ADMIN = "owner", "grantee", "member", "stranger", "admin"


class FakeCall:
    def __init__(self, ident):
        self.identity = ident
        self.data = {}
        self.requested_endpoint_version = PartialVersion("2.38")
        self.result = SimpleNamespace(data=None, data_model=None)


class UpdateCall(FakeCall):
    def __init__(self, ident, **data):
        super().__init__(ident)
        self.data = data
        self.data_model_for_partial_update = data


def identity(company, user, role=Role.user):
    return Identity(user=user, company=company, role=role)


def outcome(fn):
    """ The refusal as (error class, codes, message), or None when the call goes through """
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 - every refusal is an API error here
        return type(exc).__name__, exc.codes, exc.msg
    return None


def refusal(error):
    return type(error).__name__, error.codes, error.msg


@pytest.fixture
def company():
    return f"c-{uuid4().hex[:8]}"


@pytest.fixture
def world(company):
    """ Five users, a root project of the owner with the member in it, and six queues covering every way of access """
    for user in (OWNER, GRANTEE, MEMBER, STRANGER, ADMIN):
        User(id=user, name=user, company=company, created=datetime.utcnow()).save()
    root = Project(
        id=uuid4().hex, company=company, user=OWNER, name="root", basename="root", created=datetime.utcnow(),
        visibility=ProjectVisibility.private,
        members=[ProjectMember(user=MEMBER, role=ProjectRole.read, added=datetime.utcnow(), added_by=OWNER)],
    )
    root.save()

    def create(name, public=False):
        # made as enrolment makes an agent's queue: owned by the enroller, the visibility set afterwards
        queue = Queue(id=uuid4().hex, company=company, name=name, created=datetime.utcnow(), public=public,
                      created_by=OWNER)
        queue.save()
        if public:  # the share record the visibility writer keeps for a public queue
            Share(id=uuid4().hex, company=company, resource_type="queue", resource_id=queue.id, scope="public",
                  granted_by=OWNER, level="read", created=datetime.utcnow()).save()
        return queue.id

    queues = {
        "own": create("own"),
        "public": create("public", public=True),
        "granted": create("granted"),
        "allocated": create("allocated"),
        "private": create("private"),
    }
    svc.add_user(FakeCall(identity(company, OWNER)), company, UserRequest(queue=queues["granted"], user=GRANTEE))
    project_svc.add_queues(
        FakeCall(identity(company, OWNER)), company, AddQueuesRequest(project=root.id, queues=[queues["allocated"]])
    )

    legacy = Queue(id=uuid4().hex, company=company, name="legacy", created=datetime.utcnow(), created_by=None)
    legacy.save()
    queues["legacy"] = legacy.id
    return SimpleNamespace(company=company, root=root.id, queues=queues)


def visible(company, ident):
    return set(Queue.objects(company=company).filter(QueueAccessBLL.visible_query(ident)).scalar("id"))


# --- who sees -------------------------------------------------------------------------------------------------------

def test_who_sees_each_queue(world):
    q, c = world.queues, world.company
    assert visible(c, identity(c, OWNER)) == {q["own"], q["public"], q["granted"], q["allocated"], q["private"]}
    assert visible(c, identity(c, GRANTEE)) == {q["public"], q["granted"]}
    assert visible(c, identity(c, MEMBER)) == {q["public"], q["allocated"]}
    assert visible(c, identity(c, STRANGER)) == {q["public"]}
    assert visible(c, identity(c, ADMIN, Role.admin)) == set(q.values())


def test_a_root_member_loses_the_allocated_queue_when_removed_from_the_project(world):
    q, c = world.queues, world.company
    Project.objects(id=world.root).update_one(set__members=[])
    assert visible(c, identity(c, MEMBER)) == {q["public"]}


def test_a_queue_leaves_the_member_view_when_its_allocation_is_removed(world):
    q, c = world.queues, world.company
    project_svc.remove_queue(
        FakeCall(identity(c, OWNER)), c, RemoveQueueRequest(project=world.root, queue=q["allocated"])
    )
    assert visible(c, identity(c, MEMBER)) == {q["public"]}


def test_a_legacy_queue_without_owner_is_seen_by_admins_only(world):
    q, c = world.queues, world.company
    assert q["legacy"] in visible(c, identity(c, ADMIN, Role.admin))
    assert q["legacy"] not in visible(c, identity(c, OWNER))
    assert q["legacy"] not in visible(c, identity(c, STRANGER))


# --- who uses -------------------------------------------------------------------------------------------------------

def test_usable_queue_ids_match_what_the_user_sees(world):
    q, c = world.queues, world.company
    assert set(QueueAccessBLL.usable_queue_ids(identity(c, GRANTEE), c)) == {q["public"], q["granted"]}
    assert set(QueueAccessBLL.usable_queue_ids(identity(c, MEMBER), c)) == {q["public"], q["allocated"]}
    assert set(QueueAccessBLL.usable_queue_ids(identity(c, STRANGER), c)) == {q["public"]}
    assert set(QueueAccessBLL.usable_queue_ids(identity(c, ADMIN, Role.admin), c)) == set(q.values())


USE_TABLE = [
    # user, queue, allowed
    (OWNER, "own", True), (OWNER, "private", True), (OWNER, "allocated", True), (OWNER, "legacy", False),
    (GRANTEE, "granted", True), (GRANTEE, "public", True), (GRANTEE, "own", False), (GRANTEE, "allocated", False),
    (MEMBER, "allocated", True), (MEMBER, "public", True), (MEMBER, "granted", False), (MEMBER, "own", False),
    (STRANGER, "public", True), (STRANGER, "granted", False), (STRANGER, "allocated", False),
    (ADMIN, "own", True), (ADMIN, "legacy", True), (ADMIN, "private", True),
]


@pytest.mark.parametrize("user,queue,allowed", USE_TABLE)
def test_who_may_use_each_queue(world, user, queue, allowed):
    role = Role.admin if user == ADMIN else Role.user
    queue_id = world.queues[queue]
    result = outcome(lambda: QueueAccessBLL.assert_can_use(identity(world.company, user, role), world.company, queue_id))
    if allowed:
        assert result is None
    else:
        assert result == refusal(errors.bad_request.InvalidQueueId(id=queue_id))


def test_an_unknown_queue_is_not_found_for_a_user_and_passes_for_an_admin(world):
    missing = "no-such-queue"
    assert outcome(lambda: QueueAccessBLL.assert_can_use(identity(world.company, STRANGER), world.company, missing)) \
        == refusal(errors.bad_request.InvalidQueueId(id=missing))
    assert outcome(lambda: QueueAccessBLL.assert_can_use(identity(world.company, ADMIN, Role.admin), world.company,
                                                         missing)) is None


def test_get_by_id_refuses_a_stranger_as_not_found(world):
    call = FakeCall(identity(world.company, STRANGER))
    result = outcome(lambda: svc.get_by_id(call, world.company, QueueRequest(queue=world.queues["own"])))
    assert result == refusal(errors.bad_request.InvalidQueueId(id=world.queues["own"]))


# --- who manages ----------------------------------------------------------------------------------------------------

NOT_OWNER = errors.forbidden.NoWritePermission("only the owner or a company admin can manage the queue")

MANAGE_TABLE = [
    # user, queue, outcome: "ok", "not-found" or "not-owner"
    (OWNER, "own", "ok"), (OWNER, "public", "ok"), (OWNER, "granted", "ok"), (OWNER, "allocated", "ok"),
    (OWNER, "private", "ok"), (OWNER, "legacy", "not-found"),
    (GRANTEE, "granted", "not-owner"), (GRANTEE, "public", "not-owner"), (GRANTEE, "own", "not-found"),
    (MEMBER, "allocated", "not-owner"), (MEMBER, "public", "not-owner"), (MEMBER, "own", "not-found"),
    (STRANGER, "own", "not-found"), (STRANGER, "public", "not-owner"),
    (ADMIN, "own", "ok"), (ADMIN, "legacy", "ok"), (ADMIN, "allocated", "ok"),
]


@pytest.mark.parametrize("user,queue,expected", MANAGE_TABLE)
def test_who_may_manage_each_queue(world, user, queue, expected):
    role = Role.admin if user == ADMIN else Role.user
    queue_id = world.queues[queue]
    result = outcome(lambda: QueueAccessBLL.assert_can_manage(identity(world.company, user, role), world.company,
                                                              queue_id))
    if expected == "ok":
        assert result is None
    elif expected == "not-found":
        assert result == refusal(errors.bad_request.InvalidQueueId(id=queue_id))
    else:
        assert result == refusal(NOT_OWNER)


def test_a_grantee_and_a_member_get_the_manage_refusal_on_update_and_delete(world):
    q, c = world.queues, world.company
    grantee = identity(c, GRANTEE)
    assert outcome(lambda: svc.update(UpdateCall(grantee, tags=["x"]), c, UpdateRequest(queue=q["granted"], tags=["x"]))) \
        == refusal(NOT_OWNER)
    member = identity(c, MEMBER)
    assert outcome(lambda: svc.delete(FakeCall(member), c, DeleteRequest(queue=q["allocated"]))) == refusal(NOT_OWNER)


def test_only_the_owner_sets_the_public_flag_and_a_grantee_is_refused(world):
    q, c = world.queues, world.company
    assert outcome(lambda: svc.update(UpdateCall(identity(c, GRANTEE), public=True), c,
                                      UpdateRequest(queue=q["granted"], public=True))) == refusal(NOT_OWNER)
    assert Queue.objects(id=q["granted"]).first().public is False


def test_a_grant_is_added_once_and_removed_once(world):
    q, c = world.queues, world.company
    owner = FakeCall(identity(c, OWNER))
    assert svc.add_user(owner, c, UserRequest(queue=q["own"], user=MEMBER)).updated == 1
    assert svc.add_user(FakeCall(identity(c, OWNER)), c, UserRequest(queue=q["own"], user=MEMBER)).updated == 0
    assert q["own"] in visible(c, identity(c, MEMBER))
    svc.remove_user(FakeCall(identity(c, OWNER)), c, UserRequest(queue=q["own"], user=MEMBER))
    assert q["own"] not in visible(c, identity(c, MEMBER))


def test_removing_a_user_who_has_no_grant_is_refused(world):
    q, c = world.queues, world.company
    expected = errors.bad_request.InvalidUserId(id=STRANGER)
    assert outcome(lambda: svc.remove_user(FakeCall(identity(c, OWNER)), c,
                                           UserRequest(queue=q["own"], user=STRANGER))) == refusal(expected)


# --- who serves -----------------------------------------------------------------------------------------------------

# Without an agent's device a caller serves only the queues it owns. Public queues, grants and allocations serve no one else.
SERVE_TABLE = [
    (OWNER, "own", True), (OWNER, "private", True), (OWNER, "allocated", True), (OWNER, "legacy", False),
    (GRANTEE, "granted", False), (GRANTEE, "public", False), (GRANTEE, "allocated", False), (GRANTEE, "own", False),
    (MEMBER, "allocated", False), (MEMBER, "public", False), (MEMBER, "granted", False),
    (STRANGER, "public", False), (STRANGER, "own", False),
]


@pytest.mark.parametrize("user,queue,allowed", SERVE_TABLE)
def test_who_may_have_an_agent_serve_each_queue(world, user, queue, allowed):
    queue_id = world.queues[queue]
    result = outcome(lambda: QueueAccessBLL.assert_can_serve(identity(world.company, user), world.company, [queue_id]))
    if allowed:
        assert result is None
    else:
        assert result == refusal(errors.bad_request.InvalidQueueId(ids=[queue_id]))


def test_an_admin_serves_nothing_by_the_admin_role(world):
    """The admin role gives no queue: an admin serves only the queues the admin owns (none here)."""
    q, c = world.queues, world.company
    result = outcome(lambda: QueueAccessBLL.assert_can_serve(identity(c, ADMIN, Role.admin), c, list(q.values())))
    assert result == refusal(errors.bad_request.InvalidQueueId(ids=sorted(q.values())))


def test_an_agent_serves_its_own_queue_and_no_other(world):
    """An agent is enrolled with one queue, made for its enroller: it serves that queue only."""
    from apiserver.database.model.device import Device, DeviceKind

    q, c = world.queues, world.company
    device = Device(id=f"dev_{uuid4().hex}", user=OWNER, company=c, kind=DeviceKind.agent, name="gpu-box",
                    public_key="x", created=datetime.utcnow(), queue=q["own"])
    device.save()
    QueueAccessBLL.assert_can_serve(identity(c, OWNER), c, [q["own"]], device_id=device.id)
    result = outcome(lambda: QueueAccessBLL.assert_can_serve(identity(c, OWNER), c, [q["allocated"]],
                                                             device_id=device.id))
    assert result == refusal(errors.bad_request.InvalidQueueId(ids=[q["allocated"]]))


def test_serving_reports_only_the_queues_the_user_may_not_serve(world):
    q, c = world.queues, world.company
    result = outcome(lambda: QueueAccessBLL.assert_can_serve(identity(c, STRANGER), c, [q["public"], q["own"]]))
    assert result == refusal(errors.bad_request.InvalidQueueId(ids=sorted([q["public"], q["own"]])))


def test_serving_without_a_known_caller_is_refused(world):
    result = outcome(lambda: QueueAccessBLL.assert_can_serve(None, world.company, [world.queues["public"]]))
    assert result == refusal(errors.forbidden.NoWritePermission("the caller is not known"))


# --- allocation and project views -----------------------------------------------------------------------------------

def test_the_assignable_queues_are_public_granted_and_owned(world):
    q, c = world.queues, world.company
    assert QueueAccessBLL.assignable_queue_ids(c, OWNER) == {q["own"], q["public"], q["granted"], q["allocated"],
                                                              q["private"]}
    assert QueueAccessBLL.assignable_queue_ids(c, GRANTEE) == {q["public"], q["granted"]}
    assert QueueAccessBLL.assignable_queue_ids(c, MEMBER) == {q["public"]}
    assert QueueAccessBLL.assignable_queue_ids(c, STRANGER) == {q["public"]}


def test_the_allocated_queues_are_those_of_the_root_projects_the_user_is_in(world):
    q, c = world.queues, world.company
    assert QueueAccessBLL.allocated_queue_ids(identity(c, MEMBER)) == {q["allocated"]}
    assert QueueAccessBLL.allocated_queue_ids(identity(c, OWNER)) == {q["allocated"]}
    assert QueueAccessBLL.allocated_queue_ids(identity(c, GRANTEE)) == set()
    assert QueueAccessBLL.allocated_queue_ids(identity(c, STRANGER)) == set()


def test_a_project_owner_allocates_only_the_queues_they_may_use_without_admin_rights(world):
    q, c = world.queues, world.company
    other = Project(id=uuid4().hex, company=c, user=GRANTEE, name="other", basename="other",
                    created=datetime.utcnow(), visibility=ProjectVisibility.private)
    other.save()
    granted_only = FakeCall(identity(c, GRANTEE))
    project_svc.add_queues(granted_only, c, AddQueuesRequest(project=other.id, queues=[q["granted"]]))
    assert resource_ids_shared_with(c, "queue", other.id) == [q["granted"]]

    refused = outcome(lambda: project_svc.add_queues(FakeCall(identity(c, GRANTEE)), c,
                                                     AddQueuesRequest(project=other.id, queues=[q["own"]])))
    assert refused == refusal(errors.bad_request.InvalidQueueId(id=q["own"]))


def test_a_member_may_not_allocate_a_queue_to_the_project(world):
    q, c = world.queues, world.company
    result = outcome(lambda: project_svc.add_queues(FakeCall(identity(c, MEMBER)), c,
                                                    AddQueuesRequest(project=world.root, queues=[q["public"]])))
    assert result == refusal(errors.forbidden.NoWritePermission("only the project owner can change the queues of the project"))


def test_the_projects_of_a_queue_are_listed_to_those_who_may_see_them(world):
    q, c = world.queues, world.company
    for user, role in ((OWNER, Role.user), (MEMBER, Role.user), (ADMIN, Role.admin)):
        call = FakeCall(identity(c, user, role))
        svc.get_projects(call, c, QueueRequest(queue=q["allocated"]))
        assert [p["id"] for p in call.result.data["projects"]] == [world.root]
    call = FakeCall(identity(c, OWNER))
    svc.get_projects(call, c, QueueRequest(queue=q["own"]))
    assert call.result.data["projects"] == []
    result = outcome(lambda: svc.get_projects(FakeCall(identity(c, STRANGER)), c, QueueRequest(queue=q["allocated"])))
    assert result == refusal(errors.bad_request.InvalidQueueId(id=q["allocated"]))
