"""What the Datasets page shows about one dataset: its metadata, its description (DATASET.md) and its commit history.

A dataset of our own is a repository with a manifest.json (written by the SDK at push) and an optional DATASET.md.
Everything is read through the GitHub App, at one commit, so the same request always gives the same answer for a commit.

A dataset of a registry is not read from GitHub here: its entry, its source, its link and its metadata were read from the
registry when the registry was added or refreshed (bll/datasets), and are stored with the entry.
"""

import json
import re
from typing import Optional

from apiserver.apierrors import errors
from apiserver.bll import dataset_registry as registry_bll
from apiserver.database.model.dataset import Dataset
from apiserver.idp.github import GitHubApp, GitHubError
from apiserver.idp.github_check import repo_of

MANIFEST_FILE = "manifest.json"
DESCRIPTION_FILE = "DATASET.md"
MAX_COMMITS = 60


def empty_details(repository: str) -> dict:
    """A dataset of our own whose repository has no commit yet: visible, with nothing in it to show."""
    return {
        "kind": "own",
        "name": repository.split("/", 1)[1],
        "repository": repository,
        "link": f"https://github.com/{repository}",
        "revision": None,
        "branch": None,
        "metadata": None,
        "description": None,
        "commits": [],
    }


def own_details(app: GitHubApp, repository: str, commit: Optional[str]) -> dict:
    """A dataset of our own. commit selects a revision; the default is the head of the default branch. A repository with
    no commit yet is shown as empty (an empty repository has no branch to read)."""
    try:
        try:
            branch, head = app.default_branch_head(repository)
        except GitHubError:
            if commit is None and app.repository_visible(repository):
                return empty_details(repository)
            raise
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
            "metadata": registry_bll.parse_metadata(app.read_file(repository, MANIFEST_FILE, revision), MANIFEST_FILE),
            "description": description.decode("utf-8", "replace") if description is not None else None,
            "commits": app.list_commits(repository, branch, MAX_COMMITS),
        }
    except GitHubError as exc:
        raise errors.bad_request.ValidationError(f"the dataset could not be read ({exc})") from None


def registry_details(entry: Dataset) -> dict:
    """A dataset of a registry, from the entry stored when its registry was read: its source, its link and its metadata.
    No GitHub call. A dataset of the registry has no history of its own."""
    return {
        "kind": "registry",
        "name": entry.name,
        "repository": None,
        "registry": entry.ref.rsplit("/", 1)[0],
        "link": entry.link,
        "source": entry.source,
        "revision": None,
        "branch": None,
        "metadata": json.loads(entry.metadata) if entry.metadata else None,
        "description": None,
        "commits": [],
    }


def repository_of(repository: Optional[str]) -> str:
    """owner/name of a github.com URL, or of an owner/name given as it is. Refused for anything else."""
    repo = repo_of(repository or "")
    if not repo and re.fullmatch(r"[\w.-]+/[\w.-]+", repository or ""):
        repo = repository
    if not repo:
        raise errors.bad_request.ValidationError("the repository must be a github.com repository or owner/name")
    return repo


def details(app: Optional[GitHubApp], *, repository: Optional[str], registry_entry: Optional[Dataset],
            commit: Optional[str]) -> dict:
    """The details of a registry's entry (stored, no GitHub call) when one is given, otherwise those of a repository."""
    if registry_entry is not None:
        return registry_details(registry_entry)
    return own_details(app, repository_of(repository), commit)
