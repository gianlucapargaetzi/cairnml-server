"""Repository records: the address of a repository, the record made when it is used or added, the projects it is listed
in, and what a user sees of it. Nothing here decides a read or a token (bll/repository_access)."""

import logging
import uuid
from datetime import datetime
from typing import List, Optional

from mongoengine.errors import NotUniqueError

from apiserver.apierrors import errors
from apiserver.bll import access, membership
from apiserver.database.model.auth import Role, AuthUser
from apiserver.database.model.project import Project
from apiserver.database.model.repository import RegistryEntry, Repository
from apiserver.database.model.share import Share
from apiserver.idp import repo_address

log = logging.getLogger(__name__)


ASSIGNED, NONE_NEEDED, ANONYMOUS, COMPANY, UNKNOWN, UNREACHABLE = (
    "assigned",
    "none_needed",
    "anonymous",
    "company",
    "unknown",
    "unreachable",
)


def parse(address: str) -> repo_address.RepoAddress:
    """The address of a hosted repository, in any spelling. Refused when it names none."""
    parsed = repo_address.parse(address or "")
    if parsed is None:
        raise errors.bad_request.ValidationError(
            "give the address of a hosted repository, for example https://github.com/owner/name"
        )
    return parsed


def admin_of(company: str) -> Optional[str]:
    """The admin: the one account that owns the shared entries and the company's credentials."""
    found = AuthUser.objects(company=company, role__in=[Role.admin, Role.root]).order_by("created").first()
    return found.id if found else None


def _names(user_ids) -> dict:
    return {user.id: user.name or user.id for user in AuthUser.objects(id__in=list(user_ids)).only("name")}


def find(company: str, address: str) -> Optional[Repository]:
    """The record of a repository, by any spelling of its address."""
    key = repo_address.key_of(address or "")
    return Repository.objects(company=company, key=key).first() if key else None


def get(company: str, repository_id: str) -> Repository:
    repo = Repository.objects(company=company, id=repository_id or "").first()
    if repo is None:
        raise errors.bad_request.ValidationError("no such repository")
    return repo


def _reference_for(company: str, name: str, path: str) -> str:
    taken = (
        Repository.objects(company=company, reference__iexact=name).first() is not None
        or RegistryEntry.objects(company=company, reference__iexact=name).first() is not None
    )
    return path if taken else name


def _root_and_owner(company: str, user: str, project_id: Optional[str]) -> tuple:
    """(root project, its owner) of a run's project; (None, the user) outside any project."""
    root = membership.root_project(company, project_id) if project_id else None
    if root is None:
        return None, user
    project = Project.objects(company=company, id=root).only("user").first()
    return root, (project.user if project and project.user else user)


def _with_role(repo: Repository, role: Optional[str]) -> Repository:
    changed = False
    if role and role not in repo.roles:
        repo.roles = [*repo.roles, role]
        changed = True
    if role == "dataset" and not repo.reference:
        repo.reference = _reference_for(repo.company, repo.name, repo.path)
        changed = True
    if changed:
        repo.updated = datetime.utcnow()
    return repo


def _create(
    company: str, parsed: repo_address.RepoAddress, role: Optional[str], owner: str, now: datetime
) -> Repository:
    repo = Repository(
        id=uuid.uuid4().hex,
        company=company,
        key=parsed.key,
        host=parsed.host,
        path=parsed.path,
        name=parsed.path.rsplit("/", 1)[-1],
        roles=[],
        owner=owner,
        created=now,
        updated=now,
        last_used=now,
    )
    try:
        repo.save(force_insert=True)
    except NotUniqueError:  # another run made it first
        repo = Repository.objects(company=company, key=parsed.key).first()
    _with_role(repo, role)
    repo.save()
    return repo


def is_shared(repo: Repository) -> bool:
    return access.find_share(repo.company, "repository", repo.id, "public") is not None


