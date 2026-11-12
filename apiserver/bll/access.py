"""Who may use, change and share a resource: the one place where access is decided.

A resource is a queue or a repository (a code repository, a dataset or a registry). Its owner, an admin, and a holder of a share each have a
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

from mongoengine import NotUniqueError, Q
from mongoengine import ValidationError as ModelValidationError

from apiserver.apierrors import errors
from apiserver.bll.user_access import grant_applies, is_active, is_member, my_projects, root_project
from apiserver.database.model.auth import Role, User
from apiserver.database.model.repository import Repository
from apiserver.database.model.project import Project, ProjectRole
from apiserver.database.model.queue import Queue
from apiserver.database.model.share import LEVELS, RESOURCE_TYPES, Share
from apiserver.database.model.task.task import Task

ADMIN_ROLES = (Role.admin, Role.root)

RANK = {level: rank for rank, level in enumerate(LEVELS)}
""" read < read_write < collaborator """

ACTION_NEEDS = {
    "queue": {"use": "read", "change_content": "read_write", "manage_sharing": "collaborator"},
    "repository": {"use": "read", "manage_sharing": "collaborator"},
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
    if resource_type == "repository":
        record = Repository.objects(company=company, id=resource_id).only("owner").first()
        return record.owner if record else None
    return None


def _project_share_applies(company: str, share: Share, user: str, project_roots: Set[str], check_active: bool) -> bool:
    if share.project not in project_roots:
        return False
    return grant_applies(
        grant_root=share.project,
        project_root=share.project,
        owner_is_member=is_member(company, share.project, share.granted_by),
        user_is_member=is_member(company, share.project, user),
        owner_active=is_active(share.granted_by) if check_active else True,
        user_active=is_active(user) if check_active else True,
    )


def _share_level(company: str, share: Share, user: str, project_roots: Set[str], include_projects: bool,
                 check_active: bool) -> Optional[str]:
    """The level one share gives the user, or None when it gives them none."""
    if share.scope == "public":
        return "read"
    if share.scope == "user":
        return "read" if user and share.user == user else None
    if not include_projects:
        return None
    return share.level if _project_share_applies(company, share, user, project_roots, check_active) else None


def level_of(company: str, user: str, is_admin: bool, resource_type: str, resource_id: str,
             project_roots: Set[str], include_projects: bool = True, check_active: bool = True) -> Optional[str]:
    """The highest level the user holds on the resource, or None when nothing gives them one. With check_active a
    deactivated user holds no level at all, whatever their ownership, role or share. include_projects=False leaves out
    the project shares: a queue allocated to a project does not count, as for an agent that serves a queue.
    check_active=False leaves the activity check out; no reader uses it, since a deactivated user holds no level."""
    if check_active and user and not is_active(user):
        return None
    owner = _owner(company, resource_type, resource_id)
    if is_admin or (user and owner == user):
        return "collaborator"
    best = None
    shares = Share.objects(company=company, resource_type=resource_type, resource_id=resource_id)
    for share in shares:
        level = _share_level(company, share, user, project_roots, include_projects, check_active)
        if level is not None and (best is None or RANK[level] > RANK[best]):
            best = level
    return best


def queue_ids_held(company: str, user: str, project_roots: Set[str], include_projects: bool = True,
                   check_active: bool = True) -> Set[str]:
    """The queues the user holds a level on without being an admin: the ones they own, public ones, the ones granted to
    them, and with include_projects the ones allocated to a root project that applies to them (see level_of)."""
    if not user or (check_active and not is_active(user)):
        return set()
    held = set(Queue.objects(company=company, created_by=user).scalar("id"))
    scopes = Q(scope="public") | Q(scope="user", user=user)
    if include_projects:
        scopes |= Q(scope="projects", project__in=list(project_roots))
    for share in Share.objects(Q(company=company, resource_type="queue") & scopes):
        if share.resource_id not in held and _share_level(company, share, user, project_roots, include_projects,
                                                          check_active):
            held.add(share.resource_id)
    return held


def queue_ids_allocated(company: str, user: str, project_roots: Set[str], check_active: bool = True) -> Set[str]:
    """The queues allocated to a root project in project_roots, for which the allocation applies (see level_of)."""
    if not user or not project_roots:
        return set()
    shares = Share.objects(company=company, resource_type="queue", scope="projects", project__in=list(project_roots))
    return {share.resource_id for share in shares
            if _share_level(company, share, user, project_roots, True, check_active)}


def shared_project_ids(company: str, resource_type: str, resource_id: str) -> Set[str]:
    """The root projects a resource is shared with, whether or not the share applies today."""
    shares = Share.objects(company=company, resource_type=resource_type, resource_id=resource_id, scope="projects")
    return {share.project for share in shares}


def project_shares(company: str, resource_type: str, project: str, resource_ids: list) -> dict:
    """The project's share record of each resource, keyed by the resource id: its own level, id and inherited flag."""
    shares = Share.objects(company=company, resource_type=resource_type, scope="projects", project=project,
                           resource_id__in=list(resource_ids))
    return {share.resource_id: share for share in shares}


