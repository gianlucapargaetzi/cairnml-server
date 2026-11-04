"""The dataset registries of the server: the public datasets and adapters that the server lists.

A registry is a git repository with one small file per entry (``datasets/<name>.yaml``): its name, how its data is read
(``source``) and its link. A dataset of our own is not in a registry: it lives in its own repository and is found through
the GitHub App's installations (see :mod:`apiserver.bll.dataset_access`). An entry with ``source: repo`` is therefore
ignored here, with a logged warning, if an old registry still has one.
"""

import logging
import re
from datetime import datetime
from typing import Optional
from urllib.parse import urlsplit

from mongoengine.errors import NotUniqueError

from apiserver.database.model.dataset_registry import DatasetRegistry
from apiserver.database.model.settings import Settings
from apiserver.idp.github import GitHubApp, valid_repo

ENTRY_DIR = "datasets"
REPO_SOURCE = "repo"
SEEDED_SETTING = "dataset_registry.seeded"
CONFIG_SENDER = "config"
log = logging.getLogger(__name__)


def configured_registries() -> list:
    """The dataset registries of this server, in the order they were added. Each is a repository of owner/name, public
    (read without the GitHub App) or one the App can read."""
    _seed_from_config()
    return [registry.repository for registry in DatasetRegistry.objects().order_by("added")]


def _seed_from_config() -> None:
    """Imports the registries of the server config once, the first time the list is read. After that the database is
    the only source: a registry removed in the UI stays removed, even if the config still names it."""
    if Settings.get_by_key(SEEDED_SETTING, default=False):
        return
    from apiserver.config_repo import config

    names = [config.get("apiserver.dataset_registry.repository", "") or ""]
    names += list(config.get("apiserver.dataset_registry.repositories", []) or [])
    for name in names:
        # one value may hold several registries, separated by commas or spaces
        for part in re.split(r"[,\s]+", name or ""):
            repo = normalize_repository(part) if part else None
            if repo:
                add_registry(repo, added_by=CONFIG_SENDER)
    Settings.set_or_add_value(SEEDED_SETTING, True)


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


def list_registries() -> list:
    return [
        {
            "repository": registry.repository,
            "url": registry.url or canonical_url(registry.repository),
            "added": registry.added,
            "added_by": registry.added_by,
        }
        for registry in DatasetRegistry.objects().order_by("added")
    ]


def add_registry(repository: str, added_by: str) -> bool:
    """Adds a registry given as owner/name. False when it is already listed (compared in lower case)."""
    try:
        DatasetRegistry(
            id=repository.lower(),
            repository=repository,
            url=canonical_url(repository),
            added=datetime.utcnow(),
            added_by=added_by,
        ).save(force_insert=True)
    except NotUniqueError:
        return False
    return True


def remove_registry(repository: str) -> bool:
    """Removes a registry from the list. False when it was not listed."""
    return DatasetRegistry.objects(id=repository.lower()).delete() > 0


def _entry_field(text: str, field: str) -> Optional[str]:
    match = re.search(rf"^{field}:\s*(\S+)\s*$", text, re.M)
    return match.group(1) if match else None


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
