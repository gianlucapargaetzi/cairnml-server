import os
from unittest import TestCase

import requests

from apiserver.config_repo import config

API = f"{os.environ.get('SM_API_URL', 'http://localhost:8008')}/v2.8"


class TestIdpSettings(TestCase):
    def test_the_sign_in_settings_need_no_credentials(self):
        response = requests.post(f"{API}/devices.idp_settings", json={}, timeout=20)
        self.assertEqual(response.status_code, 200)

    def test_the_settings_name_the_issuer_and_the_sdk_client(self):
        body = requests.post(f"{API}/devices.idp_settings", json={}, timeout=20).json()["data"]
        self.assertEqual(body["issuer"], config.get("apiserver.idp.issuer"))
        self.assertEqual(body["sdk_client_id"], config.get("apiserver.idp.sdk_client_id"))
        self.assertNotIn("secret", str(body).lower())
