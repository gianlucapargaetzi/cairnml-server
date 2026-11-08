# apiserver/tests/bll/test_agent_queue_levels.py
"""An agent is a queue listener with no roles of its own. It serves a queue at the level its owner holds on that queue
(bll/access.level_of), and serving needs use. The agent's identity carries the owner as its user and the user role,
never admin. A dequeue of a task another user queued needs change_content on the queue; the caller's own task needs
use only."""
from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from apiserver.apierrors import errors
from apiserver.apimodels.tasks import DequeueRequest
from apiserver.bll.queue.queue_access import QueueAccessBLL
from apiserver.database.model.auth import Role, User
from apiserver.database.model.queue import Entry, Queue
from apiserver.database.model.share import Share
from apiserver.database.model.task.task import Execution, Task, TaskStatus
from apiserver.service_repo.auth import Identity
from apiserver.services import tasks as tasks_svc
from apiserver.tests.db import ensure_db
from apiserver.utilities.partial_version import PartialVersion

ensure_db()


@pytest.fixture
def company():
    return f"c-{uuid4().hex[:8]}"


def agent_of(company, owner):
    """The identity of an agent: its owner's user id, the user role whatever the owner's role"""
    return Identity(user=owner, company=company, role=Role.user)


def make_user(company, user_id, role=Role.user, idp_disabled=False):
    User(id=user_id, name=user_id, company=company, created=datetime.utcnow(), role=role,
         idp_disabled=idp_disabled).save()


def make_queue(company, created_by, public=False):
    queue = Queue(id=uuid4().hex, company=company, name=f"q-{uuid4().hex[:6]}", created=datetime.utcnow(),
                  public=public, system_tags=[], created_by=created_by)
    queue.save()
    return queue


def grant(company, queue, user, level="read"):
    Share(id=uuid4().hex, company=company, resource_type="queue", resource_id=queue.id, scope="user", project="",
          user=user, level=level, granted_by=queue.created_by, created=datetime.utcnow()).save()


def serve(company, owner, queue_ids, role=Role.user):
    QueueAccessBLL.assert_can_serve(Identity(user=owner, company=company, role=role), company, queue_ids)


# --- serving: an agent serves its own queue only -------------------------------------------------------------------

def agent_device(company, owner, queue_id):
    from apiserver.database.model.device import Device, DeviceKind

    device = Device(id=f"dev_{uuid4().hex}", user=owner, company=company, kind=DeviceKind.agent, name="gpu-box",
                    public_key="x", created=datetime.utcnow(), queue=queue_id)
    device.save()
    return device


def serve_as_agent(company, device, queue_ids):
    QueueAccessBLL.assert_can_serve(Identity(user=device.user, company=company, role=Role.user), company, queue_ids,
                                    device_id=device.id)


def test_an_agent_serves_its_own_queue(company):
    make_user(company, "owner")
    queue = make_queue(company, created_by="owner")
    serve_as_agent(company, agent_device(company, "owner", queue.id), [queue.id])


def test_an_agent_is_refused_a_queue_its_owner_only_holds_a_grant_on(company):
    make_user(company, "owner")
    own = make_queue(company, created_by="owner")
    granted = make_queue(company, created_by="someone-else")
    grant(company, granted, "owner", level="read")
    device = agent_device(company, "owner", own.id)
    with pytest.raises(errors.bad_request.InvalidQueueId):
        serve_as_agent(company, device, [granted.id])


def test_a_public_queue_is_not_served_by_another_users_agent(company):
    make_user(company, "stranger")
    own = make_queue(company, created_by="stranger")
    public = make_queue(company, created_by="someone-else", public=True)
    device = agent_device(company, "stranger", own.id)
    with pytest.raises(errors.bad_request.InvalidQueueId):
        serve_as_agent(company, device, [public.id])


def test_a_deactivated_owners_agent_serves_nothing(company):
    make_user(company, "owner", idp_disabled=True)
    queue = make_queue(company, created_by="owner")
    device = agent_device(company, "owner", queue.id)
    with pytest.raises(errors.bad_request.InvalidQueueId):
        serve_as_agent(company, device, [queue.id])

def test_an_admin_role_does_not_let_an_agent_serve_a_queue_it_holds_nothing_on(company):
    """The agent of a company admin acts with the user role: the admin role alone gives it no queue."""
    make_user(company, "admin-owner", role=Role.admin)
    queue = make_queue(company, created_by="someone-else")
    with pytest.raises(errors.bad_request.InvalidQueueId):
        serve(company, "admin-owner", [queue.id], role=Role.user)


def test_an_identity_with_the_admin_role_does_not_serve_a_queue_it_holds_nothing_on(company):
    """No agent carries the admin role, so the role never widens what an identity may serve."""
    make_user(company, "admin-owner", role=Role.admin)
    queue = make_queue(company, created_by="someone-else")
    with pytest.raises(errors.bad_request.InvalidQueueId):
        serve(company, "admin-owner", [queue.id], role=Role.admin)


