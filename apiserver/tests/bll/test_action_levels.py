# apiserver/tests/bll/test_action_levels.py
""" Each action on a queue, dataset, registry or credential is checked against the share level it needs (bll/access
ACTION_NEEDS), through the endpoints that perform it. Levels are granted through projects, as the writers do. """
from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from apiserver.apierrors import errors
from apiserver.apimodels.queues import AddTaskRequest, RemoveTaskRequest, TaskRequest, UpdateRequest
from apiserver.bll import datasets as datasets_bll
from apiserver.bll import user_access as user_access_bll
from apiserver.bll.dataset_credentials import _authorize
from apiserver.database.model.auth import Role
from apiserver.database.model.dataset import Dataset
from apiserver.database.model.project import Project, ProjectMember, ProjectRole, ProjectVisibility
from apiserver.database.model.queue import Entry, Queue
from apiserver.database.model.share import Share
from apiserver.database.model.task.task import Execution, Task, TaskStatus
from apiserver.database.model.user_access import UserAccess
from apiserver.service_repo.auth import Identity
from apiserver.services import queues as queues_svc
from apiserver.tests.db import ensure_db
from apiserver.utilities.partial_version import PartialVersion

ensure_db()

OWNER = "owner"
READ, READ_WRITE, COLLABORATOR = "read", "read_write", "collaborator"


@pytest.fixture
def company():
    return f"c-levels-{uuid4().hex[:8]}"


@pytest.fixture(autouse=True)
def no_elasticsearch(monkeypatch):
    """ Queue metrics are written to Elasticsearch, which these checks do not need """
    from apiserver.bll.queue.queue_metrics import QueueMetrics
    monkeypatch.setattr(QueueMetrics, "log_queue_metrics_to_es", lambda self, company_id, queues: 0)


def identity(company, user, role=Role.user):
    return Identity(user=user, company=company, role=role)


class FakeCall:
    def __init__(self, ident):
        self.identity = ident
        self.data = {}
        self.result = SimpleNamespace(data=None, data_model=None)


class UpdateCall(FakeCall):
    def __init__(self, ident, **data):
        super().__init__(ident)
        self.requested_endpoint_version = PartialVersion("2.38")
        self.data = data
        self.data_model_for_partial_update = data


def make_project(company, project_id, members=()):
    project = Project(
        id=project_id, company=company, user=OWNER, name=project_id, basename=project_id,
        created=datetime.utcnow(), visibility=ProjectVisibility.private,
        members=[ProjectMember(user=u, role=r, added=datetime.utcnow(), added_by=OWNER) for u, r in members],
    )
    project.save()
    return project


def share(company, resource_type, resource_id, project_id, level):
    Share(id=uuid4().hex, company=company, resource_type=resource_type, resource_id=resource_id, scope="projects",
          project=project_id, level=level, granted_by=OWNER, created=datetime.utcnow()).save()


@pytest.fixture
def levels(company):
    """ One project for each level, each holding one user at that level; a project for the tasks, where all four
    users may write any task, so that only the queue's level decides. """
    make_project(company, "p-read", [("u-read", ProjectRole.collaborator)])
    make_project(company, "p-rw", [("u-rw", ProjectRole.collaborator)])
    make_project(company, "p-col", [("u-col", ProjectRole.collaborator)])
    make_project(company, "p-tasks", [(u, ProjectRole.collaborator) for u in ("u-read", "u-rw", "u-col")])
    yield
    Share.objects(company=company).delete()
    Project.objects(company=company).delete()


# --- queues ------------------------------------------------------------------

@pytest.fixture
def queue(company, levels):
    queue = Queue(id=uuid4().hex, company=company, name="levels-queue", created=datetime.utcnow(), created_by=OWNER)
    queue.save()
    share(company, "queue", queue.id, "p-read", READ)
    share(company, "queue", queue.id, "p-rw", READ_WRITE)
    share(company, "queue", queue.id, "p-col", COLLABORATOR)
    yield queue
    Queue.objects(company=company).delete()
    Task.objects(company=company).delete()


def make_task(company, task_id, user, queue=None):
    now = datetime.utcnow()
    task = Task(id=task_id, company=company, user=user, name=task_id, type="training", project="p-tasks",
                created=now, status=TaskStatus.created)
    if queue is not None:
        task.status = TaskStatus.queued
        task.execution = Execution(queue=queue.id)
        Queue.objects(id=queue.id).update_one(push__entries=Entry(task=task_id, added=now))
    task.save()
    return task


def entries(queue):
    return [e.task for e in Queue.objects(id=queue.id).first().entries]


