"""Who may use, change and share a resource: the one place where access is decided.

A resource is a queue, a dataset, a registry or a credential. Its owner, an admin, and a holder of a share each have a
level on it: read, read_write or collaborator. ACTION_NEEDS is the action table; it is the only place where the table
is written. The levels of the share record (apiserver/database/model/share.py) are the same three.

- The owner and an admin (company admin or root) are collaborators.
- Otherwise the highest share that applies: a public share (read), a project share whose project is one the user is in
  and whose granting owner is still an active member of it (bll/user_access.grant_applies), or a user share to this
  user (read).
"""

import uuid
from datetime import datetime
from typing import Optional, Set

from mongoengine import NotUniqueError
from mongoengine import ValidationError as ModelValidationError

from apiserver.apierrors import errors
from apiserver.bll.user_access import grant_applies, is_active, is_member, my_projects, root_project
from apiserver.database.model.auth import Role, User
from apiserver.database.model.dataset import Dataset
from apiserver.database.model.queue import Queue
from apiserver.database.model.share import LEVELS, RESOURCE_TYPES, Share
from apiserver.database.model.user_access import UserAccess

ADMIN_ROLES = (Role.admin, Role.root)

RANK = {level: rank for rank, level in enumerate(LEVELS)}
""" read < read_write < collaborator """

ACTION_NEEDS = {
    "queue": {"use": "read", "change_content": "read_write", "manage_sharing": "collaborator"},
    "dataset": {"use": "read", "change_content": "read_write", "manage_sharing": "collaborator"},
    "registry": {"use": "read", "manage_sharing": "collaborator"},
    "credential": {"use": "read", "manage_sharing": "collaborator"},
}
""" For each resource type, the lowest level that satisfies each action. An action not listed is never allowed. """


def allows(level: Optional[str], action: str, resource_type: str) -> bool:
    needed = ACTION_NEEDS.get(resource_type, {}).get(action)
    if level is None or needed is None or level not in RANK:
        return False
    return RANK[level] >= RANK[needed]


def _owner(company: str, resource_type: str, resource_id: str) -> Optional[str]:
    if resource_type == "queue":
        queue = Queue.objects(company=company, id=resource_id).only("created_by").first()
        return queue.created_by if queue else None
    if resource_type == "credential":
        entry = UserAccess.objects(company=company, id=resource_id).only("user").first()
        return entry.user if entry else None
    if resource_type == "dataset":
        record = Dataset.objects(company=company, id=resource_id, kind__ne="registry").only("owner").first()
    elif resource_type == "registry":
        record = Dataset.objects(company=company, id=resource_id, kind="registry").only("owner").first()
    else:
        return None
    return record.owner if record else None


def _project_share_applies(company: str, share: Share, user: str, project_roots: Set[str]) -> bool:
    if share.project not in project_roots:
        return False
    return grant_applies(
        grant_root=share.project,
        project_root=share.project,
        owner_is_member=is_member(company, share.project, share.granted_by),
        user_is_member=is_member(company, share.project, user),
        owner_active=is_active(share.granted_by),
        user_active=is_active(user),
    )


def level_of(company: str, user: str, is_admin: bool, resource_type: str, resource_id: str,
             project_roots: Set[str]) -> Optional[str]:
    """The highest level the user holds on the resource, or None when nothing gives them one. A deactivated user holds
    no level at all, whatever their ownership, role or share."""
    if user and not is_active(user):
        return None
    owner = _owner(company, resource_type, resource_id)
    if is_admin or (user and owner == user):
        return "collaborator"
    best = None
    shares = Share.objects(company=company, resource_type=resource_type, resource_id=resource_id)
    for share in shares:
        if share.scope == "public":
            level = "read"
        elif share.scope == "user":
            level = "read" if user and share.user == user else None
        else:
            level = share.level if _project_share_applies(company, share, user, project_roots) else None
        if level is not None and (best is None or RANK[level] > RANK[best]):
            best = level
    return best


def require(company: str, user: str, is_admin: bool, resource_type: str, resource_id: str,
            project_roots: Set[str], action: str) -> None:
    """Raises when the user may not perform the action: not-found when they hold no level at all, and forbidden when
    they hold a level that does not allow the action."""
    level = level_of(company, user, is_admin, resource_type, resource_id, project_roots)
    if allows(level, action, resource_type):
        return
    if level is None:
        if resource_type == "queue":
            raise errors.bad_request.InvalidQueueId(id=resource_id)
        raise errors.bad_request.ValidationError(f"no such {resource_type}")
    raise errors.forbidden.NoWritePermission(f"a {level} level does not allow {action} on this {resource_type}")


