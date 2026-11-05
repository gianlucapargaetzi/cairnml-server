"""The datasets of the environment: who may see, change, allocate and delete them, and which credential reads each one.

Visibility is one of ``user`` (the owner, and admins), ``projects`` (the owner, admins and members of the projects the
dataset is allocated to) and ``Public`` (every user of the company). A dataset without a working credential is kept, and
its state says so. See docs/superpowers/specs/2026-11-05-datasets-access-design.md.

The functions that take plain dicts or plain values do no GitHub or database work, so they can be tested without either.
"""

import uuid
from datetime import datetime
from typing import Optional

from apiserver.apierrors import errors
from apiserver.bll import access
from apiserver.bll import dataset_cache
from apiserver.bll import dataset_registry as registry_bll
from apiserver.bll import user_access
from apiserver.bll.dataset_registry import canonical_url, list_entries
from apiserver.config_repo import config
from apiserver.database.model.dataset import VISIBILITIES, Dataset
from apiserver.database.model.project import Project
from apiserver.database.model.user_access import UserAccess
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


def kept_for_repository(company: str, repo: str):
    """The record that says how a job reads this repository: a dataset of its own, or a registry (the repository that
    lists datasets, which a job reads to find their entries). None when the environment keeps neither."""
    return Dataset.objects(company=company, kind__in=["repo", "registry"], ref__iexact=repo).first()


def registry_entries_named(company: str, name: str) -> list:
    """The kept registry entries with this name, from every registry: their refs end with /<name>."""
    return list(Dataset.objects(company=company, kind="registry_entry", ref__endswith=f"/{name}"))


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


def _share_type(dataset: Dataset) -> Optional[str]:
    """The resource type of the dataset's shares. A registry entry has none: it inherits from its registry."""
    if dataset.kind == "registry_entry":
        return None
    return "registry" if dataset.kind == "registry" else "dataset"


def _root_owner(company: str, root: str) -> str:
    """The owner of a root project: the granter of the shares that its allocations give (R20)."""
    return Project.objects(company=company, id=root).only("user").first().user


def _set_public_share(company: str, actor: str, dataset: Dataset, public: bool) -> None:
    """The public share follows the visibility Public, granted by the user who sets it."""
    resource_type = _share_type(dataset)
    if resource_type is None:
        return
    if public:
        access.record_grant(company, actor, resource_type, dataset.id, "public", actor=actor)
    else:
        access.remove_grant(company, resource_type, dataset.id, "public", actor=actor)


def _save(company: str, user: str, kind: str, repo: str, name: str, visibility: str, credential, state: str,
          message: str, now: datetime) -> Dataset:
    dataset = Dataset(
        id=uuid.uuid4().hex, company=company, kind=kind, ref=repo, name=name, owner=user, visibility=visibility,
        projects=[], credential=credential, state=state, state_message=message, created=now, updated=now,
        last_checked=now,
    )
    dataset.save()
    _set_public_share(company, user, dataset, visibility == "Public")
    return dataset


def _check_visibility(visibility: str) -> None:
    if visibility not in VISIBILITIES:
        raise errors.bad_request.ValidationError("the visibility is user, projects or Public")


def add_repo(company: str, user: str, url: str, visibility: str = "user", project: str = None) -> dict:
    """Adds a repository as a dataset. A public one needs no credential. A private one takes the first candidate
    that can read it. It is added either way: with no access, its state and message say so."""
    _check_visibility(visibility)
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
    _check_visibility(visibility)
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
            saved = _save(company, user, "registry_entry", entry_ref, item["name"], visibility, credential, OK, "", now)
            saved.source = item.get("source", "")
            saved.link = item.get("link", "")
            saved.save()
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
        "source": dataset.source or "",
        "link": dataset.link or "",
        "added": dataset.created.isoformat() if dataset.created else None,
        "last_checked": dataset.last_checked.isoformat() if dataset.last_checked else None,
    }


# --- Listing, visibility, allocation and deletion

def _owned(company: str, dataset_id: str) -> Dataset:
    dataset = Dataset.objects(company=company, id=dataset_id).first()
    if dataset is None:
        raise errors.bad_request.ValidationError("no such dataset")
    return dataset


def _visible_rows(company: str, user: str, is_admin: bool) -> list:
    """The datasets the user may see, from the stored records, the registries added before this was kept, and the
    repositories the App finds. Each has its state for this viewer."""
    user_projects = {project["id"] for project in user_access.my_projects(company, user)}
    stored = [view(dataset) for dataset in Dataset.objects(company=company)]
    credential_ids = {
        entry.id for entry in UserAccess.objects(company=company, id__in=[d["credential"] for d in stored if d["credential"]])
    }
    rows = []
    for dataset in stored:
        if visible_to(dataset, user, user_projects, is_admin):
            state, message = credential_state(dataset, credential_ids)
            rows.append({**dataset, "state": state, "state_message": message})
    known = {row["ref"].lower() for row in rows}
    for legacy in registry_bll.list_registries():
        if legacy["repository"].lower() in known:
            continue
        rows.append(_legacy_row(legacy))
        known.add(legacy["repository"].lower())
    app = UserAccess.objects(company=company, scope="company", kind="app").order_by("created").first()
    if app is not None:
        for entry in dataset_cache.cached_list()["datasets"]:
            if entry["repository"].lower() in known:
                continue
            rows.append(_app_row(entry["repository"], app))
            known.add(entry["repository"].lower())
    return rows


