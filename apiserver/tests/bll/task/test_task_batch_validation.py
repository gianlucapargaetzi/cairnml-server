# apiserver/tests/bll/task/test_task_batch_validation.py
#
# tasks.update_batch accepts exactly what tasks.update accepts, stores what update stores, and refuses a batch as a
# whole when one item is refused (issue #301, decision 3 of #282). The B and X2 cases of the characterisation tests
# name the old behaviour; these tests state the intended one.
from datetime import datetime
from types import SimpleNamespace
from unittest import mock
from uuid import uuid4

import pytest

from apiserver.apierrors import errors
from apiserver.apimodels.tasks import UpdateRequest
from apiserver.bll.project import project_changes
from apiserver.database.model.auth import Role
from apiserver.database.model.project import Project, ProjectMember, ProjectRole, ProjectVisibility
from apiserver.database.model.task.task import Script, Task, TaskStatus
from apiserver.service_repo.auth import Identity
from apiserver.services import tasks as svc
from apiserver.tests.db import ensure_db
from apiserver.utilities.partial_version import PartialVersion

ensure_db()

STAMPS = {"_id", "last_change", "last_changed_by", "last_update"}


@pytest.fixture
def company():
    return f"c-{uuid4().hex[:8]}"


@pytest.fixture
def side_effects():
    with (
        mock.patch.object(svc, "org_bll") as org,
        mock.patch.object(svc, "update_project_time") as project_time,
        mock.patch.object(svc, "_record_repository") as record,
        mock.patch.object(project_changes, "org_bll", org),
        mock.patch.object(project_changes, "update_project_time", project_time),
        mock.patch.object(project_changes, "record_repository", record),
    ):
        yield SimpleNamespace(org=org, project_time=project_time, record=record)


class FakeCall:
    def __init__(self, ident, data=None, batched=None):
        self.identity = ident
        self.data = data if data is not None else {}
        self.batched_data = batched
        self.requested_endpoint_version = PartialVersion("2.30")
        self.result = SimpleNamespace(data=None, data_model=None)


def make_root(company, owner="u1", members=()):
    Project(
        id="root1",
        company=company,
        user=owner,
        name="Project-root1",
        basename="Project-root1",
        created=datetime.utcnow(),
        visibility=ProjectVisibility.private,
        members=[ProjectMember(user=u, role=r, added=datetime.utcnow(), added_by=owner) for u, r in members],
    ).save()


def make_task(company, id_, owner="u1", **extra):
    fields = dict(
        id=id_,
        company=company,
        user=owner,
        project="root1",
        name=f"Task-{id_}",
        type="training",
        created=datetime(2020, 1, 1),
        status=TaskStatus.created,
    )
    fields.update(extra)
    Task(**fields).save()


def raw(task_id):
    return Task.objects(id=task_id).as_pymongo().first()


def run_update(company, data):
    call = FakeCall(Identity(user="u1", company=company, role=Role.user), data=data)
    return svc.update(call, company, UpdateRequest(task=data["task"]))


def run_batch(company, items):
    call = FakeCall(Identity(user="u1", company=company, role=Role.user), batched=items)
    svc.update_batch(call, company, None)
    return call.result.data


def test_batch_refuses_the_whole_batch_when_one_item_has_a_short_name(company, side_effects):
    make_root(company, members=[("u1", ProjectRole.read_write)])
    make_task(company, "task1")
    make_task(company, "task2")
    items = [{"task": "task1", "name": "renamed"}, {"task": "task2", "name": "ab"}]

    with pytest.raises(errors.bad_request.DataValidationError):
        run_batch(company, items)

    assert raw("task1")["name"] == "Task-task1"
    assert raw("task2")["name"] == "Task-task2"
    side_effects.project_time.assert_not_called()
    side_effects.record.assert_not_called()


def test_batch_refuses_a_short_name_with_the_error_update_gives(company, side_effects):
    make_root(company, members=[("u1", ProjectRole.read_write)])
    make_task(company, "task1")
    make_task(company, "task2")

    with pytest.raises(errors.bad_request.DataValidationError) as via_update:
        run_update(company, {"task": "task1", "name": "ab"})
    with pytest.raises(errors.bad_request.DataValidationError) as via_batch:
        run_batch(company, [{"task": "task2", "name": "ab"}])

    assert type(via_batch.value) is type(via_update.value)
    assert via_batch.value.msg == via_update.value.msg
    assert raw("task1")["name"] == "Task-task1"
    assert raw("task2")["name"] == "Task-task2"


def test_batch_stores_the_same_document_as_update_for_the_same_fields(company, side_effects):
    make_root(company, members=[("u1", ProjectRole.read_write)])
    make_task(company, "task1")
    make_task(company, "task2")
    fields = {
        "name": "  same  ",
        "display_name": "  Shown  ",
        "comment": "c",
        "tags": ["b", "a"],
        "output__error": "boom",
    }

    run_update(company, {"task": "task1", **fields})
    assert run_batch(company, [{"task": "task2", **fields}]) == {"updated": 1}

    via_update = {k: v for k, v in raw("task1").items() if k not in STAMPS}
    via_batch = {k: v for k, v in raw("task2").items() if k not in STAMPS}
    assert via_update == via_batch
    assert raw("task2")["name"] == "same"
    assert "output__error" not in raw("task2")


def test_batch_refuses_a_display_name_over_200_with_the_error_update_gives(company, side_effects):
    make_root(company, members=[("u1", ProjectRole.read_write)])
    make_task(company, "task1")

    with pytest.raises(errors.bad_request.DataValidationError):
        run_batch(company, [{"task": "task1", "display_name": "x" * 201}])

    assert raw("task1").get("display_name") is None
    side_effects.record.assert_not_called()


def test_batch_records_the_repository_of_each_written_task_like_update(company, side_effects):
    make_root(company, members=[("u1", ProjectRole.read_write)])
    make_task(company, "task1", script=Script(repository="r"))
    make_task(company, "task2", script=Script(repository="r"))

    assert run_batch(company, [{"task": "task1", "name": "one"}, {"task": "task2", "name": "two"}]) == {"updated": 2}

    recorded = sorted(c.args[2] for c in side_effects.record.call_args_list)
    assert recorded == ["task1", "task2"]
