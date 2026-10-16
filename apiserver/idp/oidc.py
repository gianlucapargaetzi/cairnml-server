"""Verification of OIDC access tokens (Keycloak) against the realm's JWKS.

Ported from the mlops server (security/oidc.py) without behaviour changes.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import jwt


class OidcError(Exception):
    pass


@dataclass(frozen=True)
class IdpIdentity:
    subject: str
    username: str
    roles: tuple[str, ...]


class OidcVerifier:
    def __init__(
        self,
        issuer: str,
        client_id: str | Sequence[str],
        jwks_url: str | None = None,
        jwks: dict | None = None,
        key_lifespan_sec: float = 300,
    ) -> None:
        if not jwks_url and not jwks:
            raise ValueError("need jwks_url or jwks")
        self._issuer = issuer
        self._client_ids = [client_id] if isinstance(client_id, str) else list(client_id)
        self._static = jwt.PyJWKSet.from_dict(jwks) if jwks else None
        # The key set expires after key_lifespan_sec, so a key the IdP removes stops verifying tokens.
        # Individual keys are not cached on their own: that cache would never expire.
        self._client = jwt.PyJWKClient(jwks_url, cache_keys=False, lifespan=key_lifespan_sec) if jwks_url else None

    def _key(self, token: str):
        if self._static is not None:
            kid = jwt.get_unverified_header(token).get("kid")
            for jwk in self._static.keys:
                if jwk.key_id == kid:
                    return jwk.key
            raise OidcError("unknown signing key")
        return self._client.get_signing_key_from_jwt(token).key

    def _decode(self, token: str, clients: Sequence[str] | None) -> dict:
        try:
            claims = jwt.decode(
                token,
                self._key(token),
                algorithms=["RS256"],
                issuer=self._issuer,
                options={"verify_aud": False, "require": ["exp", "iss", "sub"]},
            )
        except OidcError:
            raise
        except Exception as exc:  # PyJWTError, network errors fetching the JWKS
            raise OidcError("invalid token") from exc
        accepted = list(clients) if clients is not None else self._client_ids
        if claims.get("azp") not in accepted:
            raise OidcError("token was not issued for an accepted client")
        return claims

    def verify(self, token: str, clients: Sequence[str] | None = None) -> IdpIdentity:
        claims = self._decode(token, clients)
        return IdpIdentity(
            subject=claims["sub"],
            username=claims.get("preferred_username", claims["sub"]),
            roles=tuple(claims.get("realm_access", {}).get("roles", [])),
        )

    def verify_nonce(self, id_token: str, nonce: str, clients: Sequence[str] | None = None) -> None:
        claims = self._decode(id_token, clients)
        if not nonce or claims.get("nonce") != nonce:
            raise OidcError("nonce mismatch")
