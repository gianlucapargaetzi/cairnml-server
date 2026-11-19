# apiserver/tests/services/test_tasks_status_queue_characterisation.py
#
# Characterisation tests of the status and queue endpoints of the task service (tracking issue #267, step 3,
# issue #271). They pin what the endpoints do today, error codes and check order included, so that the refactor
# steps cannot change it silently. Production code is not changed here.
#
# A case marked "# suspect: pinned as is, see the decision issue (#282)" looks unintended. It is pinned as it is
# and listed in the pull request under "found, not changed".
from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from apiserver.apierrors import errors
from apiserver.apimodels.base import MoveRequest
from apiserver.apimodels.tasks import (
    ArchiveManyRequest,
    ArchiveRequest,
    CompletedRequest,
    DeleteManyRequest,
    DeleteRequest,
    DequeueManyRequest,
    DequeueRequest,
    EnqueueManyRequest,
    EnqueueRequest,
    PublishManyRequest,
    PublishRequest,
    ResetManyRequest,
    ResetRequest,
    StopManyRequest,
    StopRequest,
    UnarchiveManyRequest,
    UpdateRequest,
)
from apiserver.bll.model import ModelBLL
from apiserver.bll.queue.queue_metrics import QueueMetrics
from apiserver.bll.task.task_operations import enqueue_task
from apiserver.database.model.auth import Role
from apiserver.database.model.model import Model
from apiserver.database.model.project import Project, ProjectMember, ProjectRole, ProjectVisibility
from apiserver.database.model.queue import Entry, Queue
from apiserver.database.model.task.task import (
    Execution,
    ModelItem,
    Models,
    Task,
    TaskStatus,
    TaskStatusMessage,
)
from apiserver.idp import github_check
from apiserver.idp.access_refused import AccessRefused
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
    # Every test gets its own company so that rows of other tests never interfere
    return f"c-{uuid4().hex[:8]}"


def identity(company, user, role=Role.user):
    return Identity(user=user, company=company, role=role)


class FakeCall:
    """The call object the endpoints read: identity, data, auth (job token) and authorization (agent bearer)"""

    def __init__(self, ident, data=None, auth=None, authorization=None):
        self.identity = ident
        self.data = data if data is not None else {}
        self.batched_data = None
        self.auth = auth
        self.authorization = authorization
        self.requested_endpoint_version = PartialVersion("2.30")
        self.result = SimpleNamespace(data=None, data_model=None)


def make_root(company, id_="root1", owner="owner1", visibility=ProjectVisibility.private, members=()):
    project = Project(
        id=id_,
        company=company,
        user=owner,
        name=f"Project-{id_}",
        basename=f"Project-{id_}",
        created=datetime.utcnow(),
        visibility=visibility,
        members=[ProjectMember(user=u, role=r, added=datetime.utcnow(), added_by=owner) for u, r in members],
    )
    project.save()
    return project


def make_task(company, project_id, owner, id_="task1", status=TaskStatus.created, task_company=None, **fields):
    task = Task(
        id=id_,
        company=task_company or company,
        user=owner,
        project=project_id,
        name=f"Task-{id_}",
        type="training",
        created=datetime.utcnow(),
        status=status,
        **fields,
    )
    task.save()
    return task


def make_queue(company, created_by="owner1", id_=None):
    queue = Queue(
        id=id_ or uuid4().hex,
        company=company,
        name=f"q-{uuid4().hex[:6]}",
        created=datetime.utcnow(),
        created_by=created_by,
    )
    queue.save()
    return queue


def queue_entries(queue_id):
    return [e.task for e in Queue.objects(id=queue_id).first().entries]


def put_in_queue(queue, task_id):
    Queue.objects(id=queue.id).update_one(push__entries=Entry(task=task_id, added=datetime.utcnow()))


def failure_codes(item):
    return list(item["error"]["codes"])


def db_task(task_id):
    return Task.objects(id=task_id).first()


class FakeWorkerBLL:
    def __init__(self, workers):
        self._workers = workers

    def get_all(self, company_id):
        return self._workers


def assert_error(exc_info, code, subcode):
    assert exc_info.value.codes == (code, subcode)


# --- stop / stopped / started / failed / close / completed ----------------------------------------------------------


def test_stop_refuses_a_created_task_with_invalid_status(company):
    make_root(company)
    make_task(company, "root1", "owner1")
    with pytest.raises(errors.bad_request.InvalidTaskStatus) as exc:
        svc.stop(FakeCall(identity(company, "owner1")), company, StopRequest(task="task1"))
    assert_error(exc, 400, 110)
    assert db_task("task1").status == TaskStatus.created


def test_stop_with_force_skips_the_status_check_for_a_created_task(company):
    # the stop docstring: a status the transitions refuse is refused unless force=True skips that check
    make_root(company)
    make_task(company, "root1", "owner1")
    call = FakeCall(identity(company, "owner1"))
    svc.stop(call, company, StopRequest(task="task1", force=True))
    assert call.result.data_model.updated == 1
    assert db_task("task1").status == TaskStatus.stopped


