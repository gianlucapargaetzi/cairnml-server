"""A pipeline that blocks itself: its controller runs on the only agent of the queue its steps wait in.

An agent runs one task at a time. A controller (a pipeline, a hyperparameter search) that was started on an agent holds
that agent until it ends, and it ends only when its steps have run. When the steps are queued on a queue whose only agent
is the one running the controller, they could never start. The enqueue is refused with this reason instead, so the run
fails at once and says what to do.
"""

from typing import Optional, Sequence

from apiserver.apierrors import errors
from apiserver.bll.queue.project_pool import pick_pool_queue

MESSAGE = (
    'Pipeline dead-locked: its controller occupies the only agent of queue "{queue}", so the steps queued there could '
    "never start. A pipeline needs more than one agent: start another agent on this queue, or queue the steps on the "
    "queue of another agent."
)


def blocks_its_own_steps(workers: Sequence, queue_id: str, job_task: str) -> bool:
    """True when the one agent serving the queue is the one running the controller (the job's own task)."""
    serving = [w for w in workers if queue_id in (w.queues or [])]
    return len(serving) == 1 and bool(serving[0].task) and serving[0].task.id == job_task


def assert_not_deadlocked(call, queue_bll, company_id: str, task_id: str, queue_id: Optional[str], queue_name: Optional[str]) -> None:
    """Refuse the enqueue of a task by a job when it would wait for the agent that runs the job. No effect otherwise."""
    job_task = getattr(call.auth, "job_task", None) if call.auth is not None else None
    if not job_task:
        return
    # imported here: the module is used without a database
    from apiserver.database.model.queue import Queue
    from apiserver.database.model.task.task import Task

    if queue_name and not queue_id:
        queue = Queue.objects(company=company_id, name=queue_name).only("id", "name").first()
    elif queue_id:
        queue = Queue.objects(company=company_id, id=queue_id).only("id", "name").first()
    else:
        # no queue named: where the enqueue puts it, the project's pool or the queue the task already has
        task = Task.objects(id=task_id).only("project", "execution.queue").first()
        pooled = pick_pool_queue(company_id, call.identity, task.project if task else None, queue_bll)
        pooled = pooled or (task.execution.queue if task and task.execution else None)
        queue = Queue.objects(company=company_id, id=pooled).only("id", "name").first() if pooled else None
    if queue is None:
        return
    if blocks_its_own_steps(queue_bll.worker_bll.get_all(company_id), queue.id, job_task):
        raise errors.bad_request.ValidationError(MESSAGE.format(queue=queue.name))
