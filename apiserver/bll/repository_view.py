"""What the repositories page shows of a repository: its rows of credentials and projects, and the list of the repositories a
user sees. Reads through bll/repository_access."""

import logging
from datetime import datetime
from typing import List, Optional, Tuple


from apiserver.bll import access, membership
from apiserver.database.model.project import Project
from apiserver.database.model.repository import Assignment, RegistryEntry, Repository, RepositoryCheck
from apiserver.database.model.share import Share
from apiserver.database.model.user_access import UserAccess

log = logging.getLogger(__name__)


from apiserver.bll.repository_records import (
    UNKNOWN,
    UNREACHABLE,
    _names,
    delete_blocked,
    is_shared,
    level_of,
    linked_projects,
    visible,
)
from apiserver.bll.repository_access import Resolution, credential_choices, resolve


def _result_for(repo: Repository, user: str) -> Tuple[str, str, Optional[datetime]]:
    """(state, message, when) of the check the user's list shows: their own last check, else the shared record (a repository
    nobody of theirs checked). A registry that cannot be read is the same for everybody, so that stays what everybody sees."""
    mine = RepositoryCheck.objects(repository=repo.id, user=user).first()
    if mine is None or ("registry" in repo.roles and repo.state == "no_access"):
        return repo.state, repo.state_message or "", repo.last_checked
    return mine.state, mine.message or "", mine.checked


def _readable(resolution: Resolution) -> Optional[bool]:
    """Whether the repository can be read the way the resolution says: True, False, or None when the host could not be asked."""
    if resolution.state == UNKNOWN:
        return None
    return resolution.state != UNREACHABLE


def _project_use(share, as_: str, names_of_projects: dict, member_roots: set) -> dict:
    return {
        "project": share.project,
        "project_name": names_of_projects.get(share.project),
        "as": as_,
        "member": share.project in member_roots,
        "inherited": bool(share.inherited),
    }


def _row(
    *,
    id_,
    user,
    user_name,
    mine,
    credential,
    credential_name,
    company,
    none_needed,
    readable,
    can_edit,
    used_in,
    last_used,
) -> dict:
    return {
        "id": id_,
        "user": user,
        "user_name": user_name,
        "mine": mine,
        "credential": credential,
        "credential_name": credential_name,
        "company": company,
        "none_needed": none_needed,
        "readable": readable,
        "can_edit": can_edit,
        "used_in": used_in,
        "last_used": last_used,
    }


def _uses_of_credential(
    repo, assignment, links, owners: dict, assigned_users: set, names_of_projects: dict, member_roots: set
) -> list:
    """The projects a credential's user reads for: their own projects (as owner), and the projects of owners with no
    assignment of their own, where they are a member (as fallback)."""
    used = []
    for share in links:
        owner = owners.get(share.project)
        if assignment.user == owner:
            used.append(_project_use(share, "owner", names_of_projects, member_roots))
        elif owner not in assigned_users and membership.is_member(repo.company, share.project, assignment.user):
            used.append(_project_use(share, "fallback", names_of_projects, member_roots))
    return used


def _credential_rows(
    repo,
    user: str,
    assignments,
    links,
    owners: dict,
    assigned_users: set,
    names: dict,
    entries: dict,
    names_of_projects: dict,
    member_roots: set,
    own_state: str,
) -> list:
    """One row for each assignment: the credential in use, and whose it is."""
    rows = []
    for a in assignments:
        entry = entries.get(a.credential) if a.credential else None
        rows.append(
            _row(
                id_=a.id,
                user=a.user,
                user_name=names.get(a.user, a.user),
                mine=a.user == user,
                credential=a.credential or None,
                credential_name=entry.label if entry else ("None needed" if a.none_needed else ""),
                company=bool(entry and entry.scope == "company"),
                none_needed=bool(a.none_needed),
                readable=not (a.user == user and own_state == "no_access"),
                can_edit=a.user == user,
                used_in=_uses_of_credential(repo, a, links, owners, assigned_users, names_of_projects, member_roots),
                last_used=a.last_used.isoformat() if a.last_used else None,
            )
        )
    return rows


def _groups_by_readability(repo, unassigned: list, links, owners: dict, shared_read) -> dict:
    """The owners with no assignment, grouped by whether their projects can be read (the open lock or the closed one)."""
    groups: dict = {}
    for owner in unassigned:
        if shared_read is not None:
            readable = True  # a shared repository is read for everyone with the company credential it was shared with
        else:
            projects_of_owner = [s.project for s in links if owners.get(s.project) == owner]
            readable = _readable(resolve(repo, projects_of_owner[0] if projects_of_owner else None, owner, reach=False))
        groups.setdefault(readable, []).append(owner)
    return groups


