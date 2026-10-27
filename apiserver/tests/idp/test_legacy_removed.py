"""The old login is gone: no basic credentials, no tokens without a kind, no credential endpoints."""

import pytest
import base64
import json
import os
from unittest import TestCase

import requests
from requests.auth import HTTPBasicAuth

from apiserver.apierrors import errors
from apiserver.service_repo.auth import Token
from apiserver.service_repo.auth.auth import get_auth_func

pytestmark = pytest.mark.integration  # needs MongoDB, Elasticsearch or a running API server

API = f"{os.environ.get('SM_API_URL', 'http://localhost/cairnml/api')}/v2.8"


class TestLegacyRemoved(TestCase):
    def test_basic_credentials_are_not_an_auth_type(self):
        with self.assertRaises(errors.unauthorized.BadAuthType):
            get_auth_func("Basic")

    def test_a_token_without_a_kind_is_refused(self):
        from apiserver.service_repo.auth import Identity
        identity = Identity(user="u", company="c", role="user")
        old = Token.create_encoded_token(identity, expiration_sec=300)
        response = requests.post(f"{API}/devices.get_all", json={}, headers={"Authorization": f"Bearer {old}"}, timeout=20)
        self.assertEqual(response.status_code, 401)

    def test_basic_credentials_over_http_are_refused(self):
        response = requests.post(f"{API}/devices.get_all", json={}, auth=HTTPBasicAuth("anything", "secret"), timeout=20)
        self.assertEqual(response.status_code, 401)

    def test_the_credential_endpoints_are_gone(self):
        for endpoint in ("auth.login", "auth.create_credentials", "auth.get_credentials"):
            response = requests.post(f"{API}/{endpoint}", json={}, timeout=20)
            self.assertEqual(response.status_code, 404, endpoint)
