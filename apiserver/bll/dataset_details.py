"""What the Datasets page shows about one dataset: its metadata, its description (DATASET.md) and its commit history.

A dataset of our own is a repository with a manifest.json (written by the SDK at push) and an optional DATASET.md. A
dataset of the registry has its metadata in the registry repository, in metadata/<name>.json, written from its adapter.
Everything is read through the GitHub App, at one commit, so the same request always gives the same answer for a commit.
"""

import json
import re
from typing import Optional

from apiserver.apierrors import errors
from apiserver.bll import dataset_registry as registry_bll
from apiserver.idp.github import GitHubApp, GitHubError
from apiserver.idp.github_check import repo_of

MANIFEST_FILE = "manifest.json"
DESCRIPTION_FILE = "DATASET.md"
REGISTRY_METADATA_DIR = "metadata"
MAX_COMMITS = 60


def _metadata(raw: Optional[bytes]) -> Optional[dict]:
    """The fields the panel shows from a manifest: modalities, labels, splits and samples. None when there is no manifest."""
    if raw is None:
        return None
    try:
        manifest = json.loads(raw)
    except ValueError:
        raise errors.bad_request.ValidationError("manifest.json is not valid JSON") from None
    return {
        "modalities": manifest.get("modalities") or {},
        "labels": manifest.get("labels") or {},
        "splits": manifest.get("splits") or {},
        "samples": manifest.get("samples"),
    }


def own_details(app: GitHubApp, repository: str, commit: Optional[str]) -> dict:
    """A dataset of our own. commit selects a revision; the default is the head of the default branch."""
    try:
        branch, head = app.default_branch_head(repository)
        revision = commit or head
        if commit and not app.commit_exists(repository, commit):
            raise errors.bad_request.ValidationError(f"no commit {commit} in {repository}")
        description = app.read_file(repository, DESCRIPTION_FILE, revision)
        return {
            "kind": "own",
            "name": repository.split("/", 1)[1],
            "repository": repository,
            "link": f"https://github.com/{repository}",
            "revision": revision,
            "branch": branch,
            "metadata": _metadata(app.read_file(repository, MANIFEST_FILE, revision)),
            "description": description.decode("utf-8", "replace") if description is not None else None,
            "commits": app.list_commits(repository, branch, MAX_COMMITS),
        }
    except GitHubError as exc:
        raise errors.bad_request.ValidationError(f"the dataset could not be read ({exc})") from None


def registry_details(app: GitHubApp, registries: list, name: str) -> dict:
    """A dataset of a registry: its entry (source and link) and its metadata, read from the first registry that lists it.
    A dataset of the registry has no history of its own."""
    for registry in registries:
        try:
            entries = registry_bll.list_entries(app=app, registry=registry)
        except GitHubError:
            continue
        entry = next((e for e in entries if e["name"] == name), None)
        if entry is None:
            continue
        try:
            raw = app.read_file(registry, f"{REGISTRY_METADATA_DIR}/{name}.json")
        except GitHubError as exc:
            raise errors.bad_request.ValidationError(f"the registry could not be read ({exc})") from None
        return {
            "kind": "registry",
            "name": name,
            "repository": None,
            "registry": registry,
            "link": entry["link"],
            "source": entry["source"],
            "revision": None,
            "branch": None,
            "metadata": _metadata(raw),
            "description": None,
            "commits": [],
        }
    raise errors.bad_request.ValidationError(f"no dataset {name} in the registries of this server")


def repository_of(repository: Optional[str]) -> str:
    """owner/name of a github.com URL, or of an owner/name given as it is. Refused for anything else."""
    repo = repo_of(repository or "")
    if not repo and re.fullmatch(r"[\w.-]+/[\w.-]+", repository or ""):
        repo = repository
    if not repo:
        raise errors.bad_request.ValidationError("the repository must be a github.com repository or owner/name")
    return repo


def details(app: GitHubApp, *, repository: Optional[str], registries: list, registry_name: Optional[str],
            commit: Optional[str]) -> dict:
    if registry_name:
        return registry_details(app, registries, registry_name)
    return own_details(app, repository_of(repository), commit)