def test_stop_of_an_in_progress_task_without_worker_stops_it(company):
    make_root(company)
    make_task(company, "root1", "owner1", status=TaskStatus.in_progress)
    call = FakeCall(identity(company, "owner1"))
    svc.stop(call, company, StopRequest(task="task1"))
    assert call.result.data_model.updated == 1
    assert db_task("task1").status == TaskStatus.stopped


def test_stop_of_a_task_run_by_a_recent_worker_only_sets_stopping(company):
    make_root(company)
    make_task(
        company,
        "root1",
        "owner1",
        status=TaskStatus.in_progress,
        last_worker="w",
        last_update=datetime.utcnow(),
    )
    svc.stop(FakeCall(identity(company, "owner1")), company, StopRequest(task="task1"))
    task = db_task("task1")
    assert task.status == TaskStatus.in_progress
    assert task.status_message == TaskStatusMessage.stopping


@pytest.mark.needs_redis
def test_stop_of_a_queued_task_dequeues_it(company):
    make_root(company)
    queue = make_queue(company)
    make_task(company, "root1", "owner1", status=TaskStatus.queued, execution=Execution(queue=queue.id))
    put_in_queue(queue, "task1")
    svc.stop(FakeCall(identity(company, "owner1")), company, StopRequest(task="task1"))
    assert db_task("task1").status == TaskStatus.stopped
    assert queue_entries(queue.id) == []


def test_stop_many_reports_a_missing_id_as_a_failure(company):
    make_root(company)
    make_task(company, "root1", "owner1", id_="own", status=TaskStatus.in_progress)
    call = FakeCall(identity(company, "owner1"))
    svc.stop_many(call, company, StopManyRequest(ids=["own", "missing"]))
    succeeded = call.result.data_model.succeeded
    failed = call.result.data_model.failed
    assert [item.id for item in succeeded] == ["own"]
    assert len(failed) == 1
    assert failed[0]["id"] == "missing"
    assert failure_codes(failed[0]) == [400, 101]


def test_stop_many_is_all_or_nothing_for_a_read_write_member(company):
    make_root(company, members=[("u2", ProjectRole.read_write)])
    make_task(company, "root1", "u2", id_="own-task", status=TaskStatus.in_progress)
    make_task(company, "root1", "owner1", id_="other-task", status=TaskStatus.in_progress)
    with pytest.raises(errors.forbidden.NoWritePermission):
        svc.stop_many(
            FakeCall(identity(company, "u2")),
            company,
            StopManyRequest(ids=["own-task", "other-task"], force=True),
        )
    assert db_task("own-task").status == TaskStatus.in_progress
    assert db_task("other-task").status == TaskStatus.in_progress


def test_stopped_from_in_progress_sets_completed(company):
    make_root(company)
    make_task(company, "root1", "owner1", status=TaskStatus.in_progress)
    svc.stopped(FakeCall(identity(company, "owner1")), company, UpdateRequest(task="task1"))
    task = db_task("task1")
    assert task.status == TaskStatus.stopped
    assert task.completed is not None


def test_stopped_from_created_is_refused(company):
    make_root(company)
    make_task(company, "root1", "owner1")
    with pytest.raises(errors.bad_request.InvalidTaskStatus) as exc:
        svc.stopped(FakeCall(identity(company, "owner1")), company, UpdateRequest(task="task1"))
    assert_error(exc, 400, 110)


def test_started_from_created_sets_in_progress_and_started(company):
    make_root(company)
    make_task(company, "root1", "owner1")
    call = FakeCall(identity(company, "owner1"))
    svc.started(call, company, UpdateRequest(task="task1"))
    task = db_task("task1")
    assert task.status == TaskStatus.in_progress
    assert task.started is not None
    assert task.completed is None
    assert call.result.data_model.started == 1


# intended, see #300
def test_started_on_a_missing_id_gives_invalid_id_subcode_10(company):
    # started looks the task up with the company scope, so a missing id answers like a hidden one (InvalidTaskId)
    with pytest.raises(errors.bad_request.InvalidTaskId) as exc:
        svc.started(FakeCall(identity(company, "owner1")), company, UpdateRequest(task="missing"))
    assert_error(exc, 400, 101)
    assert exc.value.error_data == {"id": "missing", "company": company}


# intended, see #300
def test_started_on_a_hidden_task_gives_invalid_task_id_subcode_101(company):
    make_root(company, owner="owner1")
    make_task(company, "root1", "owner1")
    with pytest.raises(errors.bad_request.InvalidTaskId) as exc:
        svc.started(FakeCall(identity(company, "stranger")), company, UpdateRequest(task="task1"))
    assert_error(exc, 400, 101)
    assert db_task("task1").status == TaskStatus.created


