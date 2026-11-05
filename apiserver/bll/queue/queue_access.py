"""
Who may see, use, manage and allocate a queue.

- Any company user may create a queue. The creator is its owner (`created_by`). Queues created before ownership have
  no owner and stay admin-managed.
- A user sees a queue when it is public, when they are its owner, when they hold a grant on it, or when it is allocated
  to a root project they own or are a member of (in any role). Admins (company admin or root) see every queue.
- A queue is usable (enqueue, dequeue, peek and the other task actions) by the same users who see it.
- The owner manages the queue: update, delete, grants, metadata and the `public` flag. Admins manage every queue. A
  public queue is visible and allocatable by every company user, and any user's agent may serve it.
- A grant lets the user use the queue and allocate it to the root projects they own. Only the owner of a root project
  changes its allocations, and the owner may allocate only the queues they may use without admin rights: public
  queues, queues they hold a grant on, and queues they own.
- An agent serves a queue at the level its owner holds on it, and serving needs use: it is public, the owner owns it,
  or the owner holds a grant on it. The agent's identity is never an admin, so the admin role gives it no queue.
"""

from typing import Iterable, Set

from mongoengine import Q

from apiserver.apierrors import errors
from apiserver.bll.access import (
    allows,
    level_of,
    require,
    queue_ids_allocated,
    queue_ids_held,
    remove_grant,
    shared_project_ids,
)
from apiserver.bll.user_access import my_projects
from apiserver.database.model.auth import Role
from apiserver.database.model.project import Project
from apiserver.database.model.queue import Queue
from apiserver.database.model.task.task import Task
from apiserver.service_repo.auth import Identity

_ADMIN_ROLES = (Role.admin, Role.root)


class QueueAccessBLL:
    """ Every decision reads the share records through bll/access.py; the queue's owner is its created_by. """

    @staticmethod
    def is_admin(identity: Identity) -> bool:
        return identity.role in _ADMIN_ROLES

    @staticmethod
    def _roots(identity: Identity) -> Set[str]:
        """ The root projects the user owns or is a member of (in any role) """
        return {project["id"] for project in my_projects(identity.company, identity.user)}

    @staticmethod
    def visible_root_query(identity: Identity) -> Q:
        """ Root projects of the user's company that the user owns or is a member of (in any role) """
        return (
            Q(company=identity.company)
            & Q(parent=None)
            & (Q(user=identity.user) | Q(members__user=identity.user))
        )

    @staticmethod
    def allocated_queue_ids(identity: Identity) -> Set[str]:
        """ Queues allocated to root projects the user owns or is a member of """
        return queue_ids_allocated(identity.company, identity.user, QueueAccessBLL._roots(identity))

    @staticmethod
    def usable_query(identity: Identity) -> Q:
        """ Queues the user may see and use, without the admin exception """
        held = queue_ids_held(identity.company, identity.user, QueueAccessBLL._roots(identity))
        return Q(id__in=list(held))

    @staticmethod
    def visible_query(identity: Identity) -> Q:
        """ Listing filter: admins see every queue, others the queues they may use """
        if QueueAccessBLL.is_admin(identity):
            return Q()
        return QueueAccessBLL.usable_query(identity)

    @staticmethod
    def usable_queue_ids(identity: Identity, company_id: str) -> list:
        """ Ids of the queues the user may use; admins get every queue """
        query = Queue.objects(company=company_id)
        if not QueueAccessBLL.is_admin(identity):
            query = query.filter(QueueAccessBLL.usable_query(identity))
        return list(query.scalar("id"))

    @staticmethod
    def assert_can_use(identity: Identity, company_id: str, queue_id: str) -> None:
        """
        A queue the user may not use is reported as not found, so the API does not reveal which queues exist.
        """
        if QueueAccessBLL.is_admin(identity):
            return
        level = level_of(company_id, identity.user, False, "queue", queue_id, QueueAccessBLL._roots(identity))
        if not allows(level, "use", "queue"):
            raise errors.bad_request.InvalidQueueId(id=queue_id)

    @staticmethod
    def assert_can_manage(identity: Identity, company_id: str, queue_id: str) -> None:
        """
        A queue the user may not see is reported as not found. A visible queue that the user does not own is refused.
        """
        if QueueAccessBLL.is_admin(identity):
            return
        level = level_of(company_id, identity.user, False, "queue", queue_id, QueueAccessBLL._roots(identity))
        if level is None:
            raise errors.bad_request.InvalidQueueId(id=queue_id)
        # the owner holds the collaborator level, the one that manages a queue
        if not allows(level, "manage_sharing", "queue"):
            raise errors.forbidden.NoWritePermission(
                "only the owner or a company admin can manage the queue"
            )

    @staticmethod
    def require_action(identity: Identity, company_id: str, queue_id: str, action: str) -> None:
        """ The level check of bll/access for one action on the queue (ACTION_NEEDS) """
        require(company_id, identity.user, QueueAccessBLL.is_admin(identity), "queue", queue_id,
                QueueAccessBLL._roots(identity), action)

    @staticmethod
    def assert_can_change_tasks(identity: Identity, company_id: str, queue_id: str, task_ids: Iterable[str]) -> None:
        """
        Moving or removing a task that someone else queued needs read_write on the queue. The user's own tasks need
        only the use level, which the caller has already checked.
        """
        others = Task.objects(company=company_id, id__in=list(task_ids), user__ne=identity.user).scalar("id")
        if list(others):
            QueueAccessBLL.require_action(identity, company_id, queue_id, "change_content")

    @staticmethod
    def assert_can_serve(identity: Identity, company_id: str, queue_ids: Iterable[str]) -> None:
        """
        An agent serves a queue at the level its owner holds on it (bll/access.level_of), and serving needs use. The
        agent's identity is its owner with the user role, so an admin's agent gets no queue by the admin role alone.
        A deactivated owner holds no level, so the agent serves nothing. A public queue is served by any company user.
        An allocation to a project does not count.
        :raise errors.bad_request.InvalidQueueId: for the queues the user may not serve
        """
        if identity is None:
            raise errors.forbidden.NoWritePermission("the caller is not known")
        queue_ids = set(queue_ids)
        allowed = {
            queue_id
            for queue_id in queue_ids
            if allows(
                level_of(company_id, identity.user, False, "queue", queue_id, set(), include_projects=False),
                "use",
                "queue",
            )
        }
        invalid = queue_ids - allowed
        if invalid:
            raise errors.bad_request.InvalidQueueId(ids=sorted(invalid))

    @staticmethod
    def assignable_queue_ids(company_id: str, user_id: str) -> Set[str]:
        """ What a project owner may allocate: public, granted and owned queues, without admin rights """
        return queue_ids_held(company_id, user_id, set(), include_projects=False)

    @staticmethod
    def remove_user_allocations(company_id: str, user_id: str, queue_id: str, actor: str) -> None:
        """ Take a queue out of the allocations of the root projects the user owns (the cascade of a removed grant) """
        owned = {root.id for root in Project.objects(company=company_id, parent=None, user=user_id).only("id")}
        for root_id in sorted(shared_project_ids(company_id, "queue", queue_id) & owned):
            remove_grant(company_id, "queue", queue_id, "projects", root_id, actor=actor)
