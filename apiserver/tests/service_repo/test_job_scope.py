"""A job token may use only its own task: its task, events and artifacts."""

from types import SimpleNamespace

import pytest

from apiserver.apierrors import errors
from apiserver.service_repo import job_scope
from apiserver.service_repo.job_scope import validate_job_scope

JOB_TASK = "task-of-this-job"


@pytest.fixture(autouse=True)
def no_children(monkeypatch):
    """By default the job started no task: the database is not read."""
    monkeypatch.setattr(job_scope, "_is_child", lambda task_id, job_task: False)
    monkeypatch.setattr(job_scope, "_is_sibling", lambda task_id, job_task: task_id == "sibling-step")


def _call(data=None, batched=None, job_task=JOB_TASK):
    """Mimics APICall: a plain body is also reported as a one-item batch, as APICall.batched_data does."""
    auth = None if job_task is None else SimpleNamespace(job_task=job_task, identity=SimpleNamespace(user="the-owner"))
    if batched is None:
        batched = [data] if data else []
    return SimpleNamespace(data=data or {}, batched_data=batched, auth=auth)


def _endpoint(name):
    return SimpleNamespace(name=name)


def test_a_token_that_is_not_a_job_token_is_not_limited():
    validate_job_scope(_endpoint("projects.get_all"), _call(job_task=None))


def test_the_job_may_read_and_write_its_own_task():
    validate_job_scope(_endpoint("tasks.started"), _call({"task": JOB_TASK}))


def test_the_job_may_not_name_another_task():
    with pytest.raises(errors.bad_request.InvalidTaskId):
        validate_job_scope(_endpoint("tasks.started"), _call({"task": "someone-else"}))


def test_a_task_request_that_names_no_task_is_refused():
    with pytest.raises(errors.bad_request.InvalidTaskId):
        validate_job_scope(_endpoint("tasks.get_all_ex"), _call({}))


def test_a_list_naming_another_task_is_refused():
    with pytest.raises(errors.bad_request.InvalidTaskId):
        validate_job_scope(_endpoint("tasks.get_all_ex"), _call({"tasks": [JOB_TASK, "other"]}))


@pytest.mark.parametrize("name", ["users.get_current_user", "devices.revoke",
                                  "queues.get_default", "auth.login", "workers.status_report"])
def test_other_services_are_refused(name):
    with pytest.raises(errors.forbidden.NoWritePermission):
        validate_job_scope(_endpoint(name), _call({"task": JOB_TASK}))


def test_events_of_the_own_task_are_accepted():
    batch = [{"task": JOB_TASK, "type": "training_debug_image"}, {"task": JOB_TASK, "type": "log"}]
    validate_job_scope(_endpoint("events.add_batch"), _call({}, batched=batch))


def test_an_event_for_another_task_is_refused():
    batch = [{"task": JOB_TASK}, {"task": "other"}]
    with pytest.raises(errors.bad_request.InvalidTaskId):
        validate_job_scope(_endpoint("events.add_batch"), _call({}, batched=batch))


def test_a_datachunk_is_checked_through_its_task(monkeypatch):
    monkeypatch.setattr(job_scope, "_chunk_task_id", lambda chunk_id: JOB_TASK if chunk_id == "mine" else "other")
    validate_job_scope(_endpoint("datachunks.commit"), _call({"id": "mine"}))
    with pytest.raises(errors.bad_request.InvalidTaskId):
        validate_job_scope(_endpoint("datachunks.commit"), _call({"id": "theirs"}))


def test_a_job_may_read_the_project_of_its_own_task(monkeypatch):
    monkeypatch.setattr(job_scope, "_task_project_id", lambda task_id: "its-project" if task_id == JOB_TASK else None)
    validate_job_scope(_endpoint("projects.get_by_id"), _call({"project": "its-project"}))


def test_a_job_may_not_read_another_project(monkeypatch):
    monkeypatch.setattr(job_scope, "_task_project_id", lambda task_id: "its-project")
    with pytest.raises(errors.bad_request.InvalidTaskId):
        validate_job_scope(_endpoint("projects.get_by_id"), _call({"project": "someone-elses"}))


def test_a_job_may_not_change_projects(monkeypatch):
    monkeypatch.setattr(job_scope, "_task_project_id", lambda task_id: "its-project")
    with pytest.raises(errors.forbidden.NoWritePermission):
        validate_job_scope(_endpoint("projects.update"), _call({"project": "its-project"}))



def test_a_plain_body_is_checked_through_its_chunk_not_as_an_event(monkeypatch):
    # a commit body names the chunk by id and has no task of its own; it is not a batch of events
    monkeypatch.setattr(job_scope, "_chunk_task_id", lambda chunk_id: JOB_TASK)
    validate_job_scope(_endpoint("datachunks.commit"), _call({"id": "chunk", "parts": []}))


