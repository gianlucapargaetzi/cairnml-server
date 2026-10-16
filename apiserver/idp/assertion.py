"""Short-lived JWT assertions signed with a machine's private key (RFC 7523 style)."""

from __future__ import annotations

import time
import uuid

import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

MAX_LIFETIME = 300


class AssertionInvalid(Exception):
    """The assertion is malformed, forged, expired or otherwise unacceptable."""


def make_assertion(
    key: Ed25519PrivateKey,
    principal_id: str,
    audience: str,
    ttl: int = 120,
    now: float | None = None,
) -> str:
    iat = int(time.time() if now is None else now)
    claims = {
        "iss": principal_id,
        "sub": principal_id,
        "aud": audience.rstrip("/"),
        "iat": iat,
        "exp": iat + ttl,
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(claims, key, algorithm="EdDSA")


def unverified_issuer(token: str) -> str:
    try:
        claims = jwt.decode(token, options={"verify_signature": False})
    except jwt.PyJWTError as exc:
        raise AssertionInvalid("malformed assertion") from exc
    issuer = claims.get("iss")
    if not isinstance(issuer, str) or not issuer:
        raise AssertionInvalid("missing iss")
    return issuer


def verify_assertion(
    token: str,
    public_key: Ed25519PublicKey,
    audience: str,
    now: float | None = None,
    leeway: int = 30,
) -> dict:
    try:
        claims = jwt.decode(
            token,
            public_key,
            algorithms=["EdDSA"],
            audience=audience.rstrip("/"),
            options={
                "verify_exp": False,
                "verify_iat": False,
                "require": ["iss", "aud", "iat", "exp", "jti"],
            },
        )
    except jwt.PyJWTError as exc:
        raise AssertionInvalid("invalid assertion") from exc
    current = time.time() if now is None else now
    if claims["exp"] + leeway < current:
        raise AssertionInvalid("assertion expired")
    if claims["iat"] - leeway > current:
        raise AssertionInvalid("assertion issued in the future")
    if claims["exp"] - claims["iat"] > MAX_LIFETIME:
        raise AssertionInvalid("assertion lifetime too long")
    return claims
