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
- An agent may serve a queue only when it is public, its user owns it, holds a grant on it, or is an admin.
"""

from typing import Iterable, Set

from mongoengine import Q

from apiserver.apierrors import errors
from apiserver.bll.access import remove_grant
from apiserver.database.model.auth import Role
from apiserver.database.model.project import Project
from apiserver.database.model.queue import Queue
from apiserver.service_repo.auth import Identity

_ADMIN_ROLES = (Role.admin, Role.root)


class QueueAccessBLL:
    @staticmethod
    def is_admin(identity: Identity) -> bool:
        return identity.role in _ADMIN_ROLES

    @staticmethod
    def is_owner(identity: Identity, queue: Queue) -> bool:
        return bool(queue.created_by) and queue.created_by == identity.user

    @staticmethod
    def can_manage(identity: Identity, queue: Queue) -> bool:
        """ Admins and the owner of the queue may update, delete, manage grants and metadata """
        return QueueAccessBLL.is_admin(identity) or QueueAccessBLL.is_owner(identity, queue)

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
        roots = Project.objects(QueueAccessBLL.visible_root_query(identity)).only("queues")
        return {queue_id for root in roots for queue_id in root.queues}

    @staticmethod
    def usable_query(identity: Identity) -> Q:
        """ Queues the user may see and use, without the admin exception """
        return (
            Q(public=True)
            | Q(created_by=identity.user)
            | Q(users__user=identity.user)
            | Q(id__in=list(QueueAccessBLL.allocated_queue_ids(identity)))
        )

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
        found = (
            Queue.objects(company=company_id, id=queue_id)
            .filter(QueueAccessBLL.usable_query(identity))
            .only("id")
            .first()
        )
        if not found:
            raise errors.bad_request.InvalidQueueId(id=queue_id)

    @staticmethod
    def assert_can_manage(identity: Identity, company_id: str, queue_id: str) -> None:
        """
        A queue the user may not see is reported as not found. A visible queue that the user does not own is refused.
        """
        if QueueAccessBLL.is_admin(identity):
            return
        queue = (
            Queue.objects(company=company_id, id=queue_id)
            .filter(QueueAccessBLL.visible_query(identity))
            .only("id", "created_by")
            .first()
        )
        if not queue:
            raise errors.bad_request.InvalidQueueId(id=queue_id)
        if not QueueAccessBLL.can_manage(identity, queue):
            raise errors.forbidden.NoWritePermission(
                "only the owner or a company admin can manage the queue"
            )

    @staticmethod
    def assert_can_serve(identity: Identity, company_id: str, queue_ids: Iterable[str]) -> None:
        """
        An agent may serve a queue only when it is public, the user owns it, holds a grant on it, or is an admin.
        :raise errors.bad_request.InvalidQueueId: for the queues the user may not serve
        """
        if identity is None:
            raise errors.forbidden.NoWritePermission("the caller is not known")
        if QueueAccessBLL.is_admin(identity):
            return
        queue_ids = set(queue_ids)
        allowed = set(
            Queue.objects(
                Q(company=company_id, id__in=list(queue_ids))
                & (Q(public=True) | Q(created_by=identity.user) | Q(users__user=identity.user))
            ).scalar("id")
        )
        invalid = queue_ids - allowed
        if invalid:
            raise errors.bad_request.InvalidQueueId(ids=sorted(invalid))

    @staticmethod
    def assignable_queue_ids(company_id: str, user_id: str) -> Set[str]:
        """ What a project owner may allocate: public, granted and owned queues, without admin rights """
        return set(
            Queue.objects(
                Q(company=company_id)
                & (Q(public=True) | Q(users__user=user_id) | Q(created_by=user_id))
            ).scalar("id")
        )

    @staticmethod
    def remove_from_allocations(queue_ids: Iterable[str]) -> None:
        """ Take queues out of every project's allocations """
        Project.objects(queues__in=list(queue_ids)).update(
            pull_all__queues=list(queue_ids)
        )

    @staticmethod
    def remove_user_allocations(company_id: str, user_id: str, queue_id: str) -> None:
        """ Take a queue out of the allocations of the root projects the user owns (the cascade of a removed grant) """
        roots = [root.id for root in Project.objects(company=company_id, parent=None, user=user_id, queues=queue_id)
                 .only("id")]
        Project.objects(
            company=company_id, parent=None, user=user_id, queues=queue_id
        ).update(pull__queues=queue_id)
        for root_id in roots:
            remove_grant(company_id, "queue", queue_id, "projects", root_id)
