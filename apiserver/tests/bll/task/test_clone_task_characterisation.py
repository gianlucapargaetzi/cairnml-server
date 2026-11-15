"""Characterisation tests for TaskBLL.clone_task: they pin what the function writes, returns and refuses today.

They are meant to stay green while clone_task is split into smaller steps. Behaviour groups (each has a test below):
name/comment/tags fallbacks, system tag cleaning, queue and output artifacts, hyperparams, configuration,
script overrides, execution overrides (model, docker_cmd, model_labels), container, input models, parent folder
root, validation, project resolution (new_project_name), tag update, project time, source lookup.
Oddities are pinned as they are (for example the caller's override dicts are changed in place).
"""
from datetime import datetime, timezone
from unittest import mock
from uuid import uuid4

import pytest

from apiserver.apierrors import errors
from apiserver.apimodels.tasks import TaskInputModel
from apiserver.bll.task import task_bll as task_bll_module
from apiserver.bll.task.task_bll import TaskBLL
from apiserver.database.model.model import Model
from apiserver.database.model.project import Project
from apiserver.database.model.task.task import (
    Artifact,
    Execution,
    ModelItem,
    Models,
    Output,
    Script,
    Task,
    TaskStatus,
)
from apiserver.service_repo.auth import Identity
from apiserver.database.model.auth import Role
from apiserver.tests.db import ensure_db

ensure_db()


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
    return f"c-{uuid4().hex[:8]}"


@pytest.fixture
def stubs():
    """Recording stubs for the two side effects that leave the task collection."""
    with mock.patch.object(task_bll_module, "org_bll") as org, mock.patch.object(
        task_bll_module, "update_project_time"
    ) as project_time:
        yield mock.Mock(org=org, project_time=project_time)


def new_id(prefix="x"):
    return f"{prefix}-{uuid4().hex[:8]}"


def make_project(company, id_=None, name=None):
    id_ = id_ or new_id("p")
    Project(
        id=id_, company=company, user="u1", name=name or id_, basename=name or id_,
        created=datetime.utcnow(),
    ).save()
    return id_


def make_model(company, id_=None):
    id_ = id_ or new_id("m")
    Model(
        id=id_, company=company, user="u1", name=id_, uri="s3://m", created=datetime.utcnow(),
        ready=True,
    ).save()
    return id_


def make_source(company, **kw):
    """A fully populated source task."""
    project = kw.pop("project", None) or make_project(company)
    fields = dict(
        id=new_id("src"),
        company=company,
        user="owner",
        name="source name",
        comment="source comment",
        type="training",
        project=project,
        status=TaskStatus.published,
        created=datetime(2020, 1, 1),
        tags=["t1", "t2"],
        system_tags=["development", "archived", "keep"],
        script=Script(repository="repo", entry_point="main.py", working_dir="."),
        output=Output(destination="s3://out"),
        execution=Execution(
            queue="queue-1",
            framework="torch",
            artifacts={
                "in": Artifact(key="in", type="t", mode="input"),
                "out": Artifact(key="out", type="t", mode="output"),
            },
            model_labels={"a": 1},
        ),
        hyperparams={
            "General": {"lr": {"section": "General", "name": "lr", "value": "0.1"}},
        },
        configuration={"cfg": {"name": "cfg", "value": "x"}},
        models=Models(input=[ModelItem(name="in", model="old-model")]),
        container={"image": "img", "arguments": "args"},
    )
    fields.update(kw)
    task = Task(**fields)
    task.save()
    return task


def identity(company):
    return Identity(user="cloner", company=company, role=Role.user)


def clone(source, company=None, **kw):
    """The only place that calls TaskBLL.clone_task."""
    company = company or source.company
    return TaskBLL.clone_task(
        company_id=company,
        user_id="cloner",
        identity=identity(company),
        task_id=source.id,
        **kw,
    )


def stored(task):
    return Task.objects(id=task.id).first()


# --- what is copied -------------------------------------------------------


def test_copies_the_fields_of_the_source(company, stubs):
    src = make_source(company)
    new, new_project = clone(src)
    row = stored(new)
    assert new_project is None
    assert row.name == "source name" and row.comment == "source comment"
    assert row.type == "training" and row.project == src.project
    assert row.tags == ["t1", "t2"]
    assert row.script.repository == "repo" and row.script.entry_point == "main.py"
    assert row.execution.framework == "torch"
    assert row.hyperparams["General"]["lr"].value == "0.1"
    assert row.configuration["cfg"].value == "x"
    assert row.container["image"] == "img"
    assert [m.model for m in row.models.input] == ["old-model"]


