"""End-to-end tests of the device endpoints against the running server.

They need the test identity-provider key set (make_test_idp.py), read from TEST_IDP_DIR, and a
server started with apiserver.idp.jwks_file pointing at its jwks.json. Run inside the server
container with the server URL in SM_API_URL.
"""

import json
import os
import time
from unittest import TestCase

import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from apiserver.idp.assertion import make_assertion
from apiserver.idp.keys import private_key_to_pem

IDP_ISSUER = "http://mlops-idp:8180/realms/mlops"
SDK_CLIENT = "cairnml-sdk"
SERVER = os.environ.get("SM_API_URL", "http://localhost:8008")
PUBLIC_URL = os.environ.get("TEST_PUBLIC_URL", "http://localhost:8008")
API = f"{SERVER}/v2.8"


def _idp_key():
    directory = os.environ.get("TEST_IDP_DIR", "/opt/cairnml/test-idp")
    with open(os.path.join(directory, "idp_private.pem"), "rb") as handle:
        return serialization.load_pem_private_key(handle.read(), password=None)


def idp_token(sub="sub-alice", username="alice", roles=(), key=None, azp=SDK_CLIENT):
    now = int(time.time())
    claims = {"iss": IDP_ISSUER, "sub": sub, "preferred_username": username, "azp": azp,
              "iat": now, "exp": now + 300, "realm_access": {"roles": list(roles)}}
    return jwt.encode(claims, key or _idp_key(), algorithm="RS256", headers={"kid": "test-key"})


def call(endpoint, body=None, token=None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return requests.post(f"{API}/{endpoint}", json=body or {}, headers=headers, timeout=20)


class TestDeviceEndpoints(TestCase):
    def setUp(self):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        self.device_key = Ed25519PrivateKey.generate()
        self.public_pem = self.device_key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()

    def enrol(self, kind="client", roles=(), sub="sub-alice"):
        return call("devices.enroll", {"kind": kind, "name": "test", "public_key": self.public_pem},
                    token=idp_token(sub=sub, roles=roles))

    def test_enrol_a_laptop_with_a_valid_sign_in(self):
        response = self.enrol()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["data"]["id"].startswith("dev_"))

    def test_enrol_refuses_a_forged_sign_in(self):
        other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        response = call("devices.enroll", {"kind": "client", "public_key": self.public_pem},
                        token=idp_token(key=other))
        self.assertEqual(response.status_code, 401)

    def test_enrol_refuses_a_bad_public_key(self):
        response = call("devices.enroll", {"kind": "client", "public_key": "not a key"},
                        token=idp_token())
        self.assertEqual(response.status_code, 400)

    def test_agent_enrolment_is_refused_for_a_non_admin(self):
        response = self.enrol(kind="agent", roles=())
        self.assertEqual(response.status_code, 403)

    def test_agent_enrolment_is_allowed_for_an_admin(self):
        response = self.enrol(kind="agent", roles=("admin",), sub="sub-admin")
        self.assertEqual(response.status_code, 200, response.text)

    def test_exchange_gives_a_token_once_per_assertion(self):
        device_id = self.enrol().json()["data"]["id"]
        assertion = make_assertion(self.device_key, device_id, PUBLIC_URL)
        first = call("devices.exchange", {"assertion": assertion})
        self.assertEqual(first.status_code, 200, first.text)
        self.assertTrue(first.json()["data"]["access_token"])
        again = call("devices.exchange", {"assertion": assertion})
        self.assertEqual(again.status_code, 401)

    def test_exchange_refuses_an_assertion_signed_by_another_key(self):
        device_id = self.enrol().json()["data"]["id"]
        other = Ed25519PrivateKey.generate()
        assertion = make_assertion(other, device_id, PUBLIC_URL)
        self.assertEqual(call("devices.exchange", {"assertion": assertion}).status_code, 401)

    def test_revoked_device_is_refused_on_its_next_request(self):
        device_id = self.enrol().json()["data"]["id"]
        assertion = make_assertion(self.device_key, device_id, PUBLIC_URL)
        token = call("devices.exchange", {"assertion": assertion}).json()["data"]["access_token"]
        self.assertEqual(call("devices.get_all", token=token).status_code, 200)
        revoke = call("devices.revoke", {"id": device_id}, token=token)
        self.assertEqual(revoke.status_code, 200, revoke.text)
        self.assertEqual(call("devices.get_all", token=token).status_code, 401)
        new_assertion = make_assertion(self.device_key, device_id, PUBLIC_URL)
        self.assertEqual(call("devices.exchange", {"assertion": new_assertion}).status_code, 401)
