"""Characterisation tests for the task write endpoints update, edit and (for the cross-endpoint cases) update_batch.

They record what the service functions write, return, notify and refuse on the real database today, so that the
refactor that moves them into use cases cannot change any of it silently. Behaviour that looks wrong is pinned as it
is and marked with `# suspect: pinned as is, see the decision issue (#282)`; those cases are listed in the pull request.

Cases are named by the matrix of issue #268: U1-U18 (update), E1-E23 (edit), X1-X2 (cross-endpoint).
Side effects are recorded by stubs on the service module: the tag cache (svc.org_bll), the project time
(svc.update_project_time) and the repository recording (svc._record_repository).
"""

from datetime import datetime
from types import SimpleNamespace
from unittest import mock
from uuid import uuid4

import pytest

from apiserver.apierrors import errors
from apiserver.apimodels.tasks import UpdateRequest
from apiserver.bll.organization import Tags
from apiserver.bll.project import project_changes
from apiserver.database.model.auth import Role
from apiserver.database.model.model import Model
from apiserver.database.model.project import Project, ProjectMember, ProjectRole, ProjectVisibility
from apiserver.database.model.task.task import (
    Execution,
    ModelItem,
    Models,
    Output,
    Script,
    Task,
    TaskStatus,
)
from apiserver.service_repo.auth import Identity
from apiserver.services import tasks as svc
from apiserver.tests.db import ensure_db
from apiserver.utilities.partial_version import PartialVersion

ensure_db()

CREATED = datetime(2020, 1, 1)
T0 = datetime(2021, 1, 1)


@pytest.fixture(scope="module", autouse=True)
def _warm_up_task_indexes():
    """The first use of the Task model in a process can fail on a text index left by another version of the model
    in a shared database (IndexOptionsConflict). Take that hit here, not in the first test."""
    try:
        Task.objects.first()
    except Exception:
        pass


@pytest.fixture
def company():
    # Every test gets its own company so that rows from other tests never interfere
    return f"c-{uuid4().hex[:8]}"


@pytest.fixture
def side_effects():
    with (
        mock.patch.object(svc, "org_bll") as org,
        mock.patch.object(svc, "update_project_time") as project_time,
        mock.patch.object(svc, "_record_repository") as record,
        # update, edit and create record through the project-change module since issue 276
        mock.patch.object(project_changes, "org_bll", org),
        mock.patch.object(project_changes, "update_project_time", project_time),
        mock.patch.object(project_changes, "record_repository", record),
    ):
        yield mock.Mock(org=org, project_time=project_time, record=record)


class FakeCall:
    def __init__(self, ident, data=None, batched=None):
        self.identity = ident
        self.data = data if data is not None else {}
        self.batched_data = batched
        self.requested_endpoint_version = PartialVersion("2.30")
        self.result = SimpleNamespace(data=None, data_model=None)


def identity(company, user, role=Role.user):
    return Identity(user=user, company=company, role=role)


def make_root(company, id_="root1", owner="owner1", members=()):
    project = Project(
        id=id_,
        company=company,
        user=owner,
        name=f"Project-{id_}",
        basename=f"Project-{id_}",
        created=datetime.utcnow(),
        visibility=ProjectVisibility.private,
        members=[ProjectMember(user=u, role=r, added=datetime.utcnow(), added_by=owner) for u, r in members],
    )
    project.save()
    return project


def make_task(company, project_id, owner, id_="task1", status=TaskStatus.created, **extra):
    fields = dict(
        id=id_,
        company=company,
        user=owner,
        project=project_id,
        name=f"Task-{id_}",
        type="training",
        created=CREATED,
        status=status,
    )
    fields.update(extra)
    task = Task(**fields)
    task.save()
    return task


def make_model(company, id_):
    Model(
        id=id_,
        company=company,
        user="u1",
        name=f"model-{id_}",
        uri="s3://m",
        created=datetime.utcnow(),
        ready=True,
    ).save()
    return id_


def raw(task_id="task1"):
    return Task.objects(id=task_id).as_pymongo().first()


def setup_owned(company, owner="u1", **extra):
    """A project the owner can write to, and a task of the owner in it."""
    make_root(company, members=[(owner, ProjectRole.read_write)])
    return make_task(company, "root1", owner, **extra)


def update_call(company, user, data, role=Role.user):
    return FakeCall(identity(company, user, role), data=data)


def run_update(company, user, data):
    return svc.update(
        update_call(company, user, data),
        company,
        UpdateRequest(task=data["task"]),
    )


