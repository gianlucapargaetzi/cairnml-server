"""Devices (laptops and agents) enrol with their IdP sign-in and their public key, then exchange
a signed assertion for short-lived access tokens. Ported from the mlops server's enrol and exchange."""

import jwt
import logging
import uuid
from datetime import datetime

from apiserver.apierrors import errors
from apiserver.apimodels.devices import (
    EnrollRequest,
    ExchangeRequest,
    GitTokenRequest,
    JobTokenRequest,
    ReassignRequest,
    RevokeRequest,
)
from apiserver.bll import dataset_cache
from apiserver.bll.dataset_access import verify_dataset
from apiserver.config_repo import config
from apiserver.database.model.auth import Role, User
from apiserver.database.model.device import Device, DeviceKind
from apiserver.database.model.task.task import Task
from apiserver.idp.assertion import AssertionInvalid, unverified_issuer, verify_assertion
from apiserver.idp.audit import client_address, record as audit
from apiserver.idp.checks import AccessDenied, check_device, check_job_issue
from apiserver.idp.github import GitHubApp, GitHubError, RepoNotAccessible, TokenReader
from apiserver.bll import user_access
from apiserver.bll import datasets as datasets_bll
from apiserver.bll.dataset_registry import canonical_url
from apiserver.idp.github_check import build_from_config, public_repo, repo_of
from apiserver.idp.keys import load_public_key
from apiserver.idp import git_credentials as store
from apiserver.idp import public_origin
from apiserver.idp.oidc import OidcError
from apiserver.idp.replay import claim_jti
from apiserver.idp.tokens import issue_access_token, issue_job_token
from apiserver.idp.users import upsert_idp_user
from apiserver.idp.verifier import get_verifier
from apiserver.service_repo import APICall, endpoint
from apiserver.service_repo.auth import Token

ADMIN_ROLES = (Role.admin, Role.root, Role.system)
log = logging.getLogger(__name__)


def _bearer(call: APICall) -> str:
    header = call.authorization or ""
    if not header.lower().startswith("bearer "):
        raise errors.unauthorized.NoCredentials()
    return header[7:].strip()


def _device_view(device: Device, owner: User = None) -> dict:
    """The device as the API shows it. owner_disabled is true when the owner is disabled or no longer exists: the
    device's tokens are refused then, until an admin reassigns it."""
    return {
        "id": device.id,
        "user": device.user,
        "owner_name": owner.name if owner else None,
        "owner_disabled": owner is None or bool(owner.idp_disabled),
        "kind": device.kind,
        "name": device.name,
        "created": device.created.isoformat() if device.created else None,
        "last_used": device.last_used.isoformat() if device.last_used else None,
        "revoked": device.revoked,
    }


def _device_views(devices) -> list:
    devices = list(devices)
    owners = {u.id: u for u in User.objects(id__in=list({d.user for d in devices})).only("id", "name", "idp_disabled")}
    return [_device_view(d, owners.get(d.user)) for d in devices]


@endpoint("devices.enroll", min_version="2.8", request_data_model=EnrollRequest)
def enroll(call: APICall, _company, request: EnrollRequest):
    if request.kind not in (DeviceKind.client, DeviceKind.agent):
        raise errors.bad_request.InvalidId("kind must be client or agent")
    try:
        load_public_key(request.public_key)
    except ValueError:
        raise errors.bad_request.InvalidId("public_key is not a valid key") from None
    try:
        identity = get_verifier().verify(_bearer(call), clients=[config.get("apiserver.idp.sdk_client_id")])
    except OidcError:
        raise errors.unauthorized.InvalidToken("invalid IdP token") from None

    issuer = config.get("apiserver.idp.issuer")
    admin_role = config.get("apiserver.idp.admin_role")
    user = upsert_idp_user(issuer, identity.subject, identity.username, identity.roles, admin_role)
    if user.idp_disabled:
        raise errors.forbidden.RoleNotAllowed("user is disabled")

    device = Device(
        id=f"dev_{uuid.uuid4().hex}",
        user=user.id,
        company=user.company,
        kind=request.kind,
        name=request.name or "",
        public_key=request.public_key,
        created=datetime.utcnow(),
        revoked=False,
    )
    device.save()
    audit(user.id, "device.enrolled", ip=client_address(call), company=user.company, device=device.id, kind=device.kind)
    result = {"id": device.id, "kind": device.kind}
    if device.kind == DeviceKind.agent:
        result["notice"] = AGENT_NOTICE
    return result


