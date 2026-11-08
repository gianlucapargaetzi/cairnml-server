"""The datasets of the environment: who may see, change, allocate and delete them, and which credential reads each one.

Visibility is one of ``user`` (the owner, and admins), ``projects`` (the owner, admins and members of the projects the
dataset is allocated to) and ``Public`` (every user of the company). A dataset without a working credential is kept, and
its state says so. See docs/superpowers/specs/2026-11-05-datasets-access-design.md.

The functions that take plain dicts or plain values do no GitHub or database work, so they can be tested without either.
"""

import json
import re
import uuid
from datetime import datetime
from typing import Optional

from apiserver.apierrors import errors
from apiserver.bll import access
from apiserver.bll import dataset_cache
from apiserver.bll import user_access
from apiserver.bll.dataset_registry import canonical_url, list_entries, read_metadata
from apiserver.config_repo import config
from apiserver.database.model.auth import User
from apiserver.database.model.dataset import VISIBILITIES, Dataset
from apiserver.database.model.project import Project
from apiserver.database.model.share import Share
from apiserver.database.model.user_access import UserAccess
from apiserver.idp import git_credentials as store
from apiserver.idp.github import GitHubApp, GitHubError, PublicReader, RepoNotAccessible, TokenReader, public_repository
from apiserver.idp.github_check import repo_of

NO_ACCESS = "no_access"
OK = "ok"
# --- Rules on plain values

def credential_state(dataset: dict, credential_ids: set) -> tuple:
    """(state, message) for a dataset given the credentials the viewer can still use. A public dataset (no credential)
    is always ok. A dataset whose credential is gone has no access, and the message is the banner text."""
    credential = dataset.get("credential")
    if not credential or credential in credential_ids:
        return OK, ""
    return NO_ACCESS, f"no access to dataset {dataset.get('name')}"


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
    """True when the reader can see the repository, empty or not. A GitHub error means no."""
    try:
        return reader.repository_visible(repo)
    except GitHubError:
        return False


def _candidates(company: str, user: str, project_root, repo_url: str) -> list:
    """(entry, secret) pairs the user may use for the repository: their own accesses, then those granted to the project.
    The order is the order they are tried in."""
    host, path = store.host_and_path(canonical_url(repo_url))
    return user_access.usable_for(company, user, host, path, project_root)


_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def resolve(company: str, reference: str) -> Optional[Dataset]:
    """The stored dataset a reference names: its id, its name (names are unique in the company), or the address of its
    repository (a URL, or owner/name with github.com). None when there is none. Who may see it is checked by the caller."""
    ref = (reference or "").strip()
    if not ref:
        return None
    if _ID_RE.match(ref):
        return Dataset.objects(company=company, id=ref).first()
    repo = repo_of(ref)  # reads the host of the address: a name that merely contains "github.com" is not one
    if repo:
        return kept_for_repository(company, repo)
    return Dataset.objects(company=company, reference=ref).first()


def kept_for_repository(company: str, repo: str):
    """The record that says how a job reads this repository: a dataset of its own, or a registry (the repository that
    lists datasets, which a job reads to find their entries). None when the environment keeps neither."""
    return Dataset.objects(company=company, kind__in=["repo", "registry"], ref__iexact=repo).first()


def registry_entries_named(company: str, name: str) -> list:
    """The kept registry entries with this name, from every registry, the first added first (refs end with /<name>)."""
    return list(Dataset.objects(company=company, kind="registry_entry", ref__endswith=f"/{name}").order_by("created"))


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


def _project_roots(company: str, user: str) -> set:
    """The root projects the user owns or is a member of: the ones a project share may name for them."""
    return {project["id"] for project in user_access.my_projects(company, user)}