def run_edit(company, user, data, force=False):
    call = update_call(company, user, data)
    svc.edit(call, company, UpdateRequest(task=data["task"], force=force))
    return call.result.data_model


def run_batch(company, user, items):
    call = FakeCall(identity(company, user), batched=items)
    svc.update_batch(call, company, None)
    return call.result.data


def assert_no_side_effects(side_effects):
    side_effects.org.reset_tags.assert_not_called()
    side_effects.org.update_tags.assert_not_called()
    side_effects.project_time.assert_not_called()
    side_effects.record.assert_not_called()


# ---------------------------------------------------------------------------------------------------------------------
# A. tasks.update
# ---------------------------------------------------------------------------------------------------------------------


def test_update_writes_the_user_fields_and_returns_them(company, side_effects):  # U1
    setup_owned(company)
    res = run_update(
        company,
        "u1",
        {
            "task": "task1",
            "name": "renamed",
            "display_name": "Shown",
            "comment": "a comment",
            "status_message": "working",
            "tags": ["a"],
            "system_tags": ["s"],
        },
    )
    row = Task.objects.get(id="task1")
    assert row.name == "renamed"
    assert row.display_name == "Shown"
    assert row.comment == "a comment"
    assert row.status_message == "working"
    assert row.tags == ["a"]
    assert row.system_tags == ["s"]
    assert res.updated == 1
    assert set(res.fields) == {
        "name",
        "display_name",
        "comment",
        "status_message",
        "tags",
        "system_tags",
        "last_change",
        "last_changed_by",
    }


def test_update_ignores_fields_it_does_not_accept(company, side_effects):  # U2
    setup_owned(company)
    before = raw()
    res = run_update(
        company,
        "u1",
        {
            "task": "task1",
            "status": TaskStatus.queued,
            "type": "inference",
            "created": "2019-01-01T00:00:00Z",
            "models": {"input": []},
            "execution": {"framework": "x"},
            "script": {"repository": "r"},
            "container": {"image": "i"},
        },
    )
    assert raw() == before
    assert res.updated == 0
    assert res.fields == {}


def test_update_mixed_request_writes_only_accepted_fields(company, side_effects):  # U3
    setup_owned(company)
    res = run_update(company, "u1", {"task": "task1", "name": "accepted", "type": "inference"})
    row = Task.objects.get(id="task1")
    assert row.name == "accepted"
    assert row.type == "training"
    assert "type" not in res.fields
    assert res.updated == 1


# suspect: pinned as is, see the decision issue (#282)
def test_update_stamps_last_change_but_not_last_update(company, side_effects):  # U4
    make_root(company, members=[("u1", ProjectRole.read_write)])
    make_task(company, "root1", "u1", last_update=T0)
    run_update(company, "u1", {"task": "task1", "name": "renamed"})
    row = raw()
    assert row["name"] == "renamed"
    assert row["last_changed_by"] == "u1"
    assert row["last_change"] is not None
    # The schema says last_update covers edits too; update does not stamp it.
    assert row["last_update"] == T0


# suspect: pinned as is, see the decision issue (#282)
def test_update_is_allowed_on_a_running_task(company, side_effects):  # U5
    make_root(company, members=[("u1", ProjectRole.read_write)])
    make_task(company, "root1", "u1", status=TaskStatus.in_progress)
    run_update(company, "u1", {"task": "task1", "name": "while running"})
    assert Task.objects.get(id="task1").name == "while running"


# suspect: pinned as is, see the decision issue (#282)
def test_update_strips_name_and_clears_blank_display_name(company, side_effects):  # U6
    setup_owned(company)
    res = run_update(company, "u1", {"task": "task1", "name": "  abc  ", "display_name": "   "})
    row = Task.objects.get(id="task1")
    assert row.name == "abc"
    assert row.display_name is None
    # The response echoes the value as sent, not as stored.
    assert res.fields["name"] == "  abc  "


def test_update_refuses_a_name_shorter_than_three(company, side_effects):  # U7
    setup_owned(company)
    with pytest.raises(errors.bad_request.DataValidationError):
        run_update(company, "u1", {"task": "task1", "name": "ab"})
    assert Task.objects.get(id="task1").name == "Task-task1"


def test_update_refuses_a_display_name_over_200(company, side_effects):  # U8
    setup_owned(company)
    with pytest.raises(errors.bad_request.DataValidationError):
        run_update(company, "u1", {"task": "task1", "display_name": "x" * 201})
    assert raw().get("display_name") is None