# intended, see #300
def test_started_on_another_companys_task_gives_invalid_task_id_subcode_101(company):
    other = f"c-{uuid4().hex[:8]}"
    make_task(company, None, "owner1", task_company=other)
    with pytest.raises(errors.bad_request.InvalidTaskId) as exc:
        svc.started(FakeCall(identity(company, "owner1")), company, UpdateRequest(task="task1"))
    assert_error(exc, 400, 101)
    assert db_task("task1").status == TaskStatus.created


def test_failed_from_created_is_refused(company):
    make_root(company)
    make_task(company, "root1", "owner1")
    with pytest.raises(errors.bad_request.InvalidTaskStatus) as exc:
        svc.failed(FakeCall(identity(company, "owner1")), company, UpdateRequest(task="task1"))
    assert_error(exc, 400, 110)


def test_failed_from_in_progress_sets_completed(company):
    make_root(company)
    make_task(company, "root1", "owner1", status=TaskStatus.in_progress)
    svc.failed(FakeCall(identity(company, "owner1")), company, UpdateRequest(task="task1"))
    task = db_task("task1")
    assert task.status == TaskStatus.failed
    assert task.completed is not None


def test_close_from_stopped_closes_the_task(company):
    make_root(company)
    make_task(company, "root1", "owner1", status=TaskStatus.stopped)
    svc.close(FakeCall(identity(company, "owner1")), company, UpdateRequest(task="task1"))
    assert db_task("task1").status == TaskStatus.closed


def test_close_from_in_progress_is_refused(company):
    make_root(company)
    make_task(company, "root1", "owner1", status=TaskStatus.in_progress)
    with pytest.raises(errors.bad_request.InvalidTaskStatus) as exc:
        svc.close(FakeCall(identity(company, "owner1")), company, UpdateRequest(task="task1"))
    assert_error(exc, 400, 110)
    assert db_task("task1").status == TaskStatus.in_progress


def test_close_on_a_missing_id_gives_invalid_task_id(company):
    with pytest.raises(errors.bad_request.InvalidTaskId) as exc:
        svc.close(FakeCall(identity(company, "owner1")), company, UpdateRequest(task="missing"))
    assert_error(exc, 400, 101)


def test_completed_from_in_progress_sets_completed(company):
    make_root(company)
    make_task(company, "root1", "owner1", status=TaskStatus.in_progress)
    call = FakeCall(identity(company, "owner1"))
    svc.completed(call, company, CompletedRequest(task="task1"))
    task = db_task("task1")
    assert task.status == TaskStatus.completed
    assert task.completed is not None
    assert not call.result.data_model.published


@pytest.mark.needs_redis
def test_completed_with_publish_publishes_and_returns_status(company):
    make_root(company)
    make_task(company, "root1", "owner1", status=TaskStatus.in_progress)
    call = FakeCall(identity(company, "owner1"))
    svc.completed(call, company, CompletedRequest(task="task1", publish=True))
    assert db_task("task1").status == TaskStatus.published
    assert call.result.data_model.published
    assert call.result.data_model.fields["status"] == TaskStatus.published


# --- publish ---------------------------------------------------------------------------------------------------------


def test_publish_from_completed_succeeds(company):
    make_root(company)
    make_task(company, "root1", "owner1", status=TaskStatus.completed)
    svc.publish(FakeCall(identity(company, "owner1")), company, PublishRequest(task="task1", publish_model=False))
    task = db_task("task1")
    assert task.status == TaskStatus.published
    assert task.published is not None


def test_publish_from_created_is_refused(company):
    make_root(company)
    make_task(company, "root1", "owner1")
    with pytest.raises(errors.bad_request.InvalidTaskStatus) as exc:
        svc.publish(FakeCall(identity(company, "owner1")), company, PublishRequest(task="task1", publish_model=False))
    assert_error(exc, 400, 110)
    assert db_task("task1").status == TaskStatus.created


def test_publish_with_force_from_created_succeeds(company):
    make_root(company)
    make_task(company, "root1", "owner1")
    svc.publish(
        FakeCall(identity(company, "owner1")),
        company,
        PublishRequest(task="task1", force=True, publish_model=False),
    )
    assert db_task("task1").status == TaskStatus.published


def test_publish_failure_restores_previous_status(company, monkeypatch):
    make_root(company)
    Model(
        id="model1",
        company=company,
        user="owner1",
        name="model1",
        created=datetime.utcnow(),
        ready=False,
    ).save()
    make_task(
        company,
        "root1",
        "owner1",
        status=TaskStatus.completed,
        models=Models(output=[ModelItem(name="out", model="model1")]),
    )

    def failing_publish(model_id, company_id, identity):
        raise RuntimeError("model publish failed")

    monkeypatch.setattr(ModelBLL, "publish_model", failing_publish)
    with pytest.raises(RuntimeError):
        svc.publish(FakeCall(identity(company, "owner1")), company, PublishRequest(task="task1"))
    assert db_task("task1").status == TaskStatus.completed