def _record_visible(company: str, user: str, is_admin: bool, dataset: Dataset, roots: set) -> bool:
    """True when the user holds a level on the stored record through the share records (bll/access). A registry entry has
    no share of its own: it is visible when its registry is. With no registry listing it, its owner and admins see it."""
    if dataset.kind == "registry_entry":
        registry = Dataset.objects(company=company, kind="registry",
                                   ref__iexact=dataset.ref.rsplit("/", 1)[0]).first()
        if registry is not None:
            return _record_visible(company, user, is_admin, registry, roots)
        return user_access.is_active(user) and (is_admin or dataset.owner == user)
    resource_type = "registry" if dataset.kind == "registry" else "dataset"
    level = access.level_of(company, user, is_admin, resource_type, dataset.id, roots)
    return access.allows(level, "use", resource_type)


def can_see(company: str, user: str, is_admin: bool, dataset_id: str) -> bool:
    """True when the user may see the stored dataset, registry or registry entry with this id."""
    record = Dataset.objects(company=company, id=dataset_id).first()
    return record is not None and _record_visible(company, user, is_admin, record, _project_roots(company, user))


def _gate_of(company: str, dataset: Dataset) -> tuple:
    """The resource type and id whose share levels decide an action on the dataset. A registry entry has no share of its
    own: its registry decides, and without one, its owner and admins."""
    if dataset.kind == "registry":
        return "registry", dataset.id
    if dataset.kind == "registry_entry":
        registry = Dataset.objects(company=company, kind="registry",
                                   ref__iexact=dataset.ref.rsplit("/", 1)[0]).first()
        if registry is not None:
            return "registry", registry.id
    return "dataset", dataset.id


def require_action(company: str, user: str, is_admin: bool, dataset: Dataset, action: str) -> None:
    """Raises unless the user's level on the dataset allows the action (bll/access ACTION_NEEDS)."""
    resource_type, resource_id = _gate_of(company, dataset)
    access.require(company, user, is_admin, resource_type, resource_id, _project_roots(company, user), action)


def _app_row_level(company: str, user: str, is_admin: bool, repo: str) -> Optional[str]:
    """The level the user holds on a repository the company's App finds and no record keeps: the rows list_visible shows
    for it. The company's App is the company's shared read access, so every active user holds read on what it finds, and
    nothing more is asked of the user. The owner of the App credential, and admins, hold a collaborator level. None when
    the App does not list the repository, or the user is deactivated."""
    app = UserAccess.objects(company=company, scope="company", kind="app").order_by("created").first()
    if app is None or not user_access.is_active(user):
        return None
    listed = {str(entry["repository"]).lower() for entry in dataset_cache.cached_list()["datasets"]}
    if repo.lower() not in listed:
        return None
    return "collaborator" if is_admin or user == app.user else "read"


def held_on_repository(company: str, user: str, is_admin: bool, repo: str) -> tuple:
    """(level, resource type) the user holds on the dataset of this repository. The kept record decides when the user
    holds a level on it through its shares (bll/access). Otherwise the repository is the App's own, as list_visible
    shows it: the level comes from _app_row_level, but only when no record keeps the repository. A repository with a record
    is never the App's own row, so the level is None when its record gives the user none."""
    dataset = kept_for_repository(company, repo)
    if dataset is not None:
        resource_type, resource_id = _gate_of(company, dataset)
        level = access.level_of(company, user, is_admin, resource_type, resource_id, _project_roots(company, user))
        return level, resource_type
    return _app_row_level(company, user, is_admin, repo), "dataset"


def _require_sharing(company: str, user: str, is_admin: bool, dataset: Dataset) -> None:
    """The sharing actions refuse a user without the manage_sharing level as a bad request, as they always have."""
    try:
        require_action(company, user, is_admin, dataset, "manage_sharing")
    except errors.forbidden.NoWritePermission:
        raise errors.bad_request.ValidationError(
            "only the owner or a collaborator of this dataset may change its sharing") from None


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


def reference_of(kind: str, ref: str, name: str) -> str:
    """The name a run uses for a dataset: the repository name, or <registry>/<name> for an entry of a registry (whose ref
    is <owner>/<registry>/<name>)."""
    if kind == "registry_entry":
        return f"{ref.split('/')[1]}/{name}"
    return name


