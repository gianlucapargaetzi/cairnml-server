"""Who belongs to what: the project membership and the active state of users that every access decision reads.

Nothing here decides access on its own; bll/access and the repository resolution (bll/repositories) use these answers.
The rule for a grant is grant_applies (see bll/access).
"""

from typing import Optional

from mongoengine import Q

from apiserver.database.model.auth import AuthUser
from apiserver.database.model.project import Project


def root_project(company: str, project_id: Optional[str]) -> Optional[str]:
    """The root project of a project (grants are made on root projects); None when there is no such project."""
    current = Project.objects(company=company, id=project_id).only("id", "parent").first() if project_id else None
    seen = set()
    while current is not None and current.parent and current.id not in seen:
        seen.add(current.id)
        current = Project.objects(company=company, id=current.parent).only("id", "parent").first()
    return current.id if current is not None else None


def is_member(company: str, project_id: str, user: str) -> bool:
    """The owner of the project, or one of its members (any role)."""
    project = Project.objects(company=company, id=project_id).only("user", "members").first()
    if project is None or not user:
        return False
    return project.user == user or any(member.user == user for member in project.members)


def is_active(user: str) -> bool:
    return bool(user) and AuthUser.objects(id=user, idp_disabled=True).only("id").first() is None


def grant_applies(
    grant_root: str,
    project_root: Optional[str],
    owner_is_member: bool,
    user_is_member: bool,
    owner_active: bool,
    user_active: bool,
) -> bool:
    """The one rule for a grant: it applies to the project it names, while its owner is still a member and active, and
    the user is still a member and active."""
    return project_root == grant_root and owner_is_member and user_is_member and owner_active and user_active


def my_root_ids(company: str, user: str) -> set:
    """The ids of the root projects the user owns or is a member of, as bll/access reads them."""
    return {project["id"] for project in my_projects(company, user)}


def my_projects(company: str, user: str) -> list:
    """The root projects the user owns or is a member of: the ones they may share an access with."""
    projects = Project.objects(Q(company=company) & Q(parent=None) & (Q(user=user) | Q(members__user=user))).only(
        "id", "name"
    )
    return [{"id": project.id, "name": project.name} for project in projects]