def test_publish_many_reports_already_published_as_failure(company):
    make_root(company)
    make_task(company, "root1", "owner1", id_="done", status=TaskStatus.completed)
    make_task(company, "root1", "owner1", id_="again", status=TaskStatus.published)
    call = FakeCall(identity(company, "owner1"))
    svc.publish_many(call, company, PublishManyRequest(ids=["done", "again"], publish_model=False))
    assert [item.id for item in call.result.data_model.succeeded] == ["done"]
    assert len(call.result.data_model.failed) == 1
    assert call.result.data_model.failed[0]["id"] == "again"
    assert failure_codes(call.result.data_model.failed[0]) == [400, 110]


# --- enqueue ---------------------------------------------------------------------------------------------------------


def test_enqueue_checks_verify_watched_before_access(company):
    # the task is missing: a validation error proves the request is validated before the task is looked up
    with pytest.raises(errors.bad_request.ValidationError) as exc:
        svc.enqueue(
            FakeCall(identity(company, "owner1")),
            company,
            EnqueueRequest(task="missing", queue="q", verify_watched_queue=True, update_execution_queue=False),
        )
    assert_error(exc, 400, 12)


def test_enqueue_checks_verify_watched_before_access_on_a_hidden_task(company):
    # a hidden task fails the access check, so only the validation can answer with a validation error
    make_root(company, owner="owner1")
    make_task(company, "root1", "owner1")
    with pytest.raises(errors.bad_request.ValidationError) as exc:
        svc.enqueue(
            FakeCall(identity(company, "stranger")),
            company,
            EnqueueRequest(task="task1", queue="q", verify_watched_queue=True, update_execution_queue=False),
        )
    assert_error(exc, 400, 12)


def test_enqueue_refuses_a_read_only_member(company):
    make_root(company, members=[("u2", ProjectRole.read)])
    make_task(company, "root1", "owner1")
    with pytest.raises(errors.forbidden.NoWritePermission):
        svc.enqueue(
            FakeCall(identity(company, "u2")),
            company,
            EnqueueRequest(task="task1", queue="default"),
        )
    assert db_task("task1").status == TaskStatus.created


def test_enqueue_on_a_hidden_task_gives_invalid_task_id(company):
    make_root(company, owner="owner1")
    make_task(company, "root1", "owner1")
    with pytest.raises(errors.bad_request.InvalidTaskId) as exc:
        svc.enqueue(
            FakeCall(identity(company, "stranger")),
            company,
            EnqueueRequest(task="task1", queue="q"),
        )
    assert_error(exc, 400, 101)


def test_enqueue_on_a_missing_id_gives_invalid_task_id(company):
    queue = make_queue(company)
    with pytest.raises(errors.bad_request.InvalidTaskId) as exc:
        svc.enqueue(
            FakeCall(identity(company, "owner1")),
            company,
            EnqueueRequest(task="missing", queue=queue.id),
        )
    assert_error(exc, 400, 101)
    # the same data a hidden task gets: the two answers cannot be told apart by the caller
    assert exc.value.error_data == {"id": "missing", "company": company}


def test_enqueue_with_queue_and_queue_name_is_refused_before_the_task_lookup(company):
    with pytest.raises(errors.bad_request.ValidationError) as exc:
        enqueue_task(
            task_id="missing",
            company_id=company,
            identity=identity(company, "owner1"),
            queue_id="q1",
            queue_name="q2",
            status_message="",
            status_reason="",
        )
    assert_error(exc, 400, 12)
    assert "Either queue id or queue name should be provided" in exc.value.msg


def test_enqueue_into_a_queue_the_caller_cannot_use_is_refused(company):
    make_root(company, members=[("u2", ProjectRole.read_write)])
    make_task(company, "root1", "u2")
    queue = make_queue(company, created_by="owner")
    with pytest.raises(errors.bad_request.InvalidQueueId) as exc:
        svc.enqueue(
            FakeCall(identity(company, "u2")),
            company,
            EnqueueRequest(task="task1", queue=queue.id),
        )
    assert_error(exc, 400, 701)
    assert db_task("task1").status == TaskStatus.created


def test_enqueue_skip_execution_queue_needs_a_queued_task(company):
    make_root(company)
    queue = make_queue(company)
    make_task(company, "root1", "owner1")
    with pytest.raises(errors.bad_request.ValidationError) as exc:
        svc.enqueue(
            FakeCall(identity(company, "owner1")),
            company,
            EnqueueRequest(task="task1", queue=queue.id, update_execution_queue=False),
        )
    assert_error(exc, 400, 12)
    assert "Cannot skip setting execution queue" in exc.value.msg
    assert db_task("task1").status == TaskStatus.created


