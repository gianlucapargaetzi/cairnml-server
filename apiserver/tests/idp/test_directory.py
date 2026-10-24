from unittest import TestCase

import requests

from apiserver.idp.directory import DirectoryError, KeycloakDirectory

ISSUER = "http://idp.test/realms/cairnml"


class Resp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class FakeHttp:
    """Replies by path. Each entry maps a URL suffix to a list of responses, used in order."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def _reply(self, url):
        for suffix, replies in self.routes.items():
            if url.endswith(suffix):
                return replies.pop(0) if len(replies) > 1 else replies[0]
        raise AssertionError(f"unexpected call {url}")

    def post(self, url, data=None, **_):
        self.calls.append(("POST", url))
        return self._reply(url)

    def get(self, url, headers=None, **_):
        self.calls.append(("GET", url))
        return self._reply(url)


def token_reply(token="t1"):
    return Resp(200, {"access_token": token, "expires_in": 300})


class TestKeycloakDirectory(TestCase):
    def directory(self, routes):
        return KeycloakDirectory(ISSUER, "cairnml-server", "secret", http=FakeHttp(routes), clock=lambda: 0.0)

    def test_enabled_user_with_roles(self):
        d = self.directory({
            "/protocol/openid-connect/token": [token_reply()],
            "/users/u1": [Resp(200, {"enabled": True})],
            "/users/u1/role-mappings/realm/composite": [Resp(200, [{"name": "admin"}, {"name": "offline_access"}])],
        })
        user = d.get_user("u1")
        self.assertTrue(user.enabled)
        self.assertEqual(user.roles, ("admin", "offline_access"))

    def test_unknown_user_is_none(self):
        d = self.directory({
            "/protocol/openid-connect/token": [token_reply()],
            "/users/gone": [Resp(404)],
        })
        self.assertIsNone(d.get_user("gone"))

    def test_disabled_user(self):
        d = self.directory({
            "/protocol/openid-connect/token": [token_reply()],
            "/users/u2": [Resp(200, {"enabled": False})],
            "/users/u2/role-mappings/realm/composite": [Resp(200, [])],
        })
        self.assertFalse(d.get_user("u2").enabled)

    def test_reply_without_a_boolean_is_an_error_not_a_disablement(self):
        d = self.directory({
            "/protocol/openid-connect/token": [token_reply()],
            "/users/u3": [Resp(200, {"enabled": "yes"})],
        })
        with self.assertRaises(DirectoryError):
            d.get_user("u3")

    def test_unreachable_idp_is_a_directory_error(self):
        class Down:
            def post(self, *a, **k):
                raise requests.ConnectionError("down")
        d = KeycloakDirectory(ISSUER, "c", "s", http=Down(), clock=lambda: 0.0)
        with self.assertRaises(DirectoryError):
            d.get_user("u1")

    def test_dot_segments_are_refused(self):
        d = self.directory({"/protocol/openid-connect/token": [token_reply()]})
        with self.assertRaises(DirectoryError):
            d.get_user("..")
