"""What the list shows about one dataset: its metadata, its description (DATASET.md) and its commit history.

A dataset of our own is a repository with a manifest.json (written by the SDK at push) and an optional DATASET.md.
Everything is read through the credential that reads the repository, at one commit, so the same request always gives the same
answer for a commit.

A dataset of a registry is not read from GitHub here: its entry, its source, its link and its metadata were read from the
registry when it was first used or refreshed (bll/repository_registry), and are stored with the entry.
"""

import json
from typing import Optional

from apiserver.apierrors import errors
from apiserver.bll import repositories as repos
from apiserver.bll import repository_registry as registry_bll
from apiserver.database.model.repository import RegistryEntry, Repository
from apiserver.idp.github import GitHubApp, GitHubError

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


def registry_details(entry: RegistryEntry, registry: Repository) -> dict:
    """A dataset of a registry, from the entry stored when its registry was read: its source, its link and its metadata. No GitHub
    call. A dataset of a registry has no history of its own."""
    return {
        "kind": "registry",
        "name": entry.name,
        "repository": None,
        "registry": registry.path,
        "link": entry.link,
        "source": entry.source,
        "revision": None,
        "branch": None,
        "metadata": json.loads(entry.metadata) if entry.metadata else None,
        "description": None,
        "commits": [],
    }


def details(company: str, user: str, is_admin: bool, *, repository: Optional[str] = None, entry: Optional[str] = None,
            commit: Optional[str] = None) -> dict:
    """The details of a registry's entry (stored, no GitHub call) when ``entry`` (its id, or its reference) is given, otherwise
    those of the repository (its id, its address or its reference). Refused as missing when the user does not see it."""
    if entry:
        found = RegistryEntry.objects(company=company, id=entry).first() or RegistryEntry.objects(
            company=company, reference=entry).first()
        registry = Repository.objects(company=company, id=found.registry).first() if found else None
        if found is None or registry is None or not repos.can_see(company, user, is_admin, registry):
            raise errors.bad_request.ValidationError(f"no such dataset: {entry}")
        return registry_details(found, registry)
    repo = repos.find_reference(company, repository or "")
    if repo is None or not repos.can_see(company, user, is_admin, repo):
        raise errors.bad_request.ValidationError(f"no such repository: {repository}")
    reader = registry_bll.reader_for(repo, user)
    return own_details(reader, repo.path, commit)
