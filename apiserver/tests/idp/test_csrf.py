"""Cookie sessions can change things only from the web app's origin. Run inside the server container."""

import os
import uuid
from unittest import TestCase

import requests

from apiserver.config_repo import config

from apiserver import database as db
from apiserver.database.model.auth import User
from apiserver.idp.tokens import issue_access_token
from apiserver.idp.users import upsert_idp_user

API = f"{os.environ.get('SM_API_URL', 'http://localhost:8008')}/v2.8"
WEB = config.get("apiserver.idp.web_url")
COOKIE = "cairnml_token_basic"


class TestOriginCheck(TestCase):
    @classmethod
    def setUpClass(cls):
        db.db.initialize()

    def setUp(self):
        issuer = f"http://idp.test/csrf-{uuid.uuid4().hex}"
        self.user = upsert_idp_user(issuer, "csrf-user", "csrf", (), "admin")
        self.addCleanup(lambda: User.objects(id=self.user.id).delete())
        self.session = issue_access_token(self.user.id, self.user.company, "", kind="web", role=self.user.role)

    def post(self, headers=None, cookie=True, bearer=False):
        h = dict(headers or {})
        if bearer:
            h["Authorization"] = f"Bearer {self.session}"
        cookies = {COOKIE: self.session} if cookie else None
        return requests.post(f"{API}/devices.get_all", json={}, headers=h, cookies=cookies, timeout=20)

    def test_cookie_change_from_another_site_is_refused(self):
        self.assertEqual(self.post({"Origin": "http://evil.example"}).status_code, 403)

    def test_cookie_change_without_origin_is_refused(self):
        self.assertEqual(self.post().status_code, 403)

    def test_cookie_change_from_the_web_app_is_accepted(self):
        self.assertEqual(self.post({"Origin": WEB}).status_code, 200)

    def test_bearer_request_is_not_checked_for_origin(self):
        self.assertEqual(self.post(cookie=False, bearer=True).status_code, 200)