def test_enqueue_with_refused_repository_marks_failed_without_error(company, monkeypatch):
    make_root(company)
    queue = make_queue(company)
    make_task(company, "root1", "owner1")

    def refuse(task):
        raise AccessRefused("no access")

    monkeypatch.setattr(github_check, "check_submission", refuse)
    call = FakeCall(identity(company, "owner1"))
    svc.enqueue(call, company, EnqueueRequest(task="task1", queue=queue.id))
    assert call.result.data_model.queued == 0
    task = db_task("task1")
    assert task.status == TaskStatus.failed
    assert task.status_reason == "access refused"
    assert queue_entries(queue.id) == []


@pytest.mark.needs_redis
def test_enqueue_happy_path_sets_queue_and_status(company):
    make_root(company)
    queue = make_queue(company)
    make_task(company, "root1", "owner1")
    call = FakeCall(identity(company, "owner1"))
    svc.enqueue(call, company, EnqueueRequest(task="task1", queue=queue.id))
    assert call.result.data_model.queued == 1
    task = db_task("task1")
    assert task.status == TaskStatus.queued
    assert task.execution.queue == queue.id
    assert queue_entries(queue.id) == ["task1"]


@pytest.mark.needs_redis
def test_enqueue_verify_watched_queue_reports_workers(company, monkeypatch):
    from apiserver.services import tasks as tasks_module

    monkeypatch.setattr(tasks_module.queue_bll, "check_for_workers", lambda company_id, queue_id: True)
    make_root(company)
    queue = make_queue(company)
    make_task(company, "root1", "owner1")
    call = FakeCall(identity(company, "owner1"))
    svc.enqueue(call, company, EnqueueRequest(task="task1", queue=queue.id, verify_watched_queue=True))
    assert call.result.data_model.queue_watched is True


@pytest.mark.needs_redis
def test_enqueue_many_reports_a_missing_id_as_a_failure(company):
    make_root(company)
    queue = make_queue(company)
    make_task(company, "root1", "owner1", id_="own")
    call = FakeCall(identity(company, "owner1"))
    svc.enqueue_many(call, company, EnqueueManyRequest(ids=["own", "missing"], queue=queue.id))
    assert [item.id for item in call.result.data_model.succeeded] == ["own"]
    assert len(call.result.data_model.failed) == 1
    assert call.result.data_model.failed[0]["id"] == "missing"
    assert failure_codes(call.result.data_model.failed[0]) == [400, 101]


def test_enqueue_many_refuses_the_whole_batch_for_a_read_only_member(company):
    make_root(company, id_="rw", members=[("u2", ProjectRole.read_write)])
    make_root(company, id_="ro", members=[("u2", ProjectRole.read)])
    make_task(company, "rw", "u2", id_="writable")
    make_task(company, "ro", "owner1", id_="readonly")
    with pytest.raises(errors.forbidden.NoWritePermission):
        svc.enqueue_many(
            FakeCall(identity(company, "u2")),
            company,
            EnqueueManyRequest(ids=["writable", "readonly"], queue="q"),
        )
    assert db_task("writable").status == TaskStatus.created
    assert db_task("readonly").status == TaskStatus.created


@pytest.mark.needs_redis
# suspect: pinned as is, see the decision issue (#282)
def test_enqueue_many_has_no_verify_watched_validation(company, monkeypatch):
    from apiserver.services import tasks as tasks_module

    # enqueue_many has no update_execution_queue field, so the single endpoint's validation does not apply
    monkeypatch.setattr(tasks_module.queue_bll, "check_for_workers", lambda company_id, queue_id: True)
    make_root(company)
    queue = make_queue(company)
    make_task(company, "root1", "owner1")
    call = FakeCall(identity(company, "owner1"))
    svc.enqueue_many(
        call,
        company,
        EnqueueManyRequest(ids=["task1"], queue=queue.id, verify_watched_queue=True),
    )
    assert call.result.data_model.failed == []
    assert call.result.data_model.queue_watched is True


@pytest.mark.needs_redis
# suspect: pinned as is, see the decision issue (#282)
def test_single_enqueue_never_validates_but_enqueue_many_does(company):
    make_root(company)
    queue = make_queue(company)
    # a parent that does not exist: validation refuses it, the single enqueue does not look at it
    make_task(company, "root1", "owner1", id_="child", parent="missing-parent")
    make_task(company, "root1", "owner1", id_="child-batch", parent="missing-parent")

    single = FakeCall(identity(company, "owner1"))
    svc.enqueue(single, company, EnqueueRequest(task="child", queue=queue.id))
    assert single.result.data_model.queued == 1

    batch = FakeCall(identity(company, "owner1"))
    svc.enqueue_many(batch, company, EnqueueManyRequest(ids=["child-batch"], queue=queue.id, validate_tasks=True))
    assert batch.result.data_model.succeeded == []
    assert len(batch.result.data_model.failed) == 1
    assert failure_codes(batch.result.data_model.failed[0]) == [400, 101]
    assert db_task("child-batch").status == TaskStatus.created


# --- enqueue: check order with a job token (pipeline deadlock check) ---------------------------------------------------