def _is_admin(company: str, actor: str) -> bool:
    found = User.objects(id=actor, company=company).only("role").first()
    return found is not None and found.role in ADMIN_ROLES


def _roots_of(company: str, actor: str) -> Set[str]:
    return {project["id"] for project in my_projects(company, actor)}


def _require_active(actor: str) -> None:
    if not is_active(actor):
        raise errors.forbidden.NoWritePermission("a deactivated user may not change sharing")


def _require_sharing(company: str, actor: str, resource_type: str, resource_id: str) -> None:
    require(company, actor, _is_admin(company, actor), resource_type, resource_id, _roots_of(company, actor),
            "manage_sharing")


def _create(company: str, granted_by: str, resource_type: str, resource_id: str, scope: str, project: str,
            level: str, user: str) -> Share:
    record = Share(
        id=uuid.uuid4().hex, company=company, resource_type=resource_type, resource_id=resource_id, scope=scope,
        project=project if scope == "projects" else "", user=user if scope == "user" else "", level=level,
        granted_by=granted_by, created=datetime.utcnow(),
    )
    try:
        record.save()
    except ModelValidationError as exc:
        raise errors.bad_request.ValidationError(str(exc)) from None
    except NotUniqueError:
        raise errors.bad_request.ValidationError("the resource is already shared that way") from None
    return record


def list_shares(company: str, actor: str, resource_type: str, resource_id: str) -> list:
    """The shares of a resource, for an actor who may manage its sharing."""
    if resource_type not in RESOURCE_TYPES:
        raise errors.bad_request.ValidationError("no such resource type")
    _require_active(actor)
    _require_sharing(company, actor, resource_type, resource_id)
    return list(Share.objects(company=company, resource_type=resource_type, resource_id=resource_id))


def share(company: str, actor: str, resource_type: str, resource_id: str, scope: str, project: str,
          level: str, user: str = "") -> Share:
    """Grants the resource at the level to a project, to everyone (public) or to one user (user scope, queues only).
    The actor must be active and hold manage_sharing on the resource."""
    _require_active(actor)
    _require_sharing(company, actor, resource_type, resource_id)
    if scope == "projects" and root_project(company, project) != project:
        raise errors.bad_request.ValidationError("no such project")
    return _create(company, actor, resource_type, resource_id, scope, project, level, user)


def unshare(company: str, actor: str, share_id: str) -> None:
    """Removes a share. The actor must be active and hold manage_sharing on the resource it gives."""
    _require_active(actor)
    record = Share.objects(company=company, id=share_id).first()
    if record is None:
        raise errors.bad_request.ValidationError("no such share")
    _require_sharing(company, actor, record.resource_type, record.resource_id)
    record.delete()


# --- Writers' path. A writer that changes an old field calls these in the same operation, after its own check of the
# --- right to make the change. They do not check manage_sharing, because the granter is not always the actor: an
# --- allocation of a queue or a dataset to a project is granted by the owner of the project (R20), and the owner of a
# --- project may allocate what they only use. Both are idempotent.

def find_share(company: str, resource_type: str, resource_id: str, scope: str, project: str = "",
               user: str = "") -> Optional[Share]:
    return Share.objects(company=company, resource_type=resource_type, resource_id=resource_id, scope=scope,
                         project=project, user=user).first()


def _refuse_inactive(actor: str) -> None:
    """The writers' path refuses a deactivated caller, as share() does. An empty actor is the server itself."""
    if actor and not is_active(actor):
        raise errors.forbidden.NoWritePermission("a deactivated user may not change sharing")


def record_grant(company: str, granted_by: str, resource_type: str, resource_id: str, scope: str, project: str = "",
                 user: str = "", level: str = "read", *, actor: str) -> Share:
    """The share of a grant the old field has just gained, written for the actor who changed the field. An existing
    share of the same grant is kept as it is."""
    _refuse_inactive(actor)
    existing = find_share(company, resource_type, resource_id, scope, project, user)
    if existing is not None:
        return existing
    return _create(company, granted_by, resource_type, resource_id, scope, project, level, user)


def remove_grant(company: str, resource_type: str, resource_id: str, scope: str, project: str = "",
                 user: str = "", *, actor: str) -> None:
    """The share of a grant the old field has just lost. Nothing happens when there is none."""
    _refuse_inactive(actor)
    Share.objects(company=company, resource_type=resource_type, resource_id=resource_id, scope=scope,
                  project=project, user=user).delete()


def drop_resource_shares(company: str, resource_type: str, resource_id: str, *, actor: str) -> None:
    """Every share of a resource that has been deleted."""
    _refuse_inactive(actor)
    Share.objects(company=company, resource_type=resource_type, resource_id=resource_id).delete()
