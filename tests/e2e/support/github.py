"""The GitHub App and the test repository of the github tests (docs/e2e_testing/E2E.md, "GitHub tests").

The suite's stack has no GitHub App of its own. A test that needs one adds the owner's App to the stack as the company's
App, through the API as the admin does in the web app, and removes it again. The App's id and private key are files on the
machine that runs the suite: their paths are in the environment, and the key is read only to be sent to the server. It is
never printed, logged or kept in a repository.

Everything a test creates in the test repository is on a branch under e2e/<run id>/, one prefix for each use; the
repository's default branch is never touched, and nothing is force-pushed.
"""

import os
from dataclasses import dataclass
from pathlib import Path

import pytest

from . import settings
from .api import Api

REPO_ENV = "CAIRNML_E2E_GITHUB_REPO"
APP_ID_FILE_ENV = "CAIRNML_E2E_GITHUB_APP_ID_FILE"
APP_KEY_FILE_ENV = "CAIRNML_E2E_GITHUB_APP_KEY_FILE"
PRIVATE_REPO_ENV = "CAIRNML_E2E_GITHUB_PRIVATE_REPO"

# Every branch the suite creates in the test repository starts with this; the helpers refuse any other.
BRANCH_PREFIX = "e2e/"


@dataclass(frozen=True)
class GitHubApp:
    repo: str  # owner/name of the test repository
    credential: str  # the id of the company App the server holds

    @property
    def url(self) -> str:
        return f"https://github.com/{self.repo}.git"


def _read(variable: str) -> str:
    path = os.environ.get(variable)
    if not path:
        pytest.fail(f"{REPO_ENV} is set, so {variable} must name the file that holds the GitHub App's data")
    try:
        return Path(path).read_text().strip()
    except OSError as exc:
        pytest.fail(f"{variable} names a file that cannot be read: {exc.strerror}")


def add_company_app(api: Api, label: str) -> GitHubApp:
    """Adds the App named by the environment to the stack as the company's App. The server checks it with GitHub first."""
    repo = os.environ[REPO_ENV]
    added = api.authed(
        "user_access.add_app",
        {"label": label, "app_id": _read(APP_ID_FILE_ENV), "key": _read(APP_KEY_FILE_ENV), "scope": "company"},
    )["access"]
    return GitHubApp(repo=repo, credential=added["id"])


def new_branch_prefix() -> str:
    """The prefix of one use's branches: unique for each run of each use."""
    return f"{BRANCH_PREFIX}{settings.PREFIX}-{os.urandom(4).hex()}/"
