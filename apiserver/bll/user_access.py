"""Repository accesses that one user owns: a token for a host (and a path prefix), or a GitHub App with its ID, key and API
address. Only the owner may see, check, change or delete an access; no call returns a secret. Other users use an access
only through the repository resolution in bll/repo_access, never by reading it.
"""

import logging
import re
import uuid
from datetime import datetime
from typing import Optional, Sequence

import requests
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from mongoengine import Q

from apiserver.apierrors import errors
from apiserver.config_repo import config
from apiserver.database.model.auth import User
from apiserver.database.model.git_credential import GitCredential
from apiserver.database.model.project import Project
from apiserver.database.model.share import Share
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


def _granted_to(entry: UserAccess) -> list:
    """The root projects the access is shared with, from its share records (whether or not the share applies today)."""
    return sorted(_access().shared_project_ids(entry.company, "credential", entry.id))


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
        "granted_projects": _granted_to(entry),
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
        # the server has one GitHub App: adding another replaces it
        _access().refuse_inactive(user)
        for previous in UserAccess.objects(company=company, scope="company", kind="app"):
            previous.delete()
            _access().drop_resource_shares(company, "credential", previous.id, actor=user)
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
        "type": "GitHub App" if entry.kind == "app" else "Token",
        "name": entry.label,
        "owner": owner_name,
        "scope": entry.scope or "user",
        "valid_for": valid_for(entry.kind, entry.host, entry.path),
        "state": entry.last_state or "unchecked",
        "message": entry.last_message or "",
        "last_checked": entry.last_checked.isoformat() if entry.last_checked else None,
        "granted_projects": _granted_to(entry),
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


def server_app():
    """The server's GitHub App, as (entry, key), or None when no admin has added one. It is the company's access
    that every repository check falls back to; the key is decrypted for this call only."""
    entry = UserAccess.objects(scope="company", kind="app").order_by("created").first()
    if entry is None:
        return None
    try:
        return entry, _secret(entry)
    except store.CredentialUnreadable:
        log.warning("the GitHub App of the server cannot be read with the current key")
        return None


def _access():
    """bll/access reads this module, so it is imported where it is used"""
    from apiserver.bll import access
    return access


def delete(company: str, user: str, access_id: str, is_admin: bool = False) -> None:
    _access().refuse_inactive(user)
    entry = _owned_or_company(company, user, access_id, is_admin)
    entry.delete()
    _access().drop_resource_shares(company, "credential", entry.id, actor=user)
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


def grant(company: str, owner: str, access_id: str, project_id: str) -> dict:
    """The owner shares one of their accesses with a project. Only a member of the project may do so."""
    _access().refuse_inactive(owner)
    entry = _owned(company, owner, access_id)
    access = _access()
    access.require(company, owner, False, "credential", entry.id, my_root_ids(company, owner), "manage_sharing")
    root = root_project(company, project_id)
    if root is None:
        raise errors.bad_request.ValidationError("no such project")
    if not is_member(company, root, owner):
        raise errors.bad_request.ValidationError("you are not a member of this project")
    if access.find_share(company, "credential", entry.id, "projects", root) is None:
        # the owner, a member of the project, grants the access to it
        access.record_grant(company, owner, "credential", entry.id, "projects", root, actor=owner)
        audit(owner, "user_access.granted", company=company, kind=entry.kind, project=root)
    return _view(entry)


def revoke(company: str, actor: str, access_id: str, project_id: str, is_admin: bool) -> dict:
    """The owner of the access, or an admin, or the owner of the project, stops a grant at once."""
    _access().refuse_inactive(actor)
    entry = UserAccess.objects(company=company, id=access_id).first()
    if entry is None:
        raise errors.bad_request.ValidationError("no such access")
    root = root_project(company, project_id)
    project_owner = Project.objects(company=company, id=root).only("user").first() if root else None
    allowed = actor == entry.user or is_admin or (project_owner is not None and project_owner.user == actor)
    if not allowed:
        raise errors.bad_request.ValidationError("no such access of yours")
    access = _access()
    if root and access.find_share(company, "credential", entry.id, "projects", root) is not None:
        access.remove_grant(company, "credential", entry.id, "projects", root, actor=actor)
        audit(actor, "user_access.revoked", company=company, kind=entry.kind, project=root)
    return _view(entry)


def project_grants(company: str, actor: str, project_id: str) -> list:
    """The accesses shared with a project, for its members: the label and the kind, the owner's name and what the access
    reaches. No secret."""
    root = root_project(company, project_id)
    if root is None or not is_member(company, root, actor):
        raise errors.bad_request.ValidationError("no such project")
    result = []
    for entry, _share in _project_grants_of(company, root):
        owner = User.objects(id=entry.user).only("name").first()
        result.append({
            "id": entry.id,
            "kind": entry.kind,
            "label": entry.label,
            "host": entry.host,
            "path": entry.path,
            "app_id": entry.app_id,
            "owner": entry.user,
            "owner_name": owner.name if owner else entry.user,
            "owner_active": is_active(entry.user),
            "owner_is_member": is_member(company, root, entry.user),
        })
    return result


