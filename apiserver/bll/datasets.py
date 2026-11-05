"""The datasets of the environment: who may see, change, allocate and delete them, and which credential reads each one.

Visibility is one of ``user`` (the owner, and admins), ``projects`` (the owner, admins and members of the projects the
dataset is allocated to) and ``Public`` (every user of the company). A dataset without a working credential is kept, and
its state says so. See docs/superpowers/specs/2026-11-05-datasets-access-design.md.

The functions that take plain dicts or plain values do no GitHub or database work, so they can be tested without either.
"""

import uuid
from datetime import datetime

from apiserver.apierrors import errors
from apiserver.bll import user_access
from apiserver.bll.dataset_registry import canonical_url, list_entries
from apiserver.config_repo import config
from apiserver.database.model.dataset import Dataset
from apiserver.idp import git_credentials as store
from apiserver.idp.github import GitHubApp, GitHubError, PublicReader, TokenReader, public_repository
from apiserver.idp.github_check import repo_of

NO_ACCESS = "no_access"
OK = "ok"


# --- Rules on plain values

def visible_to(dataset: dict, user: str, user_projects: set, is_admin: bool) -> bool:
    """True when the user may see the dataset under its visibility."""
    if is_admin or dataset.get("owner") == user:
        return True
    visibility = dataset.get("visibility")
    if visibility == "Public":
        return True
    if visibility == "projects":
        return bool(set(dataset.get("projects") or []) & set(user_projects))
    return False


def credential_state(dataset: dict, credential_ids: set) -> tuple:
    """(state, message) for a dataset given the credentials the viewer can still use. A public dataset (no credential)
    is always ok. A dataset whose credential is gone has no access, and the message is the banner text."""
    credential = dataset.get("credential")
    if not credential or credential in credential_ids:
        return OK, ""
    return NO_ACCESS, f"no access to dataset {dataset.get('name')}"


def can_manage(dataset: dict, user: str, is_admin: bool) -> bool:
    """Only the owner, and admins, may change, allocate or delete a dataset."""
    return is_admin or dataset.get("owner") == user


def decide_access(name: str, public: bool, candidates: list, reads) -> tuple:
    """(credential id or None, state, message) for a dataset being added. A public one needs no credential. A private one
    takes the first candidate that can read it; with none, it is still added, with no access. ``candidates`` are the
    access entries in the order they are tried; ``reads(entry)`` says whether one can read the repository."""
    if public:
        return None, OK, ""
    for entry in candidates:
        if reads(entry):
            return entry.id, OK, ""
    return None, NO_ACCESS, f"no access to dataset {name}"


# --- Checks GitHub makes, and the database

def _api_url() -> str:
    return config.get("apiserver.idp.github_api_url")


def _reader(entry, secret: str):
    """The client that reads with this access: the App, or the token."""
    api = entry.api_url or _api_url()
    if entry.kind == "app":
        return GitHubApp(entry.app_id, secret, api)
    return TokenReader(secret, api)


def _can_read(reader, repo: str) -> bool:
    """True when the reader can see the repository's default branch. A GitHub error means no."""
    try:
        reader.default_branch_head(repo)
        return True
    except GitHubError:
        return False


def _candidates(company: str, user: str, project_root, repo_url: str) -> list:
    """(entry, secret) pairs the user may use for the repository: their own accesses, then those granted to the project.
    The order is the order they are tried in."""
    host, path = store.host_and_path(canonical_url(repo_url))
    return user_access.usable_for(company, user, host, path, project_root)


def _exists(company: str, kind: str, ref: str) -> bool:
    return Dataset.objects(company=company, kind=kind, ref__iexact=ref).first() is not None


def _parse_repo(url: str) -> str:
    repo = repo_of(url or "")
    if repo is None:
        raise errors.bad_request.ValidationError("a dataset is a GitHub repository: https://github.com/owner/name")
    return repo


