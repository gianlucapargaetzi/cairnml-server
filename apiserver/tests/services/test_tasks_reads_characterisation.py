# apiserver/tests/services/test_tasks_reads_characterisation.py
"""Characterisation tests for the task read endpoints: get_by_id, get_by_id_ex, get_all, get_all_ex and get_types.

They record what the endpoints return and refuse today, including the odd behaviour, so that the read path cannot change
silently. "Hidden" is used in two senses: a task in a private project the caller has no role in (a visibility rule),
and the system tag "hidden" (a listing filter). Behaviour that looks unintended is pinned as it is and marked with the
comment below (decision issue #282).
"""

from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from apiserver.apierrors import errors
from apiserver.apimodels.tasks import GetAllReq, GetTypesRequest, TaskRequest
from apiserver.database.model.auth import Role
from apiserver.database.model.project import Project, ProjectMember, ProjectVisibility
from apiserver.database.model.task.task import Task
from apiserver.service_repo import ServiceRepo
from apiserver.service_repo.auth import Identity
from apiserver.service_repo.validators import validate_required_fields
from apiserver.services import tasks as svc
from apiserver.tests.db import ensure_db
from apiserver.utilities.partial_version import PartialVersion

ensure_db()


@pytest.fixture
def company():
    return f"c-{uuid4().hex[:8]}"


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


def make_task(company, project, owner, id_="task1", system_tags=()):
    task = Task(
        id=id_,
        company=company,
        user=owner,
        name=f"Task-{id_}",
        project=project,
        type="training",
        created=datetime.utcnow(),
        system_tags=list(system_tags),
    )
    task.save()
    return task


def identity(company, user, role=Role.user):
    return Identity(user=user, company=company, role=role)


class FakeCall:
    def __init__(self, ident, data=None):
        self.identity = ident
        self.data = data if data is not None else {}
        self.batched_data = None
        self.requested_endpoint_version = PartialVersion("2.30")
        self.result = SimpleNamespace(data=None, data_model=None)


def ids_of(call) -> set:
    return {t["id"] for t in call.result.data["tasks"]}


def test_get_by_id_returns_the_task_to_its_owner(company):
    # R1
    make_root(company)
    make_task(company, "root1", "owner1")
    call = FakeCall(identity(company, "owner1"))
    svc.get_by_id(call, company, TaskRequest(task="task1"))
    assert call.result.data["task"]["id"] == "task1"


def test_get_by_id_hidden_and_missing_raise_the_same_error(company):
    # R2: the scope of the lookup makes a task in a hidden project indistinguishable from an unknown id
    make_root(company)
    make_task(company, "root1", "owner1")
    stranger = identity(company, "stranger")

    with pytest.raises(errors.bad_request.InvalidTaskId) as hidden:
        svc.get_by_id(FakeCall(stranger), company, TaskRequest(task="task1"))
    with pytest.raises(errors.bad_request.InvalidTaskId) as missing:
        svc.get_by_id(FakeCall(stranger), company, TaskRequest(task="no-such-task"))

    assert hidden.value.code == missing.value.code
    assert hidden.value.subcode == missing.value.subcode
    assert hidden.value.msg.replace("task1", "<id>") == missing.value.msg.replace("no-such-task", "<id>")
    assert hidden.value.error_data == {"id": "task1"}
    assert missing.value.error_data == {"id": "no-such-task"}


def test_get_by_id_returns_a_hidden_tag_task(company):
    # the system tag "hidden" is a listing filter only
    # suspect: pinned as is, see the decision issue (#282)
    make_root(company, visibility=ProjectVisibility.public)
    make_task(company, "root1", "owner1", system_tags=["hidden"])
    call = FakeCall(identity(company, "owner1"))
    svc.get_by_id(call, company, TaskRequest(task="task1"))
    assert call.result.data["task"]["id"] == "task1"


def test_get_all_ex_without_id_excludes_hidden_tag_tasks(company):
    # R4
    make_root(company, visibility=ProjectVisibility.public)
    make_task(company, "root1", "owner1", id_="plain-task")
    make_task(company, "root1", "owner1", id_="hidden-task", system_tags=["hidden"])
    call = FakeCall(identity(company, "owner1"))
    svc.get_all_ex(call, company, GetAllReq())
    assert ids_of(call) == {"plain-task"}


def test_get_all_ex_with_search_hidden_includes_hidden_tag_tasks(company):
    # R5
    make_root(company, visibility=ProjectVisibility.public)
    make_task(company, "root1", "owner1", id_="plain-task")
    make_task(company, "root1", "owner1", id_="hidden-task", system_tags=["hidden"])
    call = FakeCall(identity(company, "owner1"), data={"search_hidden": True})
    svc.get_all_ex(call, company, GetAllReq(search_hidden=True))
    assert ids_of(call) == {"plain-task", "hidden-task"}