@endpoint("devices.idp_settings", min_version="2.8")
def idp_settings(call: APICall, _company, _):
    """What a device needs to start the identity-provider sign-in. No secret is returned."""
    return {
        "issuer": public_origin.idp_issuer(),
        "sdk_client_id": config.get("apiserver.idp.sdk_client_id"),
        # The address devices must use: a device's assertion is addressed to it, so it must match exactly.
        "public_url": public_origin.api_url().rstrip("/"),
        "web_url": public_origin.web_url(),
        # Whether a GitHub App is configured. Only the flag: the App's id and key are never returned.
        # whether an admin has added the server's GitHub App: the flag only, never its ID or key
        "github_app": user_access.server_app() is not None,
    }


@endpoint("devices.exchange", min_version="2.8", request_data_model=ExchangeRequest)
def exchange(call: APICall, _company, request: ExchangeRequest):
    try:
        device_id = unverified_issuer(request.assertion)
    except AssertionInvalid:
        raise errors.unauthorized.InvalidToken("malformed assertion") from None
    device = Device.objects(id=device_id).first()
    owner = User.objects(id=device.user).first() if device else None
    try:
        check_device(device, owner)
    except AccessDenied as exc:
        audit(device_id, "token.refused", ip=client_address(call), company=device.company if device else None, device=device_id, reason=str(exc))
        raise errors.unauthorized.InvalidToken(str(exc)) from None

    audience = public_origin.api_url().rstrip("/")
    try:
        claims = verify_assertion(request.assertion, load_public_key(device.public_key), audience=audience)
    except (AssertionInvalid, ValueError):
        audit(device_id, "token.refused", ip=client_address(call), company=device.company, device=device_id, reason="bad assertion")
        raise errors.unauthorized.InvalidToken("invalid assertion") from None

    now = datetime.utcnow().timestamp()
    if not claim_jti(claims["jti"], ttl=max(1, int(claims["exp"] - now)) + 60):
        audit(device_id, "token.refused", ip=client_address(call), company=device.company, device=device_id, reason="assertion reused")
        raise errors.unauthorized.InvalidToken("assertion already used")

    # An agent acts with the user role whatever its owner's role: it never holds admin rights.
    role = Role.user if device.kind == DeviceKind.agent else owner.role
    token = issue_access_token(owner.id, owner.company, device.id, kind=device.kind,
                               role=role, user_name=owner.name)
    device.last_used = datetime.utcnow()
    device.save()
    audit(owner.id, "token.issued", ip=client_address(call), company=owner.company, device=device.id, kind=device.kind)
    return {"access_token": token, "expires_in": config.get("apiserver.idp.access_token_ttl_sec")}


@endpoint("devices.get_all", min_version="2.8")
def get_all(call: APICall, _company, _):
    identity = call.identity
    query = {} if identity.role in ADMIN_ROLES else {"user": identity.user}
    return {"devices": _device_views(Device.objects(**query))}


@endpoint("devices.revoke", min_version="2.8", request_data_model=RevokeRequest)
def revoke(call: APICall, _company, request: RevokeRequest):
    identity = call.identity
    device = Device.objects(id=request.id).first()
    if device is None:
        raise errors.bad_request.InvalidId("no such device")
    if device.user != identity.user and identity.role not in ADMIN_ROLES:
        raise errors.forbidden.RoleNotAllowed("only the owner or an admin may revoke a device")
    device.revoked = True
    device.save()
    audit(identity.user, "device.revoked", ip=client_address(call), company=device.company, device=device.id)
    return _device_view(device, User.objects(id=device.user).first())


