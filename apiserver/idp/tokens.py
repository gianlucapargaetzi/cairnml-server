"""Short-lived access tokens issued by the ClearML server after a device proves its key."""

from apiserver.config_repo import config
from apiserver.service_repo.auth import Identity, Token


def issue_access_token(user_id: str, company_id: str, device_id: str, kind: str, role: str,
                       user_name: str = "", company_name: str = "") -> str:
    """Token for a device (kind 'client' or 'agent'): carries the person's identity and the device."""
    identity = Identity(user=user_id, company=company_id, role=role,
                        user_name=user_name, company_name=company_name)
    ttl = config.get("apiserver.idp.access_token_ttl_sec")
    return Token.create_encoded_token(identity, expiration_sec=ttl, kind=kind, device=device_id)


def read_claims(token: str) -> dict:
    """Verified claims of a token issued by this server. Raises if it is forged or expired."""
    return Token.decode(token)
