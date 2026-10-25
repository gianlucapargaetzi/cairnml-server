# apiserver/tests/services/test_organization_access_enforcement.py
from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from apiserver.apimodels.organization import EntitiesCountRequest, GetProjectWorkloadsRequest
from apiserver.database.model.auth import Role
from apiserver.database.model.project import Project, ProjectVisibility
from apiserver.database.model.task.task import Task
from apiserver.service_repo.auth import Identity
from apiserver.utilities.partial_version import PartialVersion
from apiserver.services import organization as svc
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


def test_entities_count_hides_a_private_projects_tasks_from_a_stranger(company):
    make_root(company)
    make_task(company, "root1", "owner1")
    call = FakeCall(identity(company, "stranger"), data={"tasks": {}})
    svc.get_entities_count(call, company, EntitiesCountRequest(tasks={}))
    assert call.result.data["tasks"] == 0


def test_entities_count_counts_visible_tasks_for_a_member(company):
    make_root(company)
    make_task(company, "root1", "owner1")
    call = FakeCall(identity(company, "owner1"), data={"tasks": {}})
    svc.get_entities_count(call, company, EntitiesCountRequest(tasks={}))
    assert call.result.data["tasks"] == 1


def test_get_project_workloads_hides_a_private_project_from_a_stranger(company):
    # a hidden project's workloads must look exactly like a genuinely missing project's: compare
    # the two responses directly rather than asserting a fixed shape, since this aggregator has
    # no access awareness of its own and this endpoint routes a hidden id through its "missing"
    # path instead of inventing a separate response
    make_root(company)
    hidden = FakeCall(identity(company, "stranger"))
    svc.get_project_workloads(
        hidden,
        company,
        GetProjectWorkloadsRequest(projects=["root1"], from_date="2024-01-01", to_date="2024-01-02"),
    )
    missing = FakeCall(identity(company, "stranger"))
    svc.get_project_workloads(
        missing,
        company,
        GetProjectWorkloadsRequest(projects=["no-such-project"], from_date="2024-01-01", to_date="2024-01-02"),
    )
    assert hidden.result.data == missing.result.data
