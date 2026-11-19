# apiserver/tests/services/test_task_access_characterisation_270.py
"""
Characterization tests for the task access guards and the project-change bookkeeping (issue 270, tracking 267).

They record what the server does today, including behaviour that looks unintended. Nothing here changes production
code. Behaviour that looks wrong is pinned as it is and marked with
    # suspect: pinned as is, see the decision issue (#282)
and listed in the pull request under "found, not changed".

Index of the guard matrix (issue 270, section 4.2). Rows marked "existing" are already asserted by the test named
here and are not repeated:

| row | subject                                              | test                                                                  |
|-----|------------------------------------------------------|-----------------------------------------------------------------------|
| 1   | project owner writes a task                          | test_row01_project_owner_can_write_a_task_in_their_project            |
| 2   | company admin deletes someone else's task            | existing: test_tasks_access_enforcement.py::test_admin_can_delete_anyones_task |
| 3   | read_write owner deletes own task                    | existing: test_tasks_access_enforcement.py::test_read_write_owner_can_delete_their_own_task |
| 4   | read_write non-owner deletes other's task            | existing: test_tasks_access_enforcement.py::test_read_write_owner_cannot_delete_someone_elses_task |
| 5   | read_write non-owner writes other's task             | test_row05_read_write_non_owner_cannot_update_someone_elses_task      |
| 6   | collaborator writes other's task tags                | existing: test_tasks_access_enforcement.py::test_collaborator_can_update_tags_on_someone_elses_task |
| 7   | collaborator deletes other's task                    | existing: test_tasks_access_enforcement.py::test_collaborator_cannot_delete_someone_elses_task |
| 8   | read member enqueues                                 | existing: test_tasks_access_enforcement.py::test_a_read_role_cannot_enqueue_a_task |
| 9   | read member writes                                   | test_row09_read_member_cannot_update_a_task                           |
| 10  | stranger, private project, get_by_id                 | existing: test_tasks_access_enforcement.py::test_get_by_id_refuses_a_task_in_a_private_project_to_a_stranger |
| 11  | stranger, private project, delete                    | test_row11_stranger_gets_invalid_task_id_on_delete                    |
| 12  | stranger, private project, archive/move/update_tags  | test_row12_* (suspect: 403 for hidden, 400 for missing)               |
| 13  | hidden vs missing, get_task_with_write_access        | existing: bll/task/test_get_task_with_write_access.py::test_a_hidden_task_gets_the_same_answer_as_a_missing_one |
| 14  | get_many_tasks_for_writing, throw_on_forbidden=False | existing: bll/task/test_get_task_with_write_access.py                 |
| 15  | get_many_tasks_for_writing, throw_on_forbidden=True  | existing: bll/task/test_get_task_with_write_access.py                 |
| 16  | other company, same task id                          | test_row16_other_company_gets_invalid_task_id                         |
| 17  | other company, company-less (public) project, read   | test_row17_other_company_reads_a_public_task_in_a_public_project      |
| 18  | public task, write                                   | test_row18_public_task_cannot_be_written                              |
| 19  | public task in a project hidden to the caller, write | test_row19_public_task_in_a_hidden_project_answers_like_a_missing_one |
| 20  | projectless task, company member writes              | test_row20_projectless_task_is_writable_by_any_company_member         |
| 21  | collaborator moves between two administered projects | existing: test_tasks_access_enforcement.py::test_a_collaborator_can_move_a_task_between_two_projects_they_administer |
| 22  | read_write owner moves between projects              | existing: test_tasks_access_enforcement.py::test_move_needs_admin_on_the_destination_project |
| 23  | read_write owner clears project through update/edit  | existing: test_tasks_access_enforcement.py::test_clearing_the_project_through_update_or_edit_is_refused_before_any_change |
| 24  | _assert_destination_project clear, helper            | existing: test_tasks_access_enforcement.py::test_clearing_the_project_helper_needs_admin_on_the_current_project_for_tasks |
| 25  | read member on a public project, get_by_id           | test_row25_read_member_of_a_public_project_can_read_a_task            |
| 26  | read member on a public project, write               | test_row26_read_member_of_a_public_project_cannot_write               |
| 27  | models: hidden model get_by_id                       | existing: test_models_access_enforcement.py (get_by_id on hidden model) |
| 28  | models: read_write non-owner edits                   | existing: test_models_access_enforcement.py                           |
| 29  | models: collaborator edits                           | existing: test_models_access_enforcement.py                           |
| 30  | read_write owner, update moves to unadministered project | existing: test_tasks_access_enforcement.py::test_update_move_needs_admin_on_the_destination_not_just_write |

Section 4.3 (proposed tests): archive, move and update_tags for a stranger are rows 12; update by a non-owner read_write
is row 5; read member write is row 9; other company is row 16; public task write is row 18; project owner is row 1;
read-only public project write is row 26.

Section 2.2 and 2.3 (project-change bookkeeping): the bookkeeping tests at the end of this file spy on the tag-cache,
project-time and repository calls of create, update, edit, update_batch, move and delete, and assert which projects each
one is called with.
"""

