# apiserver/tests/bll/task/test_task_batch_and_writers_characterisation.py
#
# Characterisation tests of the batch and the other task field writers of the task service (tracking issue #267,
# step 1b, issue #269): tasks.update_batch, tasks.create, tasks.set_requirements, tasks.add_or_update_model,
# tasks.delete_models, tasks.update_tags, tasks.make_public, tasks.make_private and reports.update.
#
# They pin what the endpoints do today, stored values, counts, side effects and error codes included, so that the
# refactor steps cannot change it silently. Production code is not changed here.
#
# A case marked "# suspect: pinned as is, see the decision issue (#282)" looks unintended. It is pinned as it is
# and listed in the pull request under "found, not changed".
#
# Side effects are observed through stubs of the tag cache (org_bll), project time (update_project_time) and the
# repository recording (_record_repository). Where a test uses the real tag cache it carries needs_redis.
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import mock
from uuid import uuid4

import pytest
from mongoengine.errors import OperationError, ValidationError

from apiserver.apierrors import errors
from apiserver.apimodels.base import MakePublicRequest
from apiserver.apimodels.reports import UpdateReportRequest
from apiserver.apimodels.tasks import (
    AddUpdateModelRequest,
    CreateRequest,
    DeleteModelsRequest,
    ModelItemKey,
    SetRequirementsRequest,
    UpdateTagsRequest,
)
from apiserver.bll.organization import Tags
from apiserver.bll.task import TaskBLL
from apiserver.bll.project import project_changes
from apiserver.database.model.auth import Role
from apiserver.database.model.project import Project, ProjectMember, ProjectRole, ProjectVisibility
from apiserver.database.model.task.task import ModelItem, Models, Script, Task, TaskStatus, TaskType
from apiserver.service_repo.auth import Identity
from apiserver.services import reports as reports_svc
from apiserver.services import tasks as svc
from apiserver.tests.db import ensure_db
from apiserver.utilities.partial_version import PartialVersion

ensure_db()


@pytest.fixture
def company():
    # Every test gets its own company so that rows of other tests never interfere
    return f"c-{uuid4().hex[:8]}"


def identity(company, user, role=Role.user):
    return Identity(user=user, company=company, role=role)


class FakeCall:
    """The call object the endpoints read: identity, data, batched data and the requested endpoint version"""

    def __init__(self, ident, data=None, batched=None, version="2.30"):
        self.identity = ident
        self.data = data if data is not None else {}
        self.batched_data = batched
        self.requested_endpoint_version = PartialVersion(version)
        self.result = SimpleNamespace(data=None, data_model=None)


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


def make_task(
    company,
    project_id,
    owner,
    id_="task1",
    status=TaskStatus.created,
    task_type=TaskType.training,
    name=None,
    **fields,
):
    task = Task(
        id=id_,
        company=company,
        user=owner,
        project=project_id,
        name=name or f"Task-{id_}",
        type=task_type,
        created=datetime.utcnow(),
        status=status,
        **fields,
    )
    task.save()
    return task


def db_task(task_id):
    return Task.objects(id=task_id).first()


def task_name(id_="task1"):
    return f"Task-{id_}"


def _as_set(values):
    # Project lists are compared as sets: the order is an implementation detail, the membership is the rule
    return sorted(values, key=lambda p: (p is None, p or ""))


def _projects_of(call_args):
    return _as_set(call_args.kwargs["projects"])


@pytest.fixture
def side_effects():
    """Stubs of the tag cache, the project time and the repository recording of the task service"""
    with (
        mock.patch.object(svc, "org_bll") as org,
        mock.patch.object(svc, "update_project_time") as project_time,
        mock.patch.object(svc, "_record_repository") as record,
        # update, edit and create record through the project-change module since issue 276
        mock.patch.object(project_changes, "org_bll", org),
        mock.patch.object(project_changes, "update_project_time", project_time),
        mock.patch.object(project_changes, "record_repository", record),
    ):
        yield SimpleNamespace(org=org, project_time=project_time, record=record)


# ---------------------------------------------------------------------------------------------------------------------
# B series: tasks.update_batch
# ---------------------------------------------------------------------------------------------------------------------


def test_batch_writes_user_fields_for_each_item_and_returns_updated(company, side_effects):
    make_root(company, owner="owner1")
    make_task(company, "root1", "owner1", id_="task1")
    make_task(company, "root1", "owner1", id_="task2")
    call = FakeCall(
        identity(company, "owner1"),
        batched=[{"task": "task1", "name": "one"}, {"task": "task2", "name": "two"}],
    )
    svc.update_batch(call, company, None)
    assert call.result.data == {"updated": 2}
    assert db_task("task1").name == "one"
    assert db_task("task2").name == "two"


