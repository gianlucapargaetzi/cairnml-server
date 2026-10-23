"""A stand-in for the identity provider: signs RS256 tokens and publishes its keys as a JWKS."""

import json
import time

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa


class FakeIdp:
    ISSUER = "http://idp.test/realms/mlops"

    def __init__(self) -> None:
        self._key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = "test-key"

    def jwks(self) -> dict:
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self._key.public_key()))
        jwk.update(kid=self.kid, alg="RS256", use="sig")
        return {"keys": [jwk]}

    def token(self, sub="sub-dev", username="dev", roles=(), azp="cairnml-sdk", iss=None,
              run_in=300, key=None, extra=None, kid=None) -> str:
        now = int(time.time())
        claims = {
            "iss": iss or self.ISSUER,
            "sub": sub,
            "preferred_username": username,
            "azp": azp,
            "iat": now,
            "exp": now + run_in,
            "realm_access": {"roles": list(roles)},
            **(extra or {}),
        }
        return jwt.encode(claims, key or self._key, algorithm="RS256", headers={"kid": kid or self.kid})

    def other_key(self):
        return rsa.generate_private_key(public_exponent=65537, key_size=2048)
