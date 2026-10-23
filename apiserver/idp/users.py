"""Users come from the identity provider: a stable id per identity, and the role from the IdP."""

import uuid
from datetime import datetime

from apiserver.config.info import get_default_company
from apiserver.database.model.auth import Role, User


def user_id_for(issuer: str, subject: str) -> str:
    """The same identity always maps to the same user id; no CairnML password is involved."""
    return uuid.uuid5(uuid.NAMESPACE_URL, f"{issuer}|{subject}").hex


def role_for(idp_roles, admin_role: str) -> str:
    return Role.admin if admin_role in idp_roles else Role.user


def upsert_idp_user(issuer: str, subject: str, username: str, idp_roles, admin_role: str) -> User:
    """Create the CairnML user for this identity on first sight; keep its role in step with the IdP."""
    user_id = user_id_for(issuer, subject)
    user = User.objects(id=user_id).first()
    role = role_for(idp_roles, admin_role)
    if user is None:
        user = User(
            id=user_id,
            name=username,
            company=get_default_company(),
            role=role,
            idp_subject=f"{issuer}|{subject}",
            created=datetime.utcnow(),
            idp_disabled=False,
        )
    else:
        user.name = username
        user.role = role
    user.save()
    _ensure_profile(user)
    return user


def _ensure_profile(user: User) -> None:
    """The profile (name, company, preferences) that the API reads for get_current_user."""
    from apiserver.database.model.user import User as Profile

    if Profile.objects(id=user.id).first() is None:
        Profile(id=user.id, company=user.company, name=user.name, created=datetime.utcnow()).save()
    else:
        Profile.objects(id=user.id).update_one(set__name=user.name)
