import time
from unittest import TestCase

import jwt

from apiserver.idp.assertion import AssertionInvalid, MAX_LIFETIME, make_assertion, unverified_issuer, verify_assertion
from apiserver.idp.keys import generate_private_key

AUD = "https://cairnml.example.org"


class TestAssertion(TestCase):
    def setUp(self):
        self.key = generate_private_key()
        self.pub = self.key.public_key()

    def test_valid_assertion_verifies_and_names_its_issuer(self):
        token = make_assertion(self.key, "dev_1", AUD)
        self.assertEqual(unverified_issuer(token), "dev_1")
        claims = verify_assertion(token, self.pub, AUD)
        self.assertEqual(claims["iss"], "dev_1")

    def test_wrong_audience_is_refused(self):
        token = make_assertion(self.key, "dev_1", "https://other.example.org")
        with self.assertRaises(AssertionInvalid):
            verify_assertion(token, self.pub, AUD)

    def test_expired_assertion_is_refused(self):
        token = make_assertion(self.key, "dev_1", AUD, ttl=60, now=time.time() - 600)
        with self.assertRaises(AssertionInvalid):
            verify_assertion(token, self.pub, AUD)

    def test_tampered_assertion_is_refused(self):
        token = make_assertion(self.key, "dev_1", AUD)
        head, body, sig = token.split(".")
        forged = jwt.encode({"iss": "dev_2", "sub": "dev_2", "aud": AUD, "iat": int(time.time()), "exp": int(time.time()) + 60, "jti": "x"}, self.key, algorithm="EdDSA")
        with self.assertRaises(AssertionInvalid):
            verify_assertion(forged.rsplit(".", 1)[0] + "." + sig, self.pub, AUD)

    def test_assertion_signed_by_another_key_is_refused(self):
        other = generate_private_key()
        token = make_assertion(other, "dev_1", AUD)
        with self.assertRaises(AssertionInvalid):
            verify_assertion(token, self.pub, AUD)

    def test_missing_jti_is_refused(self):
        now = int(time.time())
        token = jwt.encode({"iss": "dev_1", "sub": "dev_1", "aud": AUD, "iat": now, "exp": now + 60}, self.key, algorithm="EdDSA")
        with self.assertRaises(AssertionInvalid):
            verify_assertion(token, self.pub, AUD)

    def test_assertion_from_the_future_is_refused(self):
        token = make_assertion(self.key, "dev_1", AUD, now=time.time() + 3600)
        with self.assertRaises(AssertionInvalid):
            verify_assertion(token, self.pub, AUD)

    def test_too_long_lifetime_is_refused(self):
        token = make_assertion(self.key, "dev_1", AUD, ttl=MAX_LIFETIME + 60)
        with self.assertRaises(AssertionInvalid):
            verify_assertion(token, self.pub, AUD)

    def test_garbage_has_no_issuer(self):
        with self.assertRaises(AssertionInvalid):
            unverified_issuer("not-a-token")