def test_a_job_may_create_a_model_for_its_own_task():
    validate_job_scope(_endpoint("models.create"), _call({"task": JOB_TASK, "model": "m", "name": "n", "uri": "u"}))


def test_a_job_may_not_create_a_model_for_another_task():
    with pytest.raises(errors.bad_request.InvalidTaskId):
        validate_job_scope(_endpoint("models.create"), _call({"task": "other", "model": "m", "name": "n"}))


def test_a_job_may_read_and_edit_a_model_of_its_own_task(monkeypatch):
    monkeypatch.setattr(job_scope, "_model_belongs_to_job",
                        lambda model_id, job_task, owner: model_id == "mine")
    validate_job_scope(_endpoint("models.get_by_id"), _call({"model": "mine"}))
    validate_job_scope(_endpoint("models.edit"), _call({"model": "mine", "comment": "x"}))


def test_a_job_may_not_read_or_edit_another_tasks_model(monkeypatch):
    monkeypatch.setattr(job_scope, "_model_belongs_to_job",
                        lambda model_id, job_task, owner: model_id == "mine")
    with pytest.raises(errors.bad_request.InvalidTaskId):
        validate_job_scope(_endpoint("models.get_by_id"), _call({"model": "theirs"}))
    with pytest.raises(errors.bad_request.InvalidTaskId):
        validate_job_scope(_endpoint("models.edit"), _call({"model": "theirs"}))


def test_a_model_that_does_not_exist_is_refused_like_another_task(monkeypatch):
    monkeypatch.setattr(job_scope, "_model_belongs_to_job", lambda model_id, job_task, owner: False)
    with pytest.raises(errors.bad_request.InvalidTaskId):
        validate_job_scope(_endpoint("models.get_by_id"), _call({"model": "missing"}))


def test_a_job_may_not_list_all_models_or_use_other_model_actions(monkeypatch):
    monkeypatch.setattr(job_scope, "_model_belongs_to_job", lambda model_id, job_task, owner: True)
    with pytest.raises(errors.forbidden.NoWritePermission):
        validate_job_scope(_endpoint("models.get_all"), _call({"task": JOB_TASK}))
    with pytest.raises(errors.forbidden.NoWritePermission):
        validate_job_scope(_endpoint("models.delete"), _call({"model": "mine"}))


def test_a_job_may_create_an_anonymous_model_with_no_task_yet():
    # the SDK creates the model first and names its task only when the weights are attached
    validate_job_scope(_endpoint("models.create"), _call({"model": "m", "name": "Anonymous model", "labels": {}}))


def test_a_job_may_not_use_an_unlinked_model_made_by_someone_else():
    # a model with no task is the job's only when the job's owner made it
    from types import SimpleNamespace as NS
    from unittest import mock

    model = NS(task="", user="someone-else")
    with mock.patch("apiserver.database.model.model.Model.objects") as objects:
        objects.return_value.only.return_value.first.return_value = model
        assert job_scope._model_belongs_to_job("m", JOB_TASK, "the-owner") is False
        model.user = "the-owner"
        assert job_scope._model_belongs_to_job("m", JOB_TASK, "the-owner") is True


def test_a_job_may_read_the_status_of_its_own_task():
    # the SDK reads its own task's status with tasks.get_all and an id list
    validate_job_scope(_endpoint("tasks.get_all"), _call({"id": [JOB_TASK], "only_fields": ["status"]}))


def test_a_job_may_not_read_the_status_of_another_task():
    with pytest.raises(errors.bad_request.InvalidTaskId):
        validate_job_scope(_endpoint("tasks.get_all"), _call({"id": [JOB_TASK, "other"]}))


def test_a_job_may_get_the_repository_token_for_its_own_task():
    validate_job_scope(_endpoint("devices.git_token"), _call({"task": JOB_TASK}))


def test_a_job_may_not_get_the_repository_token_for_another_task():
    with pytest.raises(errors.bad_request.InvalidTaskId):
        validate_job_scope(_endpoint("devices.git_token"), _call({"task": "someone-else"}))


def test_a_job_may_not_use_other_devices_endpoints():
    with pytest.raises(errors.forbidden.NoWritePermission):
        validate_job_scope(_endpoint("devices.enroll"), _call({"task": JOB_TASK}))


# an orchestrating job (a pipeline controller, a search) starts tasks of its own


@pytest.fixture
def children(monkeypatch):
    """The tasks the job started: their parent is the job's task."""
    monkeypatch.setattr(job_scope, "_is_child", lambda task_id, job_task: task_id in ("child-1", "child-2"))
    monkeypatch.setattr(job_scope, "_project_name_of", lambda job_task: "examples")


