import time
from unittest import TestCase

from apiserver.idp.tokens import issue_access_token, read_claims


class TestAccessTokens(TestCase):
    def test_token_carries_the_identity_kind_and_device(self):
        token = issue_access_token("u1", "c1", "dev_test", kind="client", role="user")
        claims = read_claims(token)
        self.assertEqual(claims["identity"]["user"], "u1")
        self.assertEqual(claims["identity"]["company"], "c1")
        self.assertEqual(claims["kind"], "client")
        self.assertEqual(claims["device"], "dev_test")

    def test_token_expires_after_the_configured_lifetime(self):
        token = issue_access_token("u1", "c1", "dev_test", kind="client", role="user")
        claims = read_claims(token)
        lifetime = claims["exp"] - int(time.time())
        self.assertTrue(800 < lifetime <= 900)

    def test_tampered_token_is_refused(self):
        token = issue_access_token("u1", "c1", "dev_test", kind="client", role="user")
        head, body, sig = token.split(".")
        with self.assertRaises(Exception):
            read_claims(".".join([head, body, sig[:-2] + ("AA" if not sig.endswith("AA") else "BB")]))
