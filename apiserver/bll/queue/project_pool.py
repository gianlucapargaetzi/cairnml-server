"""The queue pool of a project: the queues allocated to it (project settings → Queues).

A task enqueued with no queue goes to the project's pool: the allocated queue with an idle agent, otherwise the one
with the fewest waiting tasks. Several agents (or several workers of one agent) in the pool let a pipeline run its
controller and its steps at the same time. A project with no allocated queue has no pool, and the task goes to the
default queue as before.
"""

from typing import Dict, Optional, Sequence, Tuple


def choose_queue(stats: Dict[str, Tuple[int, int, int]]) -> Optional[str]:
    """The queue to use, from {queue id: (idle workers, all workers, waiting tasks)}.

    A queue with an idle worker comes first (the one with most idle workers), then a queue that has workers (the one with
    the fewest waiting tasks), and a queue nobody serves last. Ties go to the queue that is first in the mapping, which is
    the order of the allocation.
    """
    if not stats:
        return None
    order = list(stats)

    def rank(queue_id):
        idle, workers, waiting = stats[queue_id]
        return (0 if idle else 1 if workers else 2, -idle, waiting, order.index(queue_id))

    return min(order, key=rank)


def pool_queue_stats(queue_ids: Sequence[str], workers: Sequence, queues: Dict[str, int]) -> Dict[str, Tuple[int, int, int]]:
    """For each pool queue: its idle workers, all its workers and the number of tasks waiting in it."""
    stats = {}
    for queue_id in queue_ids:
        serving = [w for w in workers if queue_id in (w.queues or [])]
        idle = [w for w in serving if not w.task]
        stats[queue_id] = (len(idle), len(serving), queues.get(queue_id, 0))
    return stats


def pick_pool_queue(company_id: str, identity, project_id: Optional[str], queue_bll) -> Optional[str]:
    """The id of the pool queue for a task of the project, or None when the project has no pool the user may use."""
    if not project_id:
        return None
    # imported here: the module is used without a database
    from apiserver.apierrors import errors
    from apiserver.bll.access import queue_ids_allocated, resource_ids_shared_with
    from apiserver.bll.queue.queue_access import QueueAccessBLL
    from apiserver.database.model.project import Project
    from apiserver.database.model.queue import Queue

    project = Project.objects(id=project_id, company=company_id).only("path").first()
    root_id = (project.path[0] if project and project.path else project_id) if project else None
    if not root_id:
        return None
    applies = queue_ids_allocated(company_id, identity.user, {root_id})
    allocated = [q for q in resource_ids_shared_with(company_id, "queue", root_id) if q in applies]
    usable = []
    for queue_id in allocated:
        try:
            QueueAccessBLL.assert_can_use(identity, company_id, queue_id)
        except errors.bad_request.InvalidQueueId:
            continue
        usable.append(queue_id)
    if not usable:
        return None
    waiting = {q.id: len(q.entries or []) for q in Queue.objects(company=company_id, id__in=usable).only("id", "entries")}
    return choose_queue(pool_queue_stats(usable, queue_bll.worker_bll.get_all(company_id), waiting))