# suspect: pinned as is, see the decision issue (#282)
def test_update_keeps_tags_in_request_order(company, side_effects):  # U9
    setup_owned(company)
    run_update(company, "u1", {"task": "task1", "tags": ["b", "a"]})
    assert Task.objects.get(id="task1").tags == ["b", "a"]


# suspect: pinned as is, see the decision issue (#282)
def test_update_validates_tag_prefix_only_when_tags_are_sent(company, side_effects):  # U10
    setup_owned(company)
    # System tags sent alone skip the prefix check
    run_update(company, "u1", {"task": "task1", "system_tags": ["__$x"]})
    assert Task.objects.get(id="task1").system_tags == ["__$x"]
    with pytest.raises(errors.bad_request.FieldsValueError):
        run_update(company, "u1", {"task": "task1", "tags": ["__$x"]})


# suspect: pinned as is, see the decision issue (#282)
def test_update_move_resets_tags_for_both_projects_and_stamps_new_project_time_only(company, side_effects):  # U11
    make_root(company, id_="root1", owner="owner1", members=[("u2", ProjectRole.collaborator)])
    make_root(company, id_="root2", owner="owner1", members=[("u2", ProjectRole.collaborator)])
    make_task(company, "root1", "owner1")
    run_update(company, "u2", {"task": "task1", "project": "root2"})
    assert Task.objects.get(id="task1").project == "root2"
    side_effects.org.reset_tags.assert_called_once()
    assert side_effects.org.reset_tags.call_args.kwargs["projects"] == ["root2", "root1"]
    # The old project's time is not stamped.
    side_effects.project_time.assert_called_once_with("root2")


# suspect: pinned as is, see the decision issue (#282)
def test_update_tags_only_updates_tag_cache_for_sent_fields(company, side_effects):  # U12
    setup_owned(company)
    run_update(company, "u1", {"task": "task1", "tags": ["x"]})
    side_effects.org.update_tags.assert_called_once()
    kwargs = side_effects.org.update_tags.call_args.kwargs
    assert side_effects.org.update_tags.call_args.args[1] == Tags.Task
    assert kwargs["projects"] == ["root1"]
    assert kwargs["tags"] == ["x"]
    assert kwargs["system_tags"] is None
    # Tag-only writes do not stamp the project time.
    side_effects.project_time.assert_called_once_with(None)


def test_update_refuses_a_move_without_admin_on_destination(company, side_effects):  # U13
    make_root(company, id_="src", members=[("u2", ProjectRole.read_write)])
    make_root(company, id_="dst", members=[("u2", ProjectRole.read_write)])
    make_task(company, "src", "u2")
    with pytest.raises(errors.forbidden.NoWritePermission):
        run_update(company, "u2", {"task": "task1", "project": "dst"})
    assert Task.objects.get(id="task1").project == "src"
    assert_no_side_effects(side_effects)


def test_update_clearing_project_is_refused_before_any_change(company, side_effects):  # U14
    setup_owned(company)
    with pytest.raises(errors.bad_request.InvalidProjectId):
        run_update(company, "u1", {"task": "task1", "name": "changed", "project": ""})
    assert Task.objects.get(id="task1").name == "Task-task1"
    assert Task.objects.get(id="task1").project == "root1"


def test_update_missing_task_is_invalid_task_id(company, side_effects):  # U15
    make_root(company, members=[("u1", ProjectRole.read_write)])
    with pytest.raises(errors.bad_request.InvalidTaskId) as exc:
        run_update(company, "u1", {"task": "nope", "name": "x"})
    assert exc.value.error_data == {"id": "nope", "company": company}


def test_update_with_nothing_writable_has_no_side_effects(company, side_effects):  # U16
    setup_owned(company)
    before = raw()
    assert run_update(company, "u1", {"task": "task1"}).updated == 0
    assert run_update(company, "u1", {"task": "task1", "type": "inference"}).updated == 0
    assert raw() == before
    assert raw().get("last_change") is None
    assert_no_side_effects(side_effects)


def test_update_records_repository_only_when_written(company, side_effects):  # U17
    setup_owned(company, script=Script(repository="r"))
    run_update(company, "u1", {"task": "task1", "name": "renamed"})
    side_effects.record.assert_called_once_with(company, "u1", "task1")
    run_update(company, "u1", {"task": "task1"})
    assert side_effects.record.call_count == 1


