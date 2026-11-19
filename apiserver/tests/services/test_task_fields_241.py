# apiserver/tests/services/test_task_fields_241.py
# The fields the 2.41 definitions declare are the ones the handlers already read: status_message on tasks.update and
# force on tasks.enqueue are passed on as before.
from uuid import uuid4

import pytest

from apiserver.apierrors import errors
from apiserver.apimodels.tasks import EnqueueRequest, UpdateRequest
from apiserver.bll.queue.queue_metrics import QueueMetrics
from apiserver.database.model.task.task import Task, TaskStatus
from apiserver.services import tasks as svc
from apiserver.tests.db import ensure_db
from apiserver.tests.services.test_tasks_access_enforcement import FakeCall, identity, make_root, make_task

ensure_db()


@pytest.fixture
def company():
    return f"c-{uuid4().hex[:8]}"


class UserCall(FakeCall):
    """A call of a person (not a job): the deadlock check reads auth, which is None for a person."""

    def __init__(self, ident):
        super().__init__(ident)
        self.auth = None


@pytest.mark.needs_redis
def test_update_passes_status_message_on_to_the_task(company):
    make_root(company)
    make_task(company, "root1", "owner1")
    svc.update(
        FakeCall(identity(company, "owner1"), data={"task": "task1", "status_message": "waiting for data"}),
        company,
        UpdateRequest(task="task1", status_message="waiting for data"),
    )
    assert Task.objects(id="task1").first().status_message == "waiting for data"


@pytest.mark.needs_redis
def test_enqueue_with_force_queues_a_task_that_is_not_created(company, monkeypatch):
    monkeypatch.setattr(QueueMetrics, "log_queue_metrics_to_es", lambda self, company_id, queues: 0)
    make_root(company)
    make_task(company, "root1", "owner1", status=TaskStatus.in_progress)
    svc.enqueue(
        UserCall(identity(company, "owner1")),
        company,
        EnqueueRequest(task="task1", queue_name="force-queue-255", force=True),
    )
    assert Task.objects(id="task1").first().status == TaskStatus.queued


@pytest.mark.needs_redis
def test_enqueue_without_force_refuses_a_task_that_is_not_created(company, monkeypatch):
    monkeypatch.setattr(QueueMetrics, "log_queue_metrics_to_es", lambda self, company_id, queues: 0)
    make_root(company)
    make_task(company, "root1", "owner1", status=TaskStatus.in_progress)
    with pytest.raises(errors.bad_request.InvalidTaskStatus):
        svc.enqueue(
            UserCall(identity(company, "owner1")),
            company,
            EnqueueRequest(task="task1", queue_name="force-queue-255"),
        )
    assert Task.objects(id="task1").first().status == TaskStatus.in_progress
