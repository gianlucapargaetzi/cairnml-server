"""The code repositories runs use: one source per repository, one binding per root project that uses it.

Any run that names a repository records it (``discover``): the source once per company, whatever the spelling of the
address, and a binding for the run's root project, owned by the project's owner. The binding says how the project reads
the repository: an assigned credential, "None needed", or nothing yet.

``resolve`` is the one place that decides how a repository is read. Everything that asks (the check at enqueue, the token
a job gets, the badge in the web app, the warning before a dispatch) calls it:

1. a credential is assigned: that credential alone. If it cannot be used, the repository is not reachable; nothing else
   is tried instead.
2. "None needed" is assigned: it is cloned without a credential.
3. nothing is assigned: without a credential if the host allows it; otherwise through the company's access (the company
   GitHub App, or the Git credential an admin stored for the host), marked so the web app can ask for an assignment;
   otherwise not reachable.

Whether a repository is cloned without a credential is asked of the host (the git discovery request, see
idp/repo_visibility), never of the GitHub REST API, and the answer is kept on the source.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from mongoengine.errors import NotUniqueError

from apiserver.apierrors import errors
from apiserver.bll import user_access
from apiserver.config_repo import config
from apiserver.database.model.auth import User
from apiserver.database.model.code_repository import CodeRepository, RepositoryBinding
from apiserver.database.model.project import Project
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
""" How a repository is read, see ``resolve``. ANONYMOUS and COMPANY are the unassigned cases. """

APP_ONLY = "a GitHub App is used for GitHub: personal tokens are not"


# --- the records ---------------------------------------------------------------------------------------------------

def _scope_of(company: str, user: str, project_id: Optional[str]) -> tuple:
    """(scope, root project, owner) of a run: its root project and that project's owner, or the user outside any."""
    root = user_access.root_project(company, project_id) if project_id else None
    if root is None:
        return f"user:{user}", None, user
    project = Project.objects(company=company, id=root).only("user").first()
    return root, root, (project.user if project and project.user else user)


def _source_of(company: str, address: repo_address.RepoAddress, now: datetime) -> CodeRepository:
    source = CodeRepository.objects(company=company, key=address.key).first()
    if source is None:
        try:
            source = CodeRepository(id=uuid.uuid4().hex, company=company, key=address.key, host=address.host,
                                    path=address.path, created=now, last_used=now).save(force_insert=True)
        except NotUniqueError:  # another run made it first
            source = CodeRepository.objects(company=company, key=address.key).first()
    return source


def discover(company: str, repository: str, user: str, project_id: Optional[str],
             now: Optional[datetime] = None) -> Optional[RepositoryBinding]:
    """Records the repository a run uses, once per repository and root project. None when the address names no hosted
    repository. Never raises for a record that already exists; it is called when a task is saved and must not break it."""
    address = repo_address.parse(repository)
    if address is None or not user:
        return None
    now = now or datetime.utcnow()
    source = _source_of(company, address, now)
    scope, root, owner = _scope_of(company, user, project_id)
    binding = RepositoryBinding.objects(source=source.id, scope=scope).first()
    if binding is None:
        try:
            binding = RepositoryBinding(
                id=uuid.uuid4().hex, company=company, source=source.id, scope=scope, project=root, owner=owner,
                discovered_by=user, created=now, last_used=now).save(force_insert=True)
        except NotUniqueError:
            binding = RepositoryBinding.objects(source=source.id, scope=scope).first()
    else:
        binding.last_used = now
        binding.save()
    return binding


def discover_quietly(company: str, repository: str, user: str, project_id: Optional[str]) -> None:
    """``discover`` for the places that save a task: a failure is logged and never refuses the save."""
    try:
        discover(company, repository, user, project_id)
    except Exception:
        log.warning("the repository %s could not be recorded", repository, exc_info=True)