def test_get_all_ex_with_id_filter_returns_hidden_tag_task(company):
    # an id filter switches the hidden-tag filter off
    # suspect: pinned as is, see the decision issue (#282)
    make_root(company, visibility=ProjectVisibility.public)
    make_task(company, "root1", "owner1", id_="hidden-task", system_tags=["hidden"])
    call = FakeCall(identity(company, "owner1"), data={"id": ["hidden-task"]})
    svc.get_all_ex(call, company, GetAllReq())
    assert ids_of(call) == {"hidden-task"}


def test_get_all_ex_hides_a_private_project_from_a_stranger(company):
    # R7
    make_root(company, id_="privroot")
    make_task(company, "privroot", "owner1", id_="hidden-task")
    call = FakeCall(identity(company, "stranger"))
    svc.get_all_ex(call, company, GetAllReq())
    assert "hidden-task" not in ids_of(call)


def test_get_all_admin_sees_every_project(company):
    # R8
    make_root(company, id_="privroot")
    make_task(company, "privroot", "owner1", id_="private-task")
    call = FakeCall(identity(company, "admin-user", role=Role.admin))
    svc.get_all(call, company, None)
    assert "private-task" in ids_of(call)


def test_get_all_for_a_stranger_returns_projectless_tasks_only(company):
    # R9
    make_root(company, id_="privroot")
    make_task(company, None, "owner1", id_="projectless-task")
    make_task(company, "privroot", "owner1", id_="private-task")
    call = FakeCall(identity(company, "stranger"))
    svc.get_all(call, company, None)
    assert ids_of(call) == {"projectless-task"}


def test_get_by_id_ex_hides_private_project_as_missing(company):
    # R10
    make_root(company)
    make_task(company, "root1", "owner1")
    call = FakeCall(identity(company, "stranger"), data={"id": "task1"})
    svc.get_by_id_ex(call, company, None)
    assert call.result.data["tasks"] == []


def test_get_by_id_ex_missing_id_is_empty_list(company):
    # get_by_id raises InvalidTaskId for the same situation, get_by_id_ex answers with an empty list
    # suspect: pinned as is, see the decision issue (#282)
    call = FakeCall(identity(company, "owner1"), data={"id": "no-such-task"})
    svc.get_by_id_ex(call, company, None)
    assert call.result.data["tasks"] == []


def test_get_by_id_ex_without_id_is_refused_by_the_dispatcher(company):
    # R12: the required_fields check runs in the dispatcher (service_repo.validators), not inside the handler.
    # Calling the handler directly returns the visible tasks, so the refusal is pinned on the registered endpoint
    endpoint = ServiceRepo._get_endpoint("tasks.get_by_id_ex", PartialVersion("2.30"))
    assert endpoint.required_fields == ["id"]
    with pytest.raises(errors.bad_request.MissingRequiredFields) as refused:
        validate_required_fields(endpoint, FakeCall(identity(company, "owner1"), data={}))
    assert refused.value.code == 400
    assert refused.value.error_data == {"missing": ["id"]}


def test_get_types_without_projects_for_a_stranger_with_no_visible_project_is_empty(company):
    # R13
    make_root(company)
    make_task(company, "root1", "owner1")
    call = FakeCall(identity(company, "stranger"))
    svc.get_types(call, company, GetTypesRequest())
    assert call.result.data == {"types": []}


def test_get_types_with_a_hidden_project_gives_invalid_project_id(company):
    # R14
    make_root(company)
    make_task(company, "root1", "owner1")
    with pytest.raises(errors.bad_request.InvalidProjectId) as hidden:
        svc.get_types(FakeCall(identity(company, "stranger")), company, GetTypesRequest(projects=["root1"]))
    assert hidden.value.code == 400
    assert hidden.value.subcode == 401


def test_get_types_with_a_missing_project_gives_the_same_error(company):
    # R15
    make_root(company)
    make_task(company, "root1", "owner1")
    with pytest.raises(errors.bad_request.InvalidProjectId) as hidden:
        svc.get_types(FakeCall(identity(company, "stranger")), company, GetTypesRequest(projects=["root1"]))
    with pytest.raises(errors.bad_request.InvalidProjectId) as missing:
        svc.get_types(FakeCall(identity(company, "stranger")), company, GetTypesRequest(projects=["nope"]))
    assert hidden.value.code == missing.value.code
    assert hidden.value.subcode == missing.value.subcode
    assert hidden.value.msg.replace("root1", "<id>") == missing.value.msg.replace("nope", "<id>")


def test_get_types_returns_types_of_visible_projects(company):
    # R16
    make_root(company, members=[])
    make_task(company, "root1", "owner1")
    call = FakeCall(identity(company, "owner1"))
    svc.get_types(call, company, GetTypesRequest(projects=[]))
    assert "training" in call.result.data["types"]
