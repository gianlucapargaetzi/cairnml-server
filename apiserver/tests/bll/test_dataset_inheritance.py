# apiserver/tests/bll/test_dataset_inheritance.py
"""A finished run (completed or failed) shares the datasets it used with its project, as inherited shares. The credential
of a dataset is shared too only when the run's user owns it or its owner already shares it with the project (decision
3). A stopped run publishes nothing, and an error in the hook never fails the status change."""
from datetime import datetime
from uuid import uuid4

import pytest

from apiserver.bll import dataset_inheritance
from apiserver.bll.task import utils as task_utils
from apiserver.bll.task.utils import ChangeStatusRequest
from apiserver.database.model.auth import User
from apiserver.database.model.dataset import Dataset
from apiserver.database.model.project import Project, ProjectMember, ProjectRole
from apiserver.database.model.share import Share
from apiserver.database.model.task.task import ParamsItem, Task, TaskStatus
from apiserver.database.model.user_access import UserAccess
from apiserver.tests.db import ensure_db

ensure_db()

SECTION = "Datasets"
RUNNER = "runner"
OWNER = "owner1"
ROOT = "root1"


@pytest.fixture
def company():
    return f"c-{uuid4().hex[:8]}"


@pytest.fixture
def setup(company):
    for user in (RUNNER, OWNER, "other"):
        User(id=user, name=user, company=company, created=datetime.utcnow()).save()
    Project(id=ROOT, company=company, user=OWNER, name=ROOT, basename=ROOT, created=datetime.utcnow(),
            members=[ProjectMember(user=RUNNER, role=ProjectRole.read, added=datetime.utcnow(), added_by=OWNER)]).save()
    return company


def make_dataset(company, owner, ref, credential=None):
    dataset = Dataset(id=uuid4().hex, company=company, kind="repo", ref=ref, name=ref.split("/")[1], owner=owner,
                      visibility="user", credential=credential, state="ok", created=datetime.utcnow())
    dataset.save()
    return dataset


def make_credential(company, user):
    entry = UserAccess(id=uuid4().hex, company=company, user=user, kind="token", label="token", host="github.com",
                       path="", secret="secret", created=datetime.utcnow())
    entry.save()
    return entry


def make_task(company, user, refs=(), status=TaskStatus.in_progress):
    hyperparams = {SECTION: {}}
    for alias, ref in refs:
        for key, value in (("repo", ref), ("kind", "local")):
            name = f"{alias}/{key}"
            hyperparams[SECTION][name] = ParamsItem(section=SECTION, name=name, value=value, type="str")
    task = Task(id=uuid4().hex, company=company, user=user, name="train", type="training", created=datetime.utcnow(),
                started=datetime.utcnow(), status=status, project=ROOT, hyperparams=hyperparams)
    task.save()
    return task


def finish(task, status, user):
    """The status change the server makes, through the same path as the API."""
    ChangeStatusRequest(task=task, new_status=status, force=True, user_id=user).execute()


def dataset_shares(company, dataset):
    return list(Share.objects(company=company, resource_type="dataset", resource_id=dataset.id, scope="projects",
                              project=ROOT))


def credential_shares(company, entry):
    return list(Share.objects(company=company, resource_type="credential", resource_id=entry.id, scope="projects",
                              project=ROOT))


# --- which runs publish ---------------------------------------------------------------------------------------------

def test_a_completed_run_inherits_its_dataset(setup):
    company = setup
    dataset = make_dataset(company, OWNER, "owner1/cars")
    task = make_task(company, RUNNER, refs=[("cars", "owner1/cars")])
    finish(task, TaskStatus.completed, RUNNER)

    [share] = dataset_shares(company, dataset)
    assert share.level == "read"
    assert share.inherited is True
    assert share.run_id == task.id
    assert share.granted_by == OWNER  # the root project owner grants project shares (R20)


def test_a_failed_run_inherits_its_dataset(setup):
    company = setup
    dataset = make_dataset(company, OWNER, "owner1/cars")
    task = make_task(company, RUNNER, refs=[("cars", "owner1/cars")])
    finish(task, TaskStatus.failed, RUNNER)

    [share] = dataset_shares(company, dataset)
    assert share.inherited is True
    assert share.run_id == task.id


