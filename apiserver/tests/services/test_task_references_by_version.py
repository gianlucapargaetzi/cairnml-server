"""What the task endpoints return for a task's project, parent and model, at the versions before and after 2.41.

The API change check reports that these fields are declared as the ID or the object where the previous release declared
the ID. The server did not change: the expected values below are what the tag pre-2.41 returned for the same task (the
same test, run on a copy of that tag, printed them). tasks.get_by_id returns the IDs, the endpoints that join the
referenced documents return objects, with the fields the request projects.
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from apiserver.apimodels.tasks import GetAllReq, TaskRequest
from apiserver.database.model.auth import Role
from apiserver.database.model.model import Model
from apiserver.database.model.project import Project, ProjectVisibility
from apiserver.database.model.task.task import ModelItem, Models, Task
from apiserver.service_repo.auth import Identity
from apiserver.services import tasks as tasks_svc
from apiserver.tests.db import ensure_db
from apiserver.utilities.partial_version import PartialVersion

ensure_db()

PROJECTED = ["project.name", "parent.name", "models.input.model.name", "models.input.model.framework"]


class FakeCall:
    def __init__(self, identity, version, data):
        self.identity = identity
        self.data = data
        self.auth = None
        self.requested_endpoint_version = PartialVersion(version)
        self.actual_endpoint_version = PartialVersion(version)
        self.result = SimpleNamespace(data=None, data_model=None)


@pytest.fixture
def stored():
    company = f"c-{uuid4().hex[:8]}"
    ids = SimpleNamespace(
        company=company,
        project=f"p-{uuid4().hex[:8]}",
        model=f"m-{uuid4().hex[:8]}",
        parent=f"t-{uuid4().hex[:8]}",
        task=f"t-{uuid4().hex[:8]}",
    )
    now = datetime.now(UTC).replace(tzinfo=None)
    Project(
        id=ids.project,
        company=company,
        user="u",
        name="proj",
        basename="proj",
        created=now,
        visibility=ProjectVisibility.private,
    ).save()
    Model(
        id=ids.model, company=company, user="u", name="mod", created=now, uri="s3://x", framework="PyTorch", ready=True
    ).save()
    common = {
        "company": company,
        "user": "u",
        "type": "training",
        "status": "created",
        "created": now,
        "last_update": now,
    }
    Task(id=ids.parent, name="par", **common).save()
    Task(
        id=ids.task,
        name="child",
        project=ids.project,
        parent=ids.parent,
        models=Models(input=[ModelItem(name="in", model=ids.model)]),
        **common,
    ).save()
    yield ids
    Task.objects(id__in=[ids.task, ids.parent]).delete()
    Project.objects(id=ids.project).delete()
    Model.objects(id=ids.model).delete()


def identity(company):
    return Identity(user="u", company=company, role=Role.admin)


def references(task):
    return {"project": task["project"], "parent": task["parent"], "model": task["models"]["input"][0]["model"]}


def result_of(call, returned):
    return returned if returned is not None else call.result.data


@pytest.mark.parametrize("version", ["2.1", "2.13", "2.41"])
def test_get_by_id_returns_the_ids(stored, version):
    call = FakeCall(identity(stored.company), version, {"task": stored.task})
    task = result_of(call, tasks_svc.get_by_id(call, stored.company, TaskRequest(task=stored.task)))["task"]
    assert references(task) == {"project": stored.project, "parent": stored.parent, "model": stored.model}


@pytest.mark.parametrize("endpoint,request_model", [("get_by_id_ex", None), ("get_all_ex", GetAllReq())])
@pytest.mark.parametrize("version", ["2.11", "2.14", "2.41"])
def test_the_joining_endpoints_return_objects_with_the_id_when_nothing_is_projected(
    stored, endpoint, request_model, version
):
    call = FakeCall(identity(stored.company), version, {"id": [stored.task]})
    task = result_of(call, getattr(tasks_svc, endpoint)(call, stored.company, request_model))["tasks"][0]
    assert references(task) == {
        "project": {"id": stored.project},
        "parent": {"id": stored.parent},
        "model": {"id": stored.model},
    }


@pytest.mark.parametrize("endpoint,request_model", [("get_by_id_ex", None), ("get_all_ex", GetAllReq())])
@pytest.mark.parametrize("version", ["2.11", "2.14", "2.41"])
def test_the_joining_endpoints_return_the_fields_that_are_projected(stored, endpoint, request_model, version):
    call = FakeCall(identity(stored.company), version, {"id": [stored.task], "only_fields": PROJECTED})
    task = result_of(call, getattr(tasks_svc, endpoint)(call, stored.company, request_model))["tasks"][0]
    assert references(task) == {
        "project": {"id": stored.project, "name": "proj"},
        "parent": {"id": stored.parent, "name": "par"},
        "model": {"id": stored.model, "name": "mod", "framework": "PyTorch"},
    }
