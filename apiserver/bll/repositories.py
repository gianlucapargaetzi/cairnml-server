"""Repositories: code, datasets and registries of datasets are one kind of record with one credential mechanism.

A repository is one record per company and repository (whatever the spelling of its address), with the roles it is used in.
It is non-shared (owned by a user, seen by the owner and by the projects it is linked to) or shared (owned by the admin, seen
and used by every user). Credentials belong to users, never to projects: an assignment says how one user reads one repository
(with a credential of theirs, or the company's, or without one). See
docs/superpowers/specs/2026-11-11-unified-repositories-design.md.

``resolve`` is the one place that decides how a repository is read for a run in a project by a user. Everything that asks (the
check at enqueue, the token a job or a person gets, the badges, the warning before a dispatch) calls it, in this order:

1. the assignment of the project's owner (a credential that cannot be used makes the repository not readable: nothing else is
   tried instead);
2. the assignment of the user who runs, as a fallback, with a warning to the project;
3. nothing, when the host clones the repository without a credential (asked of the host with the git discovery request, never of
   the REST API);
4. the company's GitHub App, marked so the web app can ask for an assignment;
5. otherwise the repository is not readable.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Optional

from mongoengine.errors import NotUniqueError

from apiserver.apierrors import errors
from apiserver.bll import access, user_access
from apiserver.config_repo import config
from apiserver.database.model.auth import Role, User
from apiserver.database.model.project import Project
from apiserver.database.model.repository import ROLES, Assignment, RegistryEntry, Repository
from apiserver.database.model.share import Share
from apiserver.database.model.user_access import UserAccess
from apiserver.idp import git_credentials as store
from apiserver.idp import repo_address
from apiserver.idp.github import GitHubApp, GitHubError, RepoNotAccessible
from apiserver.idp.repo_visibility import is_private

log = logging.getLogger(__name__)

ANONYMOUS_YES_TTL = timedelta(hours=24)
ANONYMOUS_NO_TTL = timedelta(minutes=5)
ANONYMOUS_UNKNOWN_TTL = timedelta(minutes=1)

ASSIGNED, NONE_NEEDED, ANONYMOUS, COMPANY, UNKNOWN, UNREACHABLE = (
    "assigned", "none_needed", "anonymous", "company", "unknown", "unreachable")
""" How a repository is read, see ``resolve`` """

READ_PERMISSIONS = {"contents": "read"}
WRITE_PERMISSIONS = {"contents": "write", "workflows": "write"}


# --- addresses and records -------------------------------------------------------------------------------------------------

def parse(address: str) -> repo_address.RepoAddress:
    """The address of a hosted repository, in any spelling. Refused when it names none."""
    parsed = repo_address.parse(address or "")
    if parsed is None:
        raise errors.bad_request.ValidationError(
            "give the address of a hosted repository, for example https://github.com/owner/name")
    return parsed


def admin_of(company: str) -> Optional[str]:
    """The admin: the one account that owns the shared entries and the company's credentials."""
    found = User.objects(company=company, role__in=[Role.admin, Role.root]).order_by("created").first()
    return found.id if found else None


def _names(user_ids) -> dict:
    return {user.id: user.name or user.id for user in User.objects(id__in=list(user_ids)).only("name")}


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
    taken = (Repository.objects(company=company, reference__iexact=name).first() is not None
             or RegistryEntry.objects(company=company, reference__iexact=name).first() is not None)
    return path if taken else name


def _root_and_owner(company: str, user: str, project_id: Optional[str]) -> tuple:
    """(root project, its owner) of a run's project; (None, the user) outside any project."""
    root = user_access.root_project(company, project_id) if project_id else None
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


def _create(company: str, parsed: repo_address.RepoAddress, role: Optional[str], owner: str, now: datetime) -> Repository:
    repo = Repository(id=uuid.uuid4().hex, company=company, key=parsed.key, host=parsed.host, path=parsed.path,
                      name=parsed.path.rsplit("/", 1)[-1], roles=[], owner=owner, created=now, updated=now, last_used=now)
    try:
        repo.save(force_insert=True)
    except NotUniqueError:  # another run made it first
        repo = Repository.objects(company=company, key=parsed.key).first()
    _with_role(repo, role)
    repo.save()
    return repo