def level_of(company: str, user: str, is_admin: bool, repo: Repository) -> Optional[str]:
    """The level the user holds on the repository: collaborator for its owner and the admin, read for a shared entry and for an
    entry linked to a project they are in, none otherwise."""
    roots = membership.my_root_ids(company, user)
    level = access.level_of(company, user, is_admin, "repository", repo.id, roots)
    if (
        level is not None
        and user in (repo.dismissed or [])
        and not (roots & {s.project for s in linked_projects(repo)})
    ):
        return None  # deleted for this user, and no project of theirs lists it
    return level


def can_see(company: str, user: str, is_admin: bool, repo: Repository) -> bool:
    return level_of(company, user, is_admin, repo) is not None


def visible(company: str, user: str, is_admin: bool) -> List[Repository]:
    """The repositories the user sees: those they own, the shared ones, and those linked to a project they are in."""
    if not user or not membership.is_active(user):
        return []
    repos = list(Repository.objects(company=company).order_by("name"))
    roots = membership.my_root_ids(company, user)
    listed_in = {}
    for share in Share.objects(company=company, resource_type="repository", scope="projects"):
        listed_in.setdefault(share.resource_id, set()).add(share.project)
    repos = [r for r in repos if user not in (r.dismissed or []) or roots & listed_in.get(r.id, set())]
    if is_admin:
        return repos
    shares = {}
    for share in Share.objects(company=company, resource_type="repository", resource_id__in=[r.id for r in repos]):
        shares.setdefault(share.resource_id, []).append(share)
    return [
        repo
        for repo in repos
        if repo.owner == user
        or any(access._share_level(company, share, user, roots, True, True) for share in shares.get(repo.id, []))
    ]


def require_visible(company: str, user: str, is_admin: bool, repository_id: str) -> Repository:
    """The repository, refused as missing when the user does not see it (the same answer as for one that does not exist)."""
    repo = Repository.objects(company=company, id=repository_id or "").first()
    if repo is None or not can_see(company, user, is_admin, repo):
        raise errors.bad_request.ValidationError("no such repository")
    return repo


def find_reference(company: str, reference: str) -> Optional[Repository]:
    """The repository a dataset reference names: its id, the address of the repository, or its name (unique in the company)."""
    ref = (reference or "").strip()
    if not ref:
        return None
    found = Repository.objects(company=company, id=ref).first()
    if found is None and 8 <= len(ref) < 32 and all(c in "0123456789abcdef" for c in ref.lower()):
        starting = list(Repository.objects(company=company, id__startswith=ref.lower()).limit(2))
        found = starting[0] if len(starting) == 1 else None  # the short id the list shows, when it names one
    if found is None and repo_address.key_of(ref):
        found = find(company, ref)
    if found is None:
        found = Repository.objects(company=company, reference=ref).first()
    if found is None:
        found = Repository.objects(company=company, reference__iexact=ref).first()
    return found


def linked_projects(repo: Repository) -> List[Share]:
    return list(
        Share.objects(company=repo.company, resource_type="repository", resource_id=repo.id, scope="projects").order_by(
            "created"
        )
    )


def delete_blocked(company: str, user: str, is_admin: bool, repo: Repository) -> Optional[str]:
    """Why the user may not delete the repository, or None when they may. A repository a project lists is not deleted: the project
    depends on it. A shared repository is the admin's to delete."""
    if is_shared(repo) and not is_admin:
        return "A shared repository is deleted by the admin only."
    links = linked_projects(repo)
    if links:
        names = [p.name for p in Project.objects(company=company, id__in=[s.project for s in links]).only("name")]
        mine = membership.my_root_ids(company, user)
        seen = sorted(
            p.name
            for p in Project.objects(company=company, id__in=[s.project for s in links if s.project in mine]).only(
                "name"
            )
        )
        where = (
            ", ".join(seen[:3]) + (f" and {len(seen) - 3} more" if len(seen) > 3 else "")
            if seen
            else f"{len(names)} project(s)"
        )
        return f"It is used in {where}: take it out of the project first, because the project depends on it."
    return None
