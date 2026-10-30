"""A job token may use only its own task: its task, events and artifacts."""

from types import SimpleNamespace

import pytest

from apiserver.apierrors import errors
from apiserver.service_repo import job_scope
from apiserver.service_repo.job_scope import validate_job_scope

JOB_TASK = "task-of-this-job"


def _call(data=None, batched=None, job_task=JOB_TASK):
    auth = None if job_task is None else SimpleNamespace(job_task=job_task)
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


@pytest.mark.parametrize("name", ["projects.get_all", "models.create", "users.get_current_user", "devices.revoke",
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