def record_use(company: str, address: str, role: str, user: str, project_id: Optional[str] = None,
               run_id: Optional[str] = None, now: Optional[datetime] = None) -> Optional[Repository]:
    """Records the repository a run uses. The record is made for the owner of the run's project when there is none (never for
    the user who ran it, unless they own the project), and the repository is linked to the project. A record that exists is
    only linked and given the role. None when the address names no hosted repository."""
    parsed = repo_address.parse(address or "")
    if parsed is None or not user:
        return None
    now = now or datetime.utcnow()
    root, owner = _root_and_owner(company, user, project_id)
    repo = Repository.objects(company=company, key=parsed.key).first()
    if repo is None:
        repo = _create(company, parsed, role, owner, now)
    else:
        _with_role(repo, role)
    repo.last_used = now
    repo.save()
    if root:
        _link(company, repo, root, owner, actor=user, inherited=True, run_id=run_id)
    return repo


def record_use_quietly(company: str, address: str, role: str, user: str, project_id: Optional[str] = None,
                       run_id: Optional[str] = None) -> Optional[Repository]:
    """``record_use`` for the places that save a task: a failure is logged and never refuses the save."""
    try:
        return record_use(company, address, role, user, project_id, run_id)
    except Exception:
        log.warning("the repository %s could not be recorded", address, exc_info=True)
        return None


def add(company: str, user: str, is_admin: bool, address: str, role: str, shared: bool = False,
        project_id: Optional[str] = None, credential: Optional[str] = None, none_needed: bool = False) -> Repository:
    """Lists a repository before any run has used it. A user's entry is non-shared; only the admin may add a shared one."""
    access.refuse_inactive(user)
    if role not in ROLES:
        raise errors.bad_request.ValidationError(f"the role is one of {', '.join(ROLES)}")
    if shared and not is_admin:
        raise errors.forbidden.NoWritePermission("only the admin adds a shared repository")
    parsed = parse(address)
    root = None
    if project_id:
        root = user_access.root_project(company, project_id)
        if root is None:
            raise errors.bad_request.ValidationError("no such project")
        if not is_admin and not user_access.is_member(company, root, user):
            raise errors.forbidden.NoWritePermission("you are not a member of this project")
    now = datetime.utcnow()
    repo = Repository.objects(company=company, key=parsed.key).first()
    if repo is None:
        owner = (admin_of(company) or user) if shared else user
        repo = _create(company, parsed, role, owner, now)
    else:
        if not can_see(company, user, is_admin, repo):
            raise errors.bad_request.ValidationError(
                "this repository is already listed by someone else: ask for it to be shared")
        _with_role(repo, role).save()
    if shared and not is_shared(repo):
        set_shared(company, user, is_admin, repo.id, True)
        repo = get(company, repo.id)
    if root:
        _link(company, repo, root, _root_and_owner(company, user, root)[1], actor=user, inherited=False, run_id=None)
    if credential or none_needed:
        assign(company, user, repo.id, credential, none_needed, is_admin)
    return repo


# --- who sees what ------------------------------------------------------------------------------------------------------------

def is_shared(repo: Repository) -> bool:
    return access.find_share(repo.company, "repository", repo.id, "public") is not None


def level_of(company: str, user: str, is_admin: bool, repo: Repository) -> Optional[str]:
    """The level the user holds on the repository: collaborator for its owner and the admin, read for a shared entry and for an
    entry linked to a project they are in, none otherwise."""
    return access.level_of(company, user, is_admin, "repository", repo.id, user_access.my_root_ids(company, user))


def can_see(company: str, user: str, is_admin: bool, repo: Repository) -> bool:
    return level_of(company, user, is_admin, repo) is not None


def visible(company: str, user: str, is_admin: bool) -> List[Repository]:
    """The repositories the user sees: those they own, the shared ones, and those linked to a project they are in."""
    if not user or not user_access.is_active(user):
        return []
    repos = list(Repository.objects(company=company).order_by("name"))
    if is_admin:
        return repos
    roots = user_access.my_root_ids(company, user)
    shares = {}
    for share in Share.objects(company=company, resource_type="repository", resource_id__in=[r.id for r in repos]):
        shares.setdefault(share.resource_id, []).append(share)
    return [repo for repo in repos if repo.owner == user or any(
        access._share_level(company, share, user, roots, True, True) for share in shares.get(repo.id, []))]


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


# --- links to projects --------------------------------------------------------------------------------------------------------

