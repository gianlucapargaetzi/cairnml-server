# apiserver/tests/services/test_tags_and_peek_access_enforcement.py
from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from apiserver.apimodels.organization import TagsRequest
from apiserver.apimodels.queues import QueueRequest
from apiserver.bll.organization import OrgBLL, Tags
from apiserver.database.model.auth import Role
from apiserver.database.model.project import Project, ProjectMember, ProjectRole, ProjectVisibility
from apiserver.database.model.queue import Entry, Queue
from apiserver.database.model.task.task import Task, TaskType
from apiserver.service_repo.auth import Identity
from apiserver.utilities.partial_version import PartialVersion
from apiserver.services import organization as org_svc
from apiserver.services import queues as queues_svc
from apiserver.services import reports as reports_svc
from apiserver.tests.db import ensure_db

ensure_db()

org_bll = OrgBLL()


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


def make_task(company, project, owner, id_="task1", task_type="training", tags=()):
    task = Task(
        id=id_, company=company, user=owner, name=id_, project=project, type=task_type,
        created=datetime.utcnow(), tags=list(tags),
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


def test_peek_task_reports_a_hidden_head_task_as_empty(company):
    make_root(company)
    make_task(company, "root1", "owner1")
    Queue(
        id="q1", company=company, name="queue-one", created=datetime.utcnow(),
        entries=[Entry(task="task1", added=datetime.utcnow())],
    ).save()
    result = queues_svc.peek_task(
        FakeCall(identity(company, "stranger")), company, QueueRequest(queue="q1")
    )
    assert result == {"task": None}


def test_peek_task_reports_the_head_task_to_a_member(company):
    make_root(company, members=[("u2", ProjectRole.read)])
    make_task(company, "root1", "owner1")
    Queue(
        id="q1", company=company, name="queue-one", created=datetime.utcnow(),
        entries=[Entry(task="task1", added=datetime.utcnow())],
    ).save()
    result = queues_svc.peek_task(
        FakeCall(identity(company, "u2")), company, QueueRequest(queue="q1")
    )
    assert result == {"task": "task1"}


def test_report_tags_are_not_listed_to_a_stranger(company):
    make_root(company)
    make_task(company, "root1", "owner1", id_="report1", task_type=TaskType.report, tags=["private-tag"])
    call = FakeCall(identity(company, "stranger"))
    reports_svc.get_tags(call, company, None)
    assert "private-tag" not in call.result.data["tags"]


def test_report_tags_are_listed_to_a_member(company):
    make_root(company, members=[("u2", ProjectRole.read)])
    make_task(company, "root1", "owner1", id_="report1", task_type=TaskType.report, tags=["private-tag"])
    call = FakeCall(identity(company, "u2"))
    reports_svc.get_tags(call, company, None)
    assert "private-tag" in call.result.data["tags"]


def test_org_tags_are_not_listed_to_a_stranger_and_are_to_a_member(company):
    make_root(company, members=[("u2", ProjectRole.read)])
    make_task(company, "root1", "owner1", tags=["secret-tag"])

    stranger = FakeCall(identity(company, "stranger"))
    org_svc.get_tags(stranger, company, TagsRequest())
    assert "secret-tag" not in stranger.result.data.get("tags", [])

    member = FakeCall(identity(company, "u2"))
    org_svc.get_tags(member, company, TagsRequest())
    assert "secret-tag" in member.result.data.get("tags", [])