# suspect: pinned as is, see the decision issue (#282)
def test_update_writes_output_error_when_sent(company, side_effects):  # U18
    setup_owned(company)
    run_update(company, "u1", {"task": "task1", "output__error": "boom"})
    assert Task.objects.get(id="task1").output.error == "boom"


# ---------------------------------------------------------------------------------------------------------------------
# B. tasks.edit
# ---------------------------------------------------------------------------------------------------------------------


def test_edit_writes_create_fields_on_a_created_task(company, side_effects):  # E1
    setup_owned(company)
    res = run_edit(
        company,
        "u1",
        {
            "task": "task1",
            "name": "edited",
            "tags": ["t"],
            "type": "inference",
            "comment": "c",
            "container": {"image": "img"},
            "hyperparams": {"General": {"lr": {"section": "General", "name": "lr", "value": "0.1", "type": "float"}}},
            "runtime": {"k": 1},
        },
    )
    row = Task.objects.get(id="task1")
    assert row.name == "edited"
    assert row.tags == ["t"]
    assert row.type == "inference"
    assert row.comment == "c"
    assert row.container == {"image": "img"}
    assert row.hyperparams["General"]["lr"].value == "0.1"
    assert row.runtime == {"k": 1}
    assert res.updated == 1
    assert "last_change" in res.fields
    assert "last_changed_by" in res.fields


def test_edit_refuses_a_started_task_without_force(company, side_effects):  # E2
    make_root(company, members=[("u1", ProjectRole.read_write)])
    make_task(company, "root1", "u1", status=TaskStatus.in_progress)
    before = raw()
    with pytest.raises(errors.bad_request.InvalidTaskStatus):
        run_edit(company, "u1", {"task": "task1", "name": "x"})
    assert raw() == before


def test_edit_force_writes_a_started_task(company, side_effects):  # E3
    make_root(company, members=[("u1", ProjectRole.read_write)])
    make_task(company, "root1", "u1", status=TaskStatus.in_progress)
    run_edit(company, "u1", {"task": "task1", "name": "forced"}, force=True)
    assert Task.objects.get(id="task1").name == "forced"


# suspect: pinned as is, see the decision issue (#282)
def test_edit_can_set_status_on_a_created_task(company, side_effects):  # E4
    setup_owned(company)
    with mock.patch.object(svc, "QueueBLL") as queue_bll:
        run_edit(company, "u1", {"task": "task1", "status": TaskStatus.queued})
    row = raw()
    assert row["status"] == TaskStatus.queued
    assert row.get("status_changed") is None
    # The edit path does not touch the queue; test_queues pins the same behaviour end to end
    assert not queue_bll.mock_calls


# suspect: pinned as is, see the decision issue (#282)
def test_edit_stores_a_status_outside_the_choices(company, side_effects):  # E5
    setup_owned(company)
    run_edit(company, "u1", {"task": "task1", "status": "bogus"}, force=True)
    assert raw()["status"] == "bogus"


# suspect: pinned as is, see the decision issue (#282)
def test_edit_stores_a_type_outside_the_choices(company, side_effects):  # E6
    setup_owned(company)
    run_edit(company, "u1", {"task": "task1", "type": "bogus"})
    assert raw()["type"] == "bogus"


# suspect: pinned as is, see the decision issue (#282)
def test_edit_execution_merges_one_level(company, side_effects):  # E7
    make_root(company, members=[("u1", ProjectRole.read_write)])
    make_task(company, "root1", "u1", execution=Execution(framework="a", queue="q"))
    run_edit(company, "u1", {"task": "task1", "execution": {"model": "m"}})
    stored = raw()["execution"]
    assert stored["framework"] == "a"
    assert stored["queue"] == "q"
    # The merge keeps the existing keys, but the unknown key "model" is not stored, although the response
    # reports it (see E23). Pinned.
    assert "model" not in stored


def test_edit_script_merges_one_level(company, side_effects):  # E8
    make_root(company, members=[("u1", ProjectRole.read_write)])
    make_task(company, "root1", "u1", script=Script(repository="repo", entry_point="main.py"))
    run_edit(company, "u1", {"task": "task1", "script": {"entry_point": "x"}})
    script = Task.objects.get(id="task1").script
    assert script.repository == "repo"
    assert script.entry_point == "x"


