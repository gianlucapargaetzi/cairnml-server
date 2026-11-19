# apiserver/tests/bll/task/test_task_events_cleanup_characterisation.py
"""Characterisation tests for the event cleanup helper bll.task.task_cleanup.delete_task_events_for_cleanup.

They record what the helper does today, including the odd behaviour, so that a later move of the helper cannot change
it silently. Every collaborator is replaced by a recorder, so these tests need no Elasticsearch and no storage.
Behaviour that looks unintended is pinned as it is and marked with the comment below (decision issue #282).
"""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from apiserver.apimodels.tasks import DeleteManyRequest, ResetManyRequest, ResetRequest
from apiserver.bll.project import entity_access
from apiserver.bll.task import task_cleanup
from apiserver.bll.task.task_cleanup import CleanupResult, TaskUrls
from apiserver.database.model.auth import Role
from apiserver.service_repo.auth import Identity
from apiserver.services import e2e as e2e_service
from apiserver.services import pipelines as pipelines_service
from apiserver.services import tasks as svc
from apiserver.tests.db import ensure_db
from apiserver.utilities.partial_version import PartialVersion

ensure_db()


@pytest.fixture
def company():
    return f"c-{uuid4().hex[:8]}"


class FakeConfig:
    """Stands in for the configuration object the helper reads at call time"""

    def __init__(self, values: dict):
        self.values = values

    def get(self, key, default=None):
        return self.values.get(key, default)


class Recorder:
    """Records each call and returns a fresh set, as the real collaborators do"""

    def __init__(self, urls=()):
        self.calls = []
        self.urls = set(urls)

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return set(self.urls)


def cleanup(model_urls=(), artifact_urls=(), deleted_model_ids=()) -> CleanupResult:
    return CleanupResult(
        updated_children=0,
        updated_models=0,
        deleted_models=0,
        deleted_model_ids=set(deleted_model_ids),
        urls=TaskUrls(model_urls=list(model_urls), artifact_urls=list(artifact_urls)),
    )


class FakeCall:
    def __init__(self, ident, data=None):
        self.identity = ident
        self.data = data if data is not None else {}
        self.batched_data = None
        self.requested_endpoint_version = PartialVersion("2.30")
        self.result = SimpleNamespace(data=None, data_model=None)


@pytest.fixture
def recorders(monkeypatch):
    """Replaces every collaborator of the helper and returns the recorders by name"""
    rec = SimpleNamespace(
        schedule=Recorder(),
        collect=Recorder(urls={"ev://x"}),
        event_delete=Recorder(),
    )
    monkeypatch.setattr(task_cleanup, "schedule_for_delete", rec.schedule)
    monkeypatch.setattr(task_cleanup, "delete_task_events_and_collect_urls", rec.collect)
    monkeypatch.setattr(task_cleanup.event_bll, "delete_task_events", rec.event_delete)
    monkeypatch.setattr(task_cleanup, "config", FakeConfig({"services.async_urls_delete.enabled": True}))
    return rec


def disable_async_urls_delete(monkeypatch):
    monkeypatch.setattr(task_cleanup, "config", FakeConfig({"services.async_urls_delete.enabled": False}))


def call_helper(tasks, delete_external_artifacts=True, sync_delete=True):
    task_cleanup.delete_task_events_for_cleanup(
        company_id="c",
        user_id="u",
        tasks=tasks,
        delete_external_artifacts=delete_external_artifacts,
        sync_delete=sync_delete,
    )


def test_empty_mapping_does_nothing(recorders):
    # E1
    call_helper({}, delete_external_artifacts=True, sync_delete=True)
    assert recorders.schedule.calls == []
    assert recorders.event_delete.calls == []
    assert recorders.collect.calls == []


def test_external_branch_schedules_urls_per_task_and_event_urls_under_first_task(recorders):
    # E2
    tasks = {
        "t1": cleanup(model_urls=["datachunk://m1"], artifact_urls=["datachunk://a1"]),
        "t2": cleanup(model_urls=["datachunk://m2"]),
    }
    call_helper(tasks, delete_external_artifacts=True, sync_delete=True)

    scheduled = [kwargs for _, kwargs in recorders.schedule.calls]
    per_task = [(k["task_id"], k["urls"]) for k in scheduled[:-1]]
    assert per_task == [
        ("t1", {"datachunk://m1", "datachunk://a1"}),
        ("t2", {"datachunk://m2"}),
    ]
    # the event urls returned by the collector go to the first task
    assert scheduled[-1]["task_id"] == "t1"
    assert scheduled[-1]["urls"] == {"ev://x"}

    assert len(recorders.collect.calls) == 1
    _, collect_kwargs = recorders.collect.calls[0]
    assert collect_kwargs["task_ids"] == ["t1", "t2"]
    assert collect_kwargs["wait_for_delete"] is True


