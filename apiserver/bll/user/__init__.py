from datetime import datetime
from typing import Optional

from apiserver.apierrors import errors
from apiserver.apimodels.users import CreateRequest
from apiserver.bll import user_access
from apiserver.config.info import get_version
from apiserver.database.errors import translate_errors_context
from apiserver.database.model.auth import AuthUser, Role
from apiserver.database.model.device import Device
from apiserver.database.model.repository import RepositoryCheck
from apiserver.database.model.share import Share
from apiserver.database.model.user import UserProfile
from apiserver.idp.audit import record as audit


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
        """Deletes the user's profile and the records that belong to the user: their credentials and their assignments
        of repositories (see ``user_access.drop_user_records``), the shares granted to them and their repository checks.
        Their devices are revoked and kept, as audit history (the sign-in record stays, so a device would otherwise
        still be accepted). The records go first, so a failure leaves a user who can be deleted again, never records of
        a missing user. ``actor`` is who deletes, for the audit trail."""
        with translate_errors_context("deleting user"):
            profile = UserProfile.objects(id=user_id).only("id", "company").first()
            if profile is None:
                raise errors.bad_request.InvalidUserId(id=user_id)
            user_access.drop_user_records(profile.company, user_id, actor=actor, successor=UserBLL._successor(profile))
            UserBLL._drop_shares(profile.company, user_id)
            UserBLL._revoke_devices(profile.company, user_id, actor)
            UserBLL._drop_repository_checks(profile.company, user_id)
            res = UserProfile.objects(id=user_id).delete()
            if not res:
                raise errors.bad_request.InvalidUserId(id=user_id)

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
