"""An agent device for the scheduler tests: enrolled by an admin, then it signs assertions to get tokens,
the same way clearml-agent does on a real machine."""

import os
import uuid

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from apiserver.idp.assertion import make_assertion
from apiserver.tests.idp.test_devices_api import PUBLIC_URL, call, idp_token


class AgentDevice:
    def __init__(self, name=None):
        self.key = Ed25519PrivateKey.generate()
        pem = self.key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
        sub = f"agent-{uuid.uuid4().hex}"
        response = call(
            "devices.enroll",
            {"kind": "agent", "name": name or sub, "public_key": pem},
            token=idp_token(sub=sub, username="agent-admin", roles=("admin", "mlops-admin")),
        )
        response.raise_for_status()
        self.device = response.json()["data"]["id"]
        self.token = self._exchange()

    def _exchange(self) -> str:
        assertion = make_assertion(self.key, self.device, PUBLIC_URL)
        res = call("devices.exchange", {"assertion": assertion}).json()
        return res["data"]["access_token"]

    def headers(self):
        return {"Authorization": f"Bearer {self.token}"}