def test_batch_missing_id_refuses_the_whole_batch(company, side_effects):
    make_root(company, owner="owner1")
    make_task(company, "root1", "owner1")
    call = FakeCall(
        identity(company, "owner1"),
        batched=[{"task": "task1", "name": "renamed"}, {"task": "missing1", "name": "x"}],
    )
    with pytest.raises(errors.bad_request.InvalidTaskId):
        svc.update_batch(call, company, None)
    assert db_task("task1").name == task_name()
    side_effects.project_time.assert_not_called()


def test_batch_forbidden_item_refuses_the_whole_batch(company, side_effects):
    # u2 has read_write on the project: it may write its own task and not the task of owner1
    make_root(company, owner="owner1", members=[("u2", ProjectRole.read_write)])
    make_task(company, "root1", "owner1", id_="task1")
    make_task(company, "root1", "u2", id_="task2")
    call = FakeCall(
        identity(company, "u2"),
        batched=[{"task": "task2", "name": "mine"}, {"task": "task1", "name": "theirs"}],
    )
    with pytest.raises(errors.forbidden.NoWritePermission):
        svc.update_batch(call, company, None)
    assert db_task("task1").name == task_name("task1")
    assert db_task("task2").name == task_name("task2")
    side_effects.project_time.assert_not_called()


# suspect: pinned as is, see the decision issue (#282)
def test_batch_duplicate_task_keeps_the_last_item(company, side_effects):
    make_root(company, owner="owner1")
    make_task(company, "root1", "owner1")
    call = FakeCall(
        identity(company, "owner1"),
        batched=[{"task": "task1", "name": "one"}, {"task": "task1", "name": "two"}],
    )
    svc.update_batch(call, company, None)
    assert db_task("task1").name == "two"
    assert call.result.data == {"updated": 1}


def test_batch_item_with_nothing_writable_is_skipped(company, side_effects):
    make_root(company, owner="owner1")
    make_task(company, "root1", "owner1")
    call = FakeCall(identity(company, "owner1"), batched=[{"task": "task1", "type": "custom"}])
    svc.update_batch(call, company, None)
    assert call.result.data == {"updated": 0}
    assert db_task("task1").last_change is None


def test_batch_stamps_last_change_not_last_update(company, side_effects):
    make_root(company, owner="owner1")
    make_task(company, "root1", "owner1")
    call = FakeCall(identity(company, "owner1"), batched=[{"task": "task1", "name": "renamed"}])
    svc.update_batch(call, company, None)
    task = db_task("task1")
    assert task.last_change is not None
    assert task.last_changed_by == "owner1"
    assert task.last_update is None


# intended, see #301
def test_batch_strips_the_name(company, side_effects):
    # Checked in the stored document: the model strips on load, so db_task() alone cannot show the stored value
    make_root(company, owner="owner1")
    make_task(company, "root1", "owner1")
    call = FakeCall(identity(company, "owner1"), batched=[{"task": "task1", "name": "  abc  "}])
    svc.update_batch(call, company, None)
    assert call.result.data == {"updated": 1}
    assert Task.objects(id="task1").as_pymongo().first()["name"] == "abc"


# intended, see #301
def test_batch_does_not_enforce_name_minimum(company, side_effects):
    # The batch refuses a two-character name as update does, and stores nothing
    make_root(company, owner="owner1")
    make_task(company, "root1", "owner1")
    call = FakeCall(identity(company, "owner1"), batched=[{"task": "task1", "name": "ab"}])
    with pytest.raises(errors.bad_request.DataValidationError):
        svc.update_batch(call, company, None)
    assert db_task("task1").name == task_name()
    side_effects.project_time.assert_not_called()


# intended, see #301
def test_batch_does_not_enforce_display_name_maximum(company, side_effects):
    # The batch refuses a 201-character display name as update does, and stores nothing
    make_root(company, owner="owner1")
    make_task(company, "root1", "owner1")
    call = FakeCall(identity(company, "owner1"), batched=[{"task": "task1", "display_name": "x" * 201}])
    with pytest.raises(errors.bad_request.DataValidationError):
        svc.update_batch(call, company, None)
    assert Task.objects(id="task1").as_pymongo().first().get("display_name") is None