def ensure_source(company: str, repository: str, now: Optional[datetime] = None) -> Optional[CodeRepository]:
    """The source of a repository, made when missing; no binding. None when the address names no hosted repository."""
    address = repo_address.parse(repository)
    return _source_of(company, address, now or datetime.utcnow()) if address is not None else None


def lookup(company: str, repository: str, user: str, project_id: Optional[str]) -> Optional[RepositoryBinding]:
    """The binding of the run's scope when it exists; nothing is made."""
    source = source_for(company, repository)
    if source is None:
        return None
    scope, _root, _owner = _scope_of(company, user, project_id)
    return RepositoryBinding.objects(source=source.id, scope=scope).first()


def source_of(binding: RepositoryBinding) -> CodeRepository:
    return CodeRepository.objects(id=binding.source).first()


def github_client(resolution: "Resolution", source: CodeRepository):
    """The GitHub App client a resolution reads with (the assigned App, or the company's), or None when the read is
    anonymous or not through an App."""
    if resolution.state == COMPANY:
        return _github_app()
    if resolution.state == ASSIGNED and resolution.entry.kind == "app":
        secret = store.decrypt(resolution.entry.secret, store.key_material())
        return GitHubApp(resolution.entry.app_id, secret,
                         resolution.entry.api_url or config.get("apiserver.idp.github_api_url"))
    return None


def source_for(company: str, repository: str) -> Optional[CodeRepository]:
    key = repo_address.key_of(repository)
    return CodeRepository.objects(company=company, key=key).first() if key else None


# --- reaching a repository without a credential ---------------------------------------------------------------------

def _fresh(source: CodeRepository, now: datetime) -> bool:
    if source.anonymous_checked is None:
        return False
    ttl = ANONYMOUS_UNKNOWN_TTL if source.anonymous is None else (
        ANONYMOUS_YES_TTL if source.anonymous else ANONYMOUS_NO_TTL)
    return now - source.anonymous_checked < ttl


def anonymous(source: CodeRepository, now: Optional[datetime] = None, force: bool = False) -> Optional[bool]:
    """Whether the repository is cloned without a credential: True, False, or None when the host could not be asked.
    The answer is kept on the source for a while (long when public, short when not, shortest when unknown)."""
    now = now or datetime.utcnow()
    if not force and _fresh(source, now):
        return source.anonymous
    private = is_private(f"https://{source.host}/{source.path}")
    source.anonymous = None if private is None else not private
    source.anonymous_checked = now
    source.save()
    return source.anonymous


# --- how a repository is read ---------------------------------------------------------------------------------------

@dataclass
class Resolution:
    state: str
    entry: Optional[UserAccess] = None
    message: str = ""
    anonymous: Optional[bool] = None


def _github_app():
    from apiserver.idp.github_check import build_from_config

    try:
        return build_from_config()
    except Exception:
        log.warning("the company GitHub App could not be built", exc_info=True)
        return None


def _entry_problem(entry: Optional[UserAccess], source: CodeRepository) -> Optional[str]:
    """Why an assigned credential cannot be used, or None."""
    if entry is None:
        return "the assigned credential no longer exists"
    if entry.scope != "company" and not user_access.is_active(entry.user):
        return f"the owner of {entry.label} is disabled"
    if source.host == "github.com" and entry.kind != "app":
        return f"{entry.label} is a personal token: {APP_ONLY}"
    if source.host != "github.com":
        if entry.kind != "token":
            return f"{entry.label} is a GitHub App, which does not read {source.host}"
        if entry.host != source.host or not store.covers(entry.path or "", source.path.lower()):
            return f"{entry.label} is not valid for {source.host}/{source.path}"
    return None


