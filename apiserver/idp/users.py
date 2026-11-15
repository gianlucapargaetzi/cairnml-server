"""Users come from the identity provider: a stable id per identity, and the role from the IdP."""

import uuid
from datetime import datetime

from apiserver.config.info import get_default_company
from apiserver.database.model.auth import Role, AuthUser
from apiserver.database.model.user import UserProfile


def user_id_for(issuer: str, subject: str) -> str:
    """The same identity always maps to the same user id; no CairnML password is involved."""
    return uuid.uuid5(uuid.NAMESPACE_URL, f"{issuer}|{subject}").hex


def role_for(idp_roles, admin_role: str) -> str:
    return Role.admin if admin_role in idp_roles else Role.user


def single_admin(user_id: str, role: str, company: str = None) -> str:
    """The role the user may hold: there is one admin account, which owns the shared repositories and the company's credentials.
    A user the identity provider maps to admin stays a user while another account is the admin."""
    if role != Role.admin:
        return role
    other = AuthUser.objects(role__in=[Role.admin, Role.root], id__ne=user_id)
    if company:
        other = other.filter(company=company)
    if other.first() is not None:
        return Role.user
    return role


def upsert_idp_user(issuer: str, subject: str, username: str, idp_roles, admin_role: str) -> AuthUser:
    """Create the CairnML user for this identity on first sight; keep its role in step with the IdP."""
    user_id = user_id_for(issuer, subject)
    user = AuthUser.objects(id=user_id).first()
    role = single_admin(user_id, role_for(idp_roles, admin_role), user.company if user is not None else get_default_company())
    if user is None:
        user = AuthUser(
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


def _ensure_profile(user: AuthUser) -> None:
    """The profile (name, company, preferences) that the API reads for get_current_user."""

    if UserProfile.objects(id=user.id).first() is None:
        UserProfile(id=user.id, company=user.company, name=user.name, created=datetime.utcnow()).save()
    else:
        UserProfile.objects(id=user.id).update_one(set__name=user.name)