def _link(company: str, repo: Repository, root: str, root_owner: str, actor: str, inherited: bool, run_id: Optional[str]):
    try:
        return access.record_grant(company, root_owner, "repository", repo.id, "projects", root, level="read", actor=actor,
                                   inherited=inherited, run_id=run_id)
    except errors.bad_request.ValidationError:
        log.warning("the repository %s could not be linked to project %s", repo.key, root, exc_info=True)
        return None
    except errors.forbidden.NoWritePermission:
        return None  # a deactivated user links nothing


def link(company: str, user: str, is_admin: bool, repository_id: str, project_id: str) -> Share:
    """Lists a repository in a project, by hand. The user must see the repository and be a member of the project."""
    access.refuse_inactive(user)
    repo = require_visible(company, user, is_admin, repository_id)
    root = user_access.root_project(company, project_id)
    if root is None:
        raise errors.bad_request.ValidationError("no such project")
    if not is_admin and not user_access.is_member(company, root, user):
        raise errors.forbidden.NoWritePermission("you are not a member of this project")
    created = _link(company, repo, root, _root_and_owner(company, user, root)[1], actor=user, inherited=False, run_id=None)
    if created is None:
        raise errors.bad_request.ValidationError("the repository could not be listed in the project")
    return created


def unlink(company: str, user: str, is_admin: bool, repository_id: str, project_id: str) -> None:
    """Takes a repository out of a project's list: the project's owner and collaborators may, and so may the repository's owner.
    The repository and every assignment stay; a run that uses it again lists it again."""
    access.refuse_inactive(user)
    repo = require_visible(company, user, is_admin, repository_id)
    root = user_access.root_project(company, project_id)
    share = access.find_share(company, "repository", repo.id, "projects", root or "")
    if share is None:
        raise errors.bad_request.ValidationError("the repository is not listed in this project")
    access.unshare(company, user, share.id)


def regrant_project_links(company: str, root: str, new_owner: str, actor: str) -> None:
    """The owner of a project changed: the repositories listed in it are now listed by its new owner (R20)."""
    for repository_id in access.resource_ids_shared_with(company, "repository", root):
        access.remove_grant(company, "repository", repository_id, "projects", root, actor=actor)
        access.record_grant(company, new_owner, "repository", repository_id, "projects", root, actor=actor)


def linked_projects(repo: Repository) -> List[Share]:
    return list(Share.objects(company=repo.company, resource_type="repository", resource_id=repo.id,
                              scope="projects").order_by("created"))


# --- sharing and deleting -------------------------------------------------------------------------------------------------------

def verify_for_sharing(company: str, user: str, is_admin: bool, repository_id: str, credential: Optional[str],
                       none_needed: bool = False) -> None:
    """Raises, with the reason, unless the company credential reads the repository (or the host clones it without a credential,
    for ``none_needed``): what a shared repository is read with must be confirmed before it is shared."""
    repo = require_visible(company, user, is_admin, repository_id)
    if credential and none_needed:
        raise errors.bad_request.ValidationError("choose a company credential or None needed, not both")
    if credential:
        entry = UserAccess.objects(company=company, id=credential, scope="company").first()
        if entry is None:
            raise errors.bad_request.ValidationError("a shared repository is read with a company credential")
        _check_reads(entry, repo)
    elif none_needed:
        answer = anonymous(repo, force=True)
        if answer is not True:
            raise errors.bad_request.ValidationError(
                f"{repo.host}/{repo.path} cannot be cloned without a credential, so None needed does not apply")
    else:
        raise errors.bad_request.ValidationError("choose the company credential that reads it, or None needed for a public repository")


