"""How the content of a dataset changed from version to version: its samples and splits at every commit that matters.

The commits are the ones of the condensed history (bll/repository_history): the newest, and every one a task ran on. The numbers come
from the dataset's manifest.json at each commit, the file the SDK writes when a version is pushed.
"""

from typing import List

from apiserver.apierrors import errors
from apiserver.bll import repository_history
from apiserver.bll import repository_registry as registry_bll
from apiserver.idp.github import GitHubError

MANIFEST_FILE = "manifest.json"


def versions_of(reader, repository: str, commits: List[dict]) -> List[dict]:
    """The samples and splits of the dataset at each of the given commits (``sha``, ``message``, ``date``), oldest first. A commit the
    repository does not have is left out; a commit without a manifest, or with one that is not readable, has no numbers."""
    found = []
    for commit in commits:
        if not reader.commit_exists(repository, commit["sha"]):
            continue
        try:
            metadata = registry_bll.parse_metadata(reader.read_file(repository, MANIFEST_FILE, commit["sha"]), MANIFEST_FILE) or {}
        except errors.bad_request.ValidationError:
            metadata = {}
        samples = metadata.get("samples")
        found.append({
            "commit": commit["sha"], "date": commit.get("date", ""), "message": commit.get("message", ""),
            "samples": samples if isinstance(samples, int) and not isinstance(samples, bool) else None,
            "splits": metadata.get("splits") or {},
        })
    return sorted(found, key=lambda version: version["date"])


def versions(company: str, user: str, is_admin: bool, repo) -> List[dict]:
    """The versions of a dataset the user sees, read over git with the credential that reads it."""
    history = repository_history.history(company, user, is_admin, repo)
    commits = [node for node in history["nodes"] if node["type"] == "commit"]
    reader = registry_bll.reader_for(repo, user)
    try:
        return versions_of(reader, repo.path, commits)
    except GitHubError as exc:
        raise errors.bad_request.ValidationError(f"the versions of {repo.path} could not be read ({exc})") from None
    finally:
        getattr(reader, "close", lambda: None)()