def test_a_deactivated_owner_is_served_nothing(company):
    """R25: the agent of a deactivated owner serves no queue, not even the owner's own."""
    make_user(company, "gone", idp_disabled=True)
    own = make_queue(company, created_by="gone")
    public = make_queue(company, created_by="someone-else", public=True)
    with pytest.raises(errors.bad_request.InvalidQueueId):
        serve(company, "gone", [own.id])
    with pytest.raises(errors.bad_request.InvalidQueueId):
        serve(company, "gone", [public.id])


def test_a_deactivated_admin_owner_is_served_nothing(company):
    make_user(company, "gone-admin", role=Role.admin, idp_disabled=True)
    queue = make_queue(company, created_by="someone-else")
    with pytest.raises(errors.bad_request.InvalidQueueId):
        serve(company, "gone-admin", [queue.id], role=Role.user)


def test_an_allocation_to_a_project_does_not_let_an_agent_serve_the_queue(company):
    from apiserver.database.model.project import Project, ProjectMember, ProjectRole, ProjectVisibility

    make_user(company, "member")
    queue = make_queue(company, created_by="someone-else")
    root = Project(id=uuid4().hex, company=company, user="someone-else", name="root", basename="root",
                   created=datetime.utcnow(), visibility=ProjectVisibility.private,
                   members=[ProjectMember(user="member", role=ProjectRole.read, added=datetime.utcnow(),
                                          added_by="someone-else")])
    root.save()
    Share(id=uuid4().hex, company=company, resource_type="queue", resource_id=queue.id, scope="projects",
          project=root.id, user="", level="read", granted_by="someone-else", created=datetime.utcnow()).save()
    with pytest.raises(errors.bad_request.InvalidQueueId):
        serve(company, "member", [queue.id])


# --- dequeue: another user's task needs change_content --------------------------------------------------------------

class FakeCall:
    def __init__(self, ident):
        self.identity = ident
        self.data = {}
        self.requested_endpoint_version = PartialVersion("2.38")
        self.result = SimpleNamespace(data=None, data_model=None)


def make_queued_task(company, queue, owner, task_id):
    now = datetime.utcnow()
    Task(id=task_id, company=company, user=owner, name=task_id, type="training", created=now,
         status=TaskStatus.queued, execution=Execution(queue=queue.id)).save()
    Queue.objects(id=queue.id).update_one(push__entries=Entry(task=task_id, added=now))


def test_an_agent_with_read_on_a_queue_may_not_dequeue_a_task_another_user_queued(company, monkeypatch):
    from apiserver.bll.queue.queue_metrics import QueueMetrics

    monkeypatch.setattr(QueueMetrics, "log_queue_metrics_to_es", lambda self, company_id, queues: 0)
    make_user(company, "owner")
    make_user(company, "agent-owner")
    queue = make_queue(company, created_by="owner")
    grant(company, queue, "agent-owner", level="read")
    make_queued_task(company, queue, "owner", "task-theirs")
    with pytest.raises(errors.forbidden.NoWritePermission):
        tasks_svc.dequeue(FakeCall(agent_of(company, "agent-owner")), company,
                          DequeueRequest(task="task-theirs"))
    assert Task.objects(id="task-theirs").first().status == TaskStatus.queued
    assert [e.task for e in Queue.objects(id=queue.id).first().entries] == ["task-theirs"]


def test_the_agent_of_a_queue_owner_may_dequeue_a_task_another_user_queued(company, monkeypatch):
    from apiserver.bll.queue.queue_metrics import QueueMetrics

    monkeypatch.setattr(QueueMetrics, "log_queue_metrics_to_es", lambda self, company_id, queues: 0)
    make_user(company, "owner")
    queue = make_queue(company, created_by="owner")
    make_queued_task(company, queue, "someone-else", "task-theirs-owned")
    call = FakeCall(agent_of(company, "owner"))
    tasks_svc.dequeue(call, company, DequeueRequest(task="task-theirs-owned"))
    assert call.result.data_model.dequeued == 1
    assert Queue.objects(id=queue.id).first().entries == []


def test_an_agent_with_read_on_a_queue_may_dequeue_its_owners_own_task(company, monkeypatch):
    from apiserver.bll.queue.queue_metrics import QueueMetrics

    monkeypatch.setattr(QueueMetrics, "log_queue_metrics_to_es", lambda self, company_id, queues: 0)
    make_user(company, "agent-owner")
    queue = make_queue(company, created_by="owner")
    grant(company, queue, "agent-owner", level="read")
    make_queued_task(company, queue, "agent-owner", "task-own")
    call = FakeCall(agent_of(company, "agent-owner"))
    tasks_svc.dequeue(call, company, DequeueRequest(task="task-own"))
    assert call.result.data_model.dequeued == 1
    assert Queue.objects(id=queue.id).first().entries == []
