"""How a repository is reached: publicly, through the server's GitHub App, or through a stored token. Phase 1 knows only
the company-wide accesses that already exist (the GitHub App of the server and the company's Git credentials); a
per-user or per-project access comes later.

The result is what the badges show. It never contains a secret. A refused repository is reported, not raised: the
job that uses it is marked Failed with the reason, by the caller.
"""

import logging
import time
from typing import Optional

import requests

from apiserver.bll import user_access
from apiserver.bll.dataset_registry import canonical_url, parse_github_url
from apiserver.config_repo import config
from apiserver.idp import git_credentials as store
from apiserver.idp.github import GitHubApp, GitHubError, RepoNotAccessible, public_repository
from apiserver.idp.github_check import build_from_config
from apiserver.idp.repo_visibility import is_private
from apiserver.idp.secrets import SecretMissing

CACHE_SEC = 600
PUBLIC, APP, TOKEN, REFUSED = "public", "app", "token", "refused"
log = logging.getLogger(__name__)
_cache: dict = {}  # (company, repository) -> (when, result)


def _result(repository: str, state: str, via: str, message: str) -> dict:
    return {"repository": repository, "state": state, "via": via, "message": message}


def _app_label() -> str:
    """The name of the server's GitHub App, from the last check; a plain name when there is none yet."""
    from apiserver.bll.github_status import get_status

    status = get_status() or {}
    return (status.get("app") or {}).get("slug") or "GitHub App"


def _token_works(repo: str, token: str) -> bool:
    api_url = config.get("apiserver.idp.github_api_url").rstrip("/")
    try:
        response = requests.get(f"{api_url}/repos/{repo}", timeout=15,
                                headers={"Authorization": f"Bearer {token}",
                                         "Accept": "application/vnd.github+json"})
    except requests.RequestException:
        return False
    return response.status_code == 200


def _github(company: str, user: Optional[str], repository: str, owner: str, name: str) -> dict:
    repo = f"{owner}/{name}"
    api_url = config.get("apiserver.idp.github_api_url")
    try:
        if public_repository(api_url, repo):
            return _result(repository, PUBLIC, "", "Publicly accessible")
    except GitHubError:
        log.warning("GitHub could not say whether %s is public", repo, exc_info=True)

    # the user's own accesses first: their apps and their tokens, in the order they were added
    host, path = store.host_and_path(canonical_url(repo))
    for entry, secret in user_access.usable_for(company, user, host, path):
        if entry.kind == "app":
            try:
                GitHubApp(entry.app_id, secret, entry.api_url or api_url).installation_token(repo)
                return _result(repository, APP, entry.label, f"Accessible via your GitHub App {entry.label}")
            except (RepoNotAccessible, GitHubError):
                continue
        elif _token_works(repo, secret):
            return _result(repository, TOKEN, entry.label, f"Accessible via your token {entry.label}")

    try:
        app = build_from_config()
    except SecretMissing:
        app = None
    if app is not None:
        try:
            app.installation_token(repo)
            label = _app_label()
            return _result(repository, APP, label, f"Accessible via GitHub App {label}")
        except RepoNotAccessible:
            pass
        except GitHubError:
            return _result(repository, REFUSED, "", "GitHub could not be reached to check this repository")

    credential = store.find_credential(company, canonical_url(repo))
    if credential is not None:
        try:
            token = store.secret_of(credential)
        except Exception:
            log.warning("the token stored for %s cannot be read", credential.host, exc_info=True)
            return _result(repository, REFUSED, credential.host,
                           f"The token stored for {credential.host} cannot be read by the server")
        if _token_works(repo, token):
            return _result(repository, TOKEN, credential.host, f"Accessible via the token stored for {credential.host}")
        return _result(repository, REFUSED, credential.host,
                       f"The token stored for {credential.host} is refused by GitHub for {repo}")

    return _result(repository, REFUSED, "",
                   f"{repo} is private, and neither the GitHub App nor a stored token reaches it. "
                   "Ask an admin to install the App on the repository or to store a token.")


def _other_host(company: str, user: Optional[str], repository: str) -> dict:
    if is_private(repository) is False:
        return _result(repository, PUBLIC, "", "Publicly accessible")
    host, path = store.host_and_path(repository)
    for entry, _secret in user_access.usable_for(company, user, host, path):
        if entry.kind == "token":
            return _result(repository, TOKEN, entry.label,
                           f"Your token {entry.label} is stored for {host}; it is not verified until a job runs")
    credential = store.find_credential(company, repository)
    if credential is not None:
        # Git hosts other than GitHub are not asked with the token here: it is recorded as stored, not verified.
        return _result(repository, TOKEN, credential.host,
                       f"A token is stored for {credential.host}; it is not verified until a job runs")
    host, _path = store.host_and_path(repository)
    return _result(repository, REFUSED, "",
                   f"{host} is private or cannot be checked, and no Git credentials are stored for it. "
                   "Ask an admin to store them.")


def resolve_one(company: str, repository: str, user: Optional[str] = None, now: Optional[float] = None) -> dict:
    now = time.time() if now is None else now
    key = (company, user, repository)
    cached = _cache.get(key)
    if cached and now - cached[0] < CACHE_SEC:
        return cached[1]
    parsed = parse_github_url(repository)
    result = (_github(company, user, repository, *parsed) if parsed
              else _other_host(company, user, repository))
    _cache[key] = (now, result)
    return result


def resolve(company: str, repositories: list, user: Optional[str] = None) -> list:
    return [resolve_one(company, repository, user) for repository in repositories if repository]
