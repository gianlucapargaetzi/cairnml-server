"""The entries of a registry: the datasets a registry repository lists.

A registry is a git repository with one small file per entry (``datasets/<name>.yaml``): its name, how its data is read
(``source``) and its link, and optionally ``metadata/<name>.json``. The entries are read when the registry is first used and when
it is refreshed, and stored; they have no credential of their own: they are read with the registry. An entry whose ``source`` is
``repo`` is not a registry entry (a dataset of our own is a repository of its own) and is ignored.
"""

import json
import logging
import re
import uuid
from datetime import datetime
from typing import Optional

from apiserver.apierrors import errors
from apiserver.bll import repositories as repos
from apiserver.database.model.repository import RegistryEntry, Repository
from apiserver.idp.git_reader import GitReader
from apiserver.idp.github import GitHubError

log = logging.getLogger(__name__)

ENTRY_DIR = "datasets"
METADATA_DIR = "metadata"
REPO_SOURCE = "repo"


def _entry_field(text: str, field: str) -> Optional[str]:
    match = re.search(rf"^{field}:\s*(\S+)\s*$", text, re.M)
    return match.group(1) if match else None


def parse_metadata(raw: Optional[bytes], file_name: str) -> Optional[dict]:
    """The fields the list shows from a metadata file (a dataset's manifest, or a registry entry's metadata): modalities, labels,
    splits and samples. None when there is no file."""
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        raise errors.bad_request.ValidationError(f"{file_name} is not valid JSON") from None
    return {
        "modalities": data.get("modalities") or {},
        "labels": data.get("labels") or {},
        "splits": data.get("splits") or {},
        "samples": data.get("samples"),
    }


def list_entries(*, reader, registry: str) -> list:
    """The entries of the registry: name, how its data is read (``source``) and its link, with the metadata of each."""
    entries = []
    for file_name, text in sorted(reader.read_dir_texts(registry, ENTRY_DIR).items()):
        if not file_name.endswith(".yaml"):
            continue
        name = file_name[: -len(".yaml")]
        source = _entry_field(text, "source") or ""
        if source == REPO_SOURCE:
            log.warning("the registry %s has an entry %s with source: repo; it is ignored", registry, name)
            continue
        metadata_file = f"{METADATA_DIR}/{name}.json"
        entries.append({
            "name": name, "source": source, "link": _entry_field(text, "link") or "",
            "metadata": parse_metadata(reader.read_file(registry, metadata_file), metadata_file),
        })
    return entries


def reader_for(repo: Repository, user: str, project_root: Optional[str] = None) -> GitReader:
    """The reader of the registry for the user, over git (never the host's REST API): without a credential when the host allows
    it, else with a token of the credential that reads it (see repositories.resolve)."""
    resolution = repos.resolve(repo, project_root, user)
    if resolution.state in (repos.ANONYMOUS, repos.NONE_NEEDED, repos.UNKNOWN):
        return GitReader(repo.host)
    if resolution.state in (repos.ASSIGNED, repos.COMPANY):
        answer = repos.token_for(repo, resolution)
        return GitReader(repo.host, answer.get("token"), answer.get("username"))
    raise errors.bad_request.ValidationError(f"the registry {repo.path} could not be read: {resolution.message}")


def _dump(metadata: Optional[dict]) -> str:
    return json.dumps(metadata, sort_keys=True) if metadata is not None else ""


def _sync(company: str, repo: Repository, listed: list, now: datetime) -> dict:
    """Brings the stored entries in line with what the registry lists: a new entry is added, one whose data changed is updated,
    and one the registry no longer lists is removed. Returns the counts added, updated and removed."""
    existing = {e.name: e for e in RegistryEntry.objects(company=company, registry=repo.id)}
    counts = {"added": 0, "updated": 0, "removed": 0}
    for item in listed:
        content = {"source": item.get("source") or "", "link": item.get("link") or "",
                   "metadata": _dump(item.get("metadata"))}
        record = existing.get(item["name"])
        if record is None:
            RegistryEntry(id=uuid.uuid4().hex, company=company, registry=repo.id, name=item["name"],
                          reference=repos._reference_for(company, item["name"], f"{repo.path}/{item['name']}"),
                          created=now, updated=now, read_at=now, **content).save()
            counts["added"] += 1
            continue
        changed = any(getattr(record, key) != value for key, value in content.items())
        for key, value in content.items():
            setattr(record, key, value)
        record.read_at = now
        if changed:
            record.updated = now
            counts["updated"] += 1
        record.save()
    listed_names = {item["name"] for item in listed}
    for name, record in existing.items():
        if name not in listed_names:
            record.delete()
            counts["removed"] += 1
    return counts


def read_entries(company: str, repo: Repository, user: str, project_root: Optional[str] = None) -> dict:
    """Reads the registry and stores its entries. A registry that cannot be read keeps what it had and says why on its record
    (the error is raised too). Returns the counts added, updated and removed."""
    try:
        reader = reader_for(repo, user, project_root)
        try:
            try:
                listed = list_entries(reader=reader, registry=repo.path)
            finally:
                getattr(reader, "close", lambda: None)()
        except GitHubError as exc:
            raise errors.bad_request.ValidationError(f"the registry {repo.path} could not be read ({exc})") from None
    except errors.bad_request.ValidationError as refused:
        repo.state, repo.state_message, repo.updated = "no_access", f"the registry could not be read: {refused.msg}", datetime.utcnow()
        repo.save()
        raise
    now = datetime.utcnow()
    counts = _sync(company, repo, listed, now)
    repo.state, repo.state_message, repo.read_at, repo.last_checked, repo.updated = "ok", "", now, now, now
    repo.save()
    return counts


def refresh(company: str, user: str, is_admin: bool, repository_id: str) -> dict:
    """Reads the registry again. Only its owner and the admin may."""
    repo = repos.require_visible(company, user, is_admin, repository_id)
    if "registry" not in repo.roles:
        raise errors.bad_request.ValidationError("this repository is not a registry")
    if repos.level_of(company, user, is_admin, repo) != "collaborator":
        raise errors.forbidden.NoWritePermission("only the owner of a registry or the admin may refresh it")
    return read_entries(company, repo, user)


def ensure_registry(company: str, address: str, user: str, project_id: Optional[str], run_id: Optional[str] = None) -> Optional[Repository]:
    """The registry a run read an external dataset from: recorded and linked like any repository, with the role registry, and
    read once so that every dataset it lists is there (the ones the run did not use too). A registry that cannot be read is
    kept with a state that says why, and its owner refreshes it later. Never raises."""
    repo = repos.record_use_quietly(company, address, "registry", user, project_id, run_id)
    if repo is None:
        return None
    if repo.read_at is None:
        try:
            read_entries(company, repo, user, project_id and repos.user_access.root_project(company, project_id))
        except errors.bad_request.ValidationError as refused:
            log.warning("the registry %s could not be read: %s", repo.path, refused.msg)
        except Exception:
            log.warning("the registry %s could not be read", repo.path, exc_info=True)
    return repo
