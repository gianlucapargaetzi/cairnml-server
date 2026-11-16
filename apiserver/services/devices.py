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
from apiserver.bll import git_access
from apiserver.bll.queue.agent_queue import allocate_agent_queue
from apiserver.config_repo import config
from apiserver.database.model.auth import Role, AuthUser
from apiserver.database.model.device import Device, DeviceKind
from apiserver.database.model.queue import Queue
from apiserver.database.model.task.task import Task
from apiserver.idp.assertion import AssertionInvalid, unverified_issuer, verify_assertion
from apiserver.idp.audit import client_address, record as audit
from apiserver.idp.checks import AccessDenied, check_device, check_job_issue
from apiserver.bll import user_access
from apiserver.idp.keys import load_public_key
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


def _device_view(device: Device, owner: AuthUser = None) -> dict:
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
    owners = {
        u.id: u for u in AuthUser.objects(id__in=list({d.user for d in devices})).only("id", "name", "idp_disabled")
    }
    return [_device_view(d, owners.get(d.user)) for d in devices]


@endpoint("devices.enroll", request_data_model=EnrollRequest)
def enroll(call: APICall, _company, request: EnrollRequest):
    if request.kind not in (DeviceKind.client, DeviceKind.agent):
        raise errors.bad_request.InvalidId("kind must be client or agent")
    if request.kind == DeviceKind.agent and not (request.name or "").strip():
        raise errors.bad_request.ValidationError("an agent is enrolled with the name of its machine")
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

    now = datetime.utcnow()
    queue_id = ""
    if request.kind == DeviceKind.agent:
        # the agent serves its own queue, named after its machine, and nothing else (bll/queue/queue_access)
        queue = allocate_agent_queue(user.company, user.id, request.name.strip(), now)
        if Queue.objects(id=queue.id).first() is None:
            queue.save()
        queue_id = queue.id
    device = Device(
        id=f"dev_{uuid.uuid4().hex}",
        user=user.id,
        company=user.company,
        kind=request.kind,
        name=request.name or "",
        public_key=request.public_key,
        created=now,
        revoked=False,
        queue=queue_id,
    )
    device.save()
    audit(user.id, "device.enrolled", ip=client_address(call), company=user.company, device=device.id, kind=device.kind)
    result = {"id": device.id, "kind": device.kind}
    if device.kind == DeviceKind.agent:
        result["queue"] = queue_id
        result["notice"] = AGENT_NOTICE
    return result


@endpoint("devices.idp_settings")
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


@endpoint("devices.exchange", request_data_model=ExchangeRequest)
def exchange(call: APICall, _company, request: ExchangeRequest):
    try:
        device_id = unverified_issuer(request.assertion)
    except AssertionInvalid:
        raise errors.unauthorized.InvalidToken("malformed assertion") from None
    device = Device.objects(id=device_id).first()
    owner = AuthUser.objects(id=device.user).first() if device else None
    try:
        check_device(device, owner)
    except AccessDenied as exc:
        audit(
            device_id,
            "token.refused",
            ip=client_address(call),
            company=device.company if device else None,
            device=device_id,
            reason=str(exc),
        )
        raise errors.unauthorized.InvalidToken(str(exc)) from None

    audience = public_origin.api_url().rstrip("/")
    try:
        claims = verify_assertion(request.assertion, load_public_key(device.public_key), audience=audience)
    except (AssertionInvalid, ValueError):
        audit(
            device_id,
            "token.refused",
            ip=client_address(call),
            company=device.company,
            device=device_id,
            reason="bad assertion",
        )
        raise errors.unauthorized.InvalidToken("invalid assertion") from None

    now = datetime.utcnow().timestamp()
    if not claim_jti(claims["jti"], ttl=max(1, int(claims["exp"] - now)) + 60):
        audit(
            device_id,
            "token.refused",
            ip=client_address(call),
            company=device.company,
            device=device_id,
            reason="assertion reused",
        )
        raise errors.unauthorized.InvalidToken("assertion already used")

    # An agent acts with the user role whatever its owner's role: it never holds admin rights.
    role = Role.user if device.kind == DeviceKind.agent else owner.role
    token = issue_access_token(owner.id, owner.company, device.id, kind=device.kind, role=role, user_name=owner.name)
    device.last_used = datetime.utcnow()
    device.save()
    audit(owner.id, "token.issued", ip=client_address(call), company=owner.company, device=device.id, kind=device.kind)
    return {"access_token": token, "expires_in": config.get("apiserver.idp.access_token_ttl_sec")}


