"""Who may read a repository and with which credential: the resolution of a read (assigned credential, project owner, the
company App or none needed), the tokens it gives, and the anonymous answer of the host. Records come from bll/repository_records."""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Optional


from apiserver.apierrors import errors
from apiserver.bll import membership, user_access
from apiserver.config_repo import config
from apiserver.database.model.project import Project
from apiserver.database.model.repository import Assignment, Repository
from apiserver.database.model.user_access import UserAccess
from apiserver.idp import git_credentials as store
from apiserver.idp.github import GitHubApp, GitHubError, RepoNotAccessible
from apiserver.idp.repo_visibility import is_private

log = logging.getLogger(__name__)


from apiserver.bll.repository_records import (
    ANONYMOUS,
    ASSIGNED,
    COMPANY,
    NONE_NEEDED,
    UNKNOWN,
    UNREACHABLE,
    _names,
    is_shared,
    level_of,
)


ANONYMOUS_YES_TTL = timedelta(hours=24)


ANONYMOUS_NO_TTL = timedelta(minutes=5)


ANONYMOUS_UNKNOWN_TTL = timedelta(minutes=1)


READ_PERMISSIONS = {"contents": "read"}


WRITE_PERMISSIONS = {"contents": "write", "workflows": "write"}


def _entry_problem(entry: Optional[UserAccess], repo: Repository) -> Optional[str]:
    """Why a credential cannot be used for the repository, or None."""
    if entry is None:
        return "the assigned credential no longer exists"
    if entry.scope != "company" and not membership.is_active(entry.user):
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
        raise errors.bad_request.ValidationError(
            f"the GitHub App {entry.label} is not installed on {repo.path}"
        ) from None
    except GitHubError as exc:
        raise errors.bad_request.ValidationError(
            f"GitHub could not confirm {entry.label} reads {repo.path} ({exc})"
        ) from None


def credential_choices(company: str, user: str, repo: Repository) -> List[UserAccess]:
    """The credentials the user may assign to the repository: their own and the company's for a non-shared entry, the company's
    only for a shared one. Only those that are valid for its host."""
    found = list(UserAccess.objects(company=company, scope="company").order_by("created"))
    if not is_shared(repo):
        found = list(UserAccess.objects(company=company, user=user, scope="user").order_by("created")) + found
    return [entry for entry in found if _entry_problem(entry, repo) is None]


def _fresh(repo: Repository, now: datetime) -> bool:
    if repo.anonymous_checked is None:
        return False
    ttl = (
        ANONYMOUS_UNKNOWN_TTL if repo.anonymous is None else (ANONYMOUS_YES_TTL if repo.anonymous else ANONYMOUS_NO_TTL)
    )
    return now - repo.anonymous_checked < ttl


def anonymous(repo: Repository, now: Optional[datetime] = None, force: bool = False) -> Optional[bool]:
    """Whether the repository is cloned without a credential: True, False, or None when the host could not be asked. The answer
    is kept for a while (long when it is, short when not, shortest when unknown)."""
    now = now or datetime.utcnow()
    if not force and _fresh(repo, now):
        return repo.anonymous
    address = f"https://{repo.host}/{repo.path}"
    private = is_private(address)
    if (
        private is None
    ):  # a name that did not resolve, or a host that failed once: ask a second time before saying unknown
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
        return Resolution(
            UNREACHABLE, None, f"The assigned credential cannot be used: {problem}.", None, False, assignment.user
        )
    if fallback:
        who = _names([assignment.user]).get(assignment.user, assignment.user)
        return Resolution(
            ASSIGNED,
            entry,
            f"Read with {entry.label}, the credential of {who}: the project's owner has none "
            "assigned, so assign one for the project",
            None,
            True,
            assignment.user,
        )
    return Resolution(ASSIGNED, entry, f"Read with {entry.label}", None, False, assignment.user)


def resolve(
    repo: Repository, project_root: Optional[str], user: str, now: Optional[datetime] = None, reach: bool = True
) -> Resolution:
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
            return _assigned(found, repo, bool(owner))  # a fallback only inside a project whose owner has none
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
        label, why = (
            ("the company GitHub App", None)
            if user_access.company_apps(repo.company)
            else (None, "no company GitHub App is set up")
        )
    else:
        stored = store.find_credential(repo.company, f"https://{repo.host}/{repo.path}")
        label, why = (
            (f"the Git credential stored for {stored.host}", None)
            if stored is not None
            else (None, f"no Git credential is stored for {repo.host}")
        )
    if label:
        name = label if isinstance(label, str) else f"the company GitHub App {label.label}"
        return Resolution(
            COMPANY,
            None if isinstance(label, str) else label,
            f"Auto-Authenticated via {name}. Assigning credentials is recommended",
            public,
            True,
        )
    if public is None:
        return Resolution(UNKNOWN, None, "The host could not be asked whether this repository is public", None)
    return Resolution(UNREACHABLE, None, f"The repository is private and no credential is assigned ({why}).", False)


def _app_token(app: GitHubApp, repo: Repository, label: str, permissions: Optional[dict] = None) -> dict:
    try:
        token = app.installation_token(repo.path, permissions) if permissions else app.installation_token(repo.path)
    except RepoNotAccessible:
        raise errors.bad_request.ValidationError(f"{label} is not installed on {repo.path}") from None
    except GitHubError as exc:
        hint = ""
        if permissions and "422" in str(exc):
            hint = (
                ": the App must be allowed to write (Contents and Workflows: Read and write); change its permissions "
                "on GitHub and accept them on the installation"
            )
        raise errors.bad_request.ValidationError(
            f"GitHub did not issue a token for {repo.path} ({exc}){hint}"
        ) from None
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
        f"{repo.host}/{repo.path}: to write, assign a GitHub App that is installed on it with write access"
    )


def token_for_job(company: str, repo: Repository, project_id: Optional[str], user: str) -> tuple:
    """(the answer, the resolution) a job's token request gets for the repository, for the project and the user who queued it."""
    root = membership.root_project(company, project_id) if project_id else None
    resolution = resolve(repo, root, user)
    return token_for(repo, resolution), resolution


def token_for_person(company: str, user: str, is_admin: bool, repo: Repository, write: bool = False) -> dict:
    """The token a person's machine gets: read for anyone who sees the repository, write for its owner and the admin. The
    person reads with their own assignment, else without a credential, else with the company's App."""
    if write and level_of(company, user, is_admin, repo) != "collaborator":
        raise errors.forbidden.NoWritePermission("only the owner of a repository or the admin may write to it")
    resolution = resolve(repo, None, user)
    return token_for(repo, resolution, write)
