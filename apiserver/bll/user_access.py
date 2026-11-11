"""Repository accesses (credentials) that one user owns: a GitHub App with its ID, key and API address, and the company's, which the
admin owns. Only the owner may see, check, change or delete an access; no call returns a secret. A credential belongs to its user
and is never shared with a project: it is used through the assignments of repositories (bll/repositories), never read.
"""

import logging
import re
import uuid
from datetime import datetime
from typing import Optional, Sequence

from cryptography.hazmat.primitives.serialization import load_pem_private_key
from mongoengine import Q

from apiserver.apierrors import errors
from apiserver.config_repo import config
from apiserver.database.model.auth import User
from apiserver.database.model.project import Project
from apiserver.database.model.user_access import UserAccess
from apiserver.idp import git_credentials as store
from apiserver.idp.audit import record as audit
from apiserver.idp.github import GitHubApp, GitHubError, GitHubRefused

log = logging.getLogger(__name__)
APP_ID_RE = re.compile(r"^\d{1,12}$")
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
        "scope": entry.scope or "user",
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


def _owned_or_company(company: str, user: str, access_id: str, is_admin: bool) -> UserAccess:
    """The user's own access, or the company's App when the user is an admin."""
    entry = UserAccess.objects(company=company, id=access_id).first()
    if entry is not None and entry.scope == "company":
        if not is_admin:
            raise errors.bad_request.ValidationError("only an admin may change the company's GitHub App")
        return entry
    return _owned(company, user, access_id)


def _owned(company: str, user: str, access_id: str) -> UserAccess:
    entry = UserAccess.objects(company=company, user=user, id=access_id, scope="user").first()
    if entry is None:
        # the same answer for an access of someone else: its existence is not revealed
        raise errors.bad_request.ValidationError("no such access of yours")
    return entry


def add_app(company: str, user: str, label: str, app_id: str, api_url: str, key: str, scope: str = "user") -> dict:
    app_id = (app_id or "").strip()
    if not APP_ID_RE.match(app_id):
        raise errors.bad_request.ValidationError("the App ID is a number, as GitHub shows it")
    pem = (key or "").strip()
    if "-----BEGIN" not in pem or "PRIVATE KEY" not in pem:
        raise errors.bad_request.ValidationError("the key must be the App's private key file (a .pem file)")
    try:
        load_pem_private_key(pem.encode(), password=None)
    except (ValueError, TypeError):
        raise errors.bad_request.ValidationError("the key is not a valid private key (PEM)") from None
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
    if scope == "company":
        # the company may have several GitHub Apps (one for each group, for example): adding one keeps the others
        _access().refuse_inactive(user)
    entry = UserAccess(
        id=uuid.uuid4().hex, company=company, user=user, kind="app", label=_label(label), scope=scope,
        host="github.com", app_id=app_id, api_url=api,
        secret=store.encrypt(pem, store.key_material()), created=now, updated=now,
        last_checked=now, last_state="ok",
        last_message=f"GitHub accepts the App {info.get('slug') or app_id}",
    )
    entry.save()
    audit(user, "user_access.added", company=company, kind="app", app_id=app_id, scope=scope)
    return _view(entry)


def valid_for(kind: str, host: str, path: str) -> str:
    """What a credential is valid for, in words: the repositories of its host, or of one path prefix on it."""
    if kind == "app":
        return "Repositories where the GitHub App is installed"
    if not path:
        return f"All repositories on {host}"
    return f"{host}/{path}"


def _row(entry: UserAccess, owner_name: str, can_edit: bool, source: str = "access") -> dict:
    return {
        "id": entry.id,
        "source": source,
        "type": "GitHub App" if entry.kind == "app" else "Git credential",
        "name": entry.label,
        "owner": owner_name,
        "scope": entry.scope or "user",
        "valid_for": valid_for(entry.kind, entry.host, entry.path),
        "state": entry.last_state or "unchecked",
        "message": entry.last_message or "",
        "last_checked": entry.last_checked.isoformat() if entry.last_checked else None,
        "can_edit": can_edit,
        "app_id": entry.app_id,
    }


