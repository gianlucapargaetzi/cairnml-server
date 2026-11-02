"""The dataset registries of the server: the public datasets and adapters that the server lists.

A registry is a git repository with one small file per entry (``datasets/<name>.yaml``): its name, how its data is read
(``source``) and its link. A dataset of our own is not in a registry: it lives in its own repository and is found through
the GitHub App's installations (see :mod:`apiserver.bll.dataset_access`). An entry with ``source: repo`` is therefore
ignored here, with a logged warning, if an old registry still has one.
"""

import logging
import re
from typing import Optional

from apiserver.idp.github import GitHubApp

ENTRY_DIR = "datasets"
REPO_SOURCE = "repo"
log = logging.getLogger(__name__)


def configured_registries() -> list:
    """The dataset registries of this server: ``repository``, then the others in ``repositories``. Each is a repository
    of owner/name, public (read without the GitHub App) or one the App can read. Duplicates are dropped."""
    from apiserver.config_repo import config

    registries = [config.get("apiserver.dataset_registry.repository", "") or ""]
    registries += list(config.get("apiserver.dataset_registry.repositories", []) or [])
    result = []
    for registry in registries:
        if registry and registry not in result:
            result.append(registry)
    return result


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
