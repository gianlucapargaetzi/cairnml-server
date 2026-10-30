"""Short-lived access tokens issued by the CairnML server after a device proves its key."""

import time

import jwt
from apiserver.config_repo import config
from apiserver.service_repo.auth import Identity, Token


def _version_claims() -> dict:
    """What the SDK reads from a token to learn the server's API level and feature set (as the old login did)."""
    from apiserver.config.info import get_build_number, get_version
    from apiserver.service_repo import ServiceRepo

    return {
        "api_version": str(ServiceRepo.max_endpoint_version()),
        "server_version": str(get_version()),
        "server_build": str(get_build_number()),
        "feature_set": "basic",
    }


def issue_access_token(user_id: str, company_id: str, device_id: str, kind: str, role: str,
                       user_name: str = "", company_name: str = "") -> str:
    """Token for a device (kind 'client' or 'agent'): carries the person's identity and the device."""
    identity = Identity(user=user_id, company=company_id, role=role,
                        user_name=user_name, company_name=company_name)
    ttl = config.get("apiserver.idp.access_token_ttl_sec")
    return Token.create_encoded_token(identity, expiration_sec=ttl, kind=kind, device=device_id, **_version_claims())


def renewed_web_token(encoded: str, now: float = None):
    """A fresh web session token once the current one has used more than half of its life, else None.

    An active user keeps the session this way. An idle session still expires, and the user is checked
    again on every request, so a disabled user loses access as before.
    """
    try:
        claims = Token.decode(encoded)  # verifies the signature and the expiry
    except jwt.PyJWTError:
        return None
    if claims.get("kind") != "web" or "exp" not in claims or "iat" not in claims:
        return None
    now = time.time() if now is None else now
    life = claims["exp"] - claims["iat"]
    if claims["exp"] - now > life / 2:
        return None
    identity = Identity.from_dict(claims.get("identity") or {})
    return issue_access_token(
        identity.user, identity.company, "", kind="web", role=identity.role,
        user_name=identity.user_name or "", company_name=identity.company_name or "",
    )


def device_of_bearer(authorization):
    """The device id behind an agent's bearer token; None for any other caller."""
    if not authorization or not authorization.startswith("Bearer "):
        return None
    try:
        claims = Token.decode(authorization[len("Bearer "):])
    except jwt.PyJWTError:  # forged, expired or not a token at all
        return None
    return claims.get("device") if claims.get("kind") == "agent" else None


def read_claims(token: str) -> dict:
    """Verified claims of a token issued by this server. Raises if it is forged or expired."""
    return Token.decode(token)


def issue_job_token(owner, task_id: str, device_id: str) -> str:
    """Token for one job: acts as the task's owner, for that task only, and ends with the job's lifetime."""
    from apiserver.service_repo.auth import Identity, Token

    identity = Identity(user=owner.id, company=owner.company, role=owner.role, user_name=owner.name)
    ttl = config.get("apiserver.idp.job_token_ttl_sec")
    return Token.create_encoded_token(
        identity, expiration_sec=ttl, kind="job", task=task_id, device=device_id, **_version_claims()
    )
