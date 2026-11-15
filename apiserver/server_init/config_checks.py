"""Startup checks: the server refuses to start with settings that are removed, test-only, or unsafe for the address
it is served on. Every check returns the problems it found; check_startup_config raises them together.

Removed settings: access keys in secure.conf and fixed users belonged to the removed basic login. A deployment that
still sets them must fail loudly rather than appear to work.
"""

import os
from typing import Callable, List, Mapping

from apiserver.config_repo import config
from apiserver.idp.public_origin import public_origin_problems, trusted_proxies_problems

# --- removed settings


def removed_settings_problems(get: Callable = config.get) -> List[str]:
    problems = []
    if get("secure.credentials", None):
        problems.append("secure.credentials is set: access keys are removed, delete the section")
    if get("apiserver.auth.fixed_users.enabled", False):
        problems.append("apiserver.auth.fixed_users.enabled is true: fixed users are removed")
    return problems


# --- test-only settings


def settings_for_test_runs_problems(get: Callable = config.get, env: Mapping = os.environ) -> List[str]:
    """Settings that exist for the test suite. They are refused unless the server runs as a test run."""
    test_run = env.get("CAIRNML_TEST_MODE") == "1"
    problems = []
    if get("apiserver.idp.jwks_file", "") and not test_run:
        problems.append("apiserver.idp.jwks_file is test-only: it needs CAIRNML_TEST_MODE=1")
    if env.get("CAIRNML_ALLOW_CONFIG_SECRET") == "1" and not test_run:
        problems.append("CAIRNML_ALLOW_CONFIG_SECRET=1 is test-only: it needs CAIRNML_TEST_MODE=1")
    return problems


# --- the address the server is served on


def cookie_settings_problems(get: Callable = config.get) -> List[str]:
    """Over HTTPS the session cookie must be marked Secure, or it can be sent over plain HTTP. The address of the
    deployment counts whichever setting names it: the pinned origin, the API or the web app address."""
    addresses = [
        get("apiserver.idp.public_origin", "") or "",
        get("apiserver.idp.public_url", "") or "",
        get("apiserver.idp.web_url", "") or "",
    ]
    if any(address.lower().startswith("https://") for address in addresses) and not get(
        "apiserver.auth.cookies.secure", False
    ):
        return ["an https address is configured (apiserver.idp) but apiserver.auth.cookies.secure is false"]
    return []


def check_startup_config(get: Callable = config.get) -> None:
    problems = (
        removed_settings_problems(get)
        + settings_for_test_runs_problems(get)
        + cookie_settings_problems(get)
        + public_origin_problems(get)
        + trusted_proxies_problems(get)
    )
    if problems:
        raise RuntimeError("; ".join(problems))