def test_edit_models_replaces_only_the_given_key(company, side_effects):  # E9
    make_root(company, members=[("u1", ProjectRole.read_write)])
    make_model(company, "m-new")
    make_model(company, "m-old-in")
    make_model(company, "m-out")
    make_task(
        company,
        "root1",
        "u1",
        models=Models(
            input=[ModelItem(name="old-in", model="m-old-in")],
            output=[ModelItem(name="out", model="m-out")],
        ),
    )
    run_edit(company, "u1", {"task": "task1", "models": {"input": [{"name": "new-in", "model": "m-new"}]}})
    models = Task.objects.get(id="task1").models
    assert [m.model for m in models.input] == ["m-new"]
    assert [m.model for m in models.output] == ["m-out"]


def test_edit_hyperparams_replaces_whole_map(company, side_effects):  # E10
    make_root(company, members=[("u1", ProjectRole.read_write)])
    make_task(
        company,
        "root1",
        "u1",
        hyperparams={
            "A": {"a": {"section": "A", "name": "a", "value": "1"}},
            "B": {"b": {"section": "B", "name": "b", "value": "2"}},
        },
    )
    run_edit(
        company,
        "u1",
        {"task": "task1", "hyperparams": {"C": {"c": {"section": "C", "name": "c", "value": "3", "type": "str"}}}},
    )
    assert set(Task.objects.get(id="task1").hyperparams) == {"C"}


def test_edit_container_replaces_whole_map(company, side_effects):  # E11
    make_root(company, members=[("u1", ProjectRole.read_write)])
    make_task(company, "root1", "u1", container={"image": "i1", "arguments": "a"})
    run_edit(company, "u1", {"task": "task1", "container": {"image": "i2"}})
    assert Task.objects.get(id="task1").container == {"image": "i2"}


def test_edit_runtime_replaces_whole_map(company, side_effects):  # E12
    make_root(company, members=[("u1", ProjectRole.read_write)])
    make_task(company, "root1", "u1", runtime={"a": 1})
    run_edit(company, "u1", {"task": "task1", "runtime": {"b": 2}})
    assert Task.objects.get(id="task1").runtime == {"b": 2}


def test_edit_sets_last_update_only_for_non_user_fields(company, side_effects):  # E13
    setup_owned(company)
    run_edit(company, "u1", {"task": "task1", "name": "only name"})
    assert raw().get("last_update") is None
    run_edit(company, "u1", {"task": "task1", "type": "inference"})
    assert raw().get("last_update") is not None


def test_edit_move_resets_both_tag_caches_and_stamps_new_project_time(company, side_effects):  # E14
    make_root(company, id_="src", members=[("u2", ProjectRole.collaborator)])
    make_root(company, id_="dst", members=[("u2", ProjectRole.collaborator)])
    make_task(company, "src", "u2")
    run_edit(company, "u2", {"task": "task1", "project": "dst"})
    assert Task.objects.get(id="task1").project == "dst"
    assert side_effects.org.reset_tags.call_args.kwargs["projects"] == ["dst", "src"]
    side_effects.project_time.assert_called_once_with("dst")


def test_edit_move_without_admin_is_refused(company, side_effects):  # E15
    make_root(company, id_="src", members=[("u2", ProjectRole.read_write)])
    make_root(company, id_="dst", members=[("u2", ProjectRole.read_write)])
    make_task(company, "src", "u2")
    with pytest.raises(errors.forbidden.NoWritePermission):
        run_edit(company, "u2", {"task": "task1", "project": "dst"})
    assert Task.objects.get(id="task1").project == "src"
    assert_no_side_effects(side_effects)


def test_edit_with_nothing_to_write_returns_zero(company, side_effects):  # E16
    setup_owned(company)
    res = run_edit(company, "u1", {"task": "task1"})
    assert res.updated == 0
    assert raw().get("last_change") is None


def test_edit_stamps_model_updated_time(company, side_effects):  # E17
    setup_owned(company)
    make_model(company, "m1")
    run_edit(company, "u1", {"task": "task1", "models": {"input": [{"name": "n", "model": "m1"}]}})
    stored = Task.objects.get(id="task1").models.input[0]
    assert stored.updated is not None


def test_edit_refuses_an_unknown_input_model(company, side_effects):  # E18
    setup_owned(company)
    before = raw()
    with pytest.raises(errors.bad_request.InvalidModelId):
        run_edit(company, "u1", {"task": "task1", "models": {"input": [{"name": "n", "model": "missing"}]}})
    assert raw() == before