# intended, see #301
def test_batch_counts_modified_not_matched(company, side_effects):
    # The item renames the task to the name it already has. The count is the matched count, as update returns it:
    # the item matches its task and counts once.
    make_root(company, owner="owner1")
    make_task(company, "root1", "owner1", name="same")
    call = FakeCall(identity(company, "owner1"), batched=[{"task": "task1", "name": "same"}])
    svc.update_batch(call, company, None)
    assert call.result.data == {"updated": 1}
    assert db_task("task1").name == "same"


def test_batch_move_resets_both_projects_and_updates_both_project_times(company, side_effects):
    make_root(company, id_="src", owner="owner1")
    make_root(company, id_="dst", owner="owner1")
    make_task(company, "src", "owner1")
    call = FakeCall(identity(company, "owner1"), batched=[{"task": "task1", "project": "dst"}])
    svc.update_batch(call, company, None)
    assert call.result.data == {"updated": 1}
    assert db_task("task1").project == "dst"
    assert _projects_of(side_effects.org.reset_tags.call_args) == ["dst", "src"]
    assert _as_set(side_effects.project_time.call_args.kwargs["project_ids"]) == ["dst", "src"]


# suspect: pinned as is, see the decision issue (#282)
def test_batch_tag_only_change_updates_project_time_and_resets_its_project(company, side_effects):
    # update and edit bump no project time for a tag-only change; the batch does
    make_root(company, owner="owner1")
    make_task(company, "root1", "owner1")
    call = FakeCall(identity(company, "owner1"), batched=[{"task": "task1", "tags": ["fresh"]}])
    svc.update_batch(call, company, None)
    assert call.result.data == {"updated": 1}
    assert _projects_of(side_effects.org.reset_tags.call_args) == ["root1"]
    assert side_effects.project_time.call_args.kwargs["project_ids"] == ["root1"]


# intended, see #301
def test_batch_does_not_record_the_repository_on_a_move(company, side_effects):
    # Moving a task changes the project its repository is recorded for: the batch records it once per written task,
    # as update does
    make_root(company, id_="src", owner="owner1")
    make_root(company, id_="dst", owner="owner1")
    make_task(company, "src", "owner1", script=Script(repository="git-repo"))
    call = FakeCall(identity(company, "owner1"), batched=[{"task": "task1", "project": "dst"}])
    svc.update_batch(call, company, None)
    assert call.result.data == {"updated": 1}
    side_effects.record.assert_called_once_with(company, "owner1", "task1")


def test_batch_without_items_raises(company, side_effects):
    call = FakeCall(identity(company, "owner1"), batched=None)
    with pytest.raises(errors.bad_request.BatchContainsNoItems):
        svc.update_batch(call, company, None)


def test_batch_empty_list_is_zero(company, side_effects):
    call = FakeCall(identity(company, "owner1"), batched=[])
    svc.update_batch(call, company, None)
    assert call.result.data == {"updated": 0}


def test_batch_clearing_project_is_refused(company, side_effects):
    make_root(company, owner="owner1")
    make_task(company, "root1", "owner1")
    call = FakeCall(identity(company, "owner1"), batched=[{"task": "task1", "project": ""}])
    with pytest.raises(errors.bad_request.InvalidProjectId):
        svc.update_batch(call, company, None)
    assert db_task("task1").project == "root1"


# ---------------------------------------------------------------------------------------------------------------------
# C series: tasks.create, tasks.set_requirements
# ---------------------------------------------------------------------------------------------------------------------


# suspect: pinned as is, see the decision issue (#282)
def test_create_sorts_tags_and_stamps_all_three_times(company, side_effects):
    make_root(company, owner="owner1")
    call = FakeCall(
        identity(company, "owner1"),
        data={"name": "new-task", "type": "training", "project": "root1", "tags": ["b", "a"]},
    )
    svc.create(call, company, CreateRequest(name="new-task", type="training"))
    task = db_task(call.result.data_model.id)
    assert task.tags == ["a", "b"]
    assert task.created == task.last_update == task.last_change
    assert task.last_changed_by == "owner1"


def test_create_fails_on_a_bad_status_through_save(company):
    task = TaskBLL.create(company=company, user="owner1", fields={"name": "t", "type": "training", "status": "bogus"})
    with pytest.raises(ValidationError):
        task.save()