@endpoint("devices.reassign", min_version="2.8", request_data_model=ReassignRequest)
def reassign(call: APICall, _company, request: ReassignRequest):
    identity = call.identity
    if identity.role not in ADMIN_ROLES:
        raise errors.forbidden.RoleNotAllowed("only an admin may reassign a device")
    device = Device.objects(id=request.id).first()
    if device is None:
        raise errors.bad_request.InvalidId("no such device")
    if device.revoked:
        raise errors.bad_request.InvalidId("device is revoked")
    new_owner = User.objects(id=request.user, company=device.company).first()
    if new_owner is None:
        raise errors.bad_request.InvalidId("no such user")
    if new_owner.idp_disabled or new_owner.role not in ADMIN_ROLES:
        raise errors.bad_request.InvalidId("the new owner must be an enabled admin")
    previous_owner = device.user
    device.user = new_owner.id
    device.save()
    audit(identity.user, "device.reassigned", ip=client_address(call), company=device.company, device=device.id, previous_owner=previous_owner, new_owner=new_owner.id)
    return _device_view(device, new_owner)


def _caller_device_id(call: APICall) -> str:
    """The device behind the caller's token: the agent's own device id, from its verified token."""
    try:
        claims = Token.decode(_bearer(call))
    except jwt.PyJWTError:  # forged, expired or not a token at all
        raise errors.unauthorized.InvalidToken("invalid token") from None
    if claims.get("kind") != DeviceKind.agent:
        raise errors.unauthorized.InvalidToken("only an agent may get a job token")
    return claims.get("device")


@endpoint("devices.job_token", min_version="2.8", request_data_model=JobTokenRequest)
def job_token(call: APICall, _company, request: JobTokenRequest):
    device = Device.objects(id=_caller_device_id(call)).first()
    task = Task.objects(id=request.task).only("status", "last_device", "user").first()
    owner = User.objects(id=task.user).first() if task else None
    try:
        check_job_issue(task.status if task else None, task.last_device if task else None, device)
        if owner is None or owner.idp_disabled:
            raise AccessDenied("task owner is disabled or unknown")
    except AccessDenied as exc:
        audit(device.id if device else "unknown", "job_token.refused", ip=client_address(call), company=device.company if device else None, device=device.id if device else None, task=request.task, reason=str(exc))
        raise errors.unauthorized.InvalidToken(str(exc)) from None
    token = issue_job_token(owner, task.id, device.id)
    audit(device.id, "job_token.issued", ip=client_address(call), company=device.company, task=task.id)
    return {"access_token": token, "expires_in": config.get("apiserver.idp.job_token_ttl_sec")}