def test_new_task_has_its_own_identity_and_a_fresh_created_status(company, stubs):
    src = make_source(company)
    new, _ = clone(src)
    row = stored(new)
    assert row.id != src.id
    assert row.company == company and row.user == "cloner" and row.last_changed_by == "cloner"
    assert row.status == TaskStatus.created
    assert row.created.year >= 2026 and row.created == row.last_update == row.last_change
    assert row.started is None and row.completed is None and row.published is None


def test_the_source_is_not_modified(company, stubs):
    src = make_source(company)
    before = Task.objects(id=src.id).first().to_proper_dict()
    clone(
        src,
        name="new",
        tags=["x"],
        hyperparams_overrides={"General": {"lr": {"section": "General", "name": "lr", "value": "9"}}},
    )
    assert Task.objects(id=src.id).first().to_proper_dict() == before


def test_the_output_destination_is_copied(company, stubs):
    new, _ = clone(make_source(company))
    assert stored(new).output.destination == "s3://out"


def test_a_source_without_output_gives_no_output(company, stubs):
    new, _ = clone(make_source(company, output=None))
    assert stored(new).output is None or stored(new).output.destination is None


def test_queue_is_dropped_and_output_artifacts_are_not_copied(company, stubs):
    new, _ = clone(make_source(company))
    row = stored(new)
    assert row.execution.queue is None
    assert set(row.execution.artifacts) == {"in"}


def test_artifacts_without_a_mode_default_to_output_and_are_not_copied(company, stubs):
    src = make_source(
        company,
        execution=Execution(artifacts={"plain": Artifact(key="plain", type="t")}),
    )
    new, _ = clone(src)
    assert not stored(new).execution.artifacts


# --- name, comment, tags --------------------------------------------------


def test_given_name_comment_tags_win(company, stubs):
    new, _ = clone(make_source(company), name="new name", comment="c", tags=["g"], system_tags=["s"])
    row = stored(new)
    assert (row.name, row.comment, row.tags, row.system_tags) == ("new name", "c", ["g"], ["s"])


@pytest.mark.parametrize("empty", [None, "", []])
def test_empty_name_comment_tags_fall_back_to_the_source(company, stubs, empty):
    src = make_source(company)
    new, _ = clone(
        src,
        name=empty if isinstance(empty, (str, type(None))) else None,
        comment=empty if isinstance(empty, (str, type(None))) else None,
        tags=empty if isinstance(empty, list) else None,
        system_tags=empty if isinstance(empty, list) else None,
    )
    row = stored(new)
    assert (row.name, row.comment, row.tags) == ("source name", "source comment", ["t1", "t2"])
    assert row.system_tags == ["keep"]


def test_development_and_archived_are_removed_from_copied_system_tags(company, stubs):
    new, _ = clone(make_source(company))
    assert stored(new).system_tags == ["keep"]


def test_development_and_archived_are_kept_when_given(company, stubs):
    new, _ = clone(make_source(company), system_tags=["development", "archived"])
    assert stored(new).system_tags == ["archived", "development"]


def test_a_source_without_system_tags_gives_none(company, stubs):
    new, _ = clone(make_source(company, system_tags=[]))
    assert stored(new).system_tags == []


# --- hyperparams ----------------------------------------------------------


def test_given_hyperparams_replace_the_source_ones(company, stubs):
    hp = {"Other": {"x": {"section": "Other", "name": "x", "value": "1"}}}
    new, _ = clone(make_source(company), hyperparams=hp)
    row = stored(new)
    assert set(row.hyperparams) == {"Other"}


def test_hyperparams_overrides_merge_per_section_and_key(company, stubs):
    src = make_source(
        company,
        hyperparams={
            "General": {
                "lr": {"section": "General", "name": "lr", "value": "0.1"},
                "bs": {"section": "General", "name": "bs", "value": "8"},
            }
        },
    )
    overrides = {
        "General": {"lr": {"section": "General", "name": "lr", "value": "0.5"}},
        "New": {"k": {"section": "New", "name": "k", "value": "v"}},
    }
    new, _ = clone(src, hyperparams_overrides=overrides)
    hp = stored(new).hyperparams
    assert hp["General"]["lr"].value == "0.5"
    assert hp["General"]["bs"].value == "8"
    assert hp["New"]["k"].value == "v"