from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from apiserver.apierrors import errors
from apiserver.apimodels.base import MoveRequest
from apiserver.apimodels.tasks import (
    ArchiveRequest,
    CreateRequest,
    DeleteManyRequest,
    DeleteRequest,
    TaskRequest,
    UpdateRequest,
    UpdateTagsRequest,
)
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
        members=[ProjectMember(user=u, role=r, added=datetime.utcnow(), added_by=owner) for u, r in members],
    )
    project.save()
    return project


def make_task(company, project_id, owner, id_="task1", status=TaskStatus.created, task_company=None):
    task = Task(
        id=id_,
        company=company if task_company is None else task_company,
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
    def __init__(self, ident, data=None, batched=None):
        self.identity = ident
        self.data = data if data is not None else {}
        self.batched_data = batched
        self.requested_endpoint_version = PartialVersion("2.30")
        self.result = SimpleNamespace(data=None, data_model=None)


def task_name(id_="task1"):
    return f"Task-{id_}"


# ---------------------------------------------------------------------------------------------------------------------
# Section 4.2: the guard matrix
# ---------------------------------------------------------------------------------------------------------------------


def test_row01_project_owner_can_write_a_task_in_their_project(company):
    make_root(company, owner="owner1")
    make_task(company, "root1", "owner1")
    svc.update(
        FakeCall(identity(company, "owner1"), data={"task": "task1", "name": "renamed"}),
        company,
        UpdateRequest(task="task1"),
    )
    assert Task.objects(id="task1").first().name == "renamed"


def test_row05_read_write_non_owner_cannot_update_someone_elses_task(company):
    make_root(company, members=[("u2", ProjectRole.read_write)])
    make_task(company, "root1", "owner1")
    with pytest.raises(errors.forbidden.NoWritePermission):
        svc.update(
            FakeCall(identity(company, "u2"), data={"task": "task1", "name": "renamed"}),
            company,
            UpdateRequest(task="task1"),
        )
    assert Task.objects(id="task1").first().name == task_name()


def test_row09_read_member_cannot_update_a_task(company):
    make_root(company, members=[("u2", ProjectRole.read)])
    make_task(company, "root1", "owner1")
    with pytest.raises(errors.forbidden.NoWritePermission):
        svc.update(
            FakeCall(identity(company, "u2"), data={"task": "task1", "name": "renamed"}),
            company,
            UpdateRequest(task="task1"),
        )
    assert Task.objects(id="task1").first().name == task_name()


def test_row11_stranger_gets_invalid_task_id_on_delete(company):
    make_root(company)
    make_task(company, "root1", "owner1")
    with pytest.raises(errors.bad_request.InvalidTaskId):
        svc.delete(
            FakeCall(identity(company, "stranger")),
            company,
            DeleteRequest(task="task1", force=True),
        )
    assert Task.objects(id="task1").first() is not None


# suspect: pinned as is, see the decision issue (#282)
def test_row12_archive_answers_a_hidden_task_403_and_a_missing_one_400(company):
    # The writable-list guard runs before the per-task guard, so a hidden task is reported as forbidden
    # (403), while a missing one is reported as an invalid id (400). The rule at services/tasks.py:198 says both
    # must look the same. Pinned as it is today, see the decision issue (#282).
    make_root(company)
    make_task(company, "root1", "owner1")
    with pytest.raises(errors.forbidden.NoWritePermission):
        svc.archive(FakeCall(identity(company, "stranger")), company, ArchiveRequest(tasks=["task1"]))
    with pytest.raises(errors.bad_request.InvalidTaskId):
        svc.archive(FakeCall(identity(company, "stranger")), company, ArchiveRequest(tasks=["no-such-task"]))
    assert Task.objects(id="task1").first().status == TaskStatus.created


# suspect: pinned as is, see the decision issue (#282)
def test_row12_move_answers_a_hidden_task_403_and_a_missing_one_400(company):
    make_root(company, id_="src")
    make_root(company, id_="dst")
    make_task(company, "src", "owner1")
    with pytest.raises(errors.forbidden.NoWritePermission):
        svc.move(
            FakeCall(identity(company, "stranger"), data={"project": "dst"}),
            company,
            MoveRequest(ids=["task1"], project="dst"),
        )
    with pytest.raises(errors.bad_request.InvalidTaskId):
        svc.move(
            FakeCall(identity(company, "stranger"), data={"project": "dst"}),
            company,
            MoveRequest(ids=["no-such-task"], project="dst"),
        )
    assert Task.objects(id="task1").first().project == "src"


# suspect: pinned as is, see the decision issue (#282)
def test_row12_update_tags_answers_a_hidden_task_403_and_a_missing_one_400(company):
    make_root(company)
    make_task(company, "root1", "owner1")
    with pytest.raises(errors.forbidden.NoWritePermission):
        svc.update_tags(
            FakeCall(identity(company, "stranger")),
            company,
            UpdateTagsRequest(ids=["task1"], add_tags=["fresh"]),
        )
    with pytest.raises(errors.bad_request.InvalidTaskId):
        svc.update_tags(
            FakeCall(identity(company, "stranger")),
            company,
            UpdateTagsRequest(ids=["no-such-task"], add_tags=["fresh"]),
        )
    assert "fresh" not in Task.objects(id="task1").first().tags


@pytest.mark.parametrize(
    "call_endpoint",
    [
        pytest.param(
            lambda c, co: svc.update(
                FakeCall(identity(co, "u-other"), data={"task": "task1", "name": "renamed"}),
                co,
                UpdateRequest(task="task1"),
            ),
            id="update",
        ),
        pytest.param(
            lambda c, co: svc.delete(
                FakeCall(identity(co, "u-other")),
                co,
                DeleteRequest(task="task1", force=True),
            ),
            id="delete",
        ),
        pytest.param(
            lambda c, co: svc.archive(
                FakeCall(identity(co, "u-other")),
                co,
                ArchiveRequest(tasks=["task1"]),
            ),
            id="archive",
        ),
        pytest.param(
            lambda c, co: svc.update_tags(
                FakeCall(identity(co, "u-other")),
                co,
                UpdateTagsRequest(ids=["task1"], add_tags=["fresh"]),
            ),
            id="update_tags",
        ),
        pytest.param(
            lambda c, co: svc.get_by_id(
                FakeCall(identity(co, "u-other")),
                co,
                TaskRequest(task="task1"),
            ),
            id="get_by_id",
        ),
    ],
)
def test_row16_other_company_gets_invalid_task_id(company, call_endpoint):
    # The task belongs to `company`. A caller from another company asks for the same id: every lookup is scoped
    # to the caller's company, so the answer is the one for an unknown id
    other = f"{company}-other"
    make_root(company, members=[("u-other", ProjectRole.collaborator)])
    make_task(company, "root1", "owner1")
    with pytest.raises(errors.bad_request.InvalidTaskId):
        call_endpoint(None, other)
    assert Task.objects(id="task1").first() is not None


def test_row17_other_company_reads_a_public_task_in_a_public_project(company):
    # Public projects (company-less) and public tasks (company-less) are visible to every company
    other = f"{company}-other"
    # A company-less project and a company-less task: both are public, so every company may see them
    make_root("", id_="pubroot", visibility=ProjectVisibility.public)
    make_task(company, "pubroot", "owner1", task_company="")
    call = FakeCall(identity(other, "u-other"))
    svc.get_by_id(call, other, TaskRequest(task="task1"))
    assert call.result.data["task"]["id"] == "task1"


def test_row18_public_task_cannot_be_written(company):
    # A public task can be seen by every company but not changed: the write goes through get_for_writing,
    # which refuses company-less tasks with 403 (after the project check has passed for a collaborator)
    make_root(company, members=[("u2", ProjectRole.collaborator)])
    make_task(company, "root1", "owner1", task_company="")
    with pytest.raises(errors.forbidden.NoWritePermission):
        svc.update(
            FakeCall(identity(company, "u2"), data={"task": "task1", "name": "renamed"}),
            company,
            UpdateRequest(task="task1"),
        )
    assert Task.objects(id="task1").first().name == task_name()


def test_row19_public_task_in_a_hidden_project_answers_like_a_missing_one(company):
    # The first guard passes public tasks without a company check, then the project check hides the task:
    # the answer is InvalidTaskId, the one a missing id gets
    make_root(company)
    make_task(company, "root1", "owner1", task_company="")
    with pytest.raises(errors.bad_request.InvalidTaskId):
        svc.update(
            FakeCall(identity(company, "stranger"), data={"task": "task1", "name": "renamed"}),
            company,
            UpdateRequest(task="task1"),
        )
    with pytest.raises(errors.bad_request.InvalidTaskId):
        svc.update(
            FakeCall(identity(company, "stranger"), data={"task": "no-such-task", "name": "renamed"}),
            company,
            UpdateRequest(task="no-such-task"),
        )
    assert Task.objects(id="task1").first().name == task_name()


def test_row20_projectless_task_is_writable_by_any_company_member(company):
    # A task without a project has no project to check against and keeps its company-level behaviour
    make_task(company, None, "owner1", id_="loose-task")
    svc.update(
        FakeCall(identity(company, "stranger"), data={"task": "loose-task", "name": "renamed"}),
        company,
        UpdateRequest(task="loose-task"),
    )
    assert Task.objects(id="loose-task").first().name == "renamed"


@pytest.mark.needs_redis
def test_row20_projectless_task_is_deletable_by_any_company_member(company, monkeypatch):
    monkeypatch.setattr(svc, "_delete_task_events", lambda **kwargs: None)
    make_task(company, None, "owner1", id_="loose-task")
    svc.delete(
        FakeCall(identity(company, "stranger")),
        company,
        DeleteRequest(task="loose-task", force=True, delete_external_artifacts=False),
    )
    assert Task.objects(id="loose-task").first() is None


def test_row25_read_member_of_a_public_project_can_read_a_task(company):
    make_root(company, visibility=ProjectVisibility.public, members=[("u2", ProjectRole.read)])
    make_task(company, "root1", "owner1")
    call = FakeCall(identity(company, "u2"))
    svc.get_by_id(call, company, TaskRequest(task="task1"))
    assert call.result.data["task"]["id"] == "task1"


def test_row26_read_member_of_a_public_project_cannot_write(company):
    make_root(company, visibility=ProjectVisibility.public, members=[("u2", ProjectRole.read)])
    make_task(company, "root1", "owner1")
    with pytest.raises(errors.forbidden.NoWritePermission):
        svc.update(
            FakeCall(identity(company, "u2"), data={"task": "task1", "name": "renamed"}),
            company,
            UpdateRequest(task="task1"),
        )
    assert Task.objects(id="task1").first().name == task_name()


# ---------------------------------------------------------------------------------------------------------------------
# Section 2.2 and 2.3: project-change bookkeeping per site
# ---------------------------------------------------------------------------------------------------------------------


@pytest.fixture
def bookkeeping(monkeypatch):
    """Spies on the tag-cache, project-time and repository calls of the task service"""
    calls = SimpleNamespace(reset=[], update_tags=[], project_time=[], repository=[])

    def spy_reset(company, projects):
        calls.reset.append(list(projects))

    def spy_update_tags(company, project, fields):
        calls.update_tags.append(
            {"project": project, "tags": fields.get("tags"), "system_tags": fields.get("system_tags")}
        )

    def spy_project_time(project_ids=None):
        calls.project_time.append(project_ids)

    def spy_repository(company_id, user, task):
        calls.repository.append(getattr(task, "id", task))

    monkeypatch.setattr(svc, "_reset_cached_tags", spy_reset)
    monkeypatch.setattr(svc, "_update_cached_tags", spy_update_tags)
    monkeypatch.setattr(svc, "update_project_time", spy_project_time)
    monkeypatch.setattr(svc, "_record_repository", spy_repository)
    return calls


def _as_set(values):
    # Project lists are compared as sets: the order is an implementation detail, the membership is the rule
    return sorted(values, key=lambda p: (p is None, p or ""))


def test_create_updates_the_tag_cache_and_time_of_its_project_and_records_the_repository(company, bookkeeping):
    make_root(company, owner="owner1")
    call = FakeCall(identity(company, "owner1"), data={"name": "new-task", "type": "training", "project": "root1"})
    svc.create(call, company, CreateRequest(name="new-task", type="training"))
    created_id = call.result.data_model.id
    assert bookkeeping.reset == []
    assert bookkeeping.update_tags == [{"project": "root1", "tags": None, "system_tags": None}]
    assert bookkeeping.project_time == ["root1"]
    assert bookkeeping.repository == [created_id]


def test_update_name_only_touches_the_tag_cache_of_its_project_and_no_time(company, bookkeeping):
    make_root(company, owner="owner1")
    make_task(company, "root1", "owner1")
    svc.update(
        FakeCall(identity(company, "owner1"), data={"task": "task1", "name": "renamed"}),
        company,
        UpdateRequest(task="task1"),
    )
    assert bookkeeping.reset == []
    assert bookkeeping.update_tags == [{"project": "root1", "tags": None, "system_tags": None}]
    # The project is not in the request, so the time update is called with None, which does nothing
    assert bookkeeping.project_time == [None]
    assert bookkeeping.repository == ["task1"]


def test_update_tags_on_the_same_project_invalidates_only_its_own_project(company, bookkeeping):
    make_root(company, owner="owner1")
    make_task(company, "root1", "owner1")
    svc.update(
        FakeCall(identity(company, "owner1"), data={"task": "task1", "tags": ["fresh"]}),
        company,
        UpdateRequest(task="task1"),
    )
    assert bookkeeping.reset == []
    assert bookkeeping.update_tags == [{"project": "root1", "tags": ["fresh"], "system_tags": None}]


# suspect: pinned as is, see the decision issue (#282)
def test_update_that_moves_a_task_resets_both_projects_but_bumps_only_the_new_one(company, bookkeeping):
    # The old project's time is not bumped by update, while update_batch and move bump both. Pinned as it is
    make_root(company, id_="src", owner="owner1")
    make_root(company, id_="dst", owner="owner1")
    make_task(company, "src", "owner1")
    svc.update(
        FakeCall(identity(company, "owner1"), data={"task": "task1", "project": "dst"}),
        company,
        UpdateRequest(task="task1"),
    )
    assert _as_set(bookkeeping.reset[0]) == _as_set(["dst", "src"])
    assert bookkeeping.update_tags == []
    assert bookkeeping.project_time == ["dst"]
    assert bookkeeping.repository == ["task1"]


# suspect: pinned as is, see the decision issue (#282)
def test_edit_that_moves_a_task_resets_both_projects_but_bumps_only_the_new_one(company, bookkeeping):
    # Same as update: the old project's time is not bumped. Pinned as it is
    make_root(company, id_="src", owner="owner1")
    make_root(company, id_="dst", owner="owner1")
    make_task(company, "src", "owner1")
    svc.edit(
        FakeCall(
            identity(company, "owner1"),
            data={"task": "task1", "name": "task1-edited", "type": "training", "project": "dst"},
        ),
        company,
        UpdateRequest(task="task1"),
    )
    assert _as_set(bookkeeping.reset[0]) == _as_set(["dst", "src"])
    assert bookkeeping.project_time == ["dst"]
    assert bookkeeping.repository == ["task1"]


def test_edit_on_the_same_project_invalidates_only_its_own_project(company, bookkeeping):
    make_root(company, owner="owner1")
    make_task(company, "root1", "owner1")
    svc.edit(
        FakeCall(
            identity(company, "owner1"),
            data={"task": "task1", "name": "task1-edited", "type": "training", "tags": ["fresh"]},
        ),
        company,
        UpdateRequest(task="task1"),
    )
    assert bookkeeping.reset == []
    assert bookkeeping.update_tags == [{"project": "root1", "tags": ["fresh"], "system_tags": None}]
    assert bookkeeping.project_time == [None]
    assert bookkeeping.repository == ["task1"]


def test_update_batch_name_only_touches_no_tag_cache_time_or_repository(company, bookkeeping):
    make_root(company, owner="owner1")
    make_task(company, "root1", "owner1")
    call = FakeCall(identity(company, "owner1"), batched=[{"task": "task1", "name": "renamed"}])
    svc.update_batch(call, company, None)
    assert call.result.data == {"updated": 1}
    assert bookkeeping.reset == []
    assert bookkeeping.update_tags == []
    assert bookkeeping.project_time == []
    assert bookkeeping.repository == []


def test_update_batch_tags_resets_its_project_and_bumps_its_time(company, bookkeeping):
    make_root(company, owner="owner1")
    make_task(company, "root1", "owner1")
    call = FakeCall(identity(company, "owner1"), batched=[{"task": "task1", "tags": ["fresh"]}])
    svc.update_batch(call, company, None)
    assert call.result.data == {"updated": 1}
    assert bookkeeping.reset == [["root1"]]
    assert bookkeeping.project_time == [["root1"]]
    assert bookkeeping.update_tags == []
    assert bookkeeping.repository == []


def test_update_batch_move_resets_and_bumps_both_projects_and_records_no_repository(company, bookkeeping):
    make_root(company, id_="src", owner="owner1")
    make_root(company, id_="dst", owner="owner1")
    make_task(company, "src", "owner1")
    call = FakeCall(identity(company, "owner1"), batched=[{"task": "task1", "project": "dst"}])
    svc.update_batch(call, company, None)
    assert call.result.data == {"updated": 1}
    assert _as_set(bookkeeping.reset[0]) == _as_set(["dst", "src"])
    assert bookkeeping.update_tags == []
    assert bookkeeping.repository == []


def test_move_resets_and_bumps_both_projects_and_records_no_repository(company, bookkeeping):
    make_root(company, id_="src", owner="owner1")
    make_root(company, id_="dst", owner="owner1")
    make_task(company, "src", "owner1")
    result = svc.move(
        FakeCall(identity(company, "owner1"), data={"project": "dst"}),
        company,
        MoveRequest(ids=["task1"], project="dst"),
    )
    assert result == {"project_id": "dst"}
    assert _as_set(bookkeeping.reset[0]) == _as_set(["dst", "src"])
    assert _as_set(bookkeeping.project_time[0]) == _as_set(["dst", "src"])
    assert bookkeeping.update_tags == []
    assert bookkeeping.repository == []


@pytest.mark.needs_redis
def test_delete_resets_the_tag_cache_of_its_project_and_nothing_else(company, bookkeeping, monkeypatch):
    monkeypatch.setattr(svc, "_delete_task_events", lambda **kwargs: None)
    make_root(company, owner="owner1")
    make_task(company, "root1", "owner1")
    svc.delete(
        FakeCall(identity(company, "owner1")),
        company,
        DeleteRequest(task="task1", force=True, delete_external_artifacts=False),
    )
    assert bookkeeping.reset == [["root1"]]
    assert bookkeeping.project_time == []
    assert bookkeeping.repository == []


@pytest.mark.needs_redis
def test_delete_of_a_projectless_task_resets_with_an_empty_list(company, bookkeeping, monkeypatch):
    monkeypatch.setattr(svc, "_delete_task_events", lambda **kwargs: None)
    make_task(company, None, "owner1", id_="loose-task")
    svc.delete(
        FakeCall(identity(company, "owner1")),
        company,
        DeleteRequest(task="loose-task", force=True, delete_external_artifacts=False),
    )
    assert bookkeeping.reset == [[]]


@pytest.mark.needs_redis
def test_delete_many_resets_the_projects_of_its_tasks_and_passes_none_for_a_projectless_one(
    company, bookkeeping, monkeypatch
):
    monkeypatch.setattr(svc, "_delete_task_events", lambda **kwargs: None)
    make_root(company, owner="owner1")
    make_task(company, "root1", "owner1", id_="task1")
    make_task(company, None, "owner1", id_="loose-task")
    svc.delete_many(
        FakeCall(identity(company, "owner1")),
        company,
        DeleteManyRequest(ids=["task1", "loose-task"], force=True, delete_external_artifacts=False),
    )
    assert len(bookkeeping.reset) == 1
    assert _as_set(bookkeeping.reset[0]) == _as_set(["root1", None])
    assert bookkeeping.project_time == []
    assert bookkeeping.repository == []