def set_shared(company: str, user: str, is_admin: bool, repository_id: str, shared: bool, credential: Optional[str] = None,
               none_needed: bool = False, confirm: bool = False) -> Repository:
    """Makes an entry shared, or non-shared again. The owner or the admin makes it shared: ownership moves to the admin, and
    the previous owner's assignment is dropped, as is any assignment of a credential that is not the company's (a shared
    entry is read with company credentials only). Only the admin makes it non-shared again. With ``confirm`` the company
    credential (or None needed) that will read it is verified first and becomes the admin's assignment."""
    access.refuse_inactive(user)
    repo = require_visible(company, user, is_admin, repository_id)
    if shared:
        if repo.owner != user and not is_admin:
            raise errors.forbidden.NoWritePermission("only the owner of a repository or the admin shares it")
        if is_shared(repo):
            return repo
        admin = admin_of(company)
        if admin is None:
            raise errors.bad_request.ValidationError("there is no admin to own a shared repository")
        if confirm:
            verify_for_sharing(company, user, is_admin, repository_id, credential, none_needed)
        previous = repo.owner
        repo.owner = admin
        repo.updated = datetime.utcnow()
        repo.save()
        for assignment in Assignment.objects(repository=repo.id):
            entry = UserAccess.objects(id=assignment.credential).first() if assignment.credential else None
            owned_before = assignment.user == previous and previous != admin
            if owned_before or (entry is not None and entry.scope != "company"):
                assignment.delete()
        access.record_grant(company, admin, "repository", repo.id, "public", actor=user)
        if confirm:
            reads = Assignment.objects(repository=repo.id, user=admin).first() or Assignment(
                id=uuid.uuid4().hex, company=company, repository=repo.id, user=admin, created=datetime.utcnow())
            reads.credential, reads.none_needed = (credential or None), bool(none_needed)
            reads.save()
        return repo
    if not is_admin:
        raise errors.forbidden.NoWritePermission("only the admin makes a shared repository non-shared")
    access.remove_grant(company, "repository", repo.id, "public", actor=user)
    return repo


def delete(company: str, user: str, is_admin: bool, repository_id: str) -> None:
    """Removes the record, its entries, its assignments and its links. Only its owner or the admin may. Nothing in the
    repository itself changes."""
    access.refuse_inactive(user)
    repo = require_visible(company, user, is_admin, repository_id)
    if level_of(company, user, is_admin, repo) != "collaborator":
        raise errors.forbidden.NoWritePermission("only the owner of a repository or the admin may delete it")
    RegistryEntry.objects(company=company, registry=repo.id).delete()
    Assignment.objects(repository=repo.id).delete()
    access.drop_resource_shares(company, "repository", repo.id, actor=user)
    repo.delete()


# --- assignments ---------------------------------------------------------------------------------------------------------------

def _entry_problem(entry: Optional[UserAccess], repo: Repository) -> Optional[str]:
    """Why a credential cannot be used for the repository, or None."""
    if entry is None:
        return "the assigned credential no longer exists"
    if entry.scope != "company" and not user_access.is_active(entry.user):
        return f"the owner of {entry.label} is disabled"
    if repo.host == "github.com" and entry.kind != "app":
        return f"{entry.label} is not a GitHub App: GitHub is read with one"
    if repo.host != "github.com":
        if entry.kind != "token":
            return f"{entry.label} is a GitHub App, which does not read {repo.host}"
        if entry.host != repo.host or not store.covers(entry.path or "", repo.path.lower()):
            return f"{entry.label} is not valid for {repo.host}/{repo.path}"
    return None


def _app_of(entry: UserAccess) -> GitHubApp:
    secret = store.decrypt(entry.secret, store.key_material())
    return GitHubApp(entry.app_id, secret, entry.api_url or config.get("apiserver.idp.github_api_url"))


def _check_reads(entry: UserAccess, repo: Repository) -> None:
    """The credential must read the repository. A GitHub App is asked for a token for it; a token for another host is taken as
    valid for its host and path prefix (it is not verified until a job runs)."""
    if entry.kind != "app":
        return
    try:
        _app_of(entry).installation_token(repo.path)
    except RepoNotAccessible:
        raise errors.bad_request.ValidationError(f"the GitHub App {entry.label} is not installed on {repo.path}") from None
    except GitHubError as exc:
        raise errors.bad_request.ValidationError(f"GitHub could not confirm {entry.label} reads {repo.path} ({exc})") from None


def credential_choices(company: str, user: str, repo: Repository) -> List[UserAccess]:
    """The credentials the user may assign to the repository: their own and the company's for a non-shared entry, the company's
    only for a shared one. Only those that are valid for its host."""
    found = list(UserAccess.objects(company=company, scope="company").order_by("created"))
    if not is_shared(repo):
        found = list(UserAccess.objects(company=company, user=user, scope="user").order_by("created")) + found
    return [entry for entry in found if _entry_problem(entry, repo) is None]