def _company_reach(source: CodeRepository):
    """(label, None) when the company's access reads the repository, else (None, reason)."""
    if source.host == "github.com":
        app = _github_app()
        if app is None:
            return None, "no company GitHub App is set up"
        try:
            app.installation_token(source.path)
            return "company GitHub App", None
        except RepoNotAccessible:
            return None, "the company GitHub App is not installed on this repository"
        except GitHubError:
            return None, "GitHub could not be reached"
    stored = store.find_credential(source.company, f"https://{source.host}/{source.path}")
    if stored is not None:
        return f"the Git credential stored for {stored.host}", None
    return None, f"no Git credential is stored for {source.host}"


def resolve(source: CodeRepository, binding: Optional[RepositoryBinding], now: Optional[datetime] = None,
            reach: bool = True) -> Resolution:
    """How the binding reads the repository, see the module text. ``reach`` False skips asking GitHub whether the company
    App reaches it (the answer is then COMPANY whenever an App exists)."""
    if binding is not None and binding.credential:
        entry = UserAccess.objects(company=source.company, id=binding.credential).first()
        problem = _entry_problem(entry, source)
        if problem:
            return Resolution(UNREACHABLE, None, f"The assigned credential cannot be used: {problem}.")
        return Resolution(ASSIGNED, entry, f"Read with {entry.label}")
    if binding is not None and binding.none_needed:
        return Resolution(NONE_NEEDED, None, "None needed")
    public = anonymous(source, now)
    if public:
        return Resolution(ANONYMOUS, None, "Readable without a credential; no credential is assigned", True)
    if reach:
        label, why = _company_reach(source)
    else:
        label, why = ("company access", None) if _github_app() is not None or source.host != "github.com" else (None, "no company access")
    if label:
        return Resolution(COMPANY, None, f"Auto-Authenticated via {label}. Assigning credentials is recommended", public)
    if public is None:
        return Resolution(UNKNOWN, None, "The host could not be asked whether this repository is public", None)
    return Resolution(UNREACHABLE, None, f"The repository is private and no credential is assigned ({why}).", False)


def token_for_job(source: CodeRepository, binding: Optional[RepositoryBinding]) -> dict:
    """The answer devices.git_token gives for the repository of a job: ``token`` None when it is cloned without one.
    Refused with the reason when it cannot be read."""
    resolution = resolve(source, binding)
    url = f"https://{source.host}/{source.path}"
    if resolution.state in (NONE_NEEDED, ANONYMOUS, UNKNOWN):
        return {"token": None, "expires_at": None, "username": None, "url": url}
    if resolution.state == UNREACHABLE:
        raise errors.bad_request.ValidationError(f"{source.host}/{source.path}: {resolution.message}")
    if resolution.state == ASSIGNED:
        answer = _token_of(resolution.entry, source)
    else:
        answer = _company_token(source)
    return {**answer, "url": url}


def _app_token(app: GitHubApp, source: CodeRepository, label: str) -> dict:
    try:
        token = app.installation_token(source.path)
    except RepoNotAccessible:
        raise errors.bad_request.ValidationError(f"{label} is not installed on {source.path}") from None
    except GitHubError as exc:
        raise errors.bad_request.ValidationError(f"GitHub did not issue a token for {source.path} ({exc})") from None
    return {"token": token.token, "expires_at": token.expires_at.isoformat(), "username": "x-access-token"}


def _token_of(entry: UserAccess, source: CodeRepository) -> dict:
    secret = store.decrypt(entry.secret, store.key_material())
    if entry.kind == "app":
        app = GitHubApp(entry.app_id, secret, entry.api_url or config.get("apiserver.idp.github_api_url"))
        return _app_token(app, source, f"the GitHub App {entry.label}")
    return {"token": secret, "expires_at": None, "username": getattr(entry, "username", None) or "token"}


def _company_token(source: CodeRepository) -> dict:
    if source.host == "github.com":
        app = _github_app()
        if app is None:
            raise errors.bad_request.ValidationError("the company GitHub App is not set up")
        return _app_token(app, source, "the company GitHub App")
    stored = store.find_credential(source.company, f"https://{source.host}/{source.path}")
    if stored is None:
        raise errors.bad_request.ValidationError(f"no Git credential is stored for {source.host}")
    return {"token": store.secret_of(stored), "expires_at": None, "username": stored.username}