def my_root_ids(company: str, user: str) -> set:
    """The ids of the root projects the user owns or is a member of, as bll/access reads them."""
    return {project["id"] for project in my_projects(company, user)}


def my_projects(company: str, user: str) -> list:
    """The root projects the user owns or is a member of: the ones they may share an access with."""
    projects = Project.objects(
        Q(company=company) & Q(parent=None) & (Q(user=user) | Q(members__user=user))).only("id", "name")
    return [{"id": project.id, "name": project.name} for project in projects]


def _project_grants_of(company: str, root: str) -> list:
    """The accesses that a share of a credential names for the root project, as (entry, share) pairs in the order the
    accesses were added. The share records are the source: the old field is not read."""
    shares = {share.resource_id: share
              for share in Share.objects(company=company, resource_type="credential", scope="projects", project=root)}
    if not shares:
        return []
    entries = UserAccess.objects(company=company, id__in=list(shares)).order_by("created")
    return [(entry, shares[entry.id]) for entry in entries]


def _shared_use(company: str, user: str, share: Share, root: str) -> bool:
    """Whether a project share lets the user use the credential now. bll/access decides it: the share applies while its
    granter and the user are active members of the root project, and a share gives use at read."""
    access = _access()
    return access.allows(access._share_level(company, share, user, {root}, True, True), "use", "credential")


def _own_use(company: str, user: str, entry: UserAccess) -> bool:
    """Whether the user may use their own credential now: its owner is a collaborator, unless the user is deactivated."""
    access = _access()
    return access.allows(access.level_of(company, user, False, "credential", entry.id, set()), "use", "credential")


def usable_for(company: str, user: str, host: str, path: str, project: Optional[str] = None) -> list:
    """The accesses the user may use for a repository on this host and path, as (entry, secret) pairs.

    Their own: every app, and every token whose host matches and whose path prefix covers the repository. Shared with
    the project: the accesses a share gives to the project's root, while the granter and the user are still members and
    active. A disabled user has none."""
    if not user or not is_active(user):
        return []
    found = []
    for entry in UserAccess.objects(company=company, user=user, scope="user").order_by("created"):
        if entry.kind == "token" and (entry.host != host or not store.covers(entry.path, path)):
            continue
        if _own_use(company, user, entry):
            found.append(entry)
    root = root_project(company, project) if project else None
    if root:
        for entry, share in _project_grants_of(company, root):
            if entry.user == user:
                continue
            if entry.kind == "token" and (entry.host != host or not store.covers(entry.path, path)):
                continue
            if _shared_use(company, user, share, root):
                found.append(entry)
    pairs = []
    for entry in found:
        try:
            pairs.append((entry, _secret(entry)))
        except store.CredentialUnreadable:
            log.warning("the access %s cannot be read with the current key", entry.id)
    return pairs


def granted_for_job(company: str, enqueuer: str, root: str, host: str, path: str) -> list:
    """The accesses shared with a project that a job of the project may use, as (entry, secret) pairs: the grants whose
    owner is still a member and active, while the user who queued the job is a member and active. The agent's owner is
    not consulted: the access comes from the project. A token counts only for its host and path prefix."""
    if not root or not is_active(enqueuer) or not is_member(company, root, enqueuer):
        return []
    pairs = []
    for entry, share in _project_grants_of(company, root):
        if entry.kind == "token" and (entry.host != host or not store.covers(entry.path, path)):
            continue
        if not _shared_use(company, enqueuer, share, root):
            continue
        try:
            pairs.append((entry, _secret(entry)))
        except store.CredentialUnreadable:
            log.warning("the access %s cannot be read with the current key", entry.id)
    return pairs


def project_agent_tokens_allowed(company: str, root: str) -> bool:
    """Whether a project lets its agents be given personal access tokens shared with it. On by default, with a warning
    at the time of sharing; a project may turn it off."""
    project = Project.objects(company=company, id=root).only("agent_tokens_allowed").first()
    return project is None or project.agent_tokens_allowed is not False


def set_agent_tokens_allowed(company: str, actor: str, project_id: str, allowed: bool, is_admin: bool) -> dict:
    """The owner of the project (or an admin) decides whether its agents may be given personal tokens."""
    root = root_project(company, project_id)
    project = Project.objects(company=company, id=root).only("user").first() if root else None
    if project is None:
        raise errors.bad_request.ValidationError("no such project")
    if not (is_admin or project.user == actor):
        raise errors.bad_request.ValidationError("only the owner of the project may change this")
    Project.objects(company=company, id=root).update_one(set__agent_tokens_allowed=bool(allowed))
    audit(actor, "project.agent_tokens", company=company, project=root, allowed=bool(allowed))
    return {"project": root, "agent_tokens_allowed": bool(allowed)}
