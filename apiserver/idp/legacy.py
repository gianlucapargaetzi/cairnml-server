"""Refuses to start with settings that belonged to the removed basic login.

Access keys in secure.conf and fixed users would let a password or key log in, which the server no
longer accepts. A deployment that still sets them must fail loudly rather than appear to work.
"""

from typing import Callable, List

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


def check_no_legacy_settings(get: Callable = config.get) -> None:
    problems = legacy_settings_in_use(get)
    if problems:
        raise RuntimeError("; ".join(problems))