def resource_ids_shared_with(company: str, resource_type: str, project: str) -> list:
    """The resources shared with a root project, in the order they were shared, whether or not the share applies today."""
    shares = Share.objects(company=company, resource_type=resource_type, scope="projects", project=project).order_by(
        "created")
    return [share.resource_id for share in shares]


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
            level: str, user: str, *, inherited: bool = False, run_id: Optional[str] = None) -> Share:
    record = Share(
        id=uuid.uuid4().hex, company=company, resource_type=resource_type, resource_id=resource_id, scope=scope,
        project=project if scope == "projects" else "", user=user if scope == "user" else "", level=level,
        granted_by=granted_by, created=datetime.utcnow(), inherited=inherited, run_id=run_id,
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


def _project_steward(company: str, project_id: str, actor: str) -> bool:
    """The owner of the root project, or one of its collaborators: the ones who may unlink a repository or take a queue
    from the project (decision 5, R34, R62). A plain member is not one."""
    project = Project.objects(company=company, id=project_id).only("user", "members").first()
    if project is None or not actor:
        return False
    return project.user == actor or any(
        member.user == actor and member.role == ProjectRole.collaborator for member in project.members)


def _may_revoke_from_project(company: str, actor: str, record: Share) -> bool:
    """A repository or queue share of a project may be revoked by the project's owner and collaborators, besides
    the holders of manage_sharing on the resource (see _require_sharing)."""
    return (record.scope == "projects" and record.resource_type in ("repository", "queue")
            and _project_steward(company, record.project, actor))


def unshare(company: str, actor: str, share_id: str) -> None:
    """Removes a share. The actor must be active and either hold manage_sharing on the resource it gives, or, for a
    repository or queue shared with a project, be the project's owner or a collaborator of it."""
    _require_active(actor)
    record = Share.objects(company=company, id=share_id).first()
    if record is None:
        raise errors.bad_request.ValidationError("no such share")
    if not _may_revoke_from_project(company, actor, record):
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


def refuse_inactive(actor: str) -> None:
    """The writers' path refuses a deactivated caller, as share() does. An empty actor is the server itself."""
    if actor and not is_active(actor):
        raise errors.forbidden.NoWritePermission("a deactivated user may not change sharing")


def record_grant(company: str, granted_by: str, resource_type: str, resource_id: str, scope: str, project: str = "",
                 user: str = "", level: str = "read", *, actor: str, inherited: bool = False,
                 run_id: Optional[str] = None) -> Share:
    """The share of a grant the old field has just gained, written for the actor who changed the field. An existing
    share of the same grant is kept as it is. inherited and run_id mark a share the server made because a run used
    the resource (bll/repository_runs)."""
    refuse_inactive(actor)
    existing = find_share(company, resource_type, resource_id, scope, project, user)
    if existing is not None:
        return existing
    return _create(company, granted_by, resource_type, resource_id, scope, project, level, user,
                   inherited=inherited, run_id=run_id)


def remove_grant(company: str, resource_type: str, resource_id: str, scope: str, project: str = "",
                 user: str = "", *, actor: str) -> None:
    """The share of a grant the old field has just lost. Nothing happens when there is none."""
    refuse_inactive(actor)
    Share.objects(company=company, resource_type=resource_type, resource_id=resource_id, scope=scope,
                  project=project, user=user).delete()


def drop_resource_shares(company: str, resource_type: str, resource_id: str, *, actor: str) -> None:
    """Every share of a resource that has been deleted."""
    refuse_inactive(actor)
    Share.objects(company=company, resource_type=resource_type, resource_id=resource_id).delete()


def revoke_member_shares(company: str, project: str, member: str, *, actor: str) -> None:
    """The shares a removed member of a root project granted to it: of every resource type, inherited or not. The shares
    other people granted stay."""
    refuse_inactive(actor)
    Share.objects(company=company, scope="projects", project=project, granted_by=member).delete()


# --- Notices of inherited repositories. A run links the repositories it used to its project (bll/repository_runs);
# --- the user who ran the run sees each such share as a notice, while they still have use on the project.

def _entitled_to_notice(company: str, user: str, share: Share, project_roots: Set[str]) -> bool:
    """Whether the user is entitled to the notice of an inherited repository link: they ran the run it was made for, and the
    link still gives them use on the project (the user is an active member of it, see level_of)."""
    if share.resource_type != "repository" or share.scope != "projects" or not share.inherited or not share.run_id:
        return False
    run = Task.objects(company=company, id=share.run_id).only("user").first()
    if run is None or run.user != user:
        return False
    level = _share_level(company, share, user, project_roots, True, True)
    return allows(level, "use", "repository")


def notices(company: str, user: str) -> list:
    """The notices of inherited repositories the user has not dismissed, newest first."""
    if not user or not is_active(user):
        return []
    roots = _roots_of(company, user)
    shares = Share.objects(company=company, resource_type="repository", scope="projects", inherited=True,
                           dismissed_by__ne=user).order_by("-created")
    found = []
    for share in shares:
        if not _entitled_to_notice(company, user, share, roots):
            continue
        dataset = Repository.objects(company=company, id=share.resource_id).only("name").first()
        project = Project.objects(company=company, id=share.project).only("name").first()
        found.append({
            "id": share.id,
            "run_id": share.run_id,
            "dataset": dataset.name if dataset else "",
            "project": project.name if project else "",
            "created": share.created.isoformat() if share.created else None,
        })
    return found


def dismiss_notice(company: str, user: str, share_id: str) -> None:
    """Records the user's dismissal of a notice they are entitled to. A notice they may not see is not found, and a
    repeated dismissal changes nothing."""
    share = Share.objects(company=company, id=share_id).first()
    if share is None or not _entitled_to_notice(company, user, share, _roots_of(company, user)):
        raise errors.bad_request.ValidationError("no such dataset")
    share.update(add_to_set__dismissed_by=user)
