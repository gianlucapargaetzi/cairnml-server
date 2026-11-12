"""What a job token may do: only its own task, and the tasks it starts itself.

A job runs the user's code. The job token acts for the task's owner, so a role check alone would let the job reach
every task, project, model and user the owner can see. Here a job may use only the endpoints that the task itself
needs (its own task, events and artifacts), and every request must name its own task.

A job that orchestrates (a pipeline controller, a hyperparameter search) also starts tasks of its own: it may create
tasks in its project (or below it) whose parent the server sets to the job's task, and read, change, enqueue and stop
those children, and read (not change) the steps next to it that share its parent. A task that is not the job's or one of its children gets the answer below.

A request that names another task gets the same answer as a task that does not exist, so a job cannot tell that
the other task exists. A request to any other service is refused as not permitted.
"""

from apiserver.apierrors import errors

# Services a job may call. Everything else (models, queues, users, devices, admin) is refused.
JOB_SERVICES = ("tasks", "events", "datachunks")

# Endpoints of other services a job may use, only for its own task: the job's agent asks for the repository's token
# for its task (devices.git_token checks the task named in the request against the job's token).
JOB_OWN_TASK_ENDPOINTS = {"devices.git_token", "repositories.find", "repositories.clone"}

# Model actions a job may take, only on models linked to its own task (the model's task is the job's task).
# Listing all models, and any action not here, is refused.
JOB_MODEL_ACTIONS = {
    "models.create",
    "models.get_by_id",
    "models.edit",
    "models.update",
    "models.set_ready",
    "models.get_by_task_id",
    "models.update_for_task",
    "models.add_or_update_metadata",
    "models.delete_metadata",
}

# What an orchestrating job may look up: the project tree of its own task and a queue it enqueues on, by name.
JOB_PROJECT_TREE_ENDPOINTS = {"projects.get_all", "projects.get_all_ex", "projects.create"}
JOB_QUEUE_LOOKUP_ENDPOINTS = {"queues.get_all", "queues.get_all_ex"}

# Reads of the steps next to the job: a step of a pipeline reads the task (and so the artifacts) of the step before it.
JOB_SIBLING_READ_ENDPOINTS = {"tasks.get_by_id", "tasks.get_by_id_ex", "tasks.get_all", "tasks.get_all_ex",
                              "datachunks.prepare_download"}

# The one read of the projects service a job needs: the project of its own task, which the SDK reads when it starts.
JOB_PROJECT_READ = "projects.get_by_id"


def _named_task_ids(call) -> list:
    """The task ids a request names: 'task', 'tasks' or 'ids' in the body, and 'task' in each batched event."""
    ids = []
    data = call.data or {}
    if "task" in data:
        ids.append(data["task"])
    # "id" is a list in the status read (tasks.get_all) that the SDK makes for its own task
    for key in ("tasks", "ids", "id"):
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


def _is_child(task_id, job_task: str) -> bool:
    """True when the task was started by the job: its parent is the job's task."""
    from apiserver.database.model.task.task import Task  # imported here: the module is used without a database

    task = Task.objects(id=task_id).only("parent").first() if task_id else None
    return bool(task) and task.parent == job_task


def may_read_task_data(job_task: str, task_id: str) -> bool:
    """A job reads the data (artifacts) of its own task, of the tasks it started and of the steps next to it."""
    return task_id == job_task or _is_child(task_id, job_task) or _is_sibling(task_id, job_task)


def _is_sibling(task_id, job_task: str) -> bool:
    """True when the task has the same parent as the job's task: a step of the same pipeline."""
    from apiserver.database.model.task.task import Task  # imported here: the module is used without a database

    mine = Task.objects(id=job_task).only("parent").first()
    other = Task.objects(id=task_id).only("parent").first() if task_id else None
    return bool(mine and other and mine.parent and other.parent == mine.parent)


def _project_name_of(job_task: str):
    """The name of the project of the job's task: the root of what an orchestrating job may use."""
    from apiserver.database.model.project import Project  # imported here: the module is used without a database

    project_id = _task_project_id(job_task)
    project = Project.objects(id=project_id).only("name").first() if project_id else None
    return project.name if project else None