def test_enqueue_by_a_job_checks_access_before_the_deadlock(company, monkeypatch):
    # the deadlock check would refuse this enqueue, so a validation error here would mean the order was swapped
    make_root(company, members=[])
    queue = make_queue(company)
    make_task(company, "root1", "owner1")
    monkeypatch.setattr(
        svc.queue_bll,
        "worker_bll",
        FakeWorkerBLL([SimpleNamespace(queues=[queue.id], task=SimpleNamespace(id="job-task"))]),
    )
    with pytest.raises(errors.bad_request.InvalidTaskId) as exc:
        svc.enqueue(
            FakeCall(identity(company, "stranger"), auth=SimpleNamespace(job_task="job-task")),
            company,
            EnqueueRequest(task="task1", queue=queue.id),
        )
    assert_error(exc, 400, 101)


def test_enqueue_many_by_a_job_refuses_the_whole_batch_on_deadlock(company, monkeypatch):
    make_root(company)
    queue = make_queue(company)
    make_task(company, "root1", "owner1", id_="first")
    make_task(company, "root1", "owner1", id_="second")
    monkeypatch.setattr(
        svc.queue_bll,
        "worker_bll",
        FakeWorkerBLL([SimpleNamespace(queues=[queue.id], task=SimpleNamespace(id="job-task"))]),
    )
    with pytest.raises(errors.bad_request.ValidationError) as exc:
        svc.enqueue_many(
            FakeCall(identity(company, "owner1"), auth=SimpleNamespace(job_task="job-task")),
            company,
            EnqueueManyRequest(ids=["first", "second"], queue=queue.id),
        )
    assert_error(exc, 400, 12)
    assert "Pipeline dead-locked" in exc.value.msg
    assert db_task("first").status == TaskStatus.created
    assert db_task("second").status == TaskStatus.created


# --- dequeue -------------------------------------------------------------------------------------------------------


def test_dequeue_with_unknown_status_is_refused_before_lookup(company):
    with pytest.raises(errors.bad_request.ValidationError) as exc:
        svc.dequeue(
            FakeCall(identity(company, "owner1")),
            company,
            DequeueRequest(task="missing", new_status="nope"),
        )
    assert_error(exc, 400, 12)
    assert "Invalid task status: nope" in exc.value.msg


# intended, see #299
def test_dequeue_of_a_missing_id_reports_success(company):
    with pytest.raises(errors.bad_request.InvalidTaskId) as exc:
        svc.dequeue(FakeCall(identity(company, "owner1")), company, DequeueRequest(task="missing"))
    assert_error(exc, 400, 101)


# intended, see #299
def test_dequeue_of_a_hidden_task_gives_invalid_task_id(company):
    make_root(company, owner="owner1")
    make_task(company, "root1", "owner1")
    with pytest.raises(errors.bad_request.InvalidTaskId) as exc:
        svc.dequeue(FakeCall(identity(company, "stranger")), company, DequeueRequest(task="task1"))
    assert_error(exc, 400, 101)


def test_dequeue_of_a_task_not_queued_reports_zero_updates(company):
    make_root(company)
    make_task(company, "root1", "owner1")
    call = FakeCall(identity(company, "owner1"))
    svc.dequeue(call, company, DequeueRequest(task="task1"))
    assert call.result.data_model.dequeued == 1
    assert call.result.data_model.updated == 0


@pytest.mark.needs_redis
def test_dequeue_removes_a_queued_task_and_sets_status(company):
    make_root(company)
    queue = make_queue(company)
    make_task(company, "root1", "owner1", status=TaskStatus.queued, execution=Execution(queue=queue.id))
    put_in_queue(queue, "task1")
    call = FakeCall(identity(company, "owner1"))
    svc.dequeue(call, company, DequeueRequest(task="task1"))
    assert call.result.data_model.dequeued == 1
    assert db_task("task1").status == TaskStatus.created
    assert queue_entries(queue.id) == []


@pytest.mark.needs_redis
# suspect: pinned as is, see the decision issue (#282)
def test_dequeue_many_reports_a_missing_id_as_success(company):
    make_root(company)
    queue = make_queue(company)
    make_task(company, "root1", "owner1", id_="queued", status=TaskStatus.queued, execution=Execution(queue=queue.id))
    put_in_queue(queue, "queued")
    call = FakeCall(identity(company, "owner1"))
    svc.dequeue_many(call, company, DequeueManyRequest(ids=["queued", "missing"]))
    assert call.result.data_model.failed == []
    succeeded = {item.id: item for item in call.result.data_model.succeeded}
    assert set(succeeded) == {"queued", "missing"}
    assert succeeded["missing"].dequeued is True


# --- archive / unarchive ---------------------------------------------------------------------------------------------


@pytest.mark.needs_redis
def test_archive_adds_the_archived_tag_and_returns_count(company):
    make_root(company)
    make_task(company, "root1", "owner1")
    call = FakeCall(identity(company, "owner1"))
    svc.archive(call, company, ArchiveRequest(tasks=["task1"]))
    assert call.result.data_model.archived == 1
    assert "archived" in db_task("task1").system_tags