def test_edit_refuses_an_unknown_parent_and_changes_nothing(company, side_effects):  # E19
    setup_owned(company)
    # The parent exists, but in another company: the unscoped id check passes, the company check refuses it.
    # A parent id that exists nowhere is refused earlier by the id parser (FieldsValueError), not pinned here.
    make_task(f"c-{uuid4().hex[:8]}", None, "u9", id_="foreign-parent")
    before = raw()
    with pytest.raises(errors.bad_request.InvalidTaskId) as exc:
        run_edit(company, "u1", {"task": "task1", "parent": "foreign-parent"})
    assert exc.value.msg.startswith("Invalid task id (invalid parent)")
    assert raw() == before


def test_edit_output_dest_sets_destination_and_keeps_output_result(company, side_effects):  # E20
    make_root(company, members=[("u1", ProjectRole.read_write)])
    make_task(company, "root1", "u1", output=Output(result="success"))
    run_edit(company, "u1", {"task": "task1", "output_dest": "s3://x"})
    output = Task.objects.get(id="task1").output
    assert output.destination == "s3://x"
    assert output.result == "success"


def test_edit_type_key_is_written_as_is(company, side_effects):  # E21
    make_root(company, members=[("u1", ProjectRole.read_write)])
    make_task(company, "root1", "u1", type="inference")
    run_edit(company, "u1", {"task": "task1", "type": "training"})
    assert raw()["type"] == "training"


def test_edit_records_repository_only_when_written(company, side_effects):  # E22
    setup_owned(company, script=Script(repository="repo"))
    run_edit(company, "u1", {"task": "task1", "name": "renamed"})
    side_effects.record.assert_called_once_with(company, "u1", "task1")
    run_edit(company, "u1", {"task": "task1"})
    assert side_effects.record.call_count == 1


# suspect: pinned as is, see the decision issue (#282)
def test_edit_response_fields_show_merged_embedded_dicts(company, side_effects):  # E23
    make_root(company, members=[("u1", ProjectRole.read_write)])
    make_task(company, "root1", "u1", execution=Execution(framework="a"))
    res = run_edit(company, "u1", {"task": "task1", "execution": {"model": "m"}})
    merged = res.fields["execution"]
    assert merged["framework"] == "a"
    assert merged["model"] == "m"


# ---------------------------------------------------------------------------------------------------------------------
# C. Cross-endpoint cases
# ---------------------------------------------------------------------------------------------------------------------

SAME_USER_FIELDS = {"name": "same", "display_name": "Shown", "comment": "c", "tags": ["b", "a"], "system_tags": ["s"]}


def test_same_user_fields_give_the_same_document_through_update_and_edit(company, side_effects):  # X1
    make_root(company, members=[("u1", ProjectRole.read_write)])
    make_task(company, "root1", "u1", id_="task1")
    make_task(company, "root1", "u1", id_="task2")
    run_update(company, "u1", {"task": "task1", **SAME_USER_FIELDS})
    run_edit(company, "u1", {"task": "task2", **SAME_USER_FIELDS})

    stamps = {"_id", "id", "last_change", "last_changed_by", "last_update"}
    via_update = {k: v for k, v in raw("task1").items() if k not in stamps}
    via_edit = {k: v for k, v in raw("task2").items() if k not in stamps}
    assert via_update == via_edit
    assert raw("task1")["last_changed_by"] == raw("task2")["last_changed_by"] == "u1"


# suspect: pinned as is, see the decision issue (#282)
def test_same_user_fields_through_update_and_batch_differ_only_as_pinned(company, side_effects):  # X2
    make_root(company, members=[("u1", ProjectRole.read_write)])
    for id_ in ("task1", "task2", "task3", "task4"):
        make_task(company, "root1", "u1", id_=id_)

    # Name with surrounding spaces: update strips it, the batch write stores it as sent
    run_update(company, "u1", {"task": "task1", "name": "  abc  "})
    assert run_batch(company, "u1", [{"task": "task2", "name": "  abc  "}]) == {"updated": 1}
    assert raw("task1")["name"] == "abc"
    assert raw("task2")["name"] == "  abc  "

    # Display name over 200 characters: update refuses it, the batch write stores it
    with pytest.raises(errors.bad_request.DataValidationError):
        run_update(company, "u1", {"task": "task3", "display_name": "x" * 201})
    assert run_batch(company, "u1", [{"task": "task4", "display_name": "x" * 201}]) == {"updated": 1}
    assert raw("task3").get("display_name") is None
    assert raw("task4")["display_name"] == "x" * 201
