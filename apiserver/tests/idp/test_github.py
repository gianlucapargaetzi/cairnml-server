import time
from types import SimpleNamespace
from unittest import TestCase

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from apiserver.idp.github import GitHubApp, GitHubError, RepoNotAccessible

API = "https://api.github.com"


def _pem_pair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    return pem, key.public_key()


class Resp:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class FakeHttp:
    """Replies by (method, URL suffix). The requests made are kept for checks."""

    def __init__(self, replies):
        self.replies = replies
        self.requests = []

    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        for (m, suffix), reply in self.replies.items():
            if m == method and url.endswith(suffix):
                return reply
        raise AssertionError(f"unexpected {method} {url}")


def app(http, pem=None):
    pem = pem or _pem_pair()[0]
    return GitHubApp("123", pem, API, http=http, clock=lambda: 1000.0)


INSTALL = ("GET", "/repos/acme/proj/installation")
TOKEN = ("POST", "/app/installations/77/access_tokens")
COMMIT = ("GET", "/repos/acme/proj/commits/abc123")


class TestGitHubApp(TestCase):
    def test_app_jwt_is_signed_with_the_app_key_and_names_the_app(self):
        pem, public = _pem_pair()
        http = FakeHttp({INSTALL: Resp(404)})
        with self.assertRaises(RepoNotAccessible):
            app(http, pem).commit_exists("acme/proj", "abc123")
        auth = http.requests[0][2]["headers"]["Authorization"]
        claims = jwt.decode(auth.split(" ", 1)[1], public, algorithms=["RS256"], options={"verify_aud": False, "verify_exp": False})
        self.assertEqual(claims["iss"], "123")

    def test_an_existing_commit_is_found(self):
        http = FakeHttp({INSTALL: Resp(200, {"id": 77}), TOKEN: Resp(201, {"token": "ghs_x", "expires_at": "2099-01-01T00:00:00Z"}),
                         COMMIT: Resp(200, {"sha": "abc123"})})
        self.assertTrue(app(http).commit_exists("acme/proj", "abc123"))

    def test_a_missing_commit_is_not_found(self):
        http = FakeHttp({INSTALL: Resp(200, {"id": 77}), TOKEN: Resp(201, {"token": "ghs_x", "expires_at": "2099-01-01T00:00:00Z"}),
                         COMMIT: Resp(404)})
        self.assertFalse(app(http).commit_exists("acme/proj", "abc123"))

    def test_a_failing_github_call_is_an_error_not_a_missing_commit(self):
        http = FakeHttp({INSTALL: Resp(200, {"id": 77}), TOKEN: Resp(201, {"token": "ghs_x", "expires_at": "2099-01-01T00:00:00Z"}),
                         COMMIT: Resp(500)})
        with self.assertRaises(GitHubError):
            app(http).commit_exists("acme/proj", "abc123")

    def test_a_repository_without_the_app_is_refused(self):
        http = FakeHttp({INSTALL: Resp(404)})
        with self.assertRaises(RepoNotAccessible):
            app(http).installation_token("acme/proj")

    def test_the_token_is_scoped_to_one_repository_and_read_only(self):
        http = FakeHttp({INSTALL: Resp(200, {"id": 77}), TOKEN: Resp(201, {"token": "ghs_x", "expires_at": "2099-01-01T00:00:00Z"})})
        app(http).installation_token("acme/proj")
        body = [k for m, u, k in http.requests if u.endswith("/access_tokens")][0]["json"]
        self.assertEqual(body["repositories"], ["proj"])
        self.assertEqual(body["permissions"], {"contents": "read"})

    def test_a_malformed_token_reply_is_an_error(self):
        http = FakeHttp({INSTALL: Resp(200, {"id": 77}), TOKEN: Resp(201, {"token": "ghs_x"})})
        with self.assertRaises(GitHubError):
            app(http).installation_token("acme/proj")

    def test_requests_carry_a_timeout(self):
        http = FakeHttp({INSTALL: Resp(404)})
        with self.assertRaises(RepoNotAccessible):
            app(http).installation_token("acme/proj")
        self.assertIn("timeout", http.requests[0][2])