def _public(repo: str) -> bool:
    try:
        return public_repository(_api_url(), repo)
    except GitHubError:
        raise errors.bad_request.ValidationError("GitHub could not be reached") from None


def _project_root(company: str, project):
    return user_access.root_project(company, project) if project else None


def _decide(company: str, user: str, project, url: str, repo: str, name: str) -> tuple:
    """(credential entry or None, the reader that reads with it or None for a public repository, state, message)."""
    public = _public(repo)
    if public:
        return None, PublicReader(_api_url()), OK, ""
    pairs = _candidates(company, user, _project_root(company, project), url)
    secrets = {entry.id: secret for entry, secret in pairs}
    by_id = {entry.id: entry for entry, _ in pairs}
    credential, state, message = decide_access(
        name, False, [entry for entry, _ in pairs],
        lambda entry: _can_read(_reader(entry, secrets[entry.id]), repo),
    )
    if credential is None:
        return None, None, state, message
    entry = by_id[credential]
    return entry, _reader(entry, secrets[credential]), state, message


def _save(company: str, user: str, kind: str, repo: str, name: str, visibility: str, credential, state: str,
          message: str, now: datetime) -> Dataset:
    dataset = Dataset(
        id=uuid.uuid4().hex, company=company, kind=kind, ref=repo, name=name, owner=user, visibility=visibility,
        projects=[], credential=credential, state=state, state_message=message, created=now, updated=now,
        last_checked=now,
    )
    dataset.save()
    return dataset


def add_repo(company: str, user: str, url: str, visibility: str = "user", project: str = None) -> dict:
    """Adds a repository as a dataset. A public one needs no credential. A private one takes the first candidate
    that can read it. It is added either way: with no access, its state and message say so."""
    repo = _parse_repo(url)
    if _exists(company, "repo", repo):
        raise errors.bad_request.ValidationError(f"{repo} is already added")
    name = repo.split("/", 1)[1]
    entry, _reader_used, state, message = _decide(company, user, project, url, repo, name)
    dataset = _save(company, user, "repo", repo, name, visibility, entry.id if entry else None, state, message,
                    datetime.utcnow())
    return view(dataset)


def add_registry(company: str, user: str, url: str, visibility: str = "user", project: str = None) -> dict:
    """Adds a registry, and every dataset listed in it. The registry is checked like a repository. Each entry is saved as a
    dataset of its own, with the registry's visibility and the credential that read the registry. With no access, the
    registry is saved with no entries, and its state says so."""
    repo = _parse_repo(url)
    if _exists(company, "registry", repo):
        raise errors.bad_request.ValidationError(f"the registry {repo} is already added")
    name = repo.split("/", 1)[1]
    entry, reader, state, message = _decide(company, user, project, url, repo, name)
    now = datetime.utcnow()
    if reader is None:
        registry = _save(company, user, "registry", repo, name, visibility, None, state, message, now)
        return view(registry)
    try:
        listed = list_entries(app=reader, registry=repo)
    except GitHubError:
        raise errors.bad_request.ValidationError(f"the registry {repo} could not be read") from None
    credential = entry.id if entry else None
    registry = _save(company, user, "registry", repo, name, visibility, credential, OK, "", now)
    for item in listed:
        entry_ref = f"{repo}/{item['name']}"
        if not _exists(company, "registry_entry", entry_ref):
            _save(company, user, "registry_entry", entry_ref, item["name"], visibility, credential, OK, "", now)
    return view(registry)


def view(dataset: Dataset) -> dict:
    """What the API returns for a dataset: never a secret, only the id of the credential that reads it."""
    return {
        "id": dataset.id,
        "kind": dataset.kind,
        "ref": dataset.ref,
        "name": dataset.name,
        "owner": dataset.owner,
        "visibility": dataset.visibility,
        "projects": list(dataset.projects or []),
        "credential": dataset.credential,
        "state": dataset.state,
        "state_message": dataset.state_message or "",
        "last_checked": dataset.last_checked.isoformat() if dataset.last_checked else None,
    }
