# apiserver/tests/services/test_reports_access_enforcement.py
from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from apiserver.apierrors import errors
from apiserver.apimodels.reports import CreateReportRequest, MoveReportRequest
from apiserver.database.model.auth import Role
from apiserver.database.model.project import Project, ProjectMember, ProjectRole, ProjectVisibility
from apiserver.database.model.task.task import Task, TaskType
from apiserver.service_repo.auth import Identity
from apiserver.utilities.partial_version import PartialVersion
from apiserver.services import reports as svc
from apiserver.tests.db import ensure_db

ensure_db()


@pytest.fixture
def company():
    return f"c-{uuid4().hex[:8]}"


def make_root(company, id_="root1", owner="owner1", visibility=ProjectVisibility.private, members=()):
    project = Project(
        id=id_, company=company, user=owner, name=id_, basename=id_,
        created=datetime.utcnow(), visibility=visibility,
        members=[ProjectMember(user=u, role=r, added=datetime.utcnow(), added_by=owner) for u, r in members],
    )
    project.save()
    return project


def make_report(company, project, owner, id_="report1"):
    task = Task(
        id=id_, company=company, user=owner, name="report", project=project,
        type=TaskType.report, created=datetime.utcnow(),
    )
    task.save()
    return task


def identity(company, user, role=Role.user):
    return Identity(user=user, company=company, role=role)


class FakeCall:
    def __init__(self, ident, data=None):
        self.identity = ident
        self.data = data if data is not None else {}
        self.requested_endpoint_version = PartialVersion("2.30")
        self.authorization = None
        self.result = SimpleNamespace(data=None, data_model=None)


def test_create_report_refuses_a_hidden_project_before_reading_its_name(company):
    make_root(company, id_="root1")
    with pytest.raises(errors.bad_request.InvalidProjectId):
        svc.create_report(
            FakeCall(identity(company, "stranger")),
            company,
            CreateReportRequest(name="my-report", project="root1"),
        )


def test_create_report_refuses_a_missing_project_the_same_way(company):
    with pytest.raises(errors.bad_request.InvalidProjectId):
        svc.create_report(
            FakeCall(identity(company, "stranger")),
            company,
            CreateReportRequest(name="my-report", project="no-such-project"),
        )


def test_move_report_refuses_a_collaborator_moving_someone_elses_report(company):
    # moving a report is admin on where it leaves and where it lands, mirroring tasks.move: a
    # read_write owner of the report (write, but not admin on its project) may not move it
    make_root(company, id_="src", members=[("u2", ProjectRole.read_write)])
    make_root(company, id_="dst", members=[("u2", ProjectRole.collaborator)])
    make_report(company, "src", "u2")
    with pytest.raises(errors.forbidden.NoWritePermission):
        svc.move(
            FakeCall(identity(company, "u2"), data={"project": "dst"}),
            company,
            MoveReportRequest(task="report1", project="dst"),
        )


def test_move_report_refuses_a_hidden_destination_project(company):
    make_root(company, id_="src", members=[("u2", ProjectRole.collaborator)])
    make_root(company, id_="dst")
    make_report(company, "src", "u2")
    with pytest.raises(errors.bad_request.InvalidProjectId):
        svc.move(
            FakeCall(identity(company, "u2"), data={"project": "dst"}),
            company,
            MoveReportRequest(task="report1", project="dst"),
        )


def test_move_report_allows_a_collaborator_who_administers_both_projects(company):
    make_root(company, id_="src", members=[("u2", ProjectRole.collaborator)])
    make_root(company, id_="dst", members=[("u2", ProjectRole.collaborator)])
    make_report(company, "src", "u2")
    svc.move(
        FakeCall(identity(company, "u2"), data={"project": "dst"}),
        company,
        MoveReportRequest(task="report1", project="dst"),
    )
    landed = Project.objects(id=Task.objects(id="report1").first().project).first()
    assert landed.parent == "dst"
