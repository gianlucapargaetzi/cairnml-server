"""A bearer token for the test suite, from the same path a laptop uses: IdP sign-in, device key,
signed assertion, access token. Cached for the life of the test process."""

import os
import uuid

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

from apiserver.idp.assertion import make_assertion
from apiserver.tests.idp.test_devices_api import PUBLIC_URL, call, idp_token

_cache = {}


def suite_bearer() -> str:
    if "token" not in _cache:
        key = Ed25519PrivateKey.generate()
        pem = key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
        sub = os.environ.get("TEST_IDP_SUBJECT", f"suite-{uuid.uuid4().hex}")
        device = call("devices.enroll", {"kind": "client", "name": "test-suite", "public_key": pem},
                      token=idp_token(sub=sub, username="suite")).json()["data"]["id"]
        assertion = make_assertion(key, device, PUBLIC_URL)
        _cache["token"] = call("devices.exchange", {"assertion": assertion}).json()["data"]["access_token"]
    return _cache["token"]