def _name_in_tree(name, root) -> bool:
    """A project name that is the job's project, below it, or in its '.pipelines' sub-project (the SDK's regex anchors
    and escapes are ignored)."""
    if not root or not isinstance(name, str):
        return False
    plain = name.lstrip("^").rstrip("$").replace("\\", "")
    return plain == root or plain.startswith(root + "/") or plain.startswith(root.split("/")[0] + "/.")


def _project_in_tree(project_id, job_task: str) -> bool:
    """True when the project is the project of the job's task or one below it."""
    from apiserver.database.model.project import Project  # imported here: the module is used without a database

    project = Project.objects(id=project_id).only("name").first() if project_id else None
    return bool(project) and _name_in_tree(project.name, _project_name_of(job_task))


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

    if endpoint.name in JOB_MODEL_ACTIONS:
        _check_model_scope(endpoint.name, call, job_task)
        return

    refused = errors.forbidden.NoWritePermission(reason="a job may only use its own task, events and artifacts")

    if endpoint.name in JOB_PROJECT_TREE_ENDPOINTS:
        if _name_in_tree((call.data or {}).get("name"), _project_name_of(job_task)):
            return
        raise refused

    if endpoint.name in JOB_QUEUE_LOOKUP_ENDPOINTS:
        # the id of a queue, read by name or id: a listing of all the queues is not what the job needs
        if (call.data or {}).get("name") or (call.data or {}).get("id"):
            return
        raise refused

    if endpoint.name in ("tasks.create", "tasks.clone"):
        # a task the job starts: in the job's project (or below it). The server makes the job's task its parent,
        # whatever the body says, so the job can reach the tasks it starts and no others.
        data = call.data
        if endpoint.name == "tasks.clone" and data.get("task") != job_task and not _is_child(data.get("task"), job_task):
            raise errors.bad_request.InvalidTaskId(id="", company="")
        project = data.get("project") or (_task_project_id(job_task) if endpoint.name == "tasks.clone" else None)
        if not _project_in_tree(project, job_task):
            raise errors.bad_request.InvalidTaskId(id="", company="")
        data["parent"] = job_task
        return

    service = endpoint.name.partition(".")[0]
    if service not in JOB_SERVICES and endpoint.name not in JOB_OWN_TASK_ENDPOINTS:
        raise refused

    named = _named_task_ids(call)
    if not named and service == "datachunks" and "id" in call.data:
        named = [_chunk_task_id(call.data["id"])]
    reads_siblings = endpoint.name in JOB_SIBLING_READ_ENDPOINTS
    if not named or any(
        task_id != job_task
        and not _is_child(task_id, job_task)
        and not (reads_siblings and _is_sibling(task_id, job_task))
        for task_id in named
    ):
        # the same answer a missing task gets, so the job cannot tell that another task exists
        raise errors.bad_request.InvalidTaskId(id="", company="")


def _check_model_scope(name: str, call, job_task: str) -> None:
    """A model action of a job: the model must be linked to the job's own task.

    A new model (models.create) names its task in the body, which must be the job's task. For every other action the
    model is looked up and its task must be the job's task. A model that does not exist, or belongs to another task,
    gets the same answer.
    """
    if name == "models.create":
        # a model created with no task (the SDK's anonymous model, before its weights are attached) is not yet linked to
        # any task, and the job may create it. A model that names a task must name the job's own task.
        task = (call.data or {}).get("task")
        if task in (None, "") or task == job_task:
            return
        raise errors.bad_request.InvalidTaskId(id="", company="")
    if name == "models.update_for_task":
        if (call.data or {}).get("task") == job_task:
            return
        raise errors.bad_request.InvalidTaskId(id="", company="")

    model_id = (call.data or {}).get("model")
    if not model_id:
        raise errors.bad_request.InvalidTaskId(id="", company="")
    owner = call.auth.identity.user if call.auth is not None and call.auth.identity else None
    if not _model_belongs_to_job(model_id, job_task, owner):
        raise errors.bad_request.InvalidTaskId(id="", company="")


def _model_belongs_to_job(model_id: str, job_task: str, owner) -> bool:
    """A model is the job's when it is linked to the job's task. A model not yet linked to a task is the job's when
    the job's owner created it: the job creates its model first and attaches the task afterwards."""
    from apiserver.database.model.model import Model  # imported here: the module is used without a database

    model = Model.objects(id=model_id).only("task", "user").first()
    if model is None:
        return False
    if model.task:
        return model.task == job_task
    return bool(owner) and model.user == owner
