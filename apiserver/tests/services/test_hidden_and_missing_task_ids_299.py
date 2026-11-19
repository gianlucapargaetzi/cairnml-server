# apiserver/tests/services/test_hidden_and_missing_task_ids_299.py
"""
Issue 299: a task the caller cannot see (its project is hidden from the caller, or it belongs to another company)
must get the same answer as a task that does not exist, on the endpoints dequeue, dequeue_many, archive, archive_many,
move and update_tags. The answer is compared in full: error class, code and subcode, the payload (with the ids
replaced by a placeholder, since the two cases use different ids), and, for a hidden task, that nothing was written.
"""

from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from apiserver.apierrors import APIError
from apiserver.apimodels.base import MoveRequest
from apiserver.apimodels.tasks import (
    ArchiveManyRequest,
    ArchiveRequest,
    DequeueManyRequest,
    DequeueRequest,
    UpdateTagsRequest,
)
from apiserver.database.model.auth import Role
from apiserver.database.model.project import Project, ProjectVisibility
from apiserver.database.model.queue import Entry, Queue
from apiserver.database.model.task.task import Execution, Task, TaskStatus
from apiserver.service_repo.auth import Identity
from apiserver.services import tasks as svc
from apiserver.tests.db import ensure_db
from apiserver.utilities.partial_version import PartialVersion

ensure_db()

HIDDEN = "hidden-task"
MISSING = "missing-task"
PLACEHOLDER = "<ID>"


class FakeCall:
    def __init__(self, ident, data=None):
        self.identity = ident
        self.data = data if data is not None else {}
        self.batched_data = None
        self.requested_endpoint_version = PartialVersion("2.30")
        self.result = SimpleNamespace(data=None, data_model=None)


@pytest.fixture
def company():
    return f"c-{uuid4().hex[:8]}"


def stranger(company):
    return Identity(user="stranger", company=company, role=Role.user)


def make_project(company, id_, owner="owner1", visibility=ProjectVisibility.private):
    Project(
        id=id_,
        company=company,
        user=owner,
        name=f"Project-{id_}",
        basename=f"Project-{id_}",
        created=datetime.utcnow(),
        visibility=visibility,
    ).save()


def make_queued_task(company, project_id, id_):
    queue = Queue(
        id=uuid4().hex,
        company=company,
        name=f"q-{uuid4().hex[:6]}",
        created=datetime.utcnow(),
        created_by="owner1",
        entries=[Entry(task=id_, added=datetime.utcnow())],
    )
    queue.save()
    Task(
        id=id_,
        company=company,
        user="owner1",
        project=project_id,
        name=f"Task-{id_}",
        type="training",
        created=datetime.utcnow(),
        status=TaskStatus.queued,
        execution=Execution(queue=queue.id),
    ).save()
    return queue


def setup_hidden_project(company):
    """A task in a private project of the caller's company, which the stranger cannot see"""
    make_project(company, "hidden-root")
    make_project(company, "dst-root")
    make_queued_task(company, "hidden-root", HIDDEN)


def setup_other_company(company):
    """A task of another company, under the same id the caller asks for"""
    other = f"{company}-other"
    make_project(company, "dst-root")
    make_project(other, "other-root")
    make_queued_task(other, "other-root", HIDDEN)


SCENARIOS = {"hidden_project": setup_hidden_project, "other_company": setup_other_company}


def _snapshot(company):
    """Everything the hidden task could be changed through: the task document and the queue entries"""
    task = Task.objects(id=HIDDEN).first()
    queues = sorted(
        (q.id, sorted(e.task for e in q.entries)) for q in Queue.objects(entries__task=HIDDEN).only("id", "entries")
    )
    return (task.to_mongo().to_dict() if task else None), queues


def _scrub(value, ids):
    if isinstance(value, str):
        for id_ in ids:
            value = value.replace(id_, PLACEHOLDER)
        return value
    if isinstance(value, dict):
        return {k: _scrub(v, ids) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_scrub(v, ids) for v in value]
    return value


def _dequeue(company, task_id):
    call = FakeCall(stranger(company))
    svc.dequeue(call, company, DequeueRequest(task=task_id))
    return {"dequeued": call.result.data_model.dequeued, "updated": call.result.data_model.updated}


def _dequeue_many(company, task_id):
    call = FakeCall(stranger(company))
    svc.dequeue_many(call, company, DequeueManyRequest(ids=[task_id]))
    model = call.result.data_model
    return {
        "succeeded": [{"id": i.id, "dequeued": i.dequeued} for i in model.succeeded],
        "failed": model.failed,
    }


def _archive(company, task_id):
    call = FakeCall(stranger(company))
    svc.archive(call, company, ArchiveRequest(tasks=[task_id]))
    return {"archived": call.result.data_model.archived}


def _archive_many(company, task_id):
    call = FakeCall(stranger(company))
    svc.archive_many(call, company, ArchiveManyRequest(ids=[task_id]))
    model = call.result.data_model
    return {"succeeded": [dict(i) for i in model.succeeded], "failed": model.failed}


def _move(company, task_id):
    call = FakeCall(stranger(company), data={"project": "dst-root"})
    return svc.move(call, company, MoveRequest(ids=[task_id], project="dst-root"))


def _update_tags(company, task_id):
    return svc.update_tags(
        FakeCall(stranger(company)),
        company,
        UpdateTagsRequest(ids=[task_id], add_tags=["fresh"]),
    )


ENDPOINTS = {
    "dequeue": _dequeue,
    "dequeue_many": _dequeue_many,
    "archive": _archive,
    "archive_many": _archive_many,
    "move": _move,
    "update_tags": _update_tags,
}


def _answer(run, company, task_id):
    """The full answer of one call: the result, or the error class, code, subcode, message and payload"""
    try:
        value = run(company, task_id)
    except APIError as err:
        return ("error", err.code, err.subcode, _scrub(err.msg, [task_id]), _scrub(err.error_data, [task_id]))
    return ("ok", _scrub(value, [task_id]))


@pytest.mark.parametrize("endpoint", list(ENDPOINTS))
@pytest.mark.parametrize("hidden_kind", ["hidden_project", "other_company"])
def test_a_hidden_task_gets_the_same_answer_as_a_missing_one(company, endpoint, hidden_kind):
    run = ENDPOINTS[endpoint]
    SCENARIOS[hidden_kind](company)
    before = _snapshot(company)
    hidden_answer = _answer(run, company, HIDDEN)
    assert _snapshot(company) == before, "a call on a hidden task must not change it or its queue entries"

    missing_answer = _answer(run, company, MISSING)

    assert hidden_answer == missing_answer