@endpoint("devices.git_token", min_version="2.8", request_data_model=GitTokenRequest)
def git_token(call: APICall, company, request: GitTokenRequest):
    """A read-only token for the task's repository, for the job or the agent running it. On GitHub that is a
    GitHub App token; on another host it is the Git credential stored for the host (and path prefix)."""
    claims = Token.decode(_bearer(call))
    task = Task.objects(id=request.task).first()
    try:
        if claims.get("kind") == "job":
            if claims.get("task") != request.task:
                raise AccessDenied("this token is for another task")
        elif claims.get("kind") == DeviceKind.agent:
            check_job_issue(task.status if task else None, task.last_device if task else None,
                            Device.objects(id=claims.get("device")).first())
        else:
            raise AccessDenied("only a job or its agent may get a repository token")
    except AccessDenied as exc:
        audit(claims.get("device") or "unknown", "git_token.refused", ip=client_address(call), company=company, device=claims.get("device"), task=request.task, reason=str(exc))
        raise errors.unauthorized.InvalidToken(str(exc)) from None

    actor = claims.get("device") or claims.get("sub") or "unknown"
    app = build_from_config()
    if request.repository:
        # A dataset is a repository the GitHub App is installed on, with a dataset.yaml at its root and a footprint that
        # holds (see bll/dataset_access). A public repository is cloned without a token: the answer has no token.
        repo = repo_of(request.repository)
        if repo is None:
            raise errors.bad_request.ValidationError("the repository is not on GitHub")
        public = _public_answer(repo, actor, company, claims, request.task, call)
        if public is not None:
            return public
        # a dataset kept by the environment names the credential that reads it: the job uses that credential, shared with
        # its project, or it is refused with the reason (bll/datasets)
        kept = datasets_bll.kept_for_repository(company, repo)
        if kept is not None:
            # a registry is read as it is: it holds no dataset.yaml, so its layout is not checked (a dataset's is)
            granted = None
            if kept.credential and task is not None:
                granted = _granted_token(company, task, actor, request.task, client_address(call), repo,
                                         dataset=kept.kind == "repo", credential=kept.credential)
            if granted is None:
                reason = (f"the credential of {kept.name} is not shared with this project" if kept.credential
                          else f"no access to {kept.kind} {kept.name}")
                audit(actor, "git_token.refused", ip=client_address(call), company=company, device=claims.get("device"), task=request.task, reason=reason)
                raise errors.bad_request.ValidationError(reason)
            return granted
        # the accesses shared with the task's project come first, as for the task's own repository. The layout is
        # checked with the same access that reads the repository.
        if task is not None:
            granted = _granted_token(company, task, actor, request.task, client_address(call), repo, dataset=True)
            if granted is not None:
                return granted
        if app is None:
            raise errors.bad_request.ValidationError("GitHub is not configured")
        try:
            verify_dataset(app, repo)
        except errors.bad_request.ValidationError as exc:
            audit(actor, "git_token.refused", ip=client_address(call), company=company, device=claims.get("device"), task=request.task, reason=str(exc))
            raise
        try:
            token = _github_token(app, repo, actor, request.task, client_address(call), company)
        except RepoNotAccessible:
            raise errors.bad_request.ValidationError(f"the GitHub App is not installed on {repo}") from None
        _refresh_dataset_entry(app, repo)
        return token

    if task is None:
        raise errors.bad_request.InvalidId("task not found")
    repository = task.script.repository
    if not repository:
        raise errors.bad_request.ValidationError("the task has no repository")
    repo = repo_of(repository)
    if repo is None:
        return _stored_token(company, actor, request.task, repository, client_address(call))
    public = _public_answer(repo, actor, company, claims, request.task, call)
    if public is not None:
        return public
    # the accesses shared with the task's project come first: the project decides, not the agent's owner
    granted = _granted_token(company, task, actor, request.task, client_address(call), repo)
    if granted is not None:
        return granted
    # GitHub: the App when it is set up and installed on the repository, else the Git credentials stored for it.
    if app is not None:
        try:
            return _github_token(app, repo, actor, request.task, client_address(call), company)
        except RepoNotAccessible:
            pass
    if store.find_credential(company, repository) is not None:
        return _stored_token(company, actor, request.task, repository, client_address(call))
    if app is None:
        raise errors.bad_request.ValidationError("GitHub is not configured")
    raise errors.bad_request.ValidationError(f"the GitHub App is not installed on {repo}")


def _refresh_dataset_entry(app, repo: str) -> None:
    """Refreshes the repository's entry in the dataset list when it is missing or stale (bll/dataset_cache). The job's
    token is already issued: a failure here is logged and changes nothing in the answer."""
    try:
        dataset_cache.refresh_repository(app, repo)
    except Exception:
        log.warning("the dataset list entry of %s could not be refreshed", repo, exc_info=True)


def _public_answer(repo: str, actor: str, company, claims: dict, task_id: str, call: APICall):
    """The answer for a public repository: success with no token, since it is cloned without one. None when the
    repository is not public. The App is not asked for a public repository."""
    try:
        public = public_repo(repo)
    except GitHubError:
        raise errors.bad_request.ValidationError("GitHub could not be reached") from None
    if not public:
        return None
    audit(actor, "git_token.public", ip=client_address(call), company=company, device=claims.get("device"), task=task_id, repo=repo)
    return {"token": None, "expires_at": None, "username": None}


