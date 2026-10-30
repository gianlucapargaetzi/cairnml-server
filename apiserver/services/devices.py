"""Devices (laptops and agents) enrol with their IdP sign-in and their public key, then exchange
a signed assertion for short-lived access tokens. Ported from the mlops server's enrol and exchange."""

import jwt
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
from apiserver.bll.dataset_registry import allowed_repositories
from apiserver.config_repo import config
from apiserver.database.model.auth import Role, User
from apiserver.database.model.device import Device, DeviceKind
from apiserver.database.model.task.task import Task
from apiserver.idp.assertion import AssertionInvalid, unverified_issuer, verify_assertion
from apiserver.idp.audit import client_address, record as audit
from apiserver.idp.checks import AccessDenied, check_device, check_job_issue
from apiserver.idp.github import GitHubError, RepoNotAccessible
from apiserver.idp.github_check import build_from_config, repo_of
from apiserver.idp.keys import load_public_key
from apiserver.idp import git_credentials as store
from apiserver.idp import public_origin
from apiserver.idp.oidc import OidcError
from apiserver.idp.replay import claim_jti
from apiserver.idp.tokens import issue_access_token, issue_job_token
from apiserver.idp.users import role_for, upsert_idp_user
from apiserver.idp.verifier import get_verifier
from apiserver.service_repo import APICall, endpoint
from apiserver.service_repo.auth import Token

ADMIN_ROLES = (Role.admin, Role.root, Role.system)


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
    if request.kind == DeviceKind.agent and role_for(identity.roles, admin_role) != Role.admin:
        audit(user.id, "device.enrol_refused", ip=client_address(call), company=user.company, kind=request.kind)
        raise errors.forbidden.RoleNotAllowed("only an admin may enrol an agent")

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
    return {"id": device.id, "kind": device.kind}


@endpoint("devices.idp_settings", min_version="2.8")
def idp_settings(call: APICall, _company, _):
    """What a device needs to start the identity-provider sign-in. No secret is returned."""
    return {
        "issuer": public_origin.idp_issuer(),
        "sdk_client_id": config.get("apiserver.idp.sdk_client_id"),
        # The address devices must use: a device's assertion is addressed to it, so it must match exactly.
        "public_url": public_origin.api_url().rstrip("/"),
        "web_url": public_origin.web_url(),
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

    token = issue_access_token(owner.id, owner.company, device.id, kind=device.kind,
                               role=owner.role, user_name=owner.name)
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
        # A dataset repository is registered on GitHub, so it is read with the GitHub App only.
        if app is None:
            raise errors.bad_request.ValidationError("GitHub is not configured")
        repo = repo_of(request.repository)
        if repo is None:
            raise errors.bad_request.ValidationError("the repository is not on GitHub")
        try:
            registered = allowed_repositories(app, config.get("apiserver.dataset_registry.repository", ""))
        except GitHubError:
            raise errors.bad_request.ValidationError("the dataset registry could not be read") from None
        if repo.lower() not in registered:
            audit(actor, "git_token.refused", ip=client_address(call), company=company, device=claims.get("device"), task=request.task, reason=f"{repo} is not in the dataset registry")
            raise errors.bad_request.ValidationError(f"{repo} is not a registered dataset repository")
        try:
            return _github_token(app, repo, actor, request.task, client_address(call), company)
        except RepoNotAccessible:
            raise errors.bad_request.ValidationError(f"the GitHub App is not installed on {repo}") from None

    if task is None:
        raise errors.bad_request.InvalidId("task not found")
    repository = task.script.repository
    if not repository:
        raise errors.bad_request.ValidationError("the task has no repository")
    repo = repo_of(repository)
    if repo is None:
        return _stored_token(company, actor, request.task, repository, client_address(call))
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


def _github_token(app, repo: str, actor: str, task_id: str, ip=None, company=None) -> dict:
    """The GitHub App's read-only token for one repository. RepoNotAccessible is left to the caller."""
    try:
        token = app.installation_token(repo)
    except GitHubError:
        raise errors.bad_request.ValidationError("GitHub could not be reached") from None
    audit(actor, "git_token.issued", ip=ip, company=company, task=task_id, repo=repo)
    return {"token": token.token, "expires_at": token.expires_at.isoformat(), "username": "x-access-token"}


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
