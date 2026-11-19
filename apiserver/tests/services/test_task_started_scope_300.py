# apiserver/tests/services/test_task_started_scope_300.py
#
# tasks.started looks the task up with the same company and visibility scope as the other endpoints (issue #300).
# A missing id, a hidden id and another company's id give the same answer (InvalidTaskId, 400/101, same payload
# keys), so the endpoint cannot be used to learn whether a task of another company or of a hidden project exists.
from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from apiserver.apierrors import errors
from apiserver.apimodels.tasks import UpdateRequest
from apiserver.bll.queue.queue_metrics import QueueMetrics
from apiserver.database.model.auth import Role
from apiserver.database.model.project import Project, ProjectVisibility
from apiserver.database.model.task.task import Task, TaskStatus
from apiserver.service_repo.auth import Identity
from apiserver.services import tasks as svc
from apiserver.tests.db import ensure_db
from apiserver.utilities.partial_version import PartialVersion

ensure_db()


@pytest.fixture(autouse=True)
def no_elasticsearch(monkeypatch):
    # queue metrics are written to Elasticsearch, which these unit checks do not run
    monkeypatch.setattr(QueueMetrics, "log_queue_metrics_to_es", lambda self, company_id, queues: 0)


@pytest.fixture
def company():
    return f"c-{uuid4().hex[:8]}"


def identity(company, user, role=Role.user):
    return Identity(user=user, company=company, role=role)


class FakeCall:
    def __init__(self, ident):
        self.identity = ident
        self.data = {}
        self.batched_data = None
        self.auth = None
        self.authorization = None
        self.requested_endpoint_version = PartialVersion("2.30")
        self.result = SimpleNamespace(data=None, data_model=None)


def make_private_project(company, owner):
    Project(
        id="root1",
        company=company,
        user=owner,
        name="Project-root1",
        basename="Project-root1",
        created=datetime.utcnow(),
        visibility=ProjectVisibility.private,
    ).save()


def make_task(company, project_id, owner, id_, task_company=None, status=TaskStatus.created):
    Task(
        id=id_,
        company=task_company or company,
        user=owner,
        project=project_id,
        name=f"Task-{id_}",
        type="training",
        created=datetime.utcnow(),
        status=status,
    ).save()


def started_error(company, caller, task_id):
    with pytest.raises(errors.bad_request.InvalidTaskId) as exc:
        svc.started(FakeCall(identity(company, caller)), company, UpdateRequest(task=task_id))
    return exc


def test_started_missing_hidden_and_other_company_give_the_same_error(company):
    make_private_project(company, "owner1")
    make_task(company, "root1", "owner1", "hidden1")
    make_task(company, None, "owner1", "foreign1", task_company=f"c-{uuid4().hex[:8]}")

    answers = {
        "missing": started_error(company, "owner1", "missing"),
        "hidden": started_error(company, "stranger", "hidden1"),
        "other company": started_error(company, "owner1", "foreign1"),
    }

    codes = {name: exc.value.codes for name, exc in answers.items()}
    payload_keys = {name: set(exc.value.error_data) for name, exc in answers.items()}
    assert set(codes.values()) == {(400, 101)}
    assert set(map(frozenset, payload_keys.values())) == {frozenset({"id", "company"})}


def test_started_does_not_change_a_hidden_or_foreign_task(company):
    make_private_project(company, "owner1")
    make_task(company, "root1", "owner1", "hidden1")
    make_task(company, None, "owner1", "foreign1", task_company=f"c-{uuid4().hex[:8]}")

    started_error(company, "stranger", "hidden1")
    started_error(company, "owner1", "foreign1")

    assert Task.objects(id="hidden1").first().status == TaskStatus.created
    assert Task.objects(id="foreign1").first().status == TaskStatus.created


def test_started_on_an_own_queued_task_still_starts_it(company):
    make_private_project(company, "owner1")
    make_task(company, "root1", "owner1", "queued1", status=TaskStatus.queued)
    call = FakeCall(identity(company, "owner1"))

    svc.started(call, company, UpdateRequest(task="queued1"))

    task = Task.objects(id="queued1").first()
    assert task.status == TaskStatus.in_progress
    assert task.started is not None
    assert call.result.data_model.started == 1