def assign(company: str, user: str, repository_id: str, credential: Optional[str] = None, none_needed: bool = False,
           is_admin: bool = False) -> Optional[Assignment]:
    """Assigns the user's own credential (or the company's), or "None needed", to a repository they see; with neither, the
    assignment is cleared. The credential must read the repository. "None needed" only applies when the host clones it
    without a credential. Only the user's own assignment is changed, whoever they are."""
    access.refuse_inactive(user)
    repo = require_visible(company, user, is_admin, repository_id)
    if credential and none_needed:
        raise errors.bad_request.ValidationError("choose a credential or None needed, not both")
    if not credential and not none_needed:
        unassign(company, user, repo.id, is_admin)
        return None
    if credential:
        entry = next((e for e in credential_choices(company, user, repo) if e.id == credential), None)
        if entry is None:
            raise errors.bad_request.ValidationError(
                "that credential is not one you may assign here: " + (
                    "a shared repository is read with a company credential" if is_shared(repo)
                    else "choose one of your own credentials or the company's"))
        _check_reads(entry, repo)
    else:
        answer = anonymous(repo, force=True)
        if answer is None:
            raise errors.bad_request.ValidationError(
                f"{repo.host} could not be asked whether {repo.path} can be cloned without a credential: try again in a moment")
        if answer is not True:
            raise errors.bad_request.ValidationError(
                f"{repo.host}/{repo.path} cannot be cloned without a credential, so None needed does not apply")
    mine = Assignment.objects(repository=repo.id, user=user).first()
    if mine is None:
        mine = Assignment(id=uuid.uuid4().hex, company=company, repository=repo.id, user=user, created=datetime.utcnow())
    mine.credential, mine.none_needed = (credential or None), bool(none_needed)
    mine.save()
    return mine


def unassign(company: str, user: str, repository_id: str, is_admin: bool = False) -> None:
    """Clears the user's own assignment. Nobody else's is touched."""
    access.refuse_inactive(user)
    repo = require_visible(company, user, is_admin, repository_id)
    Assignment.objects(repository=repo.id, user=user).delete()


# --- how a repository is read ---------------------------------------------------------------------------------------------------

def _fresh(repo: Repository, now: datetime) -> bool:
    if repo.anonymous_checked is None:
        return False
    ttl = ANONYMOUS_UNKNOWN_TTL if repo.anonymous is None else (
        ANONYMOUS_YES_TTL if repo.anonymous else ANONYMOUS_NO_TTL)
    return now - repo.anonymous_checked < ttl


def anonymous(repo: Repository, now: Optional[datetime] = None, force: bool = False) -> Optional[bool]:
    """Whether the repository is cloned without a credential: True, False, or None when the host could not be asked. The answer
    is kept for a while (long when it is, short when not, shortest when unknown)."""
    now = now or datetime.utcnow()
    if not force and _fresh(repo, now):
        return repo.anonymous
    address = f"https://{repo.host}/{repo.path}"
    private = is_private(address)
    if private is None:  # a name that did not resolve, or a host that failed once: ask a second time before saying unknown
        private = is_private(address)
    repo.anonymous = None if private is None else not private
    repo.anonymous_checked = now
    repo.save()
    return repo.anonymous


@dataclass
class Resolution:
    state: str
    entry: Optional[UserAccess] = None
    message: str = ""
    anonymous: Optional[bool] = None
    warning: bool = False
    """ the read is a fallback the project should look at: the runner's own credential, or the company's """
    via_user: str = ""
    """ the user whose assignment reads it, for ASSIGNED and NONE_NEEDED """


def _company_reach(repo: Repository):
    """(the company App entry or a label, None) when the company's access reads the repository, else (None, the reason)."""
    if repo.host == "github.com":
        apps = user_access.company_apps(repo.company)
        if not apps:
            return None, "no company GitHub App is set up"
        why = "the company GitHub App is not installed on this repository"
        for entry, key in apps:
            app = GitHubApp(entry.app_id, key, entry.api_url or config.get("apiserver.idp.github_api_url"))
            try:
                app.installation_token(repo.path)
                return entry, None
            except RepoNotAccessible:
                continue
            except GitHubError:
                why = "GitHub could not be reached"
        return None, why
    stored = store.find_credential(repo.company, f"https://{repo.host}/{repo.path}")
    if stored is not None:
        return f"the Git credential stored for {stored.host}", None
    return None, f"no Git credential is stored for {repo.host}"


def _assigned(assignment: Assignment, repo: Repository, fallback: bool) -> Resolution:
    if assignment.none_needed:
        return Resolution(NONE_NEEDED, None, "None needed", True, fallback, assignment.user)
    entry = UserAccess.objects(company=repo.company, id=assignment.credential).first()
    problem = _entry_problem(entry, repo)
    if problem:
        return Resolution(UNREACHABLE, None, f"The assigned credential cannot be used: {problem}.", None, False,
                          assignment.user)
    if fallback:
        who = _names([assignment.user]).get(assignment.user, assignment.user)
        return Resolution(ASSIGNED, entry, f"Read with {entry.label}, the credential of {who}: the project's owner has none "
                          "assigned, so assign one for the project", None, True, assignment.user)
    return Resolution(ASSIGNED, entry, f"Read with {entry.label}", None, False, assignment.user)


