# apiserver/tests/services/test_queues_access_enforcement.py
from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from apiserver.apierrors import errors
from apiserver.apimodels.queues import AddTaskRequest, MoveTaskRequest
from apiserver.database.model.auth import Role
from apiserver.database.model.project import Project, ProjectVisibility
from apiserver.database.model.queue import Queue
from apiserver.database.model.task.task import Task
from apiserver.service_repo.auth import Identity
from apiserver.utilities.partial_version import PartialVersion
from apiserver.services import queues as svc
from apiserver.tests.db import ensure_db

ensure_db()


@pytest.fixture
def company():
    return f"c-{uuid4().hex[:8]}"


def make_root(company, id_="root1", owner="owner1", visibility=ProjectVisibility.private):
    project = Project(
        id=id_, company=company, user=owner, name=id_, basename=id_,
        created=datetime.utcnow(), visibility=visibility,
    )
    project.save()
    return project


def make_public_queue(company, id_=None):
    # The use rule is checked before the task rule, so the queue must be usable by the stranger for the
    # task rule to be what refuses the request
    queue = Queue(id=id_ or str(uuid4()), company=company, name=f"q-{uuid4().hex[:6]}", created=datetime.utcnow(), public=True)
    queue.save()
    return queue


def make_task(company, project, owner, id_="task1"):
    task = Task(id=id_, company=company, user=owner, name=id_, project=project, type="training", created=datetime.utcnow())
    task.save()
    return task


def identity(company, user, role=Role.user):
    return Identity(user=user, company=company, role=role)


class FakeCall:
    def __init__(self, ident, data=None):
        self.identity = ident
        self.data = data if data is not None else {}
        self.requested_endpoint_version = PartialVersion("2.30")
        self.result = SimpleNamespace(data=None, data_model=None)


def test_add_task_refuses_a_task_in_a_private_project_to_a_stranger(company):
    # queues are not project-scoped, so enqueueing by task id directly through the queue API must
    # not bypass the same write check tasks.enqueue already requires for the same operation
    make_root(company)
    make_task(company, "root1", "owner1")
    queue = make_public_queue(company)
    with pytest.raises(errors.bad_request.InvalidTaskId):
        svc.add_task(
            FakeCall(identity(company, "stranger")),
            company,
            AddTaskRequest(task="task1", queue=queue.id),
        )


def test_move_task_to_front_refuses_a_task_in_a_private_project_to_a_stranger(company):
    # reposition-in-queue endpoints are structurally identical to add_task/remove_task and need
    # the same check; this one was missed in the first fix round
    make_root(company)
    make_task(company, "root1", "owner1")
    queue = make_public_queue(company)
    with pytest.raises(errors.bad_request.InvalidTaskId):
        svc.move_task_to_front(
            FakeCall(identity(company, "stranger")),
            company,
            MoveTaskRequest(task="task1", queue=queue.id),
        )
