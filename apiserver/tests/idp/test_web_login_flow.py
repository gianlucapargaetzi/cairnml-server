"""The web sign-in flow against the real verifier and database, run inside the server container."""

import json
import os
import time
import uuid
from unittest import TestCase

import jwt
from cryptography.hazmat.primitives import serialization

from apiserver import database as db
from apiserver.database.model.auth import User
from apiserver.idp.verifier import get_verifier
from apiserver.idp.web_login import LoginRefused, finish_login, start_login

ISSUER = "http://mlops-idp:8180/realms/mlops"
WEB = "cairnml-web"
REDIRECT = "http://localhost:8008/api/login.idp_callback"


class Store:
    def __init__(self):
        self.data = {}

    def set(self, key, value, ex=None):
        self.data[key] = value

    def getdel(self, key):
        return self.data.pop(key, None)


class Reply:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body


class FakeHttp:
    def __init__(self, reply):
        self.reply = reply

    def post(self, url, data=None, timeout=None):
        return self.reply


def _sign(claims):
    with open(os.path.join(os.environ.get("TEST_IDP_DIR", "/opt/cairnml/test-idp"), "idp_private.pem"), "rb") as h:
        key = serialization.load_pem_private_key(h.read(), password=None)
    now = int(time.time())
    base = {"iss": ISSUER, "iat": now, "exp": now + 300, "azp": WEB, "realm_access": {"roles": []}}
    base.update(claims)
    return jwt.encode(base, key, algorithm="RS256", headers={"kid": "test-key"})


class TestWebLoginFlow(TestCase):
    @classmethod
    def setUpClass(cls):
        db.db.initialize()

    def setUp(self):
        self.sub = f"web-{uuid.uuid4().hex}"
        self.store = Store()
        self.addCleanup(lambda: User.objects(idp_subject=f"{ISSUER}|{self.sub}").delete())

    def login(self, nonce):
        return _sign({"sub": self.sub, "preferred_username": "webuser", "nonce": nonce, "typ": "ID"})

    def start(self):
        url = start_login(self.store, ISSUER, WEB, REDIRECT)
        state = url.split("state=")[1].split("&")[0]
        nonce = json.loads(self.store.data[f"idp:login:{state}"])["nonce"]
        return state, nonce

    def finish(self, state, token, status=200):
        body = {"id_token": token, "access_token": token}
        return finish_login(self.store, FakeHttp(Reply(status, body)), get_verifier(), ISSUER, WEB,
                            REDIRECT, state, "code-1", "admin")

    def test_sign_in_creates_the_user(self):
        state, nonce = self.start()
        user, identity = self.finish(state, self.login(nonce))
        self.assertEqual(identity.subject, self.sub)
        self.assertTrue(User.objects(id=user.id).first().idp_subject.endswith(self.sub))

    def test_a_state_can_be_used_only_once(self):
        state, nonce = self.start()
        self.finish(state, self.login(nonce))
        with self.assertRaises(LoginRefused):
            self.finish(state, self.login(nonce))

    def test_unknown_state_is_refused(self):
        with self.assertRaises(LoginRefused):
            self.finish("never-issued", self.login("n"))

    def test_wrong_nonce_is_refused(self):
        state, _ = self.start()
        with self.assertRaises(LoginRefused):
            self.finish(state, self.login("some-other-nonce"))

    def test_refused_token_exchange_is_refused(self):
        state, nonce = self.start()
        with self.assertRaises(LoginRefused):
            self.finish(state, self.login(nonce), status=400)

    def test_disabled_user_cannot_sign_in(self):
        state, nonce = self.start()
        user, _ = self.finish(state, self.login(nonce))
        User.objects(id=user.id).update_one(set__idp_disabled=True)
        state2, nonce2 = self.start()
        with self.assertRaises(LoginRefused):
            self.finish(state2, self.login(nonce2))
