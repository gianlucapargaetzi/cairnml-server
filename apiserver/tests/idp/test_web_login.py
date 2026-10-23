import base64
import hashlib
from unittest import TestCase
from urllib.parse import parse_qs, urlparse

from apiserver.idp.web_login import authorize_url, new_login, pkce_challenge


class TestWebLoginPieces(TestCase):
    def test_pkce_challenge_is_the_s256_of_the_verifier(self):
        verifier = "a" * 64
        expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        self.assertEqual(pkce_challenge(verifier), expected)

    def test_each_login_gets_fresh_state_nonce_and_verifier(self):
        a, b = new_login(), new_login()
        self.assertNotEqual(a["state"], b["state"])
        self.assertNotEqual(a["nonce"], b["nonce"])
        self.assertNotEqual(a["verifier"], b["verifier"])
        self.assertGreaterEqual(len(a["verifier"]), 43)

    def test_authorize_url_carries_the_pkce_state_and_nonce(self):
        url = authorize_url("http://idp/realms/mlops", "cairnml-web", "http://srv/callback",
                            state="s1", nonce="n1", challenge="c1")
        query = parse_qs(urlparse(url).query)
        self.assertTrue(url.startswith("http://idp/realms/mlops/protocol/openid-connect/auth?"))
        self.assertEqual(query["client_id"], ["cairnml-web"])
        self.assertEqual(query["response_type"], ["code"])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(query["code_challenge"], ["c1"])
        self.assertEqual(query["state"], ["s1"])
        self.assertEqual(query["nonce"], ["n1"])
