"""Devices (laptops and agents) enrol with their IdP sign-in and their public key, then exchange
a signed assertion for short-lived access tokens. Ported from the mlops server's enrol and exchange."""

import uuid
from datetime import datetime

from apiserver.apierrors import errors
from apiserver.apimodels.devices import EnrollRequest, ExchangeRequest, GitTokenRequest, JobTokenRequest, RevokeRequest
from apiserver.config_repo import config
from apiserver.database.model.auth import Role, User
from apiserver.database.model.device import Device, DeviceKind
from apiserver.idp.assertion import AssertionInvalid, unverified_issuer, verify_assertion
from apiserver.idp.audit import record as audit
from apiserver.idp.checks import AccessDenied, check_device
from apiserver.idp.keys import load_public_key
from apiserver.idp.oidc import OidcError
from apiserver.idp.replay import claim_jti
from apiserver.idp.tokens import issue_access_token
from apiserver.idp.users import role_for, upsert_idp_user
from apiserver.idp.verifier import get_verifier
from apiserver.service_repo import APICall, endpoint

ADMIN_ROLES = (Role.admin, Role.root, Role.system)


def _bearer(call: APICall) -> str:
    header = call.authorization or ""
    if not header.lower().startswith("bearer "):
        raise errors.unauthorized.NoCredentials()
    return header[7:].strip()


def _device_view(device: Device) -> dict:
    return {
        "id": device.id,
        "user": device.user,
        "kind": device.kind,
        "name": device.name,
        "created": device.created.isoformat() if device.created else None,
        "last_used": device.last_used.isoformat() if device.last_used else None,
        "revoked": device.revoked,
    }


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
        audit(user.id, "device.enrol_refused", kind=request.kind)
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
    audit(user.id, "device.enrolled", device=device.id, kind=device.kind)
    return {"id": device.id, "kind": device.kind}


@endpoint("devices.idp_settings", min_version="2.8")
def idp_settings(call: APICall, _company, _):
    """What a device needs to start the identity-provider sign-in. No secret is returned."""
    return {
        "issuer": config.get("apiserver.idp.issuer"),
        "sdk_client_id": config.get("apiserver.idp.sdk_client_id"),
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
        audit(device_id, "token.refused", device=device_id, reason=str(exc))
        raise errors.unauthorized.InvalidToken(str(exc)) from None

    audience = config.get("apiserver.idp.public_url")
    try:
        claims = verify_assertion(request.assertion, load_public_key(device.public_key), audience=audience)
    except (AssertionInvalid, ValueError):
        audit(device_id, "token.refused", device=device_id, reason="bad assertion")
        raise errors.unauthorized.InvalidToken("invalid assertion") from None

    now = datetime.utcnow().timestamp()
    if not claim_jti(claims["jti"], ttl=max(1, int(claims["exp"] - now)) + 60):
        audit(device_id, "token.refused", device=device_id, reason="assertion reused")
        raise errors.unauthorized.InvalidToken("assertion already used")

    token = issue_access_token(owner.id, owner.company, device.id, kind=device.kind,
                               role=owner.role, user_name=owner.name)
    device.last_used = datetime.utcnow()
    device.save()
    audit(owner.id, "token.issued", device=device.id, kind=device.kind)
    return {"access_token": token, "expires_in": config.get("apiserver.idp.access_token_ttl_sec")}


@endpoint("devices.get_all", min_version="2.8")
def get_all(call: APICall, _company, _):
    identity = call.identity
    query = {} if identity.role in ADMIN_ROLES else {"user": identity.user}
    return {"devices": [_device_view(d) for d in Device.objects(**query)]}


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
    audit(identity.user, "device.revoked", device=device.id)
    return _device_view(device)


def _caller_device_id(call: APICall) -> str:
    """The device behind the caller's token: the agent's own device id, from its verified token."""
    from apiserver.service_repo.auth import Token

    try:
        claims = Token.decode(_bearer(call))
    except Exception:
        raise errors.unauthorized.InvalidToken("invalid token") from None
    if claims.get("kind") != DeviceKind.agent:
        raise errors.unauthorized.InvalidToken("only an agent may get a job token")
    return claims.get("device")


@endpoint("devices.job_token", min_version="2.8", request_data_model=JobTokenRequest)
def job_token(call: APICall, _company, request: JobTokenRequest):
    from apiserver.database.model.task.task import Task
    from apiserver.idp.checks import AccessDenied, check_job_issue
    from apiserver.idp.tokens import issue_job_token

    device = Device.objects(id=_caller_device_id(call)).first()
    task = Task.objects(id=request.task).only("status", "last_device", "user").first()
    owner = User.objects(id=task.user).first() if task else None
    try:
        check_job_issue(task.status if task else None, task.last_device if task else None, device)
        if owner is None or owner.idp_disabled:
            raise AccessDenied("task owner is disabled or unknown")
    except AccessDenied as exc:
        audit(device.id if device else "unknown", "job_token.refused", device=device.id if device else None, task=request.task, reason=str(exc))
        raise errors.unauthorized.InvalidToken(str(exc)) from None
    token = issue_job_token(owner, task.id, device.id)
    audit(device.id, "job_token.issued", task=task.id)
    return {"access_token": token, "expires_in": config.get("apiserver.idp.job_token_ttl_sec")}


@endpoint("devices.git_token", min_version="2.8", request_data_model=GitTokenRequest)
def git_token(call: APICall, _company, request: GitTokenRequest):
    """A read-only token for the task's repository, for the job or the agent running it."""
    from apiserver.database.model.task.task import Task
    from apiserver.idp.checks import AccessDenied, check_job_issue
    from apiserver.idp.github import GitHubError, RepoNotAccessible
    from apiserver.idp.github_check import build_from_config, repo_of
    from apiserver.service_repo.auth import Token

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
        audit(claims.get("device") or "unknown", "git_token.refused", device=claims.get("device"), task=request.task, reason=str(exc))
        raise errors.unauthorized.InvalidToken(str(exc)) from None

    app = build_from_config()
    if app is None:
        raise errors.bad_request.ValidationError("GitHub is not configured")
    repo = repo_of(task.script.repository)
    if repo is None:
        raise errors.bad_request.ValidationError("the task's repository is not on GitHub")
    try:
        token = app.installation_token(repo)
    except RepoNotAccessible:
        raise errors.bad_request.ValidationError(f"the GitHub App is not installed on {repo}") from None
    except GitHubError:
        raise errors.bad_request.ValidationError("GitHub could not be reached") from None
    audit(claims.get("device") or claims.get("sub") or "unknown", "git_token.issued", task=request.task, repo=repo)
    return {"token": token.token, "expires_at": token.expires_at.isoformat(), "username": "x-access-token"}
