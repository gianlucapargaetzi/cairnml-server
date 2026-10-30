"""
Who may use and allocate a queue.

- Admins (company admin or root) manage queues: create, update, delete, metadata and grants.
- A queue is usable by a user when it is public, when the user holds a grant on it, or when it is allocated to a root
  project the user owns or is a member of (in any role). Admins may use every queue.
- A grant lets the user use the queue and allocate it to the root projects they own. Only the owner of a root project
  changes its allocations, and the owner may allocate only the queues they may use without admin rights: public
  queues and queues they hold a grant on.
"""

from typing import Iterable, Set

from mongoengine import Q

from apiserver.apierrors import errors
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
    def assert_admin(identity: Identity) -> None:
        if not QueueAccessBLL.is_admin(identity):
            raise errors.forbidden.NoWritePermission(
                "only company admins can manage queues"
            )

    @staticmethod
    def allocated_queue_ids(identity: Identity) -> Set[str]:
        """ Queues allocated to root projects the user owns or is a member of """
        roots = Project.objects(
            Q(company=identity.company)
            & Q(parent=None)
            & (Q(user=identity.user) | Q(members__user=identity.user))
        ).only("queues")
        return {queue_id for root in roots for queue_id in root.queues}

    @staticmethod
    def usable_query(identity: Identity) -> Q:
        """ Queues the user may use, without the admin exception """
        return (
            Q(public=True)
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
    def assignable_queue_ids(company_id: str, user_id: str) -> Set[str]:
        """ What a project owner may allocate: public queues and queues granted to them, without admin rights """
        return set(
            Queue.objects(
                Q(company=company_id) & (Q(public=True) | Q(users__user=user_id))
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
        Project.objects(
            company=company_id, parent=None, user=user_id, queues=queue_id
        ).update(pull__queues=queue_id)