# --- who may assign what ---------------------------------------------------------------------------------------------

OWNER, FIRST_USER, ADMIN = "owner", "first_user", "admin"


def assigner(binding: RepositoryBinding, user: str, is_admin: bool) -> Optional[str]:
    """The right the user has to assign the binding's credential: the project's owner, the first user in that project, an
    admin (company access and "None needed" only), or None."""
    if user_access.is_active(user) is False:
        return None
    if binding.owner == user:
        return OWNER
    if binding.discovered_by == user:
        return FIRST_USER
    return ADMIN if is_admin else None


def _credential_choices(company: str, binding: RepositoryBinding, user: str, right: str) -> list:
    """The credential entries the right lets the user choose from."""
    company_entries = list(UserAccess.objects(company=company, scope="company").order_by("created"))
    if right == ADMIN:
        return company_entries
    if right == OWNER:
        return list(UserAccess.objects(company=company, user=user, scope="user").order_by("created")) + company_entries
    shared = []
    if binding.project:
        shared = [entry for entry, _share in user_access._project_grants_of(company, binding.project)
                  if entry.user == binding.owner and entry.scope != "company"]
    return shared


def assignable(company: str, binding: RepositoryBinding, source: CodeRepository, user: str, is_admin: bool) -> list:
    """The credentials the user may assign to the binding now, as rows for the dialog; the secret is never read."""
    right = assigner(binding, user, is_admin)
    if right is None:
        return []
    rows = []
    for entry in _credential_choices(company, binding, user, right):
        if _entry_problem(entry, source) is None:
            rows.append({"id": entry.id, "name": entry.label, "kind": entry.kind,
                         "company": entry.scope == "company"})
    return rows


def set_credential(company: str, user: str, is_admin: bool, binding_id: str, credential: Optional[str],
                   none_needed: bool = False) -> RepositoryBinding:
    """Assigns a credential to a binding, or "None needed", or clears the assignment. The credential must be one the
    user's right lets them choose, valid for the host, and able to read the repository; "None needed" only when the host
    clones it without a credential."""
    binding = RepositoryBinding.objects(company=company, id=binding_id).first()
    if binding is None:
        raise errors.bad_request.InvalidId("no such repository binding")
    source = CodeRepository.objects(company=company, id=binding.source).first()
    right = assigner(binding, user, is_admin)
    if right is None:
        raise errors.forbidden.NoWritePermission(
            "only the owner of the project, the user who first used this repository there, or an admin may assign credentials")
    if credential and none_needed:
        raise errors.bad_request.ValidationError("choose a credential or None needed, not both")
    if credential:
        entry = next((e for e in _credential_choices(company, binding, user, right) if e.id == credential), None)
        if entry is None:
            raise errors.bad_request.ValidationError(
                "that credential is not one you may assign here: " + (
                    "choose a company credential" if right == ADMIN else
                    "choose a credential the project's owner shared with the project" if right == FIRST_USER else
                    "choose one of your own credentials or the company's"))
        problem = _entry_problem(entry, source)
        if problem:
            raise errors.bad_request.ValidationError(problem)
        _check_reads(entry, source)
        binding.credential, binding.none_needed = entry.id, False
    elif none_needed:
        if anonymous(source, force=True) is not True:
            raise errors.bad_request.ValidationError(
                f"{source.host}/{source.path} cannot be cloned without a credential, so None needed does not apply")
        binding.credential, binding.none_needed = None, True
    else:
        binding.credential, binding.none_needed = None, False
    binding.save()
    return binding


