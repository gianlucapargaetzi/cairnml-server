"""Where the suite's stack is, and the accounts the tests use.

Nothing is read from deploy/: the suite starts its own stack (support/stack.py) and configures these values from it
when the session begins. Until then they are None, and any use before that is a bug.
"""

import os
import pwd
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]

API_URL = None
WEB_URL = None
IDP_URL = None
STORAGE_URL = None
ISSUER = None
REALM = "cairnml"
ADMIN_USER = None
ADMIN_PASSWORD = None
STACK_PROJECT = None  # the compose project of this session's stack

# Prefix of every project, task and user the suite creates. Cleanup removes only what carries it.
PREFIX = "e2e"

# The real home of the user running the suite. The suite runs the SDK with a throwaway HOME, so the browsers are
# found through this path, not through ~.
REAL_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)
BROWSERS_PATH = os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or str(REAL_HOME / ".cache" / "ms-playwright")


def configure(*, api: str, web: str, idp: str, storage: str, admin_user: str, admin_password: str,
              project: str) -> None:
    global API_URL, WEB_URL, IDP_URL, STORAGE_URL, ISSUER, ADMIN_USER, ADMIN_PASSWORD, STACK_PROJECT
    API_URL, WEB_URL, IDP_URL, STORAGE_URL = api, web, idp, storage
    STACK_PROJECT = project
    ISSUER = f"{idp}/realms/cairnml"
    ADMIN_USER, ADMIN_PASSWORD = admin_user, admin_password


def github_tests_enabled() -> bool:
    """The GitHub-only tests need a repository on github.com that this machine's App can read (docs/e2e_testing/E2E.md)."""
    return bool(os.environ.get("CAIRNML_E2E_GITHUB_REPO"))