def _check_reference(company: str, reference: str) -> None:
    """Refuses a reference another dataset of the company already holds: a name must resolve to one dataset."""
    taken = Dataset.objects(company=company, reference=reference).first()
    if taken is not None:
        raise errors.bad_request.ValidationError(
            f"the name {reference} is already used by {taken.ref}: a dataset name must be unique in the company")


def _save(company: str, user: str, kind: str, repo: str, name: str, visibility: str, credential, state: str,
          message: str, now: datetime) -> Dataset:
    if visibility == "Public" and kind != "registry_entry":
        access.refuse_inactive(user)
    reference = reference_of(kind, repo, name)
    _check_reference(company, reference)
    dataset = Dataset(
        id=uuid.uuid4().hex, company=company, kind=kind, ref=repo, name=name, reference=reference, owner=user,
        visibility=visibility, credential=credential, state=state, state_message=message, created=now, updated=now,
        last_checked=now,
    )
    dataset.save()
    _set_public_share(company, user, dataset, visibility == "Public")
    return dataset


def _check_visibility(visibility: str) -> None:
    if visibility not in VISIBILITIES:
        raise errors.bad_request.ValidationError("the visibility is user, projects or Public")


APP_ONLY_MESSAGE = (
    "personal access tokens are no longer used for GitHub: add a GitHub App (your own, or the company's), and install it "
    "on the repository"
)


# --- The credential a repository dataset is read with. The dataset stores the choice: no credential means the
# repository is public, and a private one with no credential cannot be read.

def _github_url(repo: str) -> str:
    return f"https://github.com/{repo}"


def _credential_entry(company: str, user: str, credential: str, repo: str) -> tuple:
    """(entry, secret) of a credential the user may choose for a dataset: their own, or the company's. It must be able to
    read the repository, or the choice is refused with the reason."""
    entry = UserAccess.objects(company=company, id=credential).first()
    if entry is None:
        raise errors.bad_request.ValidationError(f"no such credential: {credential}")
    if entry.scope != "company" and entry.user != user:
        raise errors.bad_request.ValidationError(
            f"the credential {entry.label} is not yours: choose one of your own credentials, or the company's")
    if entry.scope != "company" and not user_access.is_active(user):
        raise errors.bad_request.ValidationError("your account is disabled: its credentials cannot be used")
    if entry.kind != "app":
        raise errors.bad_request.ValidationError(f"{entry.label} is a personal token: {APP_ONLY_MESSAGE}")
    secret = store.decrypt(entry.secret, store.key_material())
    if not _can_read(_reader(entry, secret), repo):
        raise errors.bad_request.ValidationError(f"the GitHub App {entry.label} is not installed on {repo}")
    return entry, secret


def token_for_person(company: str, user: str, dataset: Dataset, repo: str, write: bool, permissions: dict) -> dict:
    """The GitHub token a person's machine gets for a dataset repository: made with the credential the dataset is read
    with. A public dataset has no credential and needs no token (the caller answers that before this). A personal token
    reads only, because it cannot be limited to one repository. Refused with the reason otherwise."""
    if not dataset.credential:
        raise errors.bad_request.ValidationError(
            f"{repo} has no credential, so it is not readable here: its owner adds one with cairnml dataset add-credential")
    entry = UserAccess.objects(company=company, id=dataset.credential).first()
    if entry is None:
        raise errors.bad_request.ValidationError(f"the credential of {repo} no longer exists: its owner must add one")
    if entry.scope != "company" and entry.user != user:
        raise errors.bad_request.ValidationError(
            f"the credential of {repo} belongs to someone else, so it cannot be used for you")
    if entry.scope != "company" and not user_access.is_active(user):
        raise errors.bad_request.ValidationError("your account is disabled: its credentials cannot be used")
    if entry.kind != "app":
        raise errors.bad_request.ValidationError(f"{entry.label} is a personal token: {APP_ONLY_MESSAGE}")
    secret = store.decrypt(entry.secret, store.key_material())
    app = GitHubApp(entry.app_id, secret, entry.api_url or _api_url())
    try:
        token = app.installation_token(repo, permissions)
    except RepoNotAccessible:
        suffix = " with write access to its contents" if write else ""
        raise errors.bad_request.ValidationError(
            f"the GitHub App {entry.label} is not installed on {repo}{suffix}") from None
    except GitHubError as exc:
        raise errors.bad_request.ValidationError(f"GitHub did not issue a token for {repo} ({exc})") from None
    return {"token": token.token, "expires_at": token.expires_at.isoformat(), "username": "x-access-token"}