class TestQueueLevels:
    def test_read_enqueues_its_own_task(self, company, queue):
        make_task(company, "own-read", "u-read")
        svc_call = FakeCall(identity(company, "u-read"))
        queues_svc.add_task(svc_call, company, AddTaskRequest(queue=queue.id, task="own-read"))
        assert entries(queue) == ["own-read"]

    def test_read_cannot_move_another_users_task(self, company, queue):
        make_task(company, "theirs-1", OWNER, queue)
        make_task(company, "theirs-2", OWNER, queue)
        with pytest.raises(errors.forbidden.NoWritePermission):
            queues_svc.move_task_to_front(FakeCall(identity(company, "u-read")), company,
                                          TaskRequest(queue=queue.id, task="theirs-2"))
        assert entries(queue) == ["theirs-1", "theirs-2"]

    def test_read_cannot_remove_another_users_task(self, company, queue):
        make_task(company, "theirs-3", OWNER, queue)
        with pytest.raises(errors.forbidden.NoWritePermission):
            queues_svc.remove_task(FakeCall(identity(company, "u-read")), company,
                                   RemoveTaskRequest(queue=queue.id, task="theirs-3"))
        assert entries(queue) == ["theirs-3"]

    def test_read_write_moves_another_users_task(self, company, queue):
        make_task(company, "theirs-4", OWNER, queue)
        make_task(company, "theirs-5", OWNER, queue)
        call = FakeCall(identity(company, "u-rw"))
        queues_svc.move_task_to_front(call, company, TaskRequest(queue=queue.id, task="theirs-5"))
        assert entries(queue) == ["theirs-5", "theirs-4"]

    def test_read_write_removes_another_users_task(self, company, queue):
        make_task(company, "theirs-6", OWNER, queue)
        queues_svc.remove_task(FakeCall(identity(company, "u-rw")), company,
                               RemoveTaskRequest(queue=queue.id, task="theirs-6"))
        assert entries(queue) == []

    def test_collaborator_may_not_change_the_settings_of_a_queue(self, company, queue):
        """A queue's only setting that changes is its visibility, and only its owner may change it."""
        with pytest.raises(errors.bad_request.ValidationError, match="only the visibility"):
            queues_svc.update(UpdateCall(identity(company, "u-col"), tags=["by-collab"]), company,
                              UpdateRequest(queue=queue.id, tags=["by-collab"]))

    @pytest.mark.parametrize("user", ["u-read", "u-rw"])
    def test_read_and_read_write_cannot_change_the_settings(self, company, queue, user):
        with pytest.raises(errors.forbidden.NoWritePermission):
            queues_svc.update(UpdateCall(identity(company, user), tags=["x"]), company,
                              UpdateRequest(queue=queue.id, tags=["x"]))


# --- datasets and registries ----------------------------------------------------

def make_dataset(company, dataset_id, kind="repo", owner=OWNER, visibility="projects", projects=()):
    dataset = Dataset(id=dataset_id, company=company, kind=kind, ref=f"acme/{dataset_id}", name=dataset_id,
                      owner=owner, visibility=visibility, created=datetime.utcnow(), updated=datetime.utcnow(),
                      last_checked=datetime.utcnow())
    dataset.save()
    for project_id, level in projects:
        share(company, "registry" if kind == "registry" else "dataset", dataset_id, project_id, level)
    return dataset


@pytest.fixture
def dataset(company, levels):
    make_dataset(company, "d-levels", projects=[("p-read", READ), ("p-rw", READ_WRITE), ("p-col", COLLABORATOR)])
    yield "d-levels"
    Dataset.objects(company=company).delete()
    Share.objects(company=company).delete()


class TestDatasetLevels:
    def test_read_sees_and_uses_the_dataset(self, company, dataset):
        assert datasets_bll.can_see(company, "u-read", False, dataset)

    def test_read_cannot_change_the_visibility(self, company, dataset):
        with pytest.raises(errors.bad_request.ValidationError):
            datasets_bll.set_visibility(company, "u-read", dataset, "Public", [], False)
        assert Dataset.objects(id=dataset).first().visibility == "projects"

    def test_read_cannot_write_through_the_store_key(self, company, dataset):
        with pytest.raises(errors.forbidden.NoWritePermission):
            _authorize(company, "u-read", "acme/d-levels", READ_WRITE, False)

    def test_read_write_gets_the_store_key_for_writing(self, company, dataset):
        assert _authorize(company, "u-rw", "acme/d-levels", READ_WRITE, False) == f"dataset/{company}/acme/d-levels"

    def test_read_write_cannot_share_the_dataset(self, company, dataset):
        with pytest.raises(errors.bad_request.ValidationError):
            datasets_bll.allocate(company, "u-rw", dataset, "p-rw", False)

    def test_collaborator_shares_the_dataset(self, company, dataset):
        result = datasets_bll.allocate(company, "u-col", dataset, "p-col", False)
        assert "p-col" in result["projects"]

    def test_read_cannot_remove_a_registry(self, company, levels):
        make_dataset(company, "reg-levels", kind="registry", projects=[("p-read", READ)])
        with pytest.raises(errors.bad_request.ValidationError):
            datasets_bll.delete(company, "u-read", "reg-levels", False)
        assert Dataset.objects(id="reg-levels").first() is not None

    def test_collaborator_removes_a_registry(self, company, levels):
        make_dataset(company, "reg-levels-2", kind="registry", projects=[("p-col", COLLABORATOR)])
        datasets_bll.delete(company, "u-col", "reg-levels-2", False)
        assert Dataset.objects(id="reg-levels-2").first() is None


# --- credentials -------------------------------------------------------------

@pytest.fixture
def credential(company, levels):
    entry = UserAccess(id=f"cred-{uuid4().hex[:6]}", company=company, user=OWNER, kind="token", label="token",
                       host="github.com", path="", secret="stored-secret-value", scope="user",
                       created=datetime.utcnow())
    entry.save()
    share(company, "credential", entry.id, "p-read", READ)
    yield entry
    UserAccess.objects(company=company).delete()
    Share.objects(company=company).delete()


class TestCredentialLevels:
    def test_read_lets_the_project_use_the_credential(self, company, credential):
        from apiserver.bll import access
        level = access.level_of(company, "u-read", False, "credential", credential.id, {"p-read"})
        assert access.allows(level, "use", "credential")

    def test_read_never_sees_the_secret(self, company, credential):
        grants = user_access_bll.project_grants(company, "u-read", "p-read")
        assert grants and "stored-secret-value" not in repr(grants)
        assert not any("secret" in key for grant in grants for key in grant)

    def test_read_cannot_change_the_grants(self, company, credential):
        with pytest.raises(errors.bad_request.ValidationError):
            user_access_bll.grant(company, "u-read", credential.id, "p-col")
        assert Share.objects(company=company, resource_type="credential", resource_id=credential.id,
                             project="p-col").first() is None
