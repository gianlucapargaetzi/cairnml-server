"""The dataset registries of the server: the public datasets and adapters that the server lists.

A registry is a git repository with one small file per entry (``datasets/<name>.yaml``): its name, how its data is read
(``source``) and its link. A dataset of our own is not in a registry: it lives in its own repository and is found through
the GitHub App's installations (see :mod:`apiserver.bll.dataset_access`). An entry with ``source: repo`` is therefore
ignored here, with a logged warning, if an old registry still has one.
"""

import json
import logging
import re
from typing import Optional
from urllib.parse import urlsplit

from apiserver.apierrors import errors
from apiserver.database.model.dataset import Dataset
from apiserver.idp.github import GitHubApp, valid_repo

ENTRY_DIR = "datasets"
REGISTRY_METADATA_DIR = "metadata"
REPO_SOURCE = "repo"
log = logging.getLogger(__name__)


def stored_registries() -> list:
    """The registry records of this server, in the order they were added, each repository once: those of every company.
    Read from the database only. A registry's state says whether its entries were read."""
    seen, records = set(), []
    for record in Dataset.objects(kind="registry").order_by("created"):
        if record.ref.lower() not in seen:
            seen.add(record.ref.lower())
            records.append(record)
    return records


def stored_entries(registry: str) -> list:
    """The stored entries of a registry (its registry_entry records), in the order they were added. Read from the
    database only: the entries are read from GitHub when the registry is added or refreshed."""
    return list(Dataset.objects(kind="registry_entry", ref__istartswith=f"{registry}/").order_by("created"))


_GITHUB_HOSTS = ("github.com", "www.github.com")
# git@github.com:owner/name.git, the form git prints for SSH
_SCP_RE = re.compile(r"^(?:[\w.-]+@)?(github\.com):(?P<path>.+)$", re.I)


def parse_github_url(text: str) -> Optional[tuple]:
    """(owner, name) of a GitHub repository given as https://github.com/owner/name(.git), http://, www., ssh://git@...,
    or git@github.com:owner/name(.git); also owner/name or github.com/owner/name. None for anything else, including
    other hosts."""
    value = (text or "").strip().rstrip("/")
    if not value:
        return None
    path = None
    scp = _SCP_RE.match(value)
    if scp:
        path = scp.group("path")
    else:
        if "://" not in value:
            if re.match(r"^[\w.-]+\.[a-z]{2,}/", value, re.I):  # a bare host: github.com/owner/name
                value = "https://" + value
            else:
                path = value
        if path is None:
            parts = urlsplit(value)
            host = (parts.hostname or "").lower()
            if parts.scheme not in ("https", "http", "ssh", "git") or host not in _GITHUB_HOSTS:
                return None
            path = parts.path
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    pieces = path.split("/")
    if len(pieces) != 2:
        return None
    owner, name = pieces
    repo = f"{owner}/{name}"
    return (owner, name) if valid_repo(repo) else None


def normalize_repository(text: str) -> Optional[str]:
    """owner/name for a GitHub repository given in any usual form; None when it is not one."""
    parsed = parse_github_url(text)
    return f"{parsed[0]}/{parsed[1]}" if parsed else None


def canonical_url(repository: str) -> str:
    """The https address of a repository named owner/name."""
    return f"https://github.com/{repository}"


def _entry_field(text: str, field: str) -> Optional[str]:
    match = re.search(rf"^{field}:\s*(\S+)\s*$", text, re.M)
    return match.group(1) if match else None


def parse_metadata(raw: Optional[bytes], file_name: str) -> Optional[dict]:
    """The fields the Datasets page shows from a metadata file (a dataset's manifest, or a registry entry's metadata):
    modalities, labels, splits and samples. None when there is no file."""
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


def read_metadata(app: GitHubApp, registry: str, name: str) -> Optional[dict]:
    """The metadata of one entry of the registry, from its metadata/<name>.json. None when the file is absent. A GitHub
    error propagates; a file that is not JSON is refused with the file's name."""
    file_name = f"{REGISTRY_METADATA_DIR}/{name}.json"
    return parse_metadata(app.read_file(registry, file_name), file_name)


def list_entries(*, app: GitHubApp, registry: str) -> list:
    """The public entries of the registry: name, how its data is read (``source``) and its link. Read-only.

    An entry with ``source: repo`` is not listed: a dataset of our own is read from its repository, not from a registry.
    """
    if not registry:
        return []
    entries = []
    for file_name, text in sorted(app.read_dir_texts(registry, ENTRY_DIR).items()):
        if not file_name.endswith(".yaml"):
            continue
        name = file_name[: -len(".yaml")]
        source = _entry_field(text, "source") or ""
        if source == REPO_SOURCE:
            log.warning("the dataset registry %s has an entry %s with source: repo; it is ignored", registry, name)
            continue
        entries.append({
            "name": name,
            "source": source,
            "link": _entry_field(text, "link") or "",
        })
    return entries
