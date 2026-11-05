# apiserver/tests/services/test_project_settings_readers.py
"""Pins what the project settings decide about queues today: which queues a project shows, who sees them, and the
refusals of allocating and removing a queue. The state is built through the endpoints and writers, so the old fields
and the share records agree. These tests must pass before and after the readers move to the share records."""

from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from apiserver.apierrors import errors
from apiserver.bll.access import record_grant
from apiserver.apimodels.project_access import AddQueuesRequest, GetQueuesRequest, RemoveQueueRequest
from apiserver.database.model.auth import Role, User
from apiserver.database.model.project import Project, ProjectMember, ProjectRole, ProjectVisibility
from apiserver.database.model.queue import Queue
from apiserver.service_repo.auth import Identity
from apiserver.services import project_access as svc
from apiserver.tests.db import ensure_db
from apiserver.utilities.partial_version import PartialVersion

ensure_db()

OWNER, MEMBER, STRANGER, ADMIN = "owner", "member", "stranger", "admin"


class FakeCall:
    def __init__(self, ident):
        self.identity = ident
        self.requested_endpoint_version = PartialVersion("2.40")
        self.result = SimpleNamespace(data=None, data_model=None)


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
    """ An owner, a member of the root project, a stranger and a company admin; a root project of the owner, and three
    queues of the owner: two that the owner may allocate and a third that is never allocated """
    for user in (OWNER, MEMBER, STRANGER, ADMIN):
        User(id=user, name=user, company=company, created=datetime.utcnow()).save()
    root = Project(
        id=uuid4().hex, company=company, user=OWNER, name="root", basename="root", created=datetime.utcnow(),
        visibility=ProjectVisibility.private,
        members=[ProjectMember(user=MEMBER, role=ProjectRole.read, added=datetime.utcnow(), added_by=OWNER)],
    )
    root.save()

    def queue(name):
        record = Queue(id=uuid4().hex, company=company, name=name, created=datetime.utcnow(), created_by=OWNER)
        record.save()
        return record.id

    queues = {"first": queue("first"), "second": queue("second"), "spare": queue("spare")}
    return SimpleNamespace(company=company, root=root.id, queues=queues)


@pytest.fixture
def deactivate():
    """ Deactivates users for one test. User ids are global and the test database is not reset between tests, so the
    flag is cleared again afterwards. """
    touched = []

    def switch_off(user):
        User.objects(id=user).update_one(set__idp_disabled=True)
        touched.append(user)

    yield switch_off
    for user in touched:
        User.objects(id=user).update_one(unset__idp_disabled=True)


def listed(company, user, root, role=Role.user):
    call = FakeCall(identity(company, user, role))
    svc.get_queues(call, company, GetQueuesRequest(project=root))
    return [q["id"] for q in call.result.data["queues"]]


# --- what a project shows -------------------------------------------------------------------------------------------

def test_the_owner_and_a_member_see_the_allocated_queues_in_allocation_order(world):
    q, c = world.queues, world.company
    svc.add_queues(FakeCall(identity(c, OWNER)), c, AddQueuesRequest(project=world.root, queues=[q["second"]]))
    svc.add_queues(FakeCall(identity(c, OWNER)), c, AddQueuesRequest(project=world.root, queues=[q["first"]]))

    assert listed(c, OWNER, world.root) == [q["second"], q["first"]]
    assert listed(c, MEMBER, world.root) == [q["second"], q["first"]]


def test_an_admin_sees_the_allocated_queues_of_a_project_they_are_not_in(world):
    q, c = world.queues, world.company
    svc.add_queues(FakeCall(identity(c, OWNER)), c, AddQueuesRequest(project=world.root, queues=[q["first"]]))

    assert listed(c, ADMIN, world.root, Role.admin) == [q["first"]]


def test_a_stranger_is_refused_the_queues_of_the_project(world):
    c = world.company
    result = outcome(lambda: listed(c, STRANGER, world.root))
    assert result == refusal(errors.bad_request.InvalidProjectId(id=world.root))