def job_can_read(company: str, enqueuer: Optional[str], task_root: Optional[str], dataset: Dataset) -> bool:
    """Whether a job may read a stored dataset or registry: the one who queued it must see it (through its share or its
    own), or the job's project must hold it by an allocation. Project grants on credentials do not apply to datasets."""
    if not enqueuer or not user_access.is_active(enqueuer):
        return False
    roots = _project_roots(company, enqueuer) | ({task_root} if task_root else set())
    return _record_visible(company, enqueuer, False, dataset, roots)


def job_credential(company: str, dataset: Dataset, repo: str) -> tuple:
    """(entry, secret) of the credential a job reads a dataset with: the one the dataset holds, and no other. It must
    still exist, be the company's or belong to the dataset's owner, and be able to read the repository's files for a job
    (a personal token only when its owner is the dataset's owner). Refused with the reason otherwise."""
    if not dataset.credential:
        raise errors.bad_request.ValidationError(
            f"{repo} has no credential, so jobs cannot read it: its owner adds one with cairnml dataset add-credential")
    entry = UserAccess.objects(company=company, id=dataset.credential).first()
    if entry is None:
        raise errors.bad_request.ValidationError(f"the credential of {repo} no longer exists: its owner must add one")
    if entry.scope != "company" and entry.user != dataset.owner:
        raise errors.bad_request.ValidationError(
            f"the credential of {repo} belongs to someone else than its owner, so jobs cannot use it")
    if entry.scope != "company" and not user_access.is_active(entry.user):
        raise errors.bad_request.ValidationError(f"the owner of the credential of {repo} is disabled")
    if entry.kind != "app":
        raise errors.bad_request.ValidationError(f"{entry.label} is a personal token: {APP_ONLY_MESSAGE}")
    return entry, store.decrypt(entry.secret, store.key_material())


def reader_for_dataset(company: str, dataset: Dataset, repo: str):
    """The reader that shows a dataset's details: through the credential the dataset holds (see job_credential). A dataset
    with no credential is read as a public repository, which needs none; a private one then cannot be read."""
    if not dataset.credential:
        return PublicReader(_api_url())
    entry, secret = job_credential(company, dataset, repo)
    return _reader(entry, secret)


def add_repo(company: str, user: str, url: str, visibility: str = "user", credential: str = None) -> dict:
    """Adds a repository as a dataset, read with the credential given (an access id of the user's, or the company's).
    With no credential it is public: a private one is added with no access, and its message says what to add. A
    credential that cannot read the repository is refused."""
    _check_visibility(visibility)
    repo = _parse_repo(url)
    if _exists(company, "repo", repo):
        existing = Dataset.objects(company=company, kind="repo", ref__iexact=repo).first()
        if existing.owner != user or not credential:
            raise errors.bad_request.ValidationError(
                f"{repo} is already added" + ("" if existing.owner == user else f" by {existing.owner}"))
        # the owner registers their own repository again, with a credential: the credential is set on the record
        return set_credential(company, user, url, credential, is_admin=False)
    name = repo.split("/", 1)[1]
    if credential:
        entry, _secret = _credential_entry(company, user, credential, repo)
        credential_id, state, message = entry.id, OK, ""
    elif _public(repo):
        credential_id, state, message = None, OK, ""
    else:
        credential_id, state, message = None, NO_ACCESS, (
            f"{repo} is private and has no credential: add one with cairnml dataset add-credential")
    dataset = _save(company, user, "repo", repo, name, visibility, credential_id, state, message, datetime.utcnow())
    return view(dataset)