def test_hyperparams_overrides_on_a_source_without_hyperparams(company, stubs):
    src = make_source(company, hyperparams=None)
    new, _ = clone(
        src, hyperparams_overrides={"S": {"k": {"section": "S", "name": "k", "value": "v"}}}
    )
    assert stored(new).hyperparams["S"]["k"].value == "v"


def test_given_hyperparams_win_over_overrides(company, stubs):
    hp = {"A": {"a": {"section": "A", "name": "a", "value": "1"}}}
    ov = {"B": {"b": {"section": "B", "name": "b", "value": "2"}}}
    new, _ = clone(make_source(company), hyperparams=hp, hyperparams_overrides=ov)
    assert set(stored(new).hyperparams) == {"A"}


def test_hyperparams_section_and_key_names_are_escaped(company, stubs):
    hp = {"a.b": {"c.d": {"section": "a.b", "name": "c.d", "value": "1"}}}
    new, _ = clone(make_source(company), hyperparams=hp)
    raw = Task._get_collection().find_one({"_id": new.id})
    assert "a.b" not in raw["hyperparams"]
    assert stored(new).hyperparams  # one section, readable


# --- configuration --------------------------------------------------------


def test_given_configuration_replaces_the_source_one(company, stubs):
    new, _ = clone(make_source(company), configuration={"n": {"name": "n", "value": "1"}})
    assert set(stored(new).configuration) == {"n"}


def test_configuration_overrides_merge_by_key(company, stubs):
    src = make_source(
        company,
        configuration={"a": {"name": "a", "value": "1"}, "b": {"name": "b", "value": "2"}},
    )
    new, _ = clone(
        src,
        configuration_overrides={"a": {"name": "a", "value": "9"}, "c": {"name": "c", "value": "3"}},
    )
    cfg = stored(new).configuration
    assert {k: v.value for k, v in cfg.items()} == {"a": "9", "b": "2", "c": "3"}


def test_given_configuration_wins_over_overrides(company, stubs):
    new, _ = clone(
        make_source(company),
        configuration={"g": {"name": "g", "value": "1"}},
        configuration_overrides={"o": {"name": "o", "value": "2"}},
    )
    assert set(stored(new).configuration) == {"g"}


# --- script ---------------------------------------------------------------


def test_script_overrides_are_stripped_and_applied(company, stubs):
    new, _ = clone(
        make_source(company), script_overrides={"repository": "  other  ", "branch": " dev "}
    )
    script = stored(new).script
    assert script.repository == "other" and script.branch == "dev"
    assert script.entry_point == "main.py"


def test_script_overrides_are_changed_in_place(company, stubs):
    overrides = {"repository": "  other  "}
    clone(make_source(company), script_overrides=overrides)
    assert overrides == {"repository": "other"}


def test_a_source_without_script_clones_with_the_overrides_only(company, stubs):
    src = make_source(company, script=None)
    new, _ = clone(src, script_overrides={"repository": "r"})
    assert stored(new).script.repository == "r"


# --- execution overrides --------------------------------------------------


def test_execution_overrides_update_the_execution(company, stubs):
    new, _ = clone(make_source(company), execution_overrides={"framework": "tf"})
    row = stored(new)
    assert row.execution.framework == "tf"
    assert row.execution.model_labels == {"a": 1}


def test_execution_model_becomes_the_input_model_when_none_given(company, stubs):
    overrides = {"model": make_model(company)}
    model = overrides["model"]
    new, _ = clone(make_source(company), execution_overrides=overrides)
    row = stored(new)
    assert [(m.model, m.name) for m in row.models.input] == [(model, "Input Model")]
    assert row.models.input[0].updated is not None
    assert "model" not in overrides


def test_execution_model_is_ignored_when_input_models_are_given(company, stubs):
    model = make_model(company)
    new, _ = clone(
        make_source(company),
        execution_overrides={"model": "model-9"},
        input_models=[TaskInputModel(name="n", model=model)],
    )
    assert [m.model for m in stored(new).models.input] == [model]


def test_docker_cmd_splits_into_image_and_arguments(company, stubs):
    overrides = {"docker_cmd": "img:1 --gpus all"}
    new, _ = clone(make_source(company), execution_overrides=overrides)
    assert dict(stored(new).container) == {"image": "img:1", "arguments": "--gpus all"}
    assert "docker_cmd" not in overrides


def test_docker_cmd_without_arguments(company, stubs):
    new, _ = clone(make_source(company), execution_overrides={"docker_cmd": "img"})
    assert dict(stored(new).container) == {"image": "img", "arguments": ""}


