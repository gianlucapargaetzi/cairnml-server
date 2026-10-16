"""The IdP verifier used by the endpoints, built once from configuration."""

import json
from functools import lru_cache

from apiserver.config_repo import config
from apiserver.idp.oidc import OidcVerifier


@lru_cache(maxsize=1)
def get_verifier() -> OidcVerifier:
    issuer = config.get("apiserver.idp.issuer")
    client = config.get("apiserver.idp.sdk_client_id")
    jwks_file = config.get("apiserver.idp.jwks_file", "")
    if jwks_file:
        # Test-only: a fixed key set read from a file instead of the IdP's JWKS endpoint.
        with open(jwks_file, encoding="utf-8") as handle:
            return OidcVerifier(issuer, client, jwks=json.load(handle))
    return OidcVerifier(issuer, client, jwks_url=f"{issuer}/protocol/openid-connect/certs")