def test_a_stopped_run_publishes_nothing(setup):
    company = setup
    dataset = make_dataset(company, OWNER, "owner1/cars")
    task = make_task(company, RUNNER, refs=[("cars", "owner1/cars")])
    finish(task, TaskStatus.stopped, RUNNER)

    assert dataset_shares(company, dataset) == []


def test_a_run_using_no_dataset_creates_nothing(setup):
    company = setup
    make_dataset(company, OWNER, "owner1/cars")
    task = make_task(company, RUNNER, refs=[])
    finish(task, TaskStatus.completed, RUNNER)

    assert dataset_inheritance.publish_run_datasets(task) == []
    assert Share.objects(company=company).count() == 0


def test_a_dataset_already_shared_with_the_project_is_left_as_it_is(setup):
    company = setup
    dataset = make_dataset(company, OWNER, "owner1/cars")
    manual = Share(id=uuid4().hex, company=company, resource_type="dataset", resource_id=dataset.id, scope="projects",
                   project=ROOT, user="", level="read_write", granted_by=OWNER, created=datetime.utcnow())
    manual.save()
    task = make_task(company, RUNNER, refs=[("cars", "owner1/cars")])

    assert dataset_inheritance.publish_run_datasets(task) == []
    [share] = dataset_shares(company, dataset)
    assert share.id == manual.id
    assert share.inherited is False
    assert share.level == "read_write"


def test_a_deactivated_run_user_creates_no_shares(setup):
    company = setup
    dataset = make_dataset(company, OWNER, "owner1/cars")
    task = make_task(company, RUNNER, refs=[("cars", "owner1/cars")])
    User.objects(id=RUNNER).update(idp_disabled=True)

    assert dataset_inheritance.publish_run_datasets(task) == []
    assert dataset_shares(company, dataset) == []


def test_an_error_in_the_hook_is_logged_and_the_run_still_changes_status(setup, monkeypatch):
    company = setup
    make_dataset(company, OWNER, "owner1/cars")
    task = make_task(company, RUNNER, refs=[("cars", "owner1/cars")])

    def broken(task):
        raise RuntimeError("sharing is broken")

    monkeypatch.setattr(task_utils, "publish_run_datasets", broken)
    finish(task, TaskStatus.completed, RUNNER)

    assert Task.objects(id=task.id).first().status == TaskStatus.completed


# --- credentials: decision 3 -----------------------------------------------------------------------------------------

def test_a_credential_owned_by_the_run_user_is_inherited_with_the_dataset(setup):
    company = setup
    entry = make_credential(company, RUNNER)
    dataset = make_dataset(company, OWNER, "owner1/cars", credential=entry.id)
    task = make_task(company, RUNNER, refs=[("cars", "owner1/cars")])
    finish(task, TaskStatus.completed, RUNNER)

    assert len(dataset_shares(company, dataset)) == 1
    [share] = credential_shares(company, entry)
    assert share.level == "read"
    assert share.inherited is True
    assert share.run_id == task.id
    assert share.granted_by == RUNNER


def test_a_credential_of_another_user_is_not_inherited(setup):
    company = setup
    entry = make_credential(company, "other")
    dataset = make_dataset(company, OWNER, "owner1/cars", credential=entry.id)
    task = make_task(company, RUNNER, refs=[("cars", "owner1/cars")])
    finish(task, TaskStatus.completed, RUNNER)

    assert len(dataset_shares(company, dataset)) == 1
    assert credential_shares(company, entry) == []


def test_a_credential_shared_manually_by_its_owner_is_not_replaced(setup):
    company = setup
    entry = make_credential(company, "other")
    dataset = make_dataset(company, OWNER, "owner1/cars", credential=entry.id)
    manual = Share(id=uuid4().hex, company=company, resource_type="credential", resource_id=entry.id, scope="projects",
                   project=ROOT, user="", level="read", granted_by="other", created=datetime.utcnow())
    manual.save()
    task = make_task(company, RUNNER, refs=[("cars", "owner1/cars")])
    finish(task, TaskStatus.completed, RUNNER)

    [share] = credential_shares(company, entry)
    assert share.id == manual.id
    assert share.inherited is False
    assert share.granted_by == "other"
    assert len(dataset_shares(company, dataset)) == 1