def test_external_branch_runs_model_event_delete_only_when_deleted_model_ids_exist(recorders):
    # E3
    call_helper({"t1": cleanup(deleted_model_ids=["m1"])}, delete_external_artifacts=True, sync_delete=True)

    assert len(recorders.collect.calls) == 2
    _, first = recorders.collect.calls[0]
    _, second = recorders.collect.calls[1]
    assert first["task_ids"] == ["t1"]
    assert first.get("model", False) is False
    assert second["task_ids"] == ["m1"]
    assert second["model"] is True


def test_disabled_config_skips_scheduling_and_drops_urls(recorders, monkeypatch):
    # suspect: pinned as is, see the decision issue (#282)
    disable_async_urls_delete(monkeypatch)
    tasks = {"t1": cleanup(model_urls=["datachunk://m1"], artifact_urls=["datachunk://a1"])}
    call_helper(tasks, delete_external_artifacts=True, sync_delete=True)

    # the model and artifact urls are dropped without a trace
    # suspect: pinned as is, see the decision issue (#282)
    assert recorders.schedule.calls == []
    assert recorders.collect.calls == []
    assert recorders.event_delete.calls == [(("c", ["t1"]), {"wait_for_delete": True})]


def test_delete_external_artifacts_false_matches_disabled_config(recorders):
    # E5: the request flag and the config flag take the same branch
    tasks = {"t1": cleanup(model_urls=["datachunk://m1"], artifact_urls=["datachunk://a1"])}
    call_helper(tasks, delete_external_artifacts=False, sync_delete=True)

    assert recorders.schedule.calls == []
    assert recorders.collect.calls == []
    assert recorders.event_delete.calls == [(("c", ["t1"]), {"wait_for_delete": True})]


def test_sync_flag_is_passed_through_to_event_bll(recorders, monkeypatch):
    # E6
    disable_async_urls_delete(monkeypatch)
    call_helper({"t1": cleanup()}, delete_external_artifacts=True, sync_delete=False)

    assert recorders.event_delete.calls == [(("c", ["t1"]), {"wait_for_delete": False})]


def test_reset_uses_sync_delete_and_reset_many_does_not(company, monkeypatch):
    # E7: the single reset waits for the delete, the batch reset does not
    captured = []
    monkeypatch.setattr(svc, "delete_task_events_for_cleanup", lambda **kwargs: captured.append(kwargs))
    monkeypatch.setattr(svc, "reset_task", lambda **kwargs: (None, cleanup(), {"updated": 0}))
    monkeypatch.setattr(entity_access, "assert_tasks_access", lambda *args, **kwargs: None)
    monkeypatch.setattr(svc, "run_batch_operation", _run_batch_for_ids)

    identity = Identity(user="u", company=company, role=Role.user)
    svc.reset(FakeCall(identity), company, ResetRequest(task="t1", force=True))
    svc.reset_many(FakeCall(identity), company, ResetManyRequest(ids=["t2"], force=True))

    assert [kwargs["sync_delete"] for kwargs in captured] == [True, False]
    assert list(captured[0]["tasks"]) == ["t1"]
    assert list(captured[1]["tasks"]) == ["t2"]


def _run_batch_for_ids(func, ids, **_):
    return [(_id, func(task_id=_id)) for _id in ids], []


def test_delete_many_passes_only_deleted_tasks(company, monkeypatch):
    # E8: a missing task is reported as failed and does not reach the cleanup
    captured = []
    monkeypatch.setattr(svc, "delete_task_events_for_cleanup", lambda **kwargs: captured.append(kwargs))
    monkeypatch.setattr(entity_access, "assert_tasks_access", lambda *args, **kwargs: None)
    monkeypatch.setattr(svc, "_reset_cached_tags", lambda *args, **kwargs: None)

    def fake_delete_task(task_id, **kwargs):
        if task_id == "deleted-task":
            return True, SimpleNamespace(project=None), cleanup()
        return False, None, cleanup()

    monkeypatch.setattr(svc, "delete_task", fake_delete_task)
    monkeypatch.setattr(svc, "run_batch_operation", _run_batch_for_ids)

    svc.delete_many(
        FakeCall(Identity(user="u", company=company, role=Role.user)),
        company,
        DeleteManyRequest(ids=["deleted-task", "missing-task"], force=True),
    )

    assert len(captured) == 1
    assert list(captured[0]["tasks"]) == ["deleted-task"]
    assert captured[0]["sync_delete"] is False


def test_e2e_and_pipelines_keep_their_own_reference():
    # E9: the names the e2e, pipelines and tasks services use are the very helper of the cleanup module
    assert e2e_service.delete_task_events_for_cleanup is task_cleanup.delete_task_events_for_cleanup
    assert pipelines_service.delete_task_events_for_cleanup is task_cleanup.delete_task_events_for_cleanup
    assert svc.delete_task_events_for_cleanup is task_cleanup.delete_task_events_for_cleanup