# intended, see #299
def test_archive_of_a_hidden_task_gives_no_write_permission(company):
    make_root(company, owner="owner1")
    make_task(company, "root1", "owner1")
    with pytest.raises(errors.bad_request.InvalidTaskId) as exc:
        svc.archive(FakeCall(identity(company, "stranger")), company, ArchiveRequest(tasks=["task1"]))
    assert_error(exc, 400, 101)
    assert "archived" not in (db_task("task1").system_tags or [])


def test_archive_of_a_missing_id_gives_invalid_task_id_with_ids(company):
    with pytest.raises(errors.bad_request.InvalidTaskId) as exc:
        svc.archive(FakeCall(identity(company, "owner1")), company, ArchiveRequest(tasks=["missing"]))
    assert_error(exc, 400, 101)
    assert exc.value.error_data["ids"] == ["missing"]


# intended, see #299
def test_archive_many_hidden_refuses_whole_request(company):
    make_root(company, id_="own-root", members=[("u2", ProjectRole.read_write)])
    make_root(company, id_="hidden-root", owner="owner1")
    make_task(company, "own-root", "u2", id_="own")
    make_task(company, "hidden-root", "owner1", id_="hidden")
    call = FakeCall(identity(company, "u2"))
    svc.archive_many(call, company, ArchiveManyRequest(ids=["own", "hidden"]))
    assert [item["id"] for item in call.result.data_model.succeeded] == ["own"]
    assert len(call.result.data_model.failed) == 1
    assert call.result.data_model.failed[0]["id"] == "hidden"
    assert failure_codes(call.result.data_model.failed[0]) == [400, 101]
    assert "archived" in (db_task("own").system_tags or [])
    assert "archived" not in (db_task("hidden").system_tags or [])


@pytest.mark.needs_redis
def test_archive_many_missing_id_is_a_per_item_failure(company):
    make_root(company)
    make_task(company, "root1", "owner1", id_="own")
    call = FakeCall(identity(company, "owner1"))
    svc.archive_many(call, company, ArchiveManyRequest(ids=["own", "missing"]))
    assert [item["id"] for item in call.result.data_model.succeeded] == ["own"]
    assert call.result.data_model.succeeded[0]["archived"] is True
    assert len(call.result.data_model.failed) == 1
    assert call.result.data_model.failed[0]["id"] == "missing"
    assert failure_codes(call.result.data_model.failed[0]) == [400, 101]


def test_unarchive_many_removes_the_archived_tag(company):
    make_root(company)
    make_task(company, "root1", "owner1", system_tags=["archived"])
    call = FakeCall(identity(company, "owner1"))
    svc.unarchive_many(call, company, UnarchiveManyRequest(ids=["task1"]))
    assert call.result.data_model.succeeded[0]["unarchived"] is True
    assert "archived" not in (db_task("task1").system_tags or [])


# --- reset -----------------------------------------------------------------------------------------------------------


def test_reset_refuses_a_published_task_without_force(company):
    make_root(company)
    make_task(company, "root1", "owner1", status=TaskStatus.published)
    with pytest.raises(errors.bad_request.InvalidTaskStatus) as exc:
        svc.reset(FakeCall(identity(company, "owner1")), company, ResetRequest(task="task1"))
    assert_error(exc, 400, 110)
    assert db_task("task1").status == TaskStatus.published


def test_reset_of_a_stopped_task_returns_it_to_created(company, monkeypatch):
    # the event deletion goes to Elasticsearch, which this check does not need
    monkeypatch.setattr(svc, "delete_task_events_for_cleanup", lambda **kwargs: None)
    make_root(company)
    make_task(
        company,
        "root1",
        "owner1",
        status=TaskStatus.stopped,
        started=datetime.utcnow(),
        completed=datetime.utcnow(),
        execution=Execution(queue="q-old"),
    )
    svc.reset(FakeCall(identity(company, "owner1")), company, ResetRequest(task="task1"))
    task = db_task("task1")
    assert task.status == TaskStatus.created
    assert task.started is None
    assert task.completed is None
    assert not (task.execution and task.execution.queue)


def test_reset_many_uses_async_event_delete(company, monkeypatch):
    captured = []

    def capture(**kwargs):
        captured.append(kwargs["sync_delete"])

    monkeypatch.setattr(svc, "delete_task_events_for_cleanup", capture)
    make_root(company)
    make_task(company, "root1", "owner1", id_="first", status=TaskStatus.stopped)
    make_task(company, "root1", "owner1", id_="second", status=TaskStatus.stopped)
    svc.reset_many(FakeCall(identity(company, "owner1")), company, ResetManyRequest(ids=["first", "second"]))
    assert captured == [False]
    assert db_task("first").status == TaskStatus.created
    assert db_task("second").status == TaskStatus.created


