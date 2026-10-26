import re
from functools import partial

from apiserver.apierrors.errors.bad_request import CannotRemoveAllRuns
from apiserver.apimodels.pipelines import (
    StartPipelineRequest,
    DeleteRunsRequest,
)
from apiserver.bll.organization import OrgBLL
from apiserver.bll.project import ProjectBLL
from apiserver.bll.queue import QueueBLL
from apiserver.bll.task import TaskBLL
from apiserver.bll.task.task_operations import enqueue_task, delete_task
from apiserver.bll.util import run_batch_operation
from apiserver.database.model.project import Project
from apiserver.database.model.task.task import Task, TaskType
from apiserver.service_repo import APICall, endpoint
from apiserver.utilities.dicts import nested_get
from .tasks import _delete_task_events, _assert_tasks_access

org_bll = OrgBLL()
project_bll = ProjectBLL()
task_bll = TaskBLL()
queue_bll = QueueBLL()


def _update_task_name(task: Task):
    if not task or not task.project:
        return

    project = Project.objects(id=task.project).only("name").first()
    if not project:
        return

    _, _, name_prefix = project.name.rpartition("/")
    name_mask = re.compile(rf"{re.escape(name_prefix)}( #\d+)?$")
    count = Task.objects(
        project=task.project, system_tags__in=["pipeline"], name=name_mask
    ).count()
    new_name = f"{name_prefix} #{count}" if count > 0 else name_prefix
    task.update(name=new_name)


@endpoint("pipelines.delete_runs")
def delete_runs(call: APICall, company_id: str, request: DeleteRunsRequest):
    # delete_task (below) already requires write access per task via get_task_with_write_access,
    # so a hidden project's runs cannot actually be deleted; company-scoping this lookup too keeps
    # it from matching anything outside the caller's own company in the first place
    existing_runs = set(
        Task.objects(
            company=company_id, project=request.project, type=TaskType.controller
        ).scalar("id")
    )
    if not existing_runs.difference(request.ids):
        raise CannotRemoveAllRuns(project=request.project)

    # make sure that only controller tasks are deleted
    ids = existing_runs.intersection(request.ids)
    if not ids:
        return dict(succeeded=[], failed=[])

    results, failures = run_batch_operation(
        func=partial(
            delete_task,
            company_id=company_id,
            identity=call.identity,
            move_to_trash=False,
            force=True,
            delete_output_models=True,
            status_message="",
            status_reason="Pipeline run deleted",
            include_pipeline_steps=True,
        ),
        ids=list(ids),
    )

    succeeded = []
    tasks = {}
    if results:
        for _id, (deleted, task, cleanup_res) in results:
            if deleted:
                tasks[_id] = cleanup_res
            succeeded.append(
                dict(id=_id, deleted=bool(deleted), **cleanup_res.to_res_dict(False))
            )

        if tasks:
            _delete_task_events(
                company_id=company_id,
                user_id=call.identity.user,
                tasks=tasks,
                delete_external_artifacts=True,
                sync_delete=True,
            )

    call.result.data = dict(succeeded=succeeded, failed=failures)


@endpoint("pipelines.start_pipeline")
def start_pipeline(call: APICall, company_id: str, request: StartPipelineRequest):
    hyperparams = None
    if request.args:
        hyperparams = {
            "Args": {
                str(arg.name): {
                    "section": "Args",
                    "name": str(arg.name),
                    "value": str(arg.value),
                }
                for arg in request.args or []
            }
        }

    # clone_task's own fetch of the source task is company-scoped only, with no project check;
    # tasks.clone's own endpoint already requires write on the source before cloning it, so this
    # entry point needs the same bar, not a weaker one, for the same operation
    _assert_tasks_access(company_id, call.identity, [request.task], "write")

    task, _ = task_bll.clone_task(
        company_id=company_id,
        user_id=call.identity.user,
        identity=call.identity,
        task_id=request.task,
        hyperparams_overrides=hyperparams,
    )

    _update_task_name(task)

    queued, res = enqueue_task(
        task_id=task.id,
        company_id=company_id,
        identity=call.identity,
        queue_id=request.queue,
        status_message="Starting pipeline",
        status_reason="",
    )
    extra = {}
    if request.verify_watched_queue and queued:
        res_queue = nested_get(res, ("fields", "execution.queue"))
        if res_queue:
            extra["queue_watched"] = queue_bll.check_for_workers(company_id, res_queue)

    call.result.data = dict(
        pipeline=task.id,
        enqueued=bool(queued),
        **extra,
    )
