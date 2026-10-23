"""Registering a dataset repository in the dataset registry, once.

The registry is a git repository with one small file per dataset (``datasets/<name>.yaml``). A dataset of our own gets
the entry ``source: repo`` and the link to its repository; how the data is read is written in the dataset repository
itself, so the entry never needs a change.

The server adds the entry through a pull request, after it has read the dataset repository's default branch with the
GitHub App and verified its footprint (:mod:`apiserver.bll.dataset_footprint`). It reads only git data: the footprint
says the data was checked, and using the dataset checks it again.
"""

import re
from datetime import datetime, timezone
from typing import Iterable, Optional

from apiserver.apierrors import errors
from apiserver.bll import dataset_footprint as footprint
from apiserver.idp.github import GitHubApp, GitHubError, RepoNotAccessible
from apiserver.idp.github_check import repo_of

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
ENTRY_DIR = "datasets"


def entry_name(repo: str, requested: Optional[str]) -> str:
    name = (requested or repo.split("/", 1)[1]).lower()
    if name.endswith(".git"):
        name = name[:-4]
    if not NAME_RE.fullmatch(name):
        raise errors.bad_request.ValidationError(
            "the registry name must be lower case letters, digits, '.', '_' or '-', and start with a letter or digit"
        )
    return name


def entry_text(repo: str, head: str, name: str) -> str:
    when = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return (
        f"# {name}: a dataset of our own. Registered once on {when} for {repo} (checked at {head[:10]}).\n"
        f"# How the data is read is written in the dataset repository (dataset.yaml); nothing here changes with it.\n"
        f"source: repo\n"
        f"link: https://github.com/{repo}\n"
    )


def _linked_repo(text: str) -> Optional[str]:
    match = re.search(r"^link:\s*(\S+)\s*$", text, re.M)
    found = repo_of(match.group(1)) if match else None
    return found.lower() if found else None


def verify_repository(app: GitHubApp, repo: str):
    """Read the default branch of a dataset repository and verify its footprint. Returns (branch, head sha)."""
    try:
        branch, head = app.default_branch_head(repo)
        files = {
            name: app.read_file(repo, name, head)
            for name in (footprint.DVC_FILE, footprint.CONFIG_FILE, footprint.LOCK_FILE)
        }
        sizes = app.tree_sizes(repo, head)
    except RepoNotAccessible:
        raise errors.bad_request.ValidationError(f"the GitHub App is not installed on {repo}") from None
    except GitHubError as exc:
        raise errors.bad_request.ValidationError(f"GitHub could not be read for {repo}: {exc}") from None
    problems = footprint.verify_files(files, sizes)
    if problems:
        raise errors.bad_request.ValidationError(
            f"{repo}@{head[:10]} on {branch} does not pass its footprint check: " + "; ".join(problems)
        )
    return branch, head


def register(
    *,
    app: GitHubApp,
    repository: str,
    name: Optional[str],
    registry: str,
    allowed_owners: Iterable[str],
) -> dict:
    """Add a dataset repository to the registry through a pull request. Once: a second attempt is refused."""
    if not registry:
        raise errors.bad_request.ValidationError("the dataset registry is not configured on this server")
    repo = repo_of(repository)
    if repo is None:
        raise errors.bad_request.ValidationError("the repository must be on github.com")
    owners = {o.lower() for o in allowed_owners}
    if repo.split("/", 1)[0].lower() not in owners:
        raise errors.bad_request.ValidationError(f"datasets of {repo.split('/', 1)[0]} may not be registered here")
    name = entry_name(repo, name)

    _branch, head = verify_repository(app, repo)

    path = f"{ENTRY_DIR}/{name}.yaml"
    pending = f"register/{name}"
    try:
        if app.read_file(registry, path) is not None:
            raise errors.bad_request.ValidationError(f"{name} is already registered")
        for other in app.list_dir(registry, ENTRY_DIR):
            if other.endswith(".yaml"):
                text = (app.read_file(registry, f"{ENTRY_DIR}/{other}") or b"").decode(errors="replace")
                if _linked_repo(text) == repo.lower():
                    raise errors.bad_request.ValidationError(f"{repo} is already registered as {other[:-5]}")
        if app.branch_exists(registry, pending):
            raise errors.bad_request.ValidationError(f"the registration of {name} is already waiting in a pull request")
        url = app.open_registration_pull_request(
            registry, pending, path, entry_text(repo, head, name),
            message=f"Register the dataset {name}",
            title=f"Register the dataset {name}",
            body=f"Registers {repo} (checked at {head}). Opened by the server; the footprint was verified.",
        )
    except GitHubError as exc:
        raise errors.bad_request.ValidationError(
            f"the registry could not be updated ({exc}); the GitHub App needs write access to contents and pull "
            f"requests on {registry}"
        ) from None
    return {"name": name, "repository": repo, "pull_request": url}
