"""The code repositories runs use. A repository record is made the first time a run uses it (its owner is that run's
user). A public repository needs no credential; a credential on it only lifts the rate limit. A private one is read
through the credential attached to it, always: a GitHub App the owner or the company holds (see the dataset rule)."""

import uuid
from datetime import datetime
from typing import Optional

from apiserver.apierrors import errors
from apiserver.bll import datasets as datasets_bll
from apiserver.database.model.code_repository import CodeRepository
from apiserver.database.model.user_access import UserAccess
from apiserver.idp import git_credentials as store
from apiserver.idp.github import GitHubApp, GitHubError, RepoNotAccessible
from apiserver.config_repo import config


def ensure(company: str, repo: str, owner: str, now: datetime) -> CodeRepository:
    """The record of a repository a run uses: made at first use, owned by that run's user, and marked as used now."""
    record = CodeRepository.objects(company=company, ref__iexact=repo).first()
    if record is None:
        record = CodeRepository(id=uuid.uuid4().hex, company=company, ref=repo, owner=owner, created=now, last_used=now)
    else:
        record.last_used = now
    record.save()
    return record


def view(record: CodeRepository) -> dict:
    return {
        "id": record.id,
        "ref": record.ref,
        "owner": record.owner,
        "credential": record.credential or None,
        "created": record.created.isoformat() if record.created else None,
        "last_used": record.last_used.isoformat() if record.last_used else None,
    }


def list_for(company: str, user: str, is_admin: bool) -> list:
    """The repositories the user owns; an admin sees every repository of the company."""
    query = {"company": company} if is_admin else {"company": company, "owner": user}
    return [view(record) for record in CodeRepository.objects(**query).order_by("ref")]


def set_credential(company: str, user: str, repository: str, credential: Optional[str], is_admin: bool) -> dict:
    """Attaches a GitHub App to a repository, or detaches it. The owner of the repository or an admin may do it; the App
    must be the user's own or the company's, and must be able to read the repository."""
    from apiserver.idp.github_check import repo_of

    repo = repo_of(repository or "")
    if repo is None:
        raise errors.bad_request.ValidationError("a repository is on GitHub: https://github.com/owner/name")
    record = CodeRepository.objects(company=company, ref__iexact=repo).first()
    if record is None:
        raise errors.bad_request.ValidationError(f"{repo} is not used by any run yet: its record is made by its first run")
    if record.owner != user and not is_admin:
        raise errors.forbidden.NoWritePermission("only the owner of this repository or an admin may change its credential")
    if credential:
        entry, _secret = datasets_bll._credential_entry(company, user, credential, repo)
        record.credential = entry.id
    else:
        record.credential = None
    record.save()
    return view(record)


def add(company: str, user: str, repository: str, credential: Optional[str]) -> dict:
    """Lists a repository before any run has used it, owned by the caller, with the GitHub App it is read with (none for a
    public repository). A repository the caller already owns is given the credential instead of being refused. The App must
    be the user's own or the company's, and must be able to read the repository."""
    from apiserver.idp.github_check import repo_of

    repo = repo_of(repository or "")
    if repo is None:
        raise errors.bad_request.ValidationError("a repository is on GitHub: https://github.com/owner/name")
    existing = CodeRepository.objects(company=company, ref__iexact=repo).first()
    if existing is not None:
        if existing.owner != user or not credential:
            raise errors.bad_request.ValidationError(
                f"{repo} is already listed" + ("" if existing.owner == user else f" by {existing.owner}"))
        return set_credential(company, user, repository, credential, is_admin=False)
    entry_id = None
    if credential:
        entry, _secret = datasets_bll._credential_entry(company, user, credential, repo)
        entry_id = entry.id
    record = ensure(company, repo, user, datetime.utcnow())
    record.credential = entry_id
    record.save()
    return view(record)


def credential_for_job(company: str, record: CodeRepository, repo: str):
    """(entry, secret) of the App a job reads the repository with: the one attached to it. Its owner's own App, or the
    company's. Refused with the reason when none is attached, or it may not be used for this repository."""
    if not record.credential:
        raise errors.bad_request.ValidationError(
            f"{repo} is private and has no GitHub App attached: its owner attaches one with the repository's credential")
    entry = UserAccess.objects(company=company, id=record.credential).first()
    if entry is None:
        raise errors.bad_request.ValidationError(f"the GitHub App of {repo} no longer exists: its owner must attach one")
    if entry.scope != "company" and entry.user != record.owner:
        raise errors.bad_request.ValidationError(f"the GitHub App of {repo} belongs to someone else than its owner")
    if entry.kind != "app":
        raise errors.bad_request.ValidationError(f"{entry.label} is a personal token: {datasets_bll.APP_ONLY_MESSAGE}")
    return entry, store.decrypt(entry.secret, store.key_material())


def token_for_job(company: str, record: CodeRepository, repo: str) -> dict:
    """The read token a job clones the private repository with, made with the App attached to it."""
    entry, secret = credential_for_job(company, record, repo)
    app = GitHubApp(entry.app_id, secret, entry.api_url or config.get("apiserver.idp.github_api_url"))
    try:
        token = app.installation_token(repo)
    except RepoNotAccessible:
        raise errors.bad_request.ValidationError(f"the GitHub App {entry.label} is not installed on {repo}") from None
    except GitHubError as exc:
        raise errors.bad_request.ValidationError(f"GitHub did not issue a token for {repo} ({exc})") from None
    return {"token": token.token, "expires_at": token.expires_at.isoformat(), "username": "x-access-token"}