def _github_token(app, repo: str, actor: str, task_id: str, ip=None, company=None) -> dict:
    """The GitHub App's read-only token for one repository. RepoNotAccessible is left to the caller."""
    try:
        token = app.installation_token(repo)
    except GitHubError:
        raise errors.bad_request.ValidationError("GitHub could not be reached") from None
    audit(actor, "git_token.issued", ip=ip, company=company, task=task_id, repo=repo)
    return {"token": token.token, "expires_at": token.expires_at.isoformat(), "username": "x-access-token"}


AGENT_NOTICE = (
    "Projects that run jobs on this agent can give them the accesses shared with those projects: GitHub Apps, and "
    "personal tokens unless a project has turned them off. Enrol this agent only on a machine you trust with them."
)


def _granted_token(company: str, task, actor: str, task_id: str, ip, repo: str, dataset: bool = False,
                   credential: str = None):
    """A token for the repository from the accesses shared with the task's project, or None. The job is the project's:
    the access comes from its grants, while its owner is a member and active, and the user who queued the task is a
    member and active. The agent's owner's own accesses are never used here.

    For a dataset, the layout is checked with the same granted access that will read it: a repository that is not a
    valid dataset is refused with the reason, as it is with the company's App."""
    root = user_access.root_project(company, task.project) if task.project else None
    if root is None:
        return None
    host, path = store.host_and_path(canonical_url(repo))
    enqueuer = task.execution.enqueued_by if task.execution else None
    if not enqueuer:
        # a task queued before this was recorded: nothing is granted, rather than guessing who queued it
        return None
    for entry, secret in user_access.granted_for_job(company, enqueuer, root, host, path):
        if credential and entry.id != credential:
            continue  # a dataset names the one credential that reads it: no other access is used
        if entry.kind == "app":
            app = GitHubApp(entry.app_id, secret, entry.api_url or config.get("apiserver.idp.github_api_url"))
            try:
                token = app.installation_token(repo)
            except (RepoNotAccessible, GitHubError):
                continue
            if dataset:
                _check_layout(app, repo, actor, company, task_id, ip)
            audit(actor, "git_token.issued", ip=ip, company=company, task=task_id, repo=repo,
                  via="project_access", access=entry.id, owner=entry.user, project=root)
            return {"token": token.token, "expires_at": token.expires_at.isoformat(), "username": "x-access-token"}
        if user_access.project_agent_tokens_allowed(company, root):
            if dataset:
                _check_layout(TokenReader(secret, config.get("apiserver.idp.github_api_url")), repo, actor, company,
                              task_id, ip)
            audit(actor, "git_token.issued", ip=ip, company=company, task=task_id, repo=repo,
                  via="project_access_token", access=entry.id, owner=entry.user, project=root)
            return {"token": secret, "expires_at": None, "username": "x-access-token",
                    "warning": f"a personal token of {entry.user} is in use: it reaches everything that token reaches"}
    return None


def _check_layout(reader, repo: str, actor: str, company: str, task_id: str, ip) -> None:
    """The dataset layout check with the reader of a granted access. A repository that fails it is refused, and audited."""
    try:
        verify_dataset(reader, repo)
    except errors.bad_request.ValidationError as exc:
        audit(actor, "git_token.refused", ip=ip, company=company, task=task_id, repo=repo, reason=str(exc))
        raise


def _stored_token(company: str, actor: str, task_id: str, repository: str, ip=None) -> dict:
    """The Git credential stored for the repository's host (and path prefix). Refused when none is stored."""
    host, _path = store.host_and_path(repository)
    entry = store.find_credential(company, repository)
    if entry is None:
        audit(actor, "git_token.refused", ip=ip, company=company, task=task_id, host=host, reason="no Git credentials stored")
        raise errors.bad_request.ValidationError(
            f"no Git credentials are stored for {host}: an admin adds them with the git_credentials.set call")
    secret = store.secret_of(entry)
    audit(actor, "git_token.issued", ip=ip, company=company, task=task_id, host=entry.host, path=entry.path)
    return {"token": secret, "expires_at": None, "username": entry.username}