def set_credential(company: str, user: str, repository: str, credential: Optional[str], is_admin: bool) -> dict:
    """Sets the credential a repository dataset is read with, or clears it (the dataset is then public). Only its owner
    or an admin may change it, and the credential must be usable as in add_repo."""
    repo = _parse_repo(repository)
    dataset = Dataset.objects(company=company, kind="repo", ref__iexact=repo).first()
    if dataset is None:
        raise errors.bad_request.ValidationError(f"{repo} is not a dataset here: add it with cairnml dataset remote add")
    if dataset.owner != user and not is_admin:
        raise errors.forbidden.NoWritePermission("only the owner of this dataset or an admin may change its credential")
    if credential:
        entry, _secret = _credential_entry(company, user, credential, repo)
        dataset.credential, dataset.state, dataset.state_message = entry.id, OK, ""
    else:
        dataset.credential = None
        if _public(repo):
            dataset.state, dataset.state_message = OK, ""
        else:
            dataset.state = NO_ACCESS
            dataset.state_message = f"{repo} is private and has no credential: add one with cairnml dataset add-credential"
    dataset.updated = datetime.utcnow()
    dataset.save()
    return view(dataset)


def _read_registry(reader, repo: str) -> list:
    """The entries of the registry, each with its metadata: the one read of GitHub that stores them (when the registry is
    added and when it is refreshed). A GitHub error refuses the read with its reason; a metadata file that is not JSON
    refuses it too (bll/dataset_registry.read_metadata)."""
    try:
        listed = list_entries(app=reader, registry=repo)
        for item in listed:
            item["metadata"] = read_metadata(reader, repo, item["name"])
    except GitHubError as exc:
        raise errors.bad_request.ValidationError(f"the registry {repo} could not be read ({exc})") from None
    return listed


def _dump(metadata: Optional[dict]) -> str:
    return json.dumps(metadata, sort_keys=True) if metadata is not None else ""


def _sync_entries(company: str, registry: Dataset, listed: list, now: datetime) -> dict:
    """Brings the stored entries of the registry in line with what it lists: a new entry is added, one whose data changed
    is updated, and one the registry no longer lists is removed. Each entry takes the registry's credential and
    visibility. Returns the counts added, updated and removed."""
    existing = {record.ref.lower(): record for record in
                Dataset.objects(company=company, kind="registry_entry", ref__istartswith=f"{registry.ref}/")}
    counts = {"added": 0, "updated": 0, "removed": 0}
    listed_refs = set()
    # every new entry's name must be free before any is stored: a collision refuses the whole registry
    for item in listed:
        if f"{registry.ref}/{item['name']}".lower() not in existing:
            _check_reference(company, reference_of("registry_entry", f"{registry.ref}/{item['name']}", item["name"]))
    for item in listed:
        ref = f"{registry.ref}/{item['name']}"
        listed_refs.add(ref.lower())
        content = {"source": item.get("source") or "", "link": item.get("link") or "",
                   "metadata": _dump(item.get("metadata")), "credential": registry.credential,
                   "state": OK, "state_message": ""}
        record = existing.get(ref.lower())
        if record is None:
            Dataset(id=uuid.uuid4().hex, company=company, kind="registry_entry", ref=ref, name=item["name"],
                    reference=reference_of("registry_entry", ref, item["name"]), owner=registry.owner,
                    visibility=registry.visibility, created=now, updated=now, last_checked=now, read_at=now,
                    **content).save()
            counts["added"] += 1
            continue
        changed = any(getattr(record, key) != value for key, value in content.items())
        for key, value in content.items():
            setattr(record, key, value)
        record.read_at = now
        record.last_checked = now
        if changed:
            record.updated = now
            counts["updated"] += 1
        record.save()
    for ref, record in existing.items():
        if ref not in listed_refs:
            record.delete()
            counts["removed"] += 1
    return counts