def credentials_for(company: str, user: str, is_admin: bool) -> list:
    """Every credential the user sees, in one list: their own accesses, the company's GitHub App, and the company's Git
    credentials for other hosts. Each says who owns it, what it is valid for, whether it is valid, and whether this user
    may change it (an owner changes their own; an admin changes the company's)."""
    me = User.objects(id=user).only("name").first()
    rows = [_row(entry, me.name if me else user, True)
            for entry in UserAccess.objects(company=company, user=user, scope="user").order_by("created")]
    rows += [_row(entry, "Company", is_admin)
             for entry in UserAccess.objects(company=company, scope="company").order_by("created")]
    return rows


def list_for(company: str, user: str) -> dict:
    """The user's own accesses, and the company's (set by admins), which the user can use but not change."""
    own = [_view(entry) for entry in UserAccess.objects(company=company, user=user, scope="user").order_by("created")]
    company_apps = [_view(entry) for entry in UserAccess.objects(company=company, scope="company").order_by("created")]
    company_tokens = [_view(entry) for entry in UserAccess.objects(company=company, scope="company", kind="token")]
    return {"own": own, "company": company_tokens, "company_apps": company_apps}


def company_apps(company: str = None) -> list:
    """The company's GitHub Apps, as (entry, key) pairs in the order they were added. They are the company's access that every
    repository check falls back to; each key is decrypted for this call only."""
    query = UserAccess.objects(scope="company", kind="app")
    if company:
        query = query.filter(company=company)
    found = []
    for entry in query.order_by("created"):
        try:
            found.append((entry, _secret(entry)))
        except store.CredentialUnreadable:
            log.warning("the company GitHub App %s cannot be read with the current key", entry.id)
    return found


def server_app():
    """The first of the company's GitHub Apps, as (entry, key), or None when no admin has added one."""
    apps = company_apps()
    return apps[0] if apps else None


def _access():
    """bll/access reads this module, so it is imported where it is used"""
    from apiserver.bll import access
    return access


def delete(company: str, user: str, access_id: str, is_admin: bool = False) -> None:
    _access().refuse_inactive(user)
    entry = _owned_or_company(company, user, access_id, is_admin)
    entry.delete()
    from apiserver.database.model.repository import Assignment
    Assignment.objects(company=company, credential=entry.id).delete()
    audit(user, "user_access.deleted", company=company, kind=entry.kind)


def check(company: str, user: str, access_id: str, is_admin: bool = False) -> dict:
    entry = _owned_or_company(company, user, access_id, is_admin)
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
    else:
        state, message = "unverified", "Not verified here: the credential for this host is checked when a job uses it"
    entry.last_checked, entry.last_state, entry.last_message, entry.updated = now, state, message, now
    entry.save()
    audit(user, "user_access.checked", company=company, kind=entry.kind, state=state)
    return _view(entry)


def _secret(entry: UserAccess) -> str:
    return store.decrypt(entry.secret, store.key_material())


def root_project(company: str, project_id: Optional[str]) -> Optional[str]:
    """The root project of a project (grants are made on root projects); None when there is no such project."""
    current = Project.objects(company=company, id=project_id).only("id", "parent").first() if project_id else None
    seen = set()
    while current is not None and current.parent and current.id not in seen:
        seen.add(current.id)
        current = Project.objects(company=company, id=current.parent).only("id", "parent").first()
    return current.id if current is not None else None


def is_member(company: str, project_id: str, user: str) -> bool:
    """The owner of the project, or one of its members (any role)."""
    project = Project.objects(company=company, id=project_id).only("user", "members").first()
    if project is None or not user:
        return False
    return project.user == user or any(member.user == user for member in project.members)


def is_active(user: str) -> bool:
    return bool(user) and User.objects(id=user, idp_disabled=True).only("id").first() is None


def grant_applies(grant_root: str, project_root: Optional[str], owner_is_member: bool, user_is_member: bool,
                  owner_active: bool, user_active: bool) -> bool:
    """The one rule for a grant: it applies to the project it names, while its owner is still a member and active, and
    the user is still a member and active."""
    return (project_root == grant_root and owner_is_member and user_is_member and owner_active and user_active)


def my_root_ids(company: str, user: str) -> set:
    """The ids of the root projects the user owns or is a member of, as bll/access reads them."""
    return {project["id"] for project in my_projects(company, user)}


def my_projects(company: str, user: str) -> list:
    """The root projects the user owns or is a member of: the ones they may share an access with."""
    projects = Project.objects(
        Q(company=company) & Q(parent=None) & (Q(user=user) | Q(members__user=user))).only("id", "name")
    return [{"id": project.id, "name": project.name} for project in projects]
