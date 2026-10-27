import pytest
import os
from unittest import TestCase

import requests

from apiserver.config_repo import config

pytestmark = pytest.mark.integration  # needs MongoDB, Elasticsearch or a running API server

# The API is reached through the proxy on port 80, under /cairnml/api. SM_API_URL is the address the test client types.
API_ADDRESS = os.environ.get("SM_API_URL", "http://localhost/cairnml/api").rstrip("/")
API = f"{API_ADDRESS}/v2.8"
ROOT = API_ADDRESS[: -len("/api")]


class TestIdpSettings(TestCase):
    def test_the_sign_in_settings_need_no_credentials(self):
        response = requests.post(f"{API}/devices.idp_settings", json={}, timeout=20)
        self.assertEqual(response.status_code, 200)

    def test_the_settings_name_the_issuer_and_the_sdk_client(self):
        body = requests.post(f"{API}/devices.idp_settings", json={}, timeout=20).json()["data"]
        self.assertEqual(body["issuer"], f"{ROOT}/idp/realms/cairnml")
        self.assertEqual(body["sdk_client_id"], config.get("apiserver.idp.sdk_client_id"))
        self.assertNotIn("secret", str(body).lower())

    def test_the_settings_give_the_public_and_web_addresses_the_client_used(self):
        body = requests.post(f"{API}/devices.idp_settings", json={}, timeout=20).json()["data"]
        self.assertEqual(body["public_url"], API_ADDRESS)
        self.assertEqual(body["web_url"], f"{ROOT}/")
