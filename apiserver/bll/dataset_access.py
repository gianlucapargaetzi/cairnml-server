"""Which GitHub repositories are CairnML datasets, and how a job may read one.

The GitHub App is the source of trust. Its installations are the allow-list: a repository the App is installed on is a
dataset when its default branch has a ``dataset.yaml`` at its root. Its footprint (``dataset.lock.json`` against
``data.dvc``, and ``adapter.py`` for a local adapter) is verified with the SDK's footprint check every time the dataset
is read or listed. Nothing is registered: a dataset lives in its own repository.

A public repository needs no App and no token: it is cloned without one, so this module does not read it.
"""

import json
import logging

from apiserver.apierrors import errors
from apiserver.idp.github import GitHubApp, GitHubError, RepoNotAccessible
from cairnml.datasets import footprint

ADAPTER_MAX_BYTES = 1024 * 1024  # adapter.py comes from an untrusted repository: it is read only up to this size
DESCRIPTION_FILE = "DATASET.md"
DESCRIPTION_MAX_BYTES = 20 * 1024  # DATASET.md is text for the dataset view only; longer text is cut
log = logging.getLogger(__name__)


class NotADataset(errors.bad_request.ValidationError):
    """The repository is installed, but its default branch has no dataset.yaml at its root."""


def _is_local_adapter(config_text):
    """True when dataset.yaml says ``adapter: local``. A file that cannot be read is left to the footprint check to refuse."""
    if config_text is None:
        return False
    try:
        return footprint.config_format(footprint.parse_config(config_text.decode()))["kind"] == "local"
    except (ValueError, UnicodeDecodeError):
        return False


def _read_adapter(app: GitHubApp, repo: str, head: str, sizes: dict):
    """The bytes of adapter.py at the top of the repository at ``head``, or None when there is none.

    Only the bytes are kept: the file is hashed by the footprint check and never imported, run or parsed here. Its size
    is checked from the tree before it is downloaded, and again after, since the tree may not list a size.
    """
    size = sizes.get(footprint.ADAPTER_FILE)
    if size is None:
        return None
    if size > ADAPTER_MAX_BYTES:
        raise errors.bad_request.ValidationError(
            f"{footprint.ADAPTER_FILE} in {repo}@{head[:10]} is {size} bytes; the adapter may be at most "
            f"{ADAPTER_MAX_BYTES} bytes"
        )
    content = app.read_file(repo, footprint.ADAPTER_FILE, head)
    if content is not None and len(content) > ADAPTER_MAX_BYTES:
        raise errors.bad_request.ValidationError(
            f"{footprint.ADAPTER_FILE} in {repo}@{head[:10]} is {len(content)} bytes; the adapter may be at most "
            f"{ADAPTER_MAX_BYTES} bytes"
        )
    return content


def verify_dataset(app: GitHubApp, repo: str) -> tuple:
    """Read the default branch of an installed repository and verify it is a dataset with a holding footprint.

    Returns (default branch, head sha, lock). Raises NotADataset when dataset.yaml is missing at the root, and a
    validation error that says what is wrong for an installation that is missing, unreachable or fails its footprint.
    """
    try:
        branch, head = app.default_branch_head(repo)
        files = {
            name: app.read_file(repo, name, head)
            for name in (footprint.DVC_FILE, footprint.CONFIG_FILE, footprint.LOCK_FILE)
        }
        if files[footprint.CONFIG_FILE] is None:
            raise NotADataset(
                f"{repo} is not a CairnML dataset: {footprint.CONFIG_FILE} is missing on its default branch")
        sizes = app.tree_sizes(repo, head)
        if _is_local_adapter(files[footprint.CONFIG_FILE]):
            files[footprint.ADAPTER_FILE] = _read_adapter(app, repo, head, sizes)
    except NotADataset:
        raise
    except RepoNotAccessible:
        raise errors.bad_request.ValidationError(f"the GitHub App is not installed on {repo}") from None
    except GitHubError as exc:
        raise errors.bad_request.ValidationError(f"GitHub could not be read for {repo}: {exc}") from None
    problems = footprint.verify_files(files, sizes)
    if problems:
        raise errors.bad_request.ValidationError(
            f"{repo}@{head[:10]} on {branch} does not pass its footprint check: " + "; ".join(problems)
        )
    return branch, head, json.loads(files[footprint.LOCK_FILE].decode())


def footprint_summary(lock: dict) -> dict:
    """What the dataset list shows from the lock: its format kind, its sample count and its labels."""
    return {
        "format": (lock.get("format") or {}).get("kind"),
        "samples": lock.get("samples"),
        "labels": lock.get("labels") or {},
    }


def description_of(app: GitHubApp, repo: str, head: str):
    """The text of DATASET.md at the head commit, cut to DESCRIPTION_MAX_BYTES, and whether it was cut. (None, False)
    when the repository has no DATASET.md at its root. The text is data for the dataset view, never executed."""
    raw = app.read_file(repo, DESCRIPTION_FILE, head)
    if raw is None:
        return None, False
    return raw[:DESCRIPTION_MAX_BYTES].decode("utf-8", "replace"), len(raw) > DESCRIPTION_MAX_BYTES


def list_datasets(app: GitHubApp) -> list:
    """Every repository the App is installed on that is a valid dataset, by repository name.

    A repository without dataset.yaml is not a dataset and is not listed. One whose footprint does not hold, or that
    cannot be read now, is left out with a logged warning, so one bad repository does not hide the others.
    """
    datasets = []
    for installed in app.installed_repositories():
        repo = installed["repository"]
        try:
            branch, head, lock = verify_dataset(app, repo)
            description, truncated = description_of(app, repo, head)
        except NotADataset:
            continue
        except (errors.bad_request.ValidationError, GitHubError) as exc:
            log.warning("the dataset repository %s is left out of the list: %s", repo, exc)
            continue
        datasets.append({
            "repository": repo,
            "default_branch": branch,
            "head_commit": head,
            "footprint": footprint_summary(lock),
            "description": description,
            "description_truncated": truncated,
        })
    return sorted(datasets, key=lambda dataset: dataset["repository"].lower())
