# apiserver/tests/services/test_tasks_access_enforcement.py
from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from apiserver.apierrors import errors
from apiserver.apimodels.tasks import (
    DeleteManyRequest,
    DeleteRequest,
    EnqueueRequest,
    GetAllReq,
    StopManyRequest,
    TaskRequest,
    UpdateTagsRequest,
)
from apiserver.apimodels.base import MoveRequest
from apiserver.bll.project import ProjectBLL
from apiserver.bll.project.access import ProjectAccessBLL
from apiserver.database.model.auth import Role
from apiserver.database.model.project import Project, ProjectMember, ProjectRole, ProjectVisibility
from apiserver.database.model.task.task import Task, TaskStatus
from apiserver.service_repo.auth import Identity
from apiserver.utilities.partial_version import PartialVersion
from apiserver.services import tasks as svc
from apiserver.tests.db import ensure_db

ensure_db()


@pytest.fixture
def company():
    # Every test gets its own company so that rows from other tests never interfere
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
        members=[
            ProjectMember(user=u, role=r, added=datetime.utcnow(), added_by=owner)
            for u, r in members
        ],
    )
    project.save()
    return project


def make_task(company, project_id, owner, id_="task1", status=TaskStatus.created):
    task = Task(
        id=id_,
        company=company,
        user=owner,
        project=project_id,
        name=f"Task-{id_}",
        type="training",
        created=datetime.utcnow(),
        status=status,
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
        self.result = SimpleNamespace(data=None, data_model=None)


def test_read_write_owner_cannot_delete_someone_elses_task(company):
    make_root(company, members=[("u2", ProjectRole.read_write)])
    make_task(company, "root1", "owner1")
    with pytest.raises(errors.forbidden.NoWritePermission):
        svc.delete(
            FakeCall(identity(company, "u2")),
            company,
            DeleteRequest(task="task1", force=True),
        )
    assert Task.objects(id="task1").first() is not None


def test_read_write_owner_can_delete_their_own_task(company, monkeypatch):
    monkeypatch.setattr(svc, "_delete_task_events", lambda **kwargs: None)
    make_root(company, members=[("u2", ProjectRole.read_write)])
    make_task(company, "root1", "u2")
    svc.delete(
        FakeCall(identity(company, "u2")),
        company,
        DeleteRequest(task="task1", force=True, delete_external_artifacts=False),
    )
    assert Task.objects(id="task1").first() is None


def test_collaborator_can_update_tags_on_someone_elses_task(company):
    make_root(company, members=[("u2", ProjectRole.collaborator)])
    make_task(company, "root1", "owner1")
    svc.update_tags(
        FakeCall(identity(company, "u2")),
        company,
        UpdateTagsRequest(ids=["task1"], add_tags=["fresh"]),
    )
    assert "fresh" in Task.objects(id="task1").first().tags


def test_collaborator_cannot_delete_someone_elses_task(company):
    make_root(company, members=[("u2", ProjectRole.collaborator)])
    make_task(company, "root1", "owner1")
    with pytest.raises(errors.forbidden.NoWritePermission):
        svc.delete(
            FakeCall(identity(company, "u2")),
            company,
            DeleteRequest(task="task1", force=True),
        )
    assert Task.objects(id="task1").first() is not None


def test_a_read_role_cannot_enqueue_a_task(company):
    make_root(company, members=[("u2", ProjectRole.read)])
    make_task(company, "root1", "owner1")
    with pytest.raises(errors.forbidden.NoWritePermission):
        svc.enqueue(
            FakeCall(identity(company, "u2")),
            company,
            EnqueueRequest(task="task1", queue="default"),
        )
    assert Task.objects(id="task1").first().status == TaskStatus.created


def test_delete_many_is_all_or_nothing(company):
    make_root(company, members=[("u2", ProjectRole.read_write)])
    make_task(company, "root1", "u2", id_="own-task")
    make_task(company, "root1", "owner1", id_="other-task")
    with pytest.raises(errors.forbidden.NoWritePermission):
        svc.delete_many(
            FakeCall(identity(company, "u2")),
            company,
            DeleteManyRequest(ids=["own-task", "other-task"], force=True),
        )
    assert Task.objects(id="own-task").first() is not None
    assert Task.objects(id="other-task").first() is not None


def test_stop_many_is_all_or_nothing(company):
    make_root(company, members=[("u2", ProjectRole.read_write)])
    make_task(company, "root1", "u2", id_="own-task", status=TaskStatus.in_progress)
    make_task(company, "root1", "owner1", id_="other-task", status=TaskStatus.in_progress)
    with pytest.raises(errors.forbidden.NoWritePermission):
        svc.stop_many(
            FakeCall(identity(company, "u2")),
            company,
            StopManyRequest(ids=["own-task", "other-task"], force=True),
        )
    assert Task.objects(id="own-task").first().status == TaskStatus.in_progress


def test_admin_can_delete_anyones_task(company, monkeypatch):
    # The event cleanup needs Elasticsearch, which is not part of this test. The access check is what is under test
    monkeypatch.setattr(svc, "_delete_task_events", lambda **kwargs: None)
    make_root(company)
    make_task(company, "root1", "owner1")
    svc.delete(
        FakeCall(identity(company, "admin-user", role=Role.admin)),
        company,
        DeleteRequest(task="task1", force=True, delete_external_artifacts=False),
    )
    assert Task.objects(id="task1").first() is None


def test_get_by_id_refuses_a_task_in_a_private_project_to_a_stranger(company):
    make_root(company)
    make_task(company, "root1", "owner1")
    with pytest.raises(errors.bad_request.InvalidProjectId):
        svc.get_by_id(FakeCall(identity(company, "stranger")), company, TaskRequest(task="task1"))


def test_get_all_lists_only_tasks_in_visible_projects(company):
    make_root(company, id_="privroot")
    make_root(company, id_="pubroot", visibility=ProjectVisibility.public)
    make_task(company, "privroot", "owner1", id_="hidden-task")
    make_task(company, "pubroot", "owner1", id_="shown-task")
    call = FakeCall(identity(company, "stranger"))
    svc.get_all(call, company, None)
    ids = {t["id"] for t in call.result.data["tasks"]}
    assert "shown-task" in ids
    assert "hidden-task" not in ids


def test_get_all_for_a_caller_with_no_visible_project_returns_no_project_task(company):
    make_root(company, id_="privroot")
    make_task(company, "privroot", "owner1", id_="hidden-task")
    call = FakeCall(identity(company, "stranger"))
    svc.get_all(call, company, None)
    assert "hidden-task" not in {t["id"] for t in call.result.data["tasks"]}


def test_get_all_ex_hides_tasks_in_projects_the_caller_cannot_see(company):
    make_root(company, id_="privroot")
    make_task(company, "privroot", "owner1", id_="hidden-task")
    call = FakeCall(identity(company, "stranger"))
    svc.get_all_ex(call, company, GetAllReq())
    assert "hidden-task" not in {t["id"] for t in call.result.data["tasks"]}


def test_a_task_without_a_project_is_not_blocked_by_the_project_checks(company):
    # Tasks may have no project. Those keep the company-level behaviour they had before
    make_task(company, None, "owner1", id_="loose-task")
    call = FakeCall(identity(company, "stranger"))
    svc.get_by_id(call, company, TaskRequest(task="loose-task"))
    assert call.result.data["task"]["id"] == "loose-task"


def test_move_needs_admin_on_the_destination_project(company):
    make_root(company, id_="src", members=[("u2", ProjectRole.read_write)])
    make_root(company, id_="dst", members=[("u2", ProjectRole.read_write)])
    make_task(company, "src", "u2")
    with pytest.raises(errors.forbidden.NoWritePermission):
        svc.move(
            FakeCall(identity(company, "u2"), data={"project": "dst"}),
            company,
            MoveRequest(ids=["task1"], project="dst"),
        )
    assert Task.objects(id="task1").first().project == "src"


def test_move_needs_admin_on_the_current_project(company):
    make_root(company, id_="src", members=[("u2", ProjectRole.read_write)])
    make_root(company, id_="dst", members=[("u2", ProjectRole.collaborator)])
    make_task(company, "src", "u2")
    with pytest.raises(errors.forbidden.NoWritePermission):
        svc.move(
            FakeCall(identity(company, "u2"), data={"project": "dst"}),
            company,
            MoveRequest(ids=["task1"], project="dst"),
        )
    assert Task.objects(id="task1").first().project == "src"


def test_create_needs_write_on_the_project(company):
    make_root(company, members=[("u2", ProjectRole.read)])
    with pytest.raises(errors.forbidden.NoWritePermission):
        svc.create(
            FakeCall(
                identity(company, "u2"),
                data={"name": "new-task", "type": "training", "project": "root1"},
            ),
            company,
            None,
        )
    assert Task.objects(name="new-task", company=company).first() is None


def test_edit_cannot_move_a_task_into_a_project_the_caller_cannot_write(company):
    make_root(company, id_="src", members=[("u2", ProjectRole.read_write)])
    make_root(company, id_="dst", members=[("u2", ProjectRole.read)])
    make_task(company, "src", "u2")
    from apiserver.apimodels.tasks import UpdateRequest

    with pytest.raises(errors.forbidden.NoWritePermission):
        svc.edit(
            FakeCall(identity(company, "u2"), data={"task": "task1", "project": "dst"}),
            company,
            UpdateRequest(task="task1"),
        )
    assert Task.objects(id="task1").first().project == "src"


def test_visible_ids_for_a_stranger_do_not_include_the_private_project(company):
    make_root(company, id_="privroot")
    assert "privroot" not in ProjectAccessBLL.visible_project_ids(identity(company, "stranger"))


def test_edit_move_needs_admin_on_the_destination_not_just_write(company):
    # A read_write user has write on their own task in the destination, but not admin, so the
    # move through edit is refused. Before, it passed the write check and bypassed move's admin gate
    make_root(company, id_="src", members=[("u2", ProjectRole.read_write)])
    make_root(company, id_="dst", members=[("u2", ProjectRole.read_write)])
    make_task(company, "src", "u2")
    from apiserver.apimodels.tasks import UpdateRequest

    with pytest.raises(errors.forbidden.NoWritePermission):
        svc.edit(
            FakeCall(identity(company, "u2"), data={"task": "task1", "project": "dst"}),
            company,
            UpdateRequest(task="task1"),
        )
    assert Task.objects(id="task1").first().project == "src"


def test_update_move_needs_admin_on_the_destination_not_just_write(company):
    make_root(company, id_="src", members=[("u2", ProjectRole.read_write)])
    make_root(company, id_="dst", members=[("u2", ProjectRole.read_write)])
    make_task(company, "src", "u2")
    from apiserver.apimodels.tasks import UpdateRequest

    with pytest.raises(errors.forbidden.NoWritePermission):
        svc.update(
            FakeCall(identity(company, "u2"), data={"task": "task1", "project": "dst"}),
            company,
            UpdateRequest(task="task1"),
        )
    assert Task.objects(id="task1").first().project == "src"


def test_find_or_create_by_id_needs_write_on_the_project(company):
    make_root(company, id_="dst", members=[("u2", ProjectRole.read)])
    with pytest.raises(errors.forbidden.NoWritePermission):
        ProjectBLL.find_or_create(
            user="u2",
            company=company,
            identity=identity(company, "u2"),
            project_name=None,
            description="",
            project_id="dst",
        )


def test_find_or_create_by_name_refuses_a_read_only_project(company):
    make_root(company, id_="dst", members=[("u2", ProjectRole.read)])
    with pytest.raises(errors.forbidden.NoWritePermission):
        ProjectBLL.find_or_create(
            user="u2",
            company=company,
            identity=identity(company, "u2"),
            project_name="Project-dst",
            description="",
        )


def test_find_or_create_refuses_to_create_under_a_read_only_parent(company):
    make_root(company, id_="dst", members=[("u2", ProjectRole.read)])
    with pytest.raises(errors.forbidden.NoWritePermission):
        ProjectBLL.find_or_create(
            user="u2",
            company=company,
            identity=identity(company, "u2"),
            project_name="Project-dst/new-child",
            description="",
        )
    assert Project.objects(company=company, name="Project-dst/new-child").first() is None


def test_find_or_create_creates_under_a_writable_parent(company):
    make_root(company, id_="dst", members=[("u2", ProjectRole.read_write)])
    found = ProjectBLL.find_or_create(
        user="u2",
        company=company,
        identity=identity(company, "u2"),
        project_name="Project-dst/new-child",
        description="",
    )
    assert Project.objects(id=found, company=company).first() is not None


def test_clone_into_a_read_only_project_by_name_is_refused(company):
    make_root(company, id_="src", members=[("u2", ProjectRole.read_write)])
    make_root(company, id_="dst", members=[("u2", ProjectRole.read)])
    make_task(company, "src", "u2")
    from apiserver.apimodels.tasks import CloneRequest

    with pytest.raises(errors.forbidden.NoWritePermission):
        svc.clone_task(
            FakeCall(identity(company, "u2")),
            company,
            CloneRequest(task="task1", new_project_name="Project-dst/child"),
        )
    assert Project.objects(company=company, name="Project-dst/child").first() is None