def test_a_queue_leaves_the_listing_when_its_allocation_is_removed(world):
    q, c = world.queues, world.company
    svc.add_queues(FakeCall(identity(c, OWNER)), c, AddQueuesRequest(project=world.root, queues=[q["first"], q["second"]]))
    svc.remove_queue(FakeCall(identity(c, OWNER)), c, RemoveQueueRequest(project=world.root, queue=q["first"]))

    assert listed(c, OWNER, world.root) == [q["second"]]
    assert listed(c, MEMBER, world.root) == [q["second"]]


def test_a_queue_that_is_not_allocated_is_not_listed(world):
    q, c = world.queues, world.company
    svc.add_queues(FakeCall(identity(c, OWNER)), c, AddQueuesRequest(project=world.root, queues=[q["first"]]))

    assert q["spare"] not in listed(c, OWNER, world.root)


# --- what allocating and removing a queue decides -------------------------------------------------------------------

def test_a_queue_already_allocated_to_the_project_is_refused_on_a_second_allocation(world):
    q, c = world.queues, world.company
    svc.add_queues(FakeCall(identity(c, OWNER)), c, AddQueuesRequest(project=world.root, queues=[q["first"]]))

    result = outcome(lambda: svc.add_queues(FakeCall(identity(c, OWNER)), c,
                                            AddQueuesRequest(project=world.root, queues=[q["first"]])))
    assert result == refusal(errors.bad_request.ValidationError("a queue is already allocated to the project"))


def test_removing_a_queue_that_is_not_allocated_is_refused(world):
    q, c = world.queues, world.company

    result = outcome(lambda: svc.remove_queue(FakeCall(identity(c, OWNER)), c,
                                              RemoveQueueRequest(project=world.root, queue=q["spare"])))
    assert result == refusal(errors.bad_request.InvalidQueueId(id=q["spare"]))


def test_a_member_may_not_allocate_or_remove_a_queue(world):
    q, c = world.queues, world.company
    svc.add_queues(FakeCall(identity(c, OWNER)), c, AddQueuesRequest(project=world.root, queues=[q["first"]]))

    allocate = outcome(lambda: svc.add_queues(FakeCall(identity(c, MEMBER)), c,
                                              AddQueuesRequest(project=world.root, queues=[q["second"]])))
    remove = outcome(lambda: svc.remove_queue(FakeCall(identity(c, MEMBER)), c,
                                              RemoveQueueRequest(project=world.root, queue=q["first"])))
    message = "only the project owner can change the queues of the project"
    assert allocate == refusal(errors.forbidden.NoWritePermission(message))
    assert remove == refusal(errors.forbidden.NoWritePermission(message))


# --- the share records alone decide what a project shows ------------------------------------------------------------

def test_a_share_record_alone_gives_the_listing(world):
    q, c = world.queues, world.company
    record_grant(c, OWNER, "queue", q["first"], "projects", world.root, actor=OWNER)

    assert listed(c, OWNER, world.root) == [q["first"]]
    assert listed(c, MEMBER, world.root) == [q["first"]]


def test_no_share_gives_no_listing(world):
    c = world.company

    assert listed(c, OWNER, world.root) == []
    assert listed(c, MEMBER, world.root) == []


# --- a deactivated owner or member gives no queue through the project (R25) -----------------------------------------

def test_a_deactivated_owner_gives_the_project_no_queues(world, deactivate):
    q, c = world.queues, world.company
    svc.add_queues(FakeCall(identity(c, OWNER)), c, AddQueuesRequest(project=world.root, queues=[q["first"]]))
    deactivate(OWNER)

    assert listed(c, MEMBER, world.root) == []


def test_a_deactivated_member_sees_no_queues_of_the_project(world, deactivate):
    q, c = world.queues, world.company
    svc.add_queues(FakeCall(identity(c, OWNER)), c, AddQueuesRequest(project=world.root, queues=[q["first"]]))
    deactivate(MEMBER)

    assert listed(c, MEMBER, world.root) == []
    assert listed(c, OWNER, world.root) == [q["first"]]