# --- delete ----------------------------------------------------------------------------------------------------------


def test_delete_of_an_in_progress_task_is_refused_without_force(company):
    make_root(company)
    make_task(company, "root1", "owner1", status=TaskStatus.in_progress)
    with pytest.raises(errors.bad_request.TaskCannotBeDeleted) as exc:
        svc.delete(FakeCall(identity(company, "owner1")), company, DeleteRequest(task="task1", force=False))
    assert_error(exc, 400, 123)
    assert db_task("task1") is not None


@pytest.mark.needs_redis
def test_delete_of_an_archived_in_progress_task_is_allowed(company, monkeypatch):
    monkeypatch.setattr(svc, "delete_task_events_for_cleanup", lambda **kwargs: None)
    make_root(company)
    make_task(company, "root1", "owner1", status=TaskStatus.in_progress, system_tags=["archived"])
    svc.delete(FakeCall(identity(company, "owner1")), company, DeleteRequest(task="task1"))
    assert db_task("task1") is None


@pytest.mark.needs_redis
def test_delete_with_move_to_trash_keeps_a_copy_in_trash_collection(company, monkeypatch):
    monkeypatch.setattr(svc, "delete_task_events_for_cleanup", lambda **kwargs: None)
    make_root(company)
    make_task(company, "root1", "owner1")
    svc.delete(FakeCall(identity(company, "owner1")), company, DeleteRequest(task="task1", move_to_trash=True))
    assert db_task("task1") is None
    trash = Task._get_collection().database[f"{Task._get_collection_name()}__trash"]
    assert trash.count_documents({"_id": "task1"}) == 1


@pytest.mark.needs_redis
def test_delete_many_missing_id_is_a_failure(company, monkeypatch):
    monkeypatch.setattr(svc, "delete_task_events_for_cleanup", lambda **kwargs: None)
    make_root(company)
    make_task(company, "root1", "owner1", id_="own")
    call = FakeCall(identity(company, "owner1"))
    svc.delete_many(call, company, DeleteManyRequest(ids=["own", "missing"]))
    assert [item["id"] for item in call.result.data["succeeded"]] == ["own"]
    failed = call.result.data["failed"]
    assert len(failed) == 1
    assert failed[0]["id"] == "missing"
    assert failure_codes(failed[0]) == [400, 101]
    assert db_task("own") is None


# --- move ------------------------------------------------------------------------------------------------------------


def test_move_without_project_is_refused(company):
    make_root(company)
    make_task(company, "root1", "owner1")
    with pytest.raises(errors.bad_request.MissingRequiredFields) as exc:
        svc.move(FakeCall(identity(company, "owner1"), data={}), company, MoveRequest(ids=["task1"]))
    assert_error(exc, 400, 11)


def test_move_of_a_missing_id_gives_invalid_task_id_with_ids(company):
    # the endpoint takes the project from the raw call data as well as from the request, so both are given
    with pytest.raises(errors.bad_request.InvalidTaskId) as exc:
        svc.move(
            FakeCall(identity(company, "owner1"), data={"project": "dst"}),
            company,
            MoveRequest(ids=["missing"], project="dst"),
        )
    assert_error(exc, 400, 101)
    assert exc.value.error_data["ids"] == ["missing"]


# intended, see #299
def test_move_of_a_hidden_task_gives_no_write_permission(company):
    make_root(company, id_="src", owner="owner1")
    make_root(company, id_="dst", owner="owner1")
    make_task(company, "src", "owner1")
    with pytest.raises(errors.bad_request.InvalidTaskId) as exc:
        svc.move(
            FakeCall(identity(company, "stranger"), data={"project": "dst"}),
            company,
            MoveRequest(ids=["task1"], project="dst"),
        )
    assert_error(exc, 400, 101)
    assert db_task("task1").project == "src"


def test_move_needs_admin_on_the_destination_project(company):
    # the caller owns the source project, so only the destination check can refuse the move
    make_root(company, id_="src", owner="u2")
    make_root(company, id_="dst", members=[("u2", ProjectRole.read_write)])
    make_task(company, "src", "u2")
    with pytest.raises(errors.forbidden.NoWritePermission):
        svc.move(
            FakeCall(identity(company, "u2"), data={"project": "dst"}),
            company,
            MoveRequest(ids=["task1"], project="dst"),
        )
    assert db_task("task1").project == "src"


def test_move_needs_admin_on_the_current_project(company):
    make_root(company, id_="src", members=[("u2", ProjectRole.read_write)])
    make_root(company, id_="dst", members=[("u2", ProjectRole.collaborator)])
    make_task(company, "src", "u2")
    with pytest.raises(errors.forbidden.NoWritePermission):
        svc.move(
            FakeCall(identity(company, "u2"), data={"project": "dst"}),
            company,
            MoveRequest(ids=["task1"], project="dst"),
        )
    assert db_task("task1").project == "src"
