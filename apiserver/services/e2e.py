"""The end-to-end suite's reset: removes what the suite made, so a stack that is kept running starts each session from
the same state. Off unless apiserver.e2e.reset_enabled is set, which only the suite's own test stack does (see
tests/e2e/stack/compose.yaml). Admin only. It removes what carries the configured name prefix, and nothing else:
projects with the prefix (with their contents), tasks with the prefix, and scheduler workers with the prefix together
with their reservations. Scheduler events of the company are cleared too, so each session reads only its own events.
"""

from apiserver.apierrors import errors
from apiserver.bll.project.project_cleanup import delete_project
from apiserver.bll.task.task_operations import delete_task
from apiserver.config_repo import config
from apiserver.database.model.project import Project
from apiserver.database.model.scheduler import Reservation, SchedulerEvent, SchedulerWorker
from apiserver.database.model.task.task import Task
from apiserver.database.model.url_to_delete import UrlToDelete
from apiserver.service_repo import APICall, endpoint
from apiserver.services.tasks import _delete_task_events, _reset_cached_tags


def _is_admin(call: APICall) -> bool:
    return call.identity.role in ("admin", "root", "system")


def _check_allowed(call: APICall) -> str:
    """The guard of every e2e endpoint: enabled on this server, an admin caller. Returns the configured prefix."""
    if not config.get("apiserver.e2e.reset_enabled", False):
        raise errors.forbidden.NoWritePermission(reason="the end-to-end endpoints are disabled on this server")
    if not _is_admin(call):
        raise errors.forbidden.NoWritePermission(reason="only an admin may use the end-to-end endpoints")
    prefix = config.get("apiserver.e2e.name_prefix", "e2e")
    if not prefix or len(prefix) < 3:
        raise errors.server_error.ConfigError(reason="apiserver.e2e.name_prefix must be at least 3 characters")
    return prefix


@endpoint("system.e2e_deletions", min_version="2.1")
def e2e_deletions(call: APICall, company_id, _):
    """The async delete's own records for the given urls: what it has not finished yet, and why a deletion failed.
    A url that is not listed has been deleted from storage (the delete service removes its record when it is done)."""
    _check_allowed(call)
    urls = [u for u in (call.data or {}).get("urls", []) if isinstance(u, str)]
    pending = []
    if urls:
        for record in UrlToDelete.objects(company=company_id, url__in=urls).only(
                "url", "status", "retry_count", "last_failure_reason", "storage_type"):
            pending.append({
                "url": record.url,
                "status": str(record.status),
                "retry_count": record.retry_count or 0,
                "last_failure_reason": record.last_failure_reason or "",
                "storage_type": str(record.storage_type),
            })
    call.result.data = {"pending": pending}


@endpoint("system.e2e_reset", min_version="2.1")
def e2e_reset(call: APICall, company_id, _):
    prefix = _check_allowed(call)
    body = call.data or {}
    if body.get("prefix", prefix) != prefix:
        raise errors.bad_request.ValidationError(reason="the reset only removes objects with the configured prefix",
                                                 prefix=prefix)

    removed_projects = 0
    affected_projects = set()
    for project in Project.objects(company=company_id, name__startswith=prefix).only("id"):
        result, touched = delete_project(
            company=company_id,
            user=call.identity.user,
            project_id=project.id,
            force=True,
            delete_contents=True,
            delete_external_artifacts=True,
        )
        removed_projects += 1
        affected_projects.update(touched)
    _reset_cached_tags(company_id, projects=list(affected_projects))

    removed_tasks = 0
    for task in Task.objects(company=company_id, name__startswith=prefix).only("id"):
        deleted, _, cleanup = delete_task(
            task_id=task.id,
            company_id=company_id,
            identity=call.identity,
            move_to_trash=False,
            force=True,
            delete_output_models=True,
            status_message="end-to-end reset",
            status_reason="e2e",
            include_pipeline_steps=True,
        )
        if deleted:
            removed_tasks += 1
            _delete_task_events(
                company_id=company_id,
                user_id=call.identity.user,
                tasks={task.id: cleanup},
                delete_external_artifacts=True,
                sync_delete=True,
            )

    workers = [w.id for w in SchedulerWorker.objects(company=company_id, id__startswith=prefix).only("id")]
    released = Reservation.objects(company=company_id, worker__in=workers).delete() if workers else 0
    removed_workers = SchedulerWorker.objects(company=company_id, id__in=workers).delete() if workers else 0
    events = SchedulerEvent.objects(company=company_id).delete()

    call.result.data = {
        "projects": removed_projects,
        "tasks": removed_tasks,
        "workers": removed_workers,
        "reservations": released,
        "scheduler_events": events,
    }