def resolve(repo: Repository, project_root: Optional[str], user: str, now: Optional[datetime] = None,
            reach: bool = True) -> Resolution:
    """How the repository is read for a run in the project by the user, see the module text. ``reach`` False skips asking GitHub
    whether the company's App reaches it (the answer is then COMPANY whenever an App exists)."""
    owner = None
    if project_root:
        project = Project.objects(company=repo.company, id=project_root).only("user").first()
        owner = project.user if project else None
    if owner:
        found = Assignment.objects(repository=repo.id, user=owner).first()
        if found is not None:
            return _assigned(found, repo, False)
    if user and user != owner:
        found = Assignment.objects(repository=repo.id, user=user).first()
        if found is not None:
            return _assigned(found, repo, True)
    if is_shared(repo):  # a shared repository is read with the company credential the admin assigned when it was shared
        found = Assignment.objects(repository=repo.id, user=repo.owner).first()
        if found is not None:
            return _assigned(found, repo, False)
    public = anonymous(repo, now)
    if public:
        return Resolution(ANONYMOUS, None, "Readable without a credential; no credential is assigned", True)
    if reach:
        label, why = _company_reach(repo)
    elif repo.host == "github.com":
        label, why = ("the company GitHub App", None) if user_access.company_apps(repo.company) else (
            None, "no company GitHub App is set up")
    else:
        stored = store.find_credential(repo.company, f"https://{repo.host}/{repo.path}")
        label, why = ((f"the Git credential stored for {stored.host}", None) if stored is not None
                      else (None, f"no Git credential is stored for {repo.host}"))
    if label:
        name = label if isinstance(label, str) else f"the company GitHub App {label.label}"
        return Resolution(COMPANY, None if isinstance(label, str) else label,
                          f"Auto-Authenticated via {name}. Assigning credentials is recommended", public, True)
    if public is None:
        return Resolution(UNKNOWN, None, "The host could not be asked whether this repository is public", None)
    return Resolution(UNREACHABLE, None, f"The repository is private and no credential is assigned ({why}).", False)


def _app_token(app: GitHubApp, repo: Repository, label: str, permissions: Optional[dict] = None) -> dict:
    try:
        token = app.installation_token(repo.path, permissions) if permissions else app.installation_token(repo.path)
    except RepoNotAccessible:
        raise errors.bad_request.ValidationError(f"{label} is not installed on {repo.path}") from None
    except GitHubError as exc:
        raise errors.bad_request.ValidationError(f"GitHub did not issue a token for {repo.path} ({exc})") from None
    return {"token": token.token, "expires_at": token.expires_at.isoformat(), "username": "x-access-token"}


def _token_of(entry: UserAccess, repo: Repository, permissions: Optional[dict] = None) -> dict:
    if entry.kind == "app":
        return _app_token(_app_of(entry), repo, f"the GitHub App {entry.label}", permissions)
    secret = store.decrypt(entry.secret, store.key_material())
    return {"token": secret, "expires_at": None, "username": getattr(entry, "username", None) or "token"}


def _company_token(repo: Repository, permissions: Optional[dict] = None) -> dict:
    if repo.host == "github.com":
        reached, _why = _company_reach(repo)
        if reached is None or isinstance(reached, str):
            raise errors.bad_request.ValidationError("no company GitHub App is installed on this repository")
        return _app_token(_app_of(reached), repo, f"the company GitHub App {reached.label}", permissions)
    stored = store.find_credential(repo.company, f"https://{repo.host}/{repo.path}")
    if stored is None:
        raise errors.bad_request.ValidationError(f"no Git credential is stored for {repo.host}")
    return {"token": store.secret_of(stored), "expires_at": None, "username": stored.username}


