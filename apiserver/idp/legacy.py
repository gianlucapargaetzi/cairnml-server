"""Refuses to start with settings that belonged to the removed basic login.

Access keys in secure.conf and fixed users would let a password or key log in, which the server no
longer accepts. A deployment that still sets them must fail loudly rather than appear to work.
"""

import os
from typing import Callable, List, Mapping

from apiserver.config_repo import config


def legacy_settings_in_use(get: Callable = config.get) -> List[str]:
    problems = []
    if get("secure.credentials", None):
        problems.append(
            "secure.credentials is set: access keys are removed, delete the section"
        )
    if get("apiserver.auth.fixed_users.enabled", False):
        problems.append(
            "apiserver.auth.fixed_users.enabled is true: fixed users are removed"
        )
    return problems


def test_only_settings_in_use(get: Callable = config.get, env: Mapping = os.environ) -> List[str]:
    """Settings that exist for the test suite. They are refused unless the server runs as a test run."""
    test_run = env.get("CAIRNML_TEST_MODE") == "1"
    problems = []
    if get("apiserver.idp.jwks_file", "") and not test_run:
        problems.append("apiserver.idp.jwks_file is test-only: it needs CAIRNML_TEST_MODE=1")
    if env.get("CAIRNML_ALLOW_CONFIG_SECRET") == "1" and not test_run:
        problems.append("CAIRNML_ALLOW_CONFIG_SECRET=1 is test-only: it needs CAIRNML_TEST_MODE=1")
    return problems


def cookie_settings_problems(get: Callable = config.get) -> List[str]:
    """Over HTTPS the session cookie must be marked Secure, or it can be sent over plain HTTP."""
    url = get("apiserver.idp.public_url", "") or ""
    if url.lower().startswith("https://") and not get("apiserver.auth.cookies.secure", False):
        return ["apiserver.idp.public_url is https but apiserver.auth.cookies.secure is false"]
    return []


def check_no_legacy_settings(get: Callable = config.get) -> None:
    problems = legacy_settings_in_use(get) + test_only_settings_in_use(get) + cookie_settings_problems(get)
    if problems:
        raise RuntimeError("; ".join(problems))