def add_registry(company: str, user: str, url: str, visibility: str = "user", project: str = None) -> dict:
    """Adds a registry, and every dataset listed in it. The registry is checked like a repository, and read once, here:
    each entry is stored as a dataset of its own, with its metadata, the registry's visibility and the credential that
    read the registry. A registry that cannot be read is refused with the reason and nothing is written. With no access,
    the registry is saved with no entries, and its state says so."""
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
    listed = _read_registry(reader, repo)
    credential = entry.id if entry else None
    registry = _save(company, user, "registry", repo, name, visibility, credential, OK, "", now)
    registry.read_at = now
    registry.save()
    _sync_entries(company, registry, listed, now)
    return view(registry)


def _refresh_access(company: str, user: str, registry: Dataset) -> tuple:
    """(credential entry or None, reader or None, message) for a refresh: the credential that read the registry when it
    still reads it, otherwise what adding the registry would use, with the refreshing user's own accesses."""
    stored = UserAccess.objects(company=company, id=registry.credential).first() if registry.credential else None
    if stored is not None:
        try:
            reader = _reader(stored, store.decrypt(stored.secret, store.key_material()))
        except store.CredentialUnreadable:
            reader = None
        if reader is not None and _can_read(reader, registry.ref):
            return stored, reader, ""
    entry, reader, _state, message = _decide(company, user, None, canonical_url(registry.ref), registry.ref,
                                             registry.name)
    return entry, reader, message


def refresh_registry(company: str, user: str, dataset_id: str, is_admin: bool) -> dict:
    """Reads the registry again from GitHub and brings its stored entries in line with it (_sync_entries). Only the owner
    and admins may: refreshing needs the manage_sharing level of the registry. A GitHub error refuses the refresh with its
    reason, and the stored data stays as it was. Returns the counts added, updated and removed."""
    access.refuse_inactive(user)
    registry = Dataset.objects(company=company, id=dataset_id).first()
    if registry is None or registry.kind != "registry":
        raise errors.bad_request.ValidationError("no such registry")
    require_action(company, user, is_admin, registry, "manage_sharing")
    entry, reader, message = _refresh_access(company, user, registry)
    if reader is None:
        raise errors.bad_request.ValidationError(message)
    listed = _read_registry(reader, registry.ref)
    now = datetime.utcnow()
    registry.credential = entry.id if entry else None
    registry.state = OK
    registry.state_message = ""
    registry.read_at = now
    registry.last_checked = now
    registry.updated = now
    registry.save()
    return _sync_entries(company, registry, listed, now)


def _shared_projects(dataset: Dataset) -> list:
    """The root projects the dataset is shared with, from its share records. A registry entry has no share of its own."""
    resource_type = _share_type(dataset)
    if resource_type is None:
        return []
    return sorted(access.shared_project_ids(dataset.company, resource_type, dataset.id))


def _project_shares_of(dataset: Dataset) -> list:
    """The project shares of the dataset from its share records: the level, the share id and whether it is inherited
    from a run, for each root project it is shared with."""
    resource_type = _share_type(dataset)
    if resource_type is None:
        return []
    shares = Share.objects(company=dataset.company, resource_type=resource_type, resource_id=dataset.id,
                           scope="projects").order_by("created")
    return [{"project": share.project, "level": share.level, "share_id": share.id, "inherited": bool(share.inherited)}
            for share in shares]


