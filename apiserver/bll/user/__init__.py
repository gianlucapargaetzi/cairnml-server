from datetime import datetime
from typing import Optional

from mongoengine import Q

from apiserver.apierrors import errors
from apiserver.apimodels.users import CreateRequest
from apiserver.bll import repositories, user_access
from apiserver.bll.access import revoke_member_shares
from apiserver.bll.task import TaskBLL
from apiserver.config.info import get_version
from apiserver.database.errors import translate_errors_context
from apiserver.database.model.auth import AuthUser, Role
from apiserver.database.model.device import Device
from apiserver.database.model.project import Project
from apiserver.database.model.repository import RepositoryCheck
from apiserver.database.model.share import Share
from apiserver.database.model.task.task import Task, TaskStatus
from apiserver.database.model.user import UserProfile
from apiserver.idp.audit import record as audit

STOP_MESSAGE = "Stopped: its owner was deleted"
STOP_REASON = "owner deleted"


class UserBLL:
    @staticmethod
    def create(request: CreateRequest):
        user_id = request.id
        with translate_errors_context("creating user"):
            if user_id and UserProfile.objects(id=user_id).only("id"):
                raise errors.bad_request.UserIdExists(id=user_id)

            user = UserProfile(
                **request.to_struct(),
                created=datetime.utcnow(),
                created_in_version=get_version(),
            )
            user.save(force_insert=True)

    @staticmethod
    def delete(user_id: str, actor: str = ""):
        """Deletes the user: their profile, their sign-in record, and the records that belong to them: their credentials
        and their assignments of repositories (see ``user_access.drop_user_records``), the shares granted to them, their
        repository checks and their membership of projects (with the shares they granted as a member). Their devices are
        revoked and kept, as audit history. Their queued and running tasks are stopped, and so are the running tasks of
        others on their devices (nobody is left to stop them). What they own (projects, queues, repositories, tasks,
        models, datasets) stays, still naming them.

        The order matters. The sign-in record goes right after the records, so that from then on no session, device or
        job token of the user is accepted and nothing new can start in their name. The profile is the last record to go
        and the anchor of a retry: every step can be repeated, so a failure halfway leaves a user who can be deleted
        again, never records of a missing user. ``actor`` is who deletes, for the audit trail."""
        with translate_errors_context("deleting user"):
            profile = UserProfile.objects(id=user_id).only("id", "company").first()
            if profile is None:
                raise errors.bad_request.InvalidUserId(id=user_id)
            company = profile.company
            user_access.drop_user_records(company, user_id, actor=actor, successor=UserBLL._successor(profile))
            UserBLL._drop_shares(company, user_id)
            UserBLL._revoke_devices(company, user_id, actor)
            UserBLL._drop_repository_checks(company, user_id)
            AuthUser.objects(id=user_id).delete()
            memberships = UserBLL._drop_memberships(company, user_id, actor)
            stopped = UserBLL._stop_jobs(company, user_id, actor)
            UserBLL._drop_profile(company, user_id)
            audit(
                actor,
                "user.deleted",
                company=company,
                user=user_id,
                memberships_removed=memberships,
                tasks_stopped=stopped,
            )

    @staticmethod
    def _drop_profile(company: str, user: str) -> None:
        """The profile, the last record to go: while it is there a failed delete can be repeated."""
        if not UserProfile.objects(id=user, company=company).delete():
            raise errors.bad_request.InvalidUserId(id=user)

    @staticmethod
    def _drop_memberships(company: str, user: str, actor: str) -> int:
        """Takes the user out of every project they are a member of, as removing a member does (projects.remove_member):
        what the member shared with the project goes with them, and so do the assignments of repositories they no longer
        see. Returns the number of projects left."""
        roots = list(Project.objects(company=company, members__user=user).scalar("id"))
        for root in roots:  # the shares first: the projects are found by the membership, which a retry needs
            revoke_member_shares(company, root, user, actor=actor)
        if roots:
            Project.objects(company=company, id__in=roots).update(pull__members__user=user)
        repositories.prune_assignments(company, user=user)
        return len(roots)

    @staticmethod
    def _stop_jobs(company: str, user: str, actor: str) -> int:
        """Stops the user's queued and running tasks, and the running tasks of others on the user's devices: the agent
        that would report their end is the user's. A queued or running task goes to stopped (a running one is not asked
        to stop: its worker cannot answer), out of every queue. A task that changed state in between is skipped.
        Returns the number of tasks stopped."""
        devices = list(Device.objects(company=company, user=user).scalar("id"))
        own = Q(user=user, status__in=(TaskStatus.queued, TaskStatus.in_progress))
        on_device = Q(status=TaskStatus.in_progress, last_device__in=devices)
        stopped = 0
        for task_id in list(Task.objects(own | on_device, company=company).scalar("id")):
            task = (
                Task.objects(id=task_id, company=company)
                .only("id", "status", "project", "execution.queue", "company", "name", "started")
                .first()
            )
            if task is None:
                continue
            try:
                result = TaskBLL.dequeue_and_change_status(
                    task,
                    company,
                    actor,
                    STOP_MESSAGE,
                    STOP_REASON,
                    remove_from_all_queues=True,
                    new_status=TaskStatus.stopped,
                )
            except errors.bad_request.FailedChangingTaskStatus:
                continue  # it moved on in between
            stopped += result.get("updated", 0)
        return stopped

    @staticmethod
    def _drop_shares(company: str, user: str) -> None:
        """The shares granted to the user (a queue shared with one user). The shares the user granted to others stay."""
        Share.objects(company=company, user=user).delete()

    @staticmethod
    def _revoke_devices(company: str, user: str, actor: str) -> None:
        """Revokes the user's devices that are not revoked yet, one audit event each; the rows stay as history. A device
        revoked earlier is not touched and not audited again, so the step can be repeated."""
        live = list(Device.objects(company=company, user=user, revoked=False).scalar("id"))
        for device_id in live:
            # only a device this call revokes is audited: one revoked in between is left to whoever revoked it
            if Device.objects(id=device_id, revoked=False).update(set__revoked=True):
                audit(actor, "device.revoked", company=company, device=device_id, reason="owner deleted")

    @staticmethod
    def _drop_repository_checks(company: str, user: str) -> None:
        """What the user's checks of repositories found; the results are the user's own, of no use to anyone else."""
        RepositoryCheck.objects(company=company, user=user).delete()

    @staticmethod
    def _successor(profile: UserProfile) -> Optional[str]:
        """Who owns the company's accesses of a deleted user from now on: the company's first admin other than the user
        (the call comes from a system user, who is in no company). None when the company has no other admin."""
        admin = (
            AuthUser.objects(company=profile.company, role=Role.admin, id__ne=profile.id)
            .only("id")
            .order_by("created", "id")
            .first()
        )
        return admin.id if admin else None
