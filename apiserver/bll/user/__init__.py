from datetime import datetime
from typing import Optional

from apiserver.apierrors import errors
from apiserver.apimodels.users import CreateRequest
from apiserver.bll import user_access
from apiserver.config.info import get_version
from apiserver.database.errors import translate_errors_context
from apiserver.database.model.auth import AuthUser, Role
from apiserver.database.model.user import UserProfile


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
        """Deletes the user's profile and the records that belong to the user: their credentials and their assignments of
        repositories (see ``user_access.drop_user_records``). The records go first, so a failure leaves a user who can be
        deleted again, never records of a missing user. ``actor`` is who deletes, for the audit trail."""
        with translate_errors_context("deleting user"):
            profile = UserProfile.objects(id=user_id).only("id", "company").first()
            if profile is None:
                raise errors.bad_request.InvalidUserId(id=user_id)
            user_access.drop_user_records(profile.company, user_id, actor=actor, successor=UserBLL._successor(profile))
            res = UserProfile.objects(id=user_id).delete()
            if not res:
                raise errors.bad_request.InvalidUserId(id=user_id)

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