def view(dataset: Dataset) -> dict:
    """What the API returns for a dataset: never a secret, only the id of the credential that reads it."""
    return {
        "id": dataset.id,
        "kind": dataset.kind,
        "ref": dataset.ref,
        "name": dataset.name,
        "reference": dataset.reference or "",
        "owner": dataset.owner,
        "visibility": dataset.visibility,
        "projects": _shared_projects(dataset),
        "project_shares": _project_shares_of(dataset),
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


def _refuse_entry(dataset: Dataset) -> None:
    """A registry entry is shared through its registry as a whole, never on its own: it has no share record of its own."""
    if dataset.kind == "registry_entry":
        raise errors.bad_request.ValidationError("a registry entry is shared through its registry")


def _visible_rows(company: str, user: str, is_admin: bool) -> list:
    """The datasets the user may see, from the stored records and the repositories the App finds. Each has its state for
    this viewer."""
    roots = _project_roots(company, user)
    records = list(Dataset.objects(company=company))
    stored = [view(dataset) for dataset in records]
    credential_ids = {
        entry.id for entry in UserAccess.objects(company=company, id__in=[d["credential"] for d in stored if d["credential"]])
    }
    rows = []
    for record, dataset in zip(records, stored):
        if _record_visible(company, user, is_admin, record, roots):
            state, message = credential_state(dataset, credential_ids)
            rows.append({**dataset, "state": state, "state_message": message})
    known = {record.ref.lower() for record in records}
    app = UserAccess.objects(company=company, scope="company", kind="app").order_by("created").first()
    if app is not None and user_access.is_active(user):
        for entry in dataset_cache.cached_list()["datasets"]:
            if entry["repository"].lower() in known:
                continue
            known.add(entry["repository"].lower())
            # what the company's App finds is the company's shared read access: every active user sees it
            rows.append(_app_row(entry["repository"], app))
    return rows


def _app_row(repo: str, app: UserAccess) -> dict:
    """A repository the company's App finds, with no record: owned by the owner of the App credential, and shown to every
    user of the company (the App is the company's shared read access)."""
    return {
        "id": f"app:{repo.lower()}", "kind": "repo", "ref": repo, "name": repo.split("/", 1)[1], "owner": app.user,
        "visibility": "Public", "projects": [], "credential": app.id, "state": OK, "state_message": "",
        "source": "", "link": "", "added": None, "last_checked": None,
    }


def list_visible(company: str, user: str, is_admin: bool) -> list:
    """What the Datasets page lists for the user."""
    return _visible_rows(company, user, is_admin)


def regrant_project_shares(company: str, root: str, new_owner: str, actor: str) -> None:
    """The owner of a project changed: the dataset shares that the project gives are now granted by its new owner (R20).
    The shares are re-granted by the new owner."""
    for resource_type in ("dataset", "registry"):
        for resource_id in access.resource_ids_shared_with(company, resource_type, root):
            access.remove_grant(company, resource_type, resource_id, "projects", root, actor=actor)
            access.record_grant(company, new_owner, resource_type, resource_id, "projects", root, actor=actor)


def set_visibility(company: str, user: str, dataset_id: str, visibility: str, projects: list, is_admin: bool) -> dict:
    """Changes the visibility. Only the owner, or an admin, may. Projects are kept only for ``projects``."""
    access.refuse_inactive(user)
    dataset = _owned(company, dataset_id)
    _refuse_entry(dataset)
    _require_sharing(company, user, is_admin, dataset)
    if visibility not in VISIBILITIES:
        raise errors.bad_request.ValidationError("the visibility is user, projects or Public")
    roots = []
    if visibility == "projects":
        for project_id in projects or []:
            root = user_access.root_project(company, project_id)
            if root is None:
                raise errors.bad_request.ValidationError("no such project")
            roots.append(root)
    resource_type = _share_type(dataset)
    previous = set(access.shared_project_ids(company, resource_type, dataset.id)) if resource_type else set()
    dataset.visibility = visibility
    dataset.updated = datetime.utcnow()
    dataset.save()
    _set_public_share(company, user, dataset, visibility == "Public")
    if resource_type is not None:
        for root in previous - set(roots):
            access.remove_grant(company, resource_type, dataset.id, "projects", root, actor=user)
        for root in roots:
            access.record_grant(company, _root_owner(company, root), resource_type, dataset.id, "projects", root,
                                actor=user)
    return view(dataset)


def _change_allocation(company: str, user: str, dataset_id: str, project: str, is_admin: bool, add: bool) -> dict:
    """Allocates a dataset to a project, or takes it back. The user must be a member of the project and must see the
    dataset. Sharing needs the manage_sharing level on the dataset. Taking it back is also allowed to the owner of the
    project it is taken from (the spec's revoking rule)."""
    access.refuse_inactive(user)
    dataset = _owned(company, dataset_id)
    _refuse_entry(dataset)
    root = user_access.root_project(company, project)
    if root is None:
        raise errors.bad_request.ValidationError("no such project")
    if not user_access.is_member(company, root, user):
        raise errors.bad_request.ValidationError("you are not a member of this project")
    if not _record_visible(company, user, is_admin, dataset, _project_roots(company, user)):
        raise errors.bad_request.ValidationError("no such dataset of yours")
    project_owner = _root_owner(company, root) == user
    if add or not project_owner:
        _require_sharing(company, user, is_admin, dataset)
    if add and dataset.visibility == "user":
        dataset.visibility = "projects"
    if add and dataset.credential:
        # the project uses the dataset's credential, so it must be shared with the project: the owner of the credential
        # shares it here, anyone else needs the owner to share it first
        credential = UserAccess.objects(company=company, id=dataset.credential).first()
        if credential is None:
            raise errors.bad_request.ValidationError("the credential of this dataset no longer exists")
        if root not in access.shared_project_ids(company, "credential", credential.id):
            if credential.user != user and not is_admin:
                raise errors.bad_request.ValidationError(
                    "the credential that reads this dataset is not shared with this project; its owner must share it first")
            user_access.grant(company, credential.user, credential.id, root)
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
    """Removes the record only. The data stays in its repository, so nothing is deleted there. A registry's entries
    inherit from it, so they are removed with it."""
    access.refuse_inactive(user)
    dataset = _owned(company, dataset_id)
    _require_sharing(company, user, is_admin, dataset)
    resource_type = _share_type(dataset)
    if dataset.kind == "registry":
        Dataset.objects(company=company, kind="registry_entry", ref__istartswith=f"{dataset.ref}/").delete()
    dataset.delete()
    if resource_type is not None:
        access.drop_resource_shares(company, resource_type, dataset.id, actor=user)


def _names(company: str, user_ids: set) -> dict:
    """The names of the users, by id; an id with no user maps to itself."""
    names = {user.id: user.name for user in User.objects(company=company, id__in=list(user_ids)).only("id", "name")}
    return {user_id: names.get(user_id, user_id) for user_id in user_ids}


def usable_datasets(company: str, caller: str, root: str, can_manage: bool) -> list:
    """The repositories a root project can use, for its owner and collaborators: every Public one, and those shared with
    the project whose share applies (bll/access._share_level: the granter and the caller are active members). Registries
    and registry entries are not listed. A public dataset is not removable from the project; a project share is, when
    can_manage. No secret is read here."""
    roots = {root}
    rows = []
    public_ids = set()
    for dataset in Dataset.objects(company=company, kind="repo", visibility="Public").order_by("created"):
        public_ids.add(dataset.id)
        rows.append({"dataset": dataset, "scope": "public", "removable": False, "level": "read", "inherited": False,
                     "share_id": None})
    shares = Share.objects(company=company, resource_type="dataset", scope="projects", project=root).order_by("created")
    for share in shares:
        if share.resource_id in public_ids:
            continue
        level = access._share_level(company, share, caller, roots, True, True)
        if level is None:
            continue
        dataset = Dataset.objects(company=company, id=share.resource_id, kind="repo").first()
        if dataset is None:
            continue
        rows.append({"dataset": dataset, "scope": "project", "removable": bool(can_manage), "level": level,
                     "inherited": bool(share.inherited), "share_id": share.id})
    owners = _names(company, {row["dataset"].owner for row in rows})
    return [
        {
            "id": row["dataset"].id,
            "name": row["dataset"].name,
            "owner": row["dataset"].owner,
            "owner_name": owners[row["dataset"].owner],
            "visibility": row["dataset"].visibility,
            "scope": row["scope"],
            "removable": row["removable"],
            "level": row["level"],
            "inherited": row["inherited"],
            "share_id": row["share_id"],
            "repository": row["dataset"].ref,
        }
        for row in rows
    ]