@endpoint("devices.get_all")
def get_all(call: APICall, _company, _):
    identity = call.identity
    query = {} if identity.role in ADMIN_ROLES else {"user": identity.user}
    return {"devices": _device_views(Device.objects(**query))}


@endpoint("devices.revoke", request_data_model=RevokeRequest)
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
    return _device_view(device, AuthUser.objects(id=device.user).first())


@endpoint("devices.reassign", request_data_model=ReassignRequest)
def reassign(call: APICall, _company, request: ReassignRequest):
    identity = call.identity
    if identity.role not in ADMIN_ROLES:
        raise errors.forbidden.RoleNotAllowed("only an admin may reassign a device")
    device = Device.objects(id=request.id).first()
    if device is None:
        raise errors.bad_request.InvalidId("no such device")
    if device.revoked:
        raise errors.bad_request.InvalidId("device is revoked")
    new_owner = AuthUser.objects(id=request.user, company=device.company).first()
    if new_owner is None:
        raise errors.bad_request.InvalidId("no such user")
    if new_owner.idp_disabled or new_owner.role not in ADMIN_ROLES:
        raise errors.bad_request.InvalidId("the new owner must be an enabled admin")
    previous_owner = device.user
    device.user = new_owner.id
    device.save()
    audit(
        identity.user,
        "device.reassigned",
        ip=client_address(call),
        company=device.company,
        device=device.id,
        previous_owner=previous_owner,
        new_owner=new_owner.id,
    )
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


@endpoint("devices.job_token", request_data_model=JobTokenRequest)
def job_token(call: APICall, _company, request: JobTokenRequest):
    device = Device.objects(id=_caller_device_id(call)).first()
    task = Task.objects(id=request.task).only("status", "last_device", "user").first()
    owner = AuthUser.objects(id=task.user).first() if task else None
    try:
        check_job_issue(task.status if task else None, task.last_device if task else None, device)
        if owner is None or owner.idp_disabled:
            raise AccessDenied("task owner is disabled or unknown")
    except AccessDenied as exc:
        audit(
            device.id if device else "unknown",
            "job_token.refused",
            ip=client_address(call),
            company=device.company if device else None,
            device=device.id if device else None,
            task=request.task,
            reason=str(exc),
        )
        raise errors.unauthorized.InvalidToken(str(exc)) from None
    token = issue_job_token(owner, task.id, device.id)
    audit(device.id, "job_token.issued", ip=client_address(call), company=device.company, task=task.id)
    return {"access_token": token, "expires_in": config.get("apiserver.idp.job_token_ttl_sec")}


@endpoint("devices.git_token", request_data_model=GitTokenRequest)
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
            check_job_issue(
                task.status if task else None,
                task.last_device if task else None,
                Device.objects(id=claims.get("device")).first(),
            )
        else:
            raise AccessDenied("only a job or its agent may get a repository token")
    except AccessDenied as exc:
        audit(
            claims.get("device") or "unknown",
            "git_token.refused",
            ip=client_address(call),
            company=company,
            device=claims.get("device"),
            task=request.task,
            reason=str(exc),
        )
        raise errors.unauthorized.InvalidToken(str(exc)) from None

    actor = claims.get("device") or claims.get("sub") or "unknown"
    if task is None:
        raise errors.bad_request.InvalidId("task not found")
    return git_access.issue_for_task(
        company, actor, claims.get("device"), task, request.task, request.repository, client_address(call)
    )


AGENT_NOTICE = (
    "Projects that run jobs on this agent can give them the GitHub Apps shared with those projects. Enrol this agent only "
    "on a machine you trust with them."
)
