"""What a job token may do: only its own task.

A job runs the user's code. The job token acts for the task's owner, so a role check alone would let the job reach
every task, project, model and user the owner can see. Here a job may use only the endpoints that the task itself
needs (its own task, events and artifacts), and every request must name its own task.

A request that names another task gets the same answer as a task that does not exist, so a job cannot tell that
the other task exists. A request to any other service is refused as not permitted.
"""

from apiserver.apierrors import errors

# Services a job may call. Everything else (models, queues, users, devices, admin) is refused.
JOB_SERVICES = ("tasks", "events", "datachunks")

# The one read of the projects service a job needs: the project of its own task, which the SDK reads when it starts.
JOB_PROJECT_READ = "projects.get_by_id"


def _named_task_ids(call) -> list:
    """The task ids a request names: 'task', 'tasks' or 'ids' in the body, and 'task' in each batched event."""
    ids = []
    data = call.data or {}
    if "task" in data:
        ids.append(data["task"])
    for key in ("tasks", "ids"):
        if isinstance(data.get(key), list):
            ids.extend(data[key])
    # APICall returns a plain body as a one-item batch, so batched events count only when there is no body
    if not data:
        for event in call.batched_data or []:
            ids.append(event.get("task") if isinstance(event, dict) else None)
    return ids


def _task_project_id(task_id: str):
    """The project of the job's task."""
    from apiserver.database.model.task.task import Task  # imported here: the module is used without a database

    task = Task.objects(id=task_id).only("project").first()
    return task.project if task else None


def _chunk_task_id(chunk_id: str):
    """The task that owns a datachunk, for requests that name the chunk rather than the task."""
    from apiserver.database.model.datachunk import Datachunk  # imported here: the module is used without a database

    chunk = Datachunk.objects(id=chunk_id).only("task").first()
    return chunk.task if chunk else None


def validate_job_scope(endpoint, call) -> None:
    """Refuse a request made with a job token unless it is for the job's own task. No effect for other tokens."""
    job_task = getattr(call.auth, "job_task", None) if call.auth is not None else None
    if not job_task:
        return

    if endpoint.name == JOB_PROJECT_READ:
        # read only, and only the project the job's own task is in
        project_id = (call.data or {}).get("project")
        if not project_id or project_id != _task_project_id(job_task):
            raise errors.bad_request.InvalidTaskId(id="", company="")
        return

    service = endpoint.name.partition(".")[0]
    if service not in JOB_SERVICES:
        raise errors.forbidden.NoWritePermission(
            reason="a job may only use its own task, events and artifacts"
        )

    named = _named_task_ids(call)
    if not named and service == "datachunks" and "id" in call.data:
        named = [_chunk_task_id(call.data["id"])]
    if not named or any(task_id != job_task for task_id in named):
        # the same answer a missing task gets, so the job cannot tell that another task exists
        raise errors.bad_request.InvalidTaskId(id="", company="")