def _legacy_row(legacy: dict) -> dict:
    """A registry added before datasets were kept: public, owned by the user who added it."""
    repo = legacy["repository"]
    return {
        "id": f"registry:{repo.lower()}", "kind": "registry", "ref": repo, "name": repo.split("/", 1)[1],
        "owner": legacy["added_by"], "visibility": "Public", "projects": [], "credential": None,
        "state": OK, "state_message": "", "source": "", "link": "",
        "added": legacy["added"].isoformat() if legacy.get("added") else None, "last_checked": None,
    }


def _app_row(repo: str, app: UserAccess) -> dict:
    """A repository the company's App finds: owned by the owner of the App credential, and public to everyone."""
    return {
        "id": f"app:{repo.lower()}", "kind": "repo", "ref": repo, "name": repo.split("/", 1)[1], "owner": app.user,
        "visibility": "Public", "projects": [], "credential": app.id, "state": OK, "state_message": "",
        "source": "", "link": "", "added": None, "last_checked": None,
    }


def list_visible(company: str, user: str, is_admin: bool) -> list:
    """What the Datasets page lists for the user."""
    return _visible_rows(company, user, is_admin)


def set_visibility(company: str, user: str, dataset_id: str, visibility: str, projects: list, is_admin: bool) -> dict:
    """Changes the visibility. Only the owner, or an admin, may. Projects are kept only for ``projects``."""
    dataset = _owned(company, dataset_id)
    if not can_manage(view(dataset), user, is_admin):
        raise errors.bad_request.ValidationError("only the owner of this dataset may change it")
    if visibility not in VISIBILITIES:
        raise errors.bad_request.ValidationError("the visibility is user, projects or Public")
    roots = []
    if visibility == "projects":
        for project_id in projects or []:
            root = user_access.root_project(company, project_id)
            if root is None:
                raise errors.bad_request.ValidationError("no such project")
            roots.append(root)
    previous = set(dataset.projects or [])
    dataset.visibility = visibility
    dataset.projects = roots
    dataset.updated = datetime.utcnow()
    dataset.save()
    _set_public_share(company, user, dataset, visibility == "Public")
    resource_type = _share_type(dataset)
    if resource_type is not None:
        for root in previous - set(roots):
            access.remove_grant(company, resource_type, dataset.id, "projects", root, actor=user)
        for root in roots:
            access.record_grant(company, _root_owner(company, root), resource_type, dataset.id, "projects", root,
                                actor=user)
    return view(dataset)


def _change_allocation(company: str, user: str, dataset_id: str, project: str, is_admin: bool, add: bool) -> dict:
    """Allocates a dataset to a project, or takes it back. The user must be a member of the project and must see the
    dataset. A dataset kept for its owner only is shared with the project by its owner (or an admin)."""
    dataset = _owned(company, dataset_id)
    root = user_access.root_project(company, project)
    if root is None:
        raise errors.bad_request.ValidationError("no such project")
    if not user_access.is_member(company, root, user):
        raise errors.bad_request.ValidationError("you are not a member of this project")
    user_projects = {p["id"] for p in user_access.my_projects(company, user)}
    current = view(dataset)
    if not visible_to(current, user, user_projects, is_admin):
        raise errors.bad_request.ValidationError("no such dataset of yours")
    if add and dataset.visibility == "user":
        if not can_manage(current, user, is_admin):
            raise errors.bad_request.ValidationError("only the owner of this dataset may share it with a project")
        dataset.visibility = "projects"
    if add and dataset.credential:
        # the project uses the dataset's credential, so it must be shared with the project: the owner of the credential
        # shares it here, anyone else needs the owner to share it first
        credential = UserAccess.objects(company=company, id=dataset.credential).first()
        if credential is None:
            raise errors.bad_request.ValidationError("the credential of this dataset no longer exists")
        if root not in (credential.granted_projects or []):
            if credential.user != user and not is_admin:
                raise errors.bad_request.ValidationError(
                    "the credential that reads this dataset is not shared with this project; its owner must share it first")
            user_access.grant(company, credential.user, credential.id, root)
    # the project is recorded whatever the visibility, so a Public dataset shows in the project's list as well
    dataset.projects = [p for p in dataset.projects if p != root] + ([root] if add else [])
    dataset.updated = datetime.utcnow()
    dataset.save()
    resource_type = _share_type(dataset)
    if resource_type is not None:
        if add:
            access.record_grant(company, _root_owner(company, root), resource_type, dataset.id, "projects", root,
                                actor=user)
        else:
            access.remove_grant(company, resource_type, dataset.id, "projects", root, actor=user)
    return view(dataset)


def allocate(company: str, user: str, dataset_id: str, project: str, is_admin: bool) -> dict:
    return _change_allocation(company, user, dataset_id, project, is_admin, add=True)


def unallocate(company: str, user: str, dataset_id: str, project: str, is_admin: bool) -> dict:
    return _change_allocation(company, user, dataset_id, project, is_admin, add=False)


def delete(company: str, user: str, dataset_id: str, is_admin: bool) -> None:
    """Removes the record only. The data stays in its repository, so nothing is deleted there."""
    dataset = _owned(company, dataset_id)
    if not can_manage(view(dataset), user, is_admin):
        raise errors.bad_request.ValidationError("only the owner of this dataset may remove it")
    resource_type = _share_type(dataset)
    dataset.delete()
    if resource_type is not None:
        access.drop_resource_shares(company, resource_type, dataset.id, actor=user)