def token_for(repo: Repository, resolution: Resolution, write: bool = False) -> dict:
    """The answer a job or a person gets for the repository: ``token`` None when it is cloned without one. Refused with the
    reason when it cannot be read. A write needs a GitHub App with write access, never "none"."""
    url = f"https://{repo.host}/{repo.path}"
    permissions = WRITE_PERMISSIONS if write else None
    if resolution.state in (NONE_NEEDED, ANONYMOUS, UNKNOWN) and not write:
        return {"token": None, "expires_at": None, "username": None, "url": url}
    if resolution.state == UNREACHABLE:
        raise errors.bad_request.ValidationError(f"{repo.host}/{repo.path}: {resolution.message}")
    if resolution.state == ASSIGNED:
        answer = {**_token_of(resolution.entry, repo, permissions), "url": url}
        if resolution.via_user:
            Assignment.objects(repository=repo.id, user=resolution.via_user).update(last_used=datetime.utcnow())
        return answer
    if resolution.state == COMPANY:
        return {**_company_token(repo, permissions), "url": url}
    raise errors.bad_request.ValidationError(
        f"{repo.host}/{repo.path}: to write, assign a GitHub App that is installed on it with write access")


def token_for_job(company: str, repo: Repository, project_id: Optional[str], user: str) -> tuple:
    """(the answer, the resolution) a job's token request gets for the repository, for the project and the user who queued it."""
    root = user_access.root_project(company, project_id) if project_id else None
    resolution = resolve(repo, root, user)
    return token_for(repo, resolution), resolution


def token_for_person(company: str, user: str, is_admin: bool, repo: Repository, write: bool = False) -> dict:
    """The token a person's machine gets: read for anyone who sees the repository, write for its owner and the admin. The
    person reads with their own assignment, else without a credential, else with the company's App."""
    if write and level_of(company, user, is_admin, repo) != "collaborator":
        raise errors.forbidden.NoWritePermission("only the owner of a repository or the admin may write to it")
    resolution = resolve(repo, None, user)
    return token_for(repo, resolution, write)


# --- checking ---------------------------------------------------------------------------------------------------------------------

def check(company: str, user: str, is_admin: bool, repository_id: str) -> Repository:
    """Looks at the repository again: asks the host whether it is cloned without a credential (the kept answer is renewed) and
    tries the user's own assignment. Stores what it finds on the record."""
    repo = require_visible(company, user, is_admin, repository_id)
    anonymous(repo, force=True)
    resolution = resolve(repo, None, user)
    state, message = "ok", ""
    mine = Assignment.objects(repository=repo.id, user=user).first()
    if mine is not None and mine.credential and resolution.state == ASSIGNED:
        mine.update(last_used=datetime.utcnow())
        try:
            _check_reads(resolution.entry, repo)
        except errors.bad_request.ValidationError as refused:
            state, message = "no_access", f"The assigned credential cannot read it: {refused.msg}."
    elif resolution.state == UNREACHABLE:
        state, message = "no_access", resolution.message
    now = datetime.utcnow()
    repo.state, repo.state_message, repo.last_checked, repo.updated = state, message, now, now
    repo.save()
    return repo


# --- what the API shows -------------------------------------------------------------------------------------------------------------

def _assignment_rows(repo: Repository, user: str, links: List[Share], owners: dict, names_of_projects: dict,
                     member_roots: set) -> list:
    """One row per user who reads the repository, with the projects each reads for: an assignment is a row, and a project whose
    owner has no assignment gets a row of the owner without a credential (state ``public`` when the host allows it, else ``none``)."""
    assignments = list(Assignment.objects(repository=repo.id).order_by("created"))
    assigned_users = {a.user for a in assignments}
    unassigned = sorted({owners[s.project] for s in links if owners.get(s.project) and owners[s.project] not in assigned_users})
    names = _names(assigned_users | set(unassigned))
    entries = {e.id: e for e in UserAccess.objects(id__in=[a.credential for a in assignments if a.credential])}

    def project(share, as_):
        return {"project": share.project, "project_name": names_of_projects.get(share.project), "as": as_,
                "member": share.project in member_roots, "inherited": bool(share.inherited)}

    rows = []
    for a in assignments:
        used = []
        for share in links:
            owner = owners.get(share.project)
            if a.user == owner:
                used.append(project(share, "owner"))
            elif owner not in assigned_users and user_access.is_member(repo.company, share.project, a.user):
                used.append(project(share, "fallback"))
        entry = entries.get(a.credential) if a.credential else None
        rows.append({
            "id": a.id, "user": a.user, "user_name": names.get(a.user, a.user), "mine": a.user == user,
            "credential": a.credential or None,
            "credential_name": entry.label if entry else ("None needed" if a.none_needed else ""),
            "company": bool(entry and entry.scope == "company"), "none_needed": bool(a.none_needed),
            "state": "public" if a.none_needed else "ok",
            "can_edit": a.user == user, "used_in": used,
            "last_used": a.last_used.isoformat() if a.last_used else None,
        })
    shared_read = next((a for a in assignments if a.user == repo.owner), None) if is_shared(repo) else None
    shared_entry = entries.get(shared_read.credential) if shared_read is not None and shared_read.credential else None
    for owner in unassigned:
        if shared_read is not None:  # a shared repository is read for everyone with the company credential it was shared with
            rows.append({
                "id": "", "user": owner, "user_name": names.get(owner, owner), "mine": owner == user,
                "credential": shared_read.credential or None,
                "credential_name": shared_entry.label if shared_entry else "None needed", "company": shared_entry is not None,
                "none_needed": bool(shared_read.none_needed), "state": "public" if shared_read.none_needed else "ok",
                "can_edit": False, "used_in": [project(s, "owner") for s in links if owners.get(s.project) == owner],
                "last_used": shared_read.last_used.isoformat() if shared_read.last_used else None,
            })
            continue
        rows.append({
            "id": "", "user": owner, "user_name": names.get(owner, owner), "mine": owner == user, "credential": None,
            "credential_name": "", "company": False, "none_needed": False,
            "state": "public" if repo.anonymous else "none", "can_edit": owner == user,
            "used_in": [project(s, "owner") for s in links if owners.get(s.project) == owner], "last_used": None,
        })
    return rows


