"""A job token may use only its own task: its task, events and artifacts."""

from types import SimpleNamespace

import pytest

from apiserver.apierrors import errors
from apiserver.service_repo import job_scope
from apiserver.service_repo.job_scope import validate_job_scope

JOB_TASK = "task-of-this-job"


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


@pytest.mark.parametrize("name", ["projects.get_all", "users.get_current_user", "devices.revoke",
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