def _check_reads(entry: UserAccess, source: CodeRepository) -> None:
    """The credential must read the repository. A GitHub App is asked for a token for it; a token for another host is
    taken as valid for its host and path prefix (it is not verified until a job runs)."""
    if entry.kind != "app":
        return
    secret = store.decrypt(entry.secret, store.key_material())
    app = GitHubApp(entry.app_id, secret, entry.api_url or config.get("apiserver.idp.github_api_url"))
    try:
        app.installation_token(source.path)
    except RepoNotAccessible:
        raise errors.bad_request.ValidationError(f"the GitHub App {entry.label} is not installed on {source.path}") from None
    except GitHubError as exc:
        raise errors.bad_request.ValidationError(f"GitHub could not confirm {entry.label} reads {source.path} ({exc})") from None


# --- the list ---------------------------------------------------------------------------------------------------------

def _names(ids) -> dict:
    return {user.id: user.name or user.id for user in User.objects(id__in=list(ids)).only("name")}


def _view_binding(binding: RepositoryBinding, source: CodeRepository, resolution: Resolution, right: Optional[str],
                  names: dict, project_names: dict, assignable_rows: list) -> dict:
    return {
        "id": binding.id,
        "project": binding.project,
        "project_name": project_names.get(binding.project) if binding.project else None,
        "owner": binding.owner,
        "owner_name": names.get(binding.owner, binding.owner),
        "discovered_by": binding.discovered_by,
        "discovered_by_name": names.get(binding.discovered_by, binding.discovered_by),
        "credential": binding.credential or None,
        "none_needed": bool(binding.none_needed),
        "state": resolution.state,
        "message": resolution.message,
        "can_assign": right is not None,
        "assignable": assignable_rows,
        "created": binding.created.isoformat() if binding.created else None,
        "last_used": binding.last_used.isoformat() if binding.last_used else None,
    }


def list_for(company: str, user: str, is_admin: bool) -> list:
    """One row per repository, with a binding per project the user can see: an admin sees every binding, anyone else the
    bindings of the projects they own or are a member of, and the ones they discovered."""
    mine = set(user_access.my_root_ids(company, user))
    bindings = [b for b in RepositoryBinding.objects(company=company)
                if is_admin or b.owner == user or b.discovered_by == user or b.project in mine]
    by_source: dict = {}
    for binding in bindings:
        by_source.setdefault(binding.source, []).append(binding)
    sources = {s.id: s for s in CodeRepository.objects(company=company, id__in=list(by_source))}
    names = _names({b.owner for b in bindings} | {b.discovered_by for b in bindings})
    project_names = {p.id: p.name for p in Project.objects(company=company, id__in=[b.project for b in bindings if b.project]).only("name")}
    rows = []
    for source_id, group in by_source.items():
        source = sources.get(source_id)
        if source is None:
            continue
        views = []
        for b in sorted(group, key=lambda b: b.created or datetime.min):
            right = assigner(b, user, is_admin)
            views.append(_view_binding(b, source, resolve(source, b, reach=False), right, names, project_names,
                                       assignable(source.company, b, source, user, is_admin) if right else []))
        rows.append({"id": source.id, "key": source.key, "host": source.host, "path": source.path,
                     "url": f"https://{source.host}/{source.path}", "anonymous": source.anonymous, "bindings": views})
    return sorted(rows, key=lambda row: row["key"])


def add(company: str, user: str, is_admin: bool, repository: str, project_id: Optional[str]) -> RepositoryBinding:
    """Lists a repository before any run has used it: its source and the binding of the given project (the user's own
    scope without one). The user must be a member of the project."""
    address = repo_address.parse(repository)
    if address is None:
        raise errors.bad_request.ValidationError(
            "give the address of a hosted repository, for example https://github.com/owner/name")
    if project_id:
        root = user_access.root_project(company, project_id)
        if root is None:
            raise errors.bad_request.ValidationError("no such project")
        if not is_admin and not user_access.is_member(company, root, user):
            raise errors.forbidden.NoWritePermission("you are not a member of this project")
    return discover(company, address.url, user, project_id)
