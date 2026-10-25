# apiserver/tests/services/test_events_access_enforcement.py
from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from apiserver.apierrors import errors
from apiserver.apimodels.events import GetMetricsAndVariantsRequest, ModelRequest
from apiserver.database.model.auth import Role
from apiserver.database.model.model import Model
from apiserver.database.model.project import Project, ProjectMember, ProjectRole, ProjectVisibility
from apiserver.database.model.task.task import Task
from apiserver.service_repo.auth import Identity
from apiserver.utilities.partial_version import PartialVersion
from apiserver.services import events as svc
from apiserver.tests.db import ensure_db

ensure_db()


@pytest.fixture
def company():
    return f"c-{uuid4().hex[:8]}"


def make_root(company, id_="root1", owner="owner1", visibility=ProjectVisibility.private, members=()):
    project = Project(
        id=id_,
        company=company,
        user=owner,
        name=id_,
        basename=id_,
        created=datetime.utcnow(),
        visibility=visibility,
        members=[ProjectMember(user=u, role=r, added=datetime.utcnow(), added_by=owner) for u, r in members],
    )
    project.save()
    return project


def make_task(company, project, owner, id_="task1"):
    task = Task(id=id_, company=company, user=owner, name=id_, project=project, type="training", created=datetime.utcnow())
    task.save()
    return task


def make_model(company, project, owner, id_="model1"):
    model = Model(id=id_, company=company, user=owner, name=id_, project=project, created=datetime.utcnow(), ready=False)
    model.save()
    return model


def identity(company, user, role=Role.user):
    return Identity(user=user, company=company, role=role)


class FakeCall:
    def __init__(self, ident, data=None):
        self.identity = ident
        self.data = data if data is not None else {}
        self.requested_endpoint_version = PartialVersion("2.30")
        self.worker = None
        self.result = SimpleNamespace(data=None, data_model=None)


def test_get_vector_metrics_refuses_a_task_in_a_private_project_to_a_stranger(company):
    make_root(company)
    make_task(company, "root1", "owner1")
    with pytest.raises(errors.bad_request.InvalidTaskId):
        svc.get_vector_metrics_and_variants(
            FakeCall(identity(company, "stranger")),
            company,
            GetMetricsAndVariantsRequest(task="task1"),
        )


def test_assert_task_or_model_exists_allows_a_member(company):
    make_root(company, members=[("u2", ProjectRole.read)])
    make_task(company, "root1", "owner1")
    result = svc._assert_task_or_model_exists(
        company, "task1", model_events=False, identity=identity(company, "u2")
    )
    assert result[0].id == "task1"


def test_add_event_refuses_a_task_in_a_private_project_to_a_stranger(company):
    # add_events never raises an access-specific error directly: a forbidden task is silently
    # excluded from the valid set, so with only one event requested, none of them get added
    make_root(company)
    make_task(company, "root1", "owner1")
    call = FakeCall(identity(company, "stranger"), data={"task": "task1", "metric": "m", "variant": "v", "type": "training_stats_scalar"})
    with pytest.raises(errors.bad_request.EventsNotAdded):
        svc.event_bll.add_events(
            company_id=company, identity=call.identity, events=[call.data], worker=None
        )


def test_delete_for_model_refuses_a_model_in_a_private_project_to_a_stranger(company):
    # the model exists (so it is not InvalidModelId), but the stranger has no role on its private
    # project: the same InvalidProjectId a hidden project gives everywhere else in this feature
    make_root(company)
    make_model(company, "root1", "owner1")
    with pytest.raises(errors.bad_request.InvalidProjectId):
        svc.delete_for_model(
            FakeCall(identity(company, "stranger")),
            company,
            ModelRequest(model="model1"),
        )
