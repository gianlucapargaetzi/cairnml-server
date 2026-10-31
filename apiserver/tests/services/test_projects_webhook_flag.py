from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from apiserver.apimodels.projects import ProjectRequest, ProjectsGetRequest
from apiserver.database.model.auth import Role
from apiserver.database.model.project import Project, ProjectVisibility
from apiserver.service_repo.auth import Identity
from apiserver.services import projects as svc
from apiserver.tests.db import ensure_db
from apiserver.utilities.partial_version import PartialVersion

ensure_db()

URL = "https://hooks.example.org/services/secret-token"


@pytest.fixture
def company():
    return f"c-{uuid4().hex[:8]}"


def make_project(company, id_, webhook=None):
    Project(
        id=id_,
        company=company,
        user="owner1",
        name=id_,
        basename=id_,
        created=datetime.utcnow(),
        visibility=ProjectVisibility.public,
        notification_webhook=webhook,
    ).save()


class FakeCall:
    def __init__(self, ident):
        self.identity = ident
        self.data = {}
        self.requested_endpoint_version = PartialVersion("2.39")
        self.result = SimpleNamespace(data=None, data_model=None)


def admin(company):
    return Identity(user="admin1", company=company, role=Role.admin)


def test_get_by_id_reports_a_set_webhook_without_its_address(company):
    project_id = f"hook-{uuid4().hex[:8]}"
    make_project(company, project_id, webhook=URL)
    call = FakeCall(admin(company))
    svc.get_by_id(call, company, ProjectRequest(project=project_id))
    project = call.result.data["project"]
    assert project["has_notification_webhook"] is True
    assert "notification_webhook" not in project
    assert URL not in repr(project)


def test_get_by_id_reports_no_webhook_when_none_is_set(company):
    project_id = f"nohook-{uuid4().hex[:8]}"
    make_project(company, project_id, webhook=None)
    call = FakeCall(admin(company))
    svc.get_by_id(call, company, ProjectRequest(project=project_id))
    assert call.result.data["project"]["has_notification_webhook"] is False


def test_get_all_ex_reports_the_flag_per_project_and_never_the_address(company):
    with_hook = f"yes-{uuid4().hex[:8]}"
    cleared = f"cleared-{uuid4().hex[:8]}"
    never = f"never-{uuid4().hex[:8]}"
    make_project(company, with_hook, webhook=URL)
    make_project(company, cleared, webhook="")
    make_project(company, never, webhook=None)
    call = FakeCall(admin(company))
    call.data = {"id": [with_hook, cleared, never]}

    svc.get_all_ex(call, company, ProjectsGetRequest())
    by_id = {p["id"]: p for p in call.result.data["projects"]}
    assert by_id[with_hook]["has_notification_webhook"] is True
    assert by_id[cleared]["has_notification_webhook"] is False
    assert by_id[never]["has_notification_webhook"] is False
    assert URL not in repr(call.result.data)
    assert all("notification_webhook" not in p for p in by_id.values())


def test_update_stores_the_webhook_and_an_empty_value_clears_it(company):
    project_id = f"upd-{uuid4().hex[:8]}"
    make_project(company, project_id, webhook=None)
    call = FakeCall(admin(company))
    call.data = {"project": project_id, "notification_webhook": URL}
    svc.update(call, company, ProjectRequest(project=project_id))
    assert call.result.data_model.fields["notification_webhook"] == URL
    assert Project.objects(id=project_id).only("notification_webhook").first().notification_webhook == URL

    call = FakeCall(admin(company))
    call.data = {"project": project_id, "notification_webhook": ""}
    svc.update(call, company, ProjectRequest(project=project_id))
    assert Project.objects(id=project_id).only("notification_webhook").first().notification_webhook is None
