"""The server's own secrets never appear in an API answer, successful or refused. Run inside the server container."""

import pytest
import os
from unittest import TestCase

import requests

from apiserver.idp.secrets import SecretMissing, read_secret
from apiserver.tests.idp.bearer import suite_bearer
from apiserver.tests.idp.test_devices_api import API

pytestmark = pytest.mark.integration  # needs MongoDB, Elasticsearch or a running API server

ENDPOINTS = [
    ("login.supported_modes", {}),
    ("devices.idp_settings", {}),
    ("devices.get_all", {}),
    ("users.get_current_user", {}),
    ("devices.exchange", {"assertion": "not-a-real-assertion"}),
    ("devices.enroll", {"kind": "client", "public_key": "x"}),
    ("tasks.get_all", {"only_fields": ["id"]}),
]


class TestNoSecretInResponses(TestCase):
    def setUp(self):
        self.secrets = []
        for name in ("token_secret", "session_secret"):
            try:
                self.secrets.append(read_secret(name))
            except SecretMissing:
                pass
        if not self.secrets:
            self.skipTest("no server secrets are mounted in this environment")

    def test_no_answer_holds_a_server_secret(self):
        for endpoint, body in ENDPOINTS:
            for headers in ({}, {"Authorization": f"Bearer {suite_bearer()}"}):
                response = requests.post(f"{API}/{endpoint}", json=body, headers=headers, timeout=20)
                for secret in self.secrets:
                    self.assertNotIn(secret, response.text, f"{endpoint} answered with a server secret")