def test_create_updates_tag_cache_and_project_time_and_records_repository(company, side_effects):
    make_root(company, owner="owner1")
    call = FakeCall(
        identity(company, "owner1"),
        data={
            "name": "new-task",
            "type": "training",
            "project": "root1",
            "script": {"repository": "git-repo"},
        },
    )
    svc.create(call, company, CreateRequest(name="new-task", type="training"))
    created_id = call.result.data_model.id
    assert side_effects.org.update_tags.call_args.kwargs["projects"] == ["root1"]
    assert side_effects.project_time.call_args.args == ("root1",)
    side_effects.record.assert_called_once()
    assert side_effects.record.call_args.args[2].id == created_id


def test_set_requirements_replaces_requirements_and_stamps_last_update(company, side_effects):
    make_root(company, owner="owner1")
    make_task(company, "root1", "owner1", script=Script(repository="git-repo"))
    call = FakeCall(identity(company, "owner1"))
    svc.set_requirements(
        call,
        company,
        SetRequirementsRequest(task="task1", requirements={"pip": ["numpy"]}),
    )
    assert call.result.data_model.updated == 1
    assert call.result.data_model.fields == {"script.requirements": {"pip": ["numpy"]}}
    task = db_task("task1")
    assert task.script.requirements == {"pip": ["numpy"]}
    assert task.last_update is not None
    assert task.last_changed_by == "owner1"


# suspect: pinned as is, see the decision issue (#282)
def test_set_requirements_on_null_script_fails_in_the_database_not_in_the_guard(company, side_effects):
    # A script stored as null is loaded as an empty document, so the MissingTaskFields guard does not fire; the write
    # is then refused by MongoDB ("Cannot create field 'requirements' in element {script: null}")
    make_root(company, owner="owner1")
    make_task(company, "root1", "owner1")
    Task._get_collection().update_one({"_id": "task1"}, {"$set": {"script": None}})
    with pytest.raises(OperationError):
        svc.set_requirements(
            FakeCall(identity(company, "owner1")),
            company,
            SetRequirementsRequest(task="task1", requirements={"pip": ["numpy"]}),
        )
    assert Task._get_collection().find_one({"_id": "task1"})["script"] is None


# suspect: pinned as is, see the decision issue (#282)
def test_set_requirements_on_empty_script_writes_requirements(company, side_effects):
    # A task saved without a script is loaded with an empty script document, which is truthy: the no-script guard
    # does not fire for it and the requirements are written.
    make_root(company, owner="owner1")
    make_task(company, "root1", "owner1")
    call = FakeCall(identity(company, "owner1"))
    svc.set_requirements(
        call,
        company,
        SetRequirementsRequest(task="task1", requirements={"pip": ["numpy"]}),
    )
    assert call.result.data_model.updated == 1
    assert db_task("task1").script.requirements == {"pip": ["numpy"]}


# ---------------------------------------------------------------------------------------------------------------------
# M series: tasks.add_or_update_model, tasks.delete_models
# ---------------------------------------------------------------------------------------------------------------------


def test_add_or_update_model_updates_then_pushes(company, side_effects):
    make_root(company, owner="owner1")
    make_task(
        company,
        "root1",
        "owner1",
        models=Models(input=[ModelItem(name="m1", model="model1")]),
    )
    # an existing name is updated in place
    call = FakeCall(identity(company, "owner1"))
    response = svc.add_or_update_model(
        call,
        company,
        AddUpdateModelRequest(task="task1", name="m1", model="model2", type="input"),
    )
    assert response == {"updated": 1}
    # a new name is pushed
    response = svc.add_or_update_model(
        call,
        company,
        AddUpdateModelRequest(task="task1", name="m2", model="model3", type="input"),
    )
    assert response == {"updated": 1}
    models = {m.name: m.model for m in db_task("task1").models.input}
    assert models == {"m1": "model2", "m2": "model3"}


# suspect: pinned as is, see the decision issue (#282)
def test_add_or_update_model_recomputes_active_duration_of_a_running_task(company, side_effects):
    make_root(company, owner="owner1")
    make_task(
        company,
        "root1",
        "owner1",
        status=TaskStatus.in_progress,
        started=datetime.utcnow() - timedelta(minutes=5),
    )
    svc.add_or_update_model(
        FakeCall(identity(company, "owner1")),
        company,
        AddUpdateModelRequest(task="task1", name="m1", model="model1", type="output"),
    )
    assert db_task("task1").active_duration is not None