def view(company: str, user: str, is_admin: bool, repo: Repository, entries: bool = True) -> dict:
    """One repository as the list shows it to the user: what it is, who owns it, where it is listed, the assignments on it
    (the user's own are editable, the others' are read-only) and how the user reads it now. Never a secret."""
    links = linked_projects(repo)
    projects = list(Project.objects(company=company, id__in=[s.project for s in links]).only("name", "user"))
    owners = {p.id: p.user for p in projects}
    names = {p.id: p.name for p in projects}
    level = level_of(company, user, is_admin, repo)
    shared = is_shared(repo)
    mine_roots = user_access.my_root_ids(company, user)
    resolution = resolve(repo, None, user, reach=False)
    owner_name = "Company" if shared else _names([repo.owner]).get(repo.owner, repo.owner)
    row = {
        "id": repo.id, "key": repo.key, "host": repo.host, "path": repo.path, "url": f"https://{repo.host}/{repo.path}",
        "name": repo.name, "reference": repo.reference or "", "roles": list(repo.roles), "owner": repo.owner,
        "owner_name": owner_name, "shared": shared, "state": repo.state, "state_message": repo.state_message or "",
        "anonymous": repo.anonymous, "added": repo.created.isoformat() if repo.created else None,
        "last_used": repo.last_used.isoformat() if repo.last_used else None,
        "last_checked": repo.last_checked.isoformat() if repo.last_checked else None,
        "projects": [{"project": s.project, "project_name": names.get(s.project), "share_id": s.id,
                      "inherited": bool(s.inherited), "member": s.project in mine_roots} for s in links],
        "assignments": _assignment_rows(repo, user, links, owners, names, mine_roots),
        "relation": "shared" if shared else ("own" if repo.owner == user else "inherited"),
        "access": {"state": resolution.state, "message": resolution.message, "warning": resolution.warning},
        "can_delete": level == "collaborator",
        "can_share": (not shared and (repo.owner == user or is_admin)) or (shared and is_admin),
        "can_assign": level is not None,
        "credentials": [{"id": e.id, "name": e.label, "kind": e.kind, "company": e.scope == "company"}
                        for e in credential_choices(company, user, repo)],
    }
    if entries and "registry" in repo.roles:
        row["entries"] = [{"id": e.id, "name": e.name, "reference": e.reference, "source": e.source, "link": e.link}
                          for e in RegistryEntry.objects(company=company, registry=repo.id).order_by("name")]
    return row


def list_for(company: str, user: str, is_admin: bool, project: Optional[str] = None) -> list:
    """The repositories the user sees, one row each. With ``project``, only those listed in that project."""
    repos = visible(company, user, is_admin)
    if project:
        root = user_access.root_project(company, project)
        if root is None:
            return []
        listed = set(access.resource_ids_shared_with(company, "repository", root))
        repos = [r for r in repos if r.id in listed]
    return [view(company, user, is_admin, repo) for repo in repos]
