"""Repository accesses that one user owns: a token for a host (and a path prefix), or a GitHub App with its ID, key and API
address. Only the owner may see, check, change or delete an access; no call returns a secret. Other users use an access
only through the repository resolution in bll/repo_access, never by reading it.
"""

import logging
import re
import uuid
from datetime import datetime
from typing import Optional

import requests

from apiserver.apierrors import errors
from apiserver.config_repo import config
from apiserver.database.model.auth import User
from apiserver.database.model.git_credential import GitCredential
from apiserver.database.model.user_access import UserAccess
from apiserver.idp import git_credentials as store
from apiserver.idp.audit import record as audit
from apiserver.idp.github import GitHubApp, GitHubError, GitHubRefused

log = logging.getLogger(__name__)
APP_ID_RE = re.compile(r"^\d{1,12}$")
GITHUB_HOSTS = ("github.com", "www.github.com")
MAX_LABEL = 80


def _label(text: str) -> str:
    value = (text or "").strip()
    if not value or len(value) > MAX_LABEL:
        raise errors.bad_request.ValidationError(f"a name is needed, up to {MAX_LABEL} characters")
    return value


def _view(entry: UserAccess) -> dict:
    """What the owner may see: never the secret."""
    return {
        "id": entry.id,
        "kind": entry.kind,
        "label": entry.label,
        "host": entry.host,
        "path": entry.path,
        "app_id": entry.app_id,
        "api_url": entry.api_url,
        "created": entry.created.isoformat() if entry.created else None,
        "last_checked": entry.last_checked.isoformat() if entry.last_checked else None,
        "last_state": entry.last_state,
        "last_message": entry.last_message,
    }


def _owned(company: str, user: str, access_id: str) -> UserAccess:
    entry = UserAccess.objects(company=company, user=user, id=access_id).first()
    if entry is None:
        # the same answer for an access of someone else: its existence is not revealed
        raise errors.bad_request.ValidationError("no such access of yours")
    return entry


def add_token(company: str, user: str, label: str, host: str, path: str, token: str) -> dict:
    value = (token or "").strip()
    if not value:
        raise errors.bad_request.ValidationError("the token is empty")
    host = store.normalise_host(host or "github.com")
    path = store.normalise_path(path or "")
    now = datetime.utcnow()
    entry = UserAccess(
        id=uuid.uuid4().hex, company=company, user=user, kind="token", label=_label(label),
        host=host, path=path, secret=store.encrypt(value, store.key_material()), created=now, updated=now,
    )
    entry.save()
    audit(user, "user_access.added", company=company, kind="token", host=host, path=path)
    return _view(entry)


def add_app(company: str, user: str, label: str, app_id: str, api_url: str, key: str) -> dict:
    app_id = (app_id or "").strip()
    if not APP_ID_RE.match(app_id):
        raise errors.bad_request.ValidationError("the App ID is a number, as GitHub shows it")
    pem = (key or "").strip()
    if "-----BEGIN" not in pem or "PRIVATE KEY" not in pem:
        raise errors.bad_request.ValidationError("the key must be the App's private key file (a .pem file)")
    api = (api_url or "").strip() or config.get("apiserver.idp.github_api_url")
    if not api.startswith(("https://", "http://")):
        raise errors.bad_request.ValidationError("the API address must start with https://")
    try:
        info = GitHubApp(app_id, pem, api).app_info()
    except GitHubRefused:
        raise errors.bad_request.ValidationError("GitHub did not accept this App ID or key") from None
    except GitHubError:
        raise errors.bad_request.ValidationError("GitHub could not be reached to check the App") from None
    now = datetime.utcnow()
    entry = UserAccess(
        id=uuid.uuid4().hex, company=company, user=user, kind="app", label=_label(label),
        host="github.com", app_id=app_id, api_url=api,
        secret=store.encrypt(pem, store.key_material()), created=now, updated=now,
        last_checked=now, last_state="ok",
        last_message=f"GitHub accepts the App {info.get('slug') or app_id}",
    )
    entry.save()
    audit(user, "user_access.added", company=company, kind="app", app_id=app_id)
    return _view(entry)


def list_for(company: str, user: str) -> dict:
    """The user's own accesses, and the company's (set by admins), which the user can use but not change."""
    own = [_view(entry) for entry in UserAccess.objects(company=company, user=user).order_by("created")]
    company_tokens = [
        {"host": entry.host, "path": entry.path, "username": entry.username}
        for entry in GitCredential.objects(company=company).order_by("host", "path")
    ]
    return {"own": own, "company": company_tokens}


def delete(company: str, user: str, access_id: str) -> None:
    entry = _owned(company, user, access_id)
    entry.delete()
    audit(user, "user_access.deleted", company=company, kind=entry.kind)


def check(company: str, user: str, access_id: str) -> dict:
    entry = _owned(company, user, access_id)
    secret = _secret(entry)
    now = datetime.utcnow()
    if entry.kind == "app":
        app = GitHubApp(entry.app_id, secret, entry.api_url)
        try:
            info = app.app_info()
            reach = len(app.installed_repositories())
            state, message = "ok", f"GitHub accepts the App {info.get('slug') or entry.app_id}; installed on {reach} repositories"
        except GitHubRefused:
            state, message = "refused", "GitHub did not accept this App ID or key any more"
        except GitHubError:
            state, message = "unreachable", "GitHub could not be reached"
    elif entry.host in GITHUB_HOSTS:
        state, message = ("ok", "GitHub accepts this token") if token_works(secret) else (
            "refused", "GitHub refuses this token")
    else:
        state, message = "unverified", "Not verified here: a token for this host is checked when a job uses it"
    entry.last_checked, entry.last_state, entry.last_message, entry.updated = now, state, message, now
    entry.save()
    audit(user, "user_access.checked", company=company, kind=entry.kind, state=state)
    return _view(entry)


def token_works(token: str, api_url: Optional[str] = None) -> bool:
    api = (api_url or config.get("apiserver.idp.github_api_url")).rstrip("/")
    try:
        response = requests.get(f"{api}/user", timeout=15, headers={
            "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
    except requests.RequestException:
        return False
    return response.status_code == 200


def _secret(entry: UserAccess) -> str:
    return store.decrypt(entry.secret, store.key_material())


def usable_for(company: str, user: str, host: str, path: str) -> list:
    """The accesses the user may use for a repository on this host and path, as (entry, secret) pairs: every app of the
    user, and every token whose host matches and whose path prefix covers the repository. A disabled user has none."""
    if not user or User.objects(id=user, idp_disabled=True).only("id").first():
        return []
    found = []
    for entry in UserAccess.objects(company=company, user=user).order_by("created"):
        if entry.kind == "token" and (entry.host != host or not store.covers(entry.path, path)):
            continue
        try:
            found.append((entry, _secret(entry)))
        except store.CredentialUnreadable:
            log.warning("the owner's access %s cannot be read with the current key", entry.id)
    return found