def test_a_task_the_job_creates_gets_the_job_as_parent(monkeypatch):
    monkeypatch.setattr(job_scope, "_project_in_tree", lambda project, job_task: project == "p-examples")
    call = _call({"project": "p-examples", "name": "step", "parent": "someone-else"})
    validate_job_scope(_endpoint("tasks.create"), call)
    assert call.data["parent"] == JOB_TASK


def test_the_job_may_not_create_a_task_in_another_project(monkeypatch):
    monkeypatch.setattr(job_scope, "_project_in_tree", lambda project, job_task: project == "p-examples")
    for body in ({"project": "other"}, {"name": "step"}):
        with pytest.raises(errors.bad_request.InvalidTaskId):
            validate_job_scope(_endpoint("tasks.create"), _call(body))


def test_the_job_may_clone_its_own_or_a_child_task_but_not_another(monkeypatch, children):
    monkeypatch.setattr(job_scope, "_project_in_tree", lambda project, job_task: True)
    monkeypatch.setattr(job_scope, "_task_project_id", lambda job_task: "p-examples")
    call = _call({"task": "child-1"})
    validate_job_scope(_endpoint("tasks.clone"), call)
    assert call.data["parent"] == JOB_TASK
    with pytest.raises(errors.bad_request.InvalidTaskId):
        validate_job_scope(_endpoint("tasks.clone"), _call({"task": "someone-elses"}))


def test_the_job_may_enqueue_and_read_the_tasks_it_started(children):
    validate_job_scope(_endpoint("tasks.enqueue"), _call({"task": "child-1"}))
    validate_job_scope(_endpoint("tasks.get_all_ex"), _call({"id": ["child-1", JOB_TASK]}))


def test_the_job_may_not_reach_a_task_it_did_not_start(children):
    with pytest.raises(errors.bad_request.InvalidTaskId):
        validate_job_scope(_endpoint("tasks.enqueue"), _call({"task": "someone-elses"}))
    with pytest.raises(errors.bad_request.InvalidTaskId):
        validate_job_scope(_endpoint("tasks.get_all_ex"), _call({"id": ["child-1", "someone-elses"]}))


@pytest.mark.parametrize("name", ["projects.get_all", "projects.create"])
def test_the_job_may_use_its_project_and_the_pipelines_below_it(children, name):
    validate_job_scope(_endpoint(name), _call({"name": "^examples/\\.pipelines/greenhouse\\ workflow$"}))
    validate_job_scope(_endpoint(name), _call({"name": "examples"}))


@pytest.mark.parametrize("name", ["projects.get_all", "projects.create"])
def test_the_job_may_not_list_or_create_other_projects(children, name):
    for body in ({}, {"name": "secret-project"}, {"name": "^other/.pipelines/x$"}):
        with pytest.raises(errors.forbidden.NoWritePermission):
            validate_job_scope(_endpoint(name), _call(body))


def test_the_job_may_look_up_a_queue_by_name_but_not_list_them():
    validate_job_scope(_endpoint("queues.get_all"), _call({"name": "parg-nb-f4cad6"}))
    with pytest.raises(errors.forbidden.NoWritePermission):
        validate_job_scope(_endpoint("queues.get_all"), _call({}))


def test_a_step_may_read_but_not_change_the_step_next_to_it():
    validate_job_scope(_endpoint("tasks.get_by_id_ex"), _call({"task": "sibling-step"}))
    for name in ("tasks.edit", "tasks.enqueue", "tasks.delete", "tasks.stop"):
        with pytest.raises(errors.bad_request.InvalidTaskId):
            validate_job_scope(_endpoint(name), _call({"task": "sibling-step"}))
    with pytest.raises(errors.bad_request.InvalidTaskId):
        validate_job_scope(_endpoint("tasks.get_by_id_ex"), _call({"task": "another-pipelines-step"}))


def test_a_step_may_download_the_artifact_of_the_step_next_to_it(monkeypatch):
    validate_job_scope(_endpoint("datachunks.prepare_download"), _call({"task": "sibling-step", "kind": "artifact", "name": "w"}))
    monkeypatch.setattr(job_scope, "_chunk_task_id", lambda chunk_id: "sibling-step")
    validate_job_scope(_endpoint("datachunks.prepare_download"), _call({"id": "chunk-1"}))
    with pytest.raises(errors.bad_request.InvalidTaskId):
        validate_job_scope(_endpoint("datachunks.prepare_upload"), _call({"task": "sibling-step"}))


def test_a_job_reads_the_data_of_its_own_task_its_children_and_its_steps_only(monkeypatch):
    monkeypatch.setattr(job_scope, "_is_child", lambda task_id, job_task: task_id == "child")
    monkeypatch.setattr(job_scope, "_is_sibling", lambda task_id, job_task: task_id == "sibling-step")
    for task in (JOB_TASK, "child", "sibling-step"):
        assert job_scope.may_read_task_data(JOB_TASK, task)
    assert not job_scope.may_read_task_data(JOB_TASK, "someone-elses")