def test_docker_cmd_is_ignored_when_a_container_is_given(company, stubs):
    new, _ = clone(
        make_source(company),
        execution_overrides={"docker_cmd": "img:1 a"},
        container={"image": "given"},
    )
    assert dict(stored(new).container) == {"image": "given"}


def test_model_labels_are_stored_as_ints(company, stubs):
    new, _ = clone(make_source(company), execution_overrides={"model_labels": {"x": "3"}})
    assert dict(stored(new).execution.model_labels) == {"x": 3}


def test_model_labels_keys_are_escaped_in_the_stored_row(company, stubs):
    new, _ = clone(make_source(company), execution_overrides={"model_labels": {"a.b": 1}})
    raw = Task._get_collection().find_one({"_id": new.id})
    assert "a.b" not in raw["execution"]["model_labels"]


def test_model_labels_of_the_source_are_kept_as_ints(company, stubs):
    new, _ = clone(make_source(company))
    assert dict(stored(new).execution.model_labels) == {"a": 1}


def test_legacy_execution_parameters_and_configuration_have_no_effect_on_the_task(company, stubs):
    overrides = {"parameters": {"a": "1"}, "configuration": {"b": "2"}}
    src = make_source(company)
    new, _ = clone(src, execution_overrides=overrides)
    row = stored(new)
    assert set(row.hyperparams) == {"General"}
    assert set(row.configuration) == {"cfg"}
    assert overrides == {}


def test_execution_overrides_artifacts_are_prepared_and_output_ones_dropped(company, stubs):
    new, _ = clone(
        make_source(company),
        execution_overrides={"artifacts": [{"key": "k", "type": "t", "mode": "output"}]},
    )
    arts = stored(new).execution.artifacts
    assert "k" not in (arts or {})


# --- container, input models ----------------------------------------------


def test_given_container_is_escaped_and_wins(company, stubs):
    new, _ = clone(make_source(company), container={"image": "i", "a.b": "c"})
    raw = Task._get_collection().find_one({"_id": new.id})
    assert "a.b" not in raw["container"]
    assert stored(new).container["image"] == "i"


def test_container_falls_back_to_the_source(company, stubs):
    new, _ = clone(make_source(company), container={})
    assert dict(stored(new).container) == {"image": "img", "arguments": "args"}


def test_given_input_models_are_stamped_and_win(company, stubs):
    model = make_model(company)
    new, _ = clone(make_source(company), input_models=[TaskInputModel(name="n", model=model)])
    row = stored(new)
    assert [(m.model, m.name) for m in row.models.input] == [(model, "n")]
    assert row.models.input[0].updated is not None


def test_input_models_fall_back_to_the_source(company, stubs):
    new, _ = clone(make_source(company), input_models=[])
    assert [m.model for m in stored(new).models.input] == ["old-model"]


# --- parent ---------------------------------------------------------------


def test_the_parent_is_the_folder_root_of_the_source(company, stubs):
    root = make_source(company)
    mid = make_source(company, parent=root.id)
    new, _ = clone(mid)
    assert stored(new).parent == root.id


def test_the_source_is_the_parent_when_it_has_none(company, stubs):
    src = make_source(company)
    new, _ = clone(src)
    assert stored(new).parent == src.id


def test_a_given_parent_is_resolved_to_its_root(company, stubs):
    root = make_source(company)
    mid = make_source(company, parent=root.id)
    src = make_source(company)
    new, _ = clone(src, parent=mid.id)
    assert stored(new).parent == root.id


# --- validation -----------------------------------------------------------


def test_a_missing_input_model_of_the_source_is_refused_with_validate_references(company, stubs):
    src = make_source(company)
    before = Task.objects(company=company).count()
    with pytest.raises(errors.bad_request.InvalidModelId):
        clone(src, validate_references=True)
    assert Task.objects(company=company).count() == before


def test_a_given_missing_input_model_is_always_refused(company, stubs):
    src = make_source(company)
    before = Task.objects(company=company).count()
    with pytest.raises(errors.bad_request.InvalidModelId):
        clone(src, input_models=[TaskInputModel(name="n", model="nope")])
    assert Task.objects(company=company).count() == before


def test_a_missing_given_parent_is_refused_only_when_validated(company, stubs):
    src = make_source(company)
    with pytest.raises(errors.bad_request.InvalidTaskId):
        clone(src, parent="nope")
    assert Task.objects(company=company).count() == 1