def _owner_rows(
    repo,
    user: str,
    unassigned: list,
    links,
    owners: dict,
    shared_read,
    shared_entry,
    names: dict,
    names_of_projects: dict,
    member_roots: set,
) -> list:
    """The "empty" rows: the owners with no assignment, together on one row per readability."""
    rows = []
    for readable, group in _groups_by_readability(repo, unassigned, links, owners, shared_read).items():
        used = [
            _project_use(s, "owner", names_of_projects, member_roots) for s in links if owners.get(s.project) in group
        ]
        who = ", ".join(names.get(o, o) for o in group[:2]) + (f" and {len(group) - 2} more" if len(group) > 2 else "")
        rows.append(
            _row(
                id_="",
                user=group[0] if len(group) == 1 else "",
                user_name=who,
                mine=user in group,
                credential=shared_read.credential if shared_read is not None else None,
                credential_name=(shared_entry.label if shared_entry else "None needed")
                if shared_read is not None
                else "",
                company=shared_entry is not None,
                none_needed=bool(shared_read and shared_read.none_needed),
                readable=readable,
                can_edit=shared_read is None and user in group,
                used_in=used,
                last_used=shared_read.last_used.isoformat()
                if shared_read is not None and shared_read.last_used
                else None,
            )
        )
    return rows


def _assignment_rows(
    repo, user: str, links: List[Share], owners: dict, names_of_projects: dict, member_roots: set, own_state: str
) -> list:
    """One row for each credential in use on the repository, with the projects it reads for, and the "empty" rows: the projects whose
    owner has no assignment, together on one row (two when the host reads them differently: a missing credential is no missing
    access when the repository is public). ``readable`` is whether the row's reading works: the open lock, or the closed one."""
    assignments = list(Assignment.objects(repository=repo.id).order_by("created"))
    assigned_users = {a.user for a in assignments}
    unassigned = sorted(
        {owners[s.project] for s in links if owners.get(s.project) and owners[s.project] not in assigned_users}
    )
    names = _names(assigned_users | set(unassigned))
    entries = {e.id: e for e in UserAccess.objects(id__in=[a.credential for a in assignments if a.credential])}
    shared_read = next((a for a in assignments if a.user == repo.owner), None) if is_shared(repo) else None
    shared_entry = entries.get(shared_read.credential) if shared_read is not None and shared_read.credential else None
    return _credential_rows(
        repo,
        user,
        assignments,
        links,
        owners,
        assigned_users,
        names,
        entries,
        names_of_projects,
        member_roots,
        own_state,
    ) + _owner_rows(
        repo, user, unassigned, links, owners, shared_read, shared_entry, names, names_of_projects, member_roots
    )


def view(company: str, user: str, is_admin: bool, repo: Repository, entries: bool = True) -> dict:
    """One repository as the list shows it to the user: what it is, who owns it, where it is listed, the assignments on it
    (the user's own are editable, the others' are read-only) and how the user reads it now. Never a secret."""
    links = linked_projects(repo)
    projects = list(Project.objects(company=company, id__in=[s.project for s in links]).only("name", "user"))
    owners = {p.id: p.user for p in projects}
    names = {p.id: p.name for p in projects}
    level = level_of(company, user, is_admin, repo)
    shared = is_shared(repo)
    mine_roots = membership.my_root_ids(company, user)
    resolution = resolve(repo, None, user, reach=False)
    owner_name = "Company" if shared else _names([repo.owner]).get(repo.owner, repo.owner)
    state, state_message, last_checked = _result_for(repo, user)
    row = {
        "id": repo.id,
        "key": repo.key,
        "host": repo.host,
        "path": repo.path,
        "url": f"https://{repo.host}/{repo.path}",
        "name": repo.name,
        "reference": repo.reference or "",
        "roles": list(repo.roles),
        "owner": repo.owner,
        "owner_name": owner_name,
        "shared": shared,
        "state": state,
        "state_message": state_message,
        "anonymous": repo.anonymous,
        "added": repo.created.isoformat() if repo.created else None,
        "last_used": repo.last_used.isoformat() if repo.last_used else None,
        "last_checked": last_checked.isoformat() if last_checked else None,
        "projects": [
            {
                "project": s.project,
                "project_name": names.get(s.project),
                "share_id": s.id,
                "inherited": bool(s.inherited),
                "member": s.project in mine_roots,
            }
            for s in links
        ],
        "assignments": _assignment_rows(repo, user, links, owners, names, mine_roots, state),
        "relation": "shared" if shared else ("own" if repo.owner == user else "inherited"),
        "access": {
            "state": resolution.state,
            "message": resolution.message,
            "warning": resolution.warning,
            "readable": _readable(resolution),
        },
        "can_delete": delete_blocked(company, user, is_admin, repo) is None and level is not None,
        "delete_blocked": delete_blocked(company, user, is_admin, repo),
        "can_share": (not shared and (repo.owner == user or is_admin)) or (shared and is_admin),
        "can_assign": level is not None,
        "credentials": [
            {"id": e.id, "name": e.label, "kind": e.kind, "company": e.scope == "company"}
            for e in credential_choices(company, user, repo)
        ],
    }
    if entries and "registry" in repo.roles:
        row["entries"] = [
            {"id": e.id, "name": e.name, "reference": e.reference, "source": e.source, "link": e.link}
            for e in RegistryEntry.objects(company=company, registry=repo.id).order_by("name")
        ]
    return row


def list_for(company: str, user: str, is_admin: bool, project: Optional[str] = None) -> list:
    """The repositories the user sees, one row each. With ``project``, only those listed in that project."""
    repos = visible(company, user, is_admin)
    if project:
        root = membership.root_project(company, project)
        if root is None:
            return []
        listed = set(access.resource_ids_shared_with(company, "repository", root))
        repos = [r for r in repos if r.id in listed]
    return [view(company, user, is_admin, repo) for repo in repos]