def test_delete_models_removes_matched_names_and_stamps_last_change_only(company, side_effects):
    make_root(company, owner="owner1")
    make_task(
        company,
        "root1",
        "owner1",
        models=Models(
            input=[ModelItem(name="m1", model="model1"), ModelItem(name="m2", model="model2")],
            output=[ModelItem(name="m3", model="model3")],
        ),
    )
    call = FakeCall(identity(company, "owner1"))
    response = svc.delete_models(
        call,
        company,
        DeleteModelsRequest(
            task="task1",
            models=[
                ModelItemKey(name="m1", type="input"),
                ModelItemKey(name="m3", type="output"),
                ModelItemKey(name="missing", type="input"),
            ],
        ),
    )
    assert response == {"updated": 1}
    task = db_task("task1")
    assert [m.name for m in task.models.input] == ["m2"]
    assert task.models.output == []
    assert task.last_change is not None
    assert task.last_update is None


# ---------------------------------------------------------------------------------------------------------------------
# T series: tasks.update_tags
# ---------------------------------------------------------------------------------------------------------------------


# suspect: pinned as is, see the decision issue (#282)
@pytest.mark.needs_redis
def test_update_tags_counts_add_and_remove_separately(company):
    make_root(company, owner="owner1")
    make_task(company, "root1", "owner1", id_="task1", tags=["y"])
    make_task(company, "root1", "owner1", id_="task2", tags=["y"])
    response = svc.update_tags(
        FakeCall(identity(company, "owner1")),
        company,
        UpdateTagsRequest(ids=["task1", "task2"], add_tags=["x"], remove_tags=["y"]),
    )
    # two tasks gained "x" and two lost "y": the response adds both counts
    assert response == {"updated": 4}
    assert sorted(db_task("task1").tags) == ["x"]
    assert sorted(db_task("task2").tags) == ["x"]


def test_update_tags_invalidates_tag_cache_for_the_projects(company):
    make_root(company, id_="root1", owner="owner1")
    make_root(company, id_="root2", owner="owner1")
    make_task(company, "root1", "owner1", id_="task1")
    make_task(company, "root2", "owner1", id_="task2")
    with mock.patch.object(svc.org_bll, "update_tags") as update_tags:
        response = svc.update_tags(
            FakeCall(identity(company, "owner1")),
            company,
            UpdateTagsRequest(ids=["task1", "task2"], add_tags=["x"], remove_tags=[]),
        )
    assert response == {"updated": 2}
    assert update_tags.call_args.kwargs["entity"] == Tags.Task
    assert _projects_of(update_tags.call_args) == ["root1", "root2"]


# ---------------------------------------------------------------------------------------------------------------------
# P series: tasks.make_public, tasks.make_private
# ---------------------------------------------------------------------------------------------------------------------


# suspect: pinned as is, see the decision issue (#282)
def test_make_public_flips_company_and_stamps_last_change_not_last_update(company, side_effects):
    make_root(company, owner="owner1")
    make_task(company, "root1", "owner1")
    call = FakeCall(identity(company, "owner1"))
    svc.make_public(call, company, MakePublicRequest(ids=["task1"]))
    assert call.result.data == {"updated": 1}
    task = db_task("task1")
    assert task.company == ""
    assert task.company_origin == company
    assert task.last_change is not None
    assert task.last_update is None
    side_effects.org.reset_tags.assert_not_called()
    side_effects.org.update_tags.assert_not_called()


def test_make_private_round_trip(company, side_effects):
    make_root(company, owner="owner1")
    make_task(company, "root1", "owner1")
    svc.make_public(FakeCall(identity(company, "owner1")), company, MakePublicRequest(ids=["task1"]))
    call = FakeCall(identity(company, "owner1"))
    svc.make_private(call, company, MakePublicRequest(ids=["task1"]))
    assert call.result.data == {"updated": 1}
    task = db_task("task1")
    assert task.company == company
    assert task.company_origin is None


# ---------------------------------------------------------------------------------------------------------------------
# R series: reports.update
# ---------------------------------------------------------------------------------------------------------------------


# suspect: pinned as is, see the decision issue (#282)
def test_reports_update_sets_last_update_and_skips_tag_cache(company):
    make_root(company, owner="owner1")
    make_task(company, "root1", "owner1", task_type=TaskType.report)
    call = FakeCall(identity(company, "owner1"), data={"tags": ["b", "a"]})
    with mock.patch.object(reports_svc, "org_bll") as org:
        response = reports_svc.update_report(
            call,
            company,
            UpdateReportRequest(task="task1", tags=["b", "a"]),
        )
    assert response.updated == 1
    task = db_task("task1")
    assert task.last_update is not None
    assert task.last_changed_by == "owner1"
    org.reset_tags.assert_not_called()
    org.update_tags.assert_not_called()