def test_a_missing_given_project_is_refused(company, stubs):
    src = make_source(company)
    with pytest.raises(errors.bad_request.InvalidProjectId):
        clone(src, project="no-project")
    assert Task.objects(company=company).count() == 1


def test_a_given_project_is_used(company, stubs):
    src = make_source(company)
    other = make_project(company)
    new, _ = clone(src, project=other)
    assert stored(new).project == other


def test_a_source_project_that_is_gone_is_refused_only_with_validate_references(company, stubs):
    src = make_source(company, project="gone-project")
    new, _ = clone(src)
    assert stored(new).project == "gone-project"
    with pytest.raises(errors.bad_request.InvalidProjectId):
        clone(src, validate_references=True)


def test_invalid_tags_are_refused_before_anything_is_read(company, stubs):
    src = make_source(company)
    with pytest.raises(errors.bad_request.FieldsValueError):
        clone(src, tags=["__$or"])
    stubs.project_time.assert_not_called()


def test_a_missing_source_is_refused(company, stubs):
    src = make_source(company)
    src.id = "no-such-task"
    with pytest.raises(errors.bad_request.InvalidTaskId):
        clone(src)


def test_a_public_task_of_another_company_can_be_cloned(company, stubs):
    other = f"other-{uuid4().hex[:6]}"
    src = make_source(other, system_tags=["public"])
    Task.objects(id=src.id).update(company="")
    new, _ = clone(src, company=company)
    assert stored(new).company == company


def test_a_private_task_of_another_company_is_refused(company, stubs):
    src = make_source(f"other-{uuid4().hex[:6]}")
    with pytest.raises(errors.bad_request.InvalidTaskId):
        clone(src, company=company)


# --- new project ----------------------------------------------------------


def test_new_project_name_finds_or_creates_the_project(company, stubs):
    src = make_source(company)
    with mock.patch.object(task_bll_module.ProjectBLL, "find_or_create", return_value="pid") as foc:
        with mock.patch.object(TaskBLL, "validate"):
            new, data = clone(src, new_project_name="Proj/Sub")
    assert data == {"id": "pid", "name": "Proj/Sub"}
    assert stored(new).project == "pid"
    kwargs = foc.call_args.kwargs
    assert kwargs["project_name"] == "Proj/Sub" and kwargs["user"] == "cloner"
    assert kwargs["company"] == company and kwargs["description"] == ""


def test_new_project_name_really_creates_a_project(company, stubs):
    src = make_source(company)
    new, data = clone(src, new_project_name=f"Proj-{company}")
    assert Project.objects(id=data["id"], company=company).first().name == f"Proj-{company}"
    assert stored(new).project == data["id"]


def test_project_wins_over_new_project_name(company, stubs):
    src = make_source(company)
    other = make_project(company)
    new, data = clone(src, project=other, new_project_name="ignored")
    assert data is None and stored(new).project == other
    assert Project.objects(company=company, name="ignored").first() is None


# --- tags and project time ------------------------------------------------


def test_update_tags_gets_the_given_tags_when_the_project_is_unchanged(company, stubs):
    src = make_source(company)
    new, _ = clone(src, tags=["g"], system_tags=["s"])
    stubs.org.update_tags.assert_called_once()
    args, kwargs = stubs.org.update_tags.call_args
    assert args[0] == company
    assert kwargs == dict(projects=[src.project], tags=["g"], system_tags=["s"])


def test_update_tags_gets_none_when_nothing_was_given_and_the_project_is_unchanged(company, stubs):
    src = make_source(company)
    clone(src)
    kwargs = stubs.org.update_tags.call_args.kwargs
    assert kwargs["tags"] is None and kwargs["system_tags"] is None


def test_update_tags_gets_the_new_tasks_tags_when_the_project_changes(company, stubs):
    src = make_source(company)
    other = make_project(company)
    new, _ = clone(src, project=other)
    kwargs = stubs.org.update_tags.call_args.kwargs
    assert kwargs["projects"] == [other]
    assert kwargs["tags"] == ["t1", "t2"] and kwargs["system_tags"] == ["keep"]


def test_the_project_time_is_updated_once_for_the_new_project(company, stubs):
    src = make_source(company)
    other = make_project(company)
    clone(src, project=other)
    stubs.project_time.assert_called_once_with(other)


def test_nothing_is_tagged_or_touched_when_validation_fails(company, stubs):
    src = make_source(company)
    with pytest.raises(errors.bad_request.InvalidProjectId):
        clone(src, project="no-project")
    stubs.org.update_tags.assert_not_called()
    stubs.project_time.assert_not_called()
