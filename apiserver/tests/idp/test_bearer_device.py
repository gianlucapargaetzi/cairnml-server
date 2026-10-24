"""A bearer that is not a valid token of this server is refused cleanly, whatever is wrong with it."""

from unittest import TestCase

import jwt

from apiserver.idp.tokens import device_of_bearer


class TestDeviceOfBearer(TestCase):
    def test_no_header_or_another_scheme_is_no_device(self):
        self.assertIsNone(device_of_bearer(None))
        self.assertIsNone(device_of_bearer(""))
        self.assertIsNone(device_of_bearer("Basic abc"))

    def test_text_that_is_not_a_token_is_no_device(self):
        self.assertIsNone(device_of_bearer("Bearer not-a-token"))
        self.assertIsNone(device_of_bearer("Bearer a.b.c"))

    def test_a_token_signed_with_another_secret_is_no_device(self):
        forged = jwt.encode({"kind": "agent", "device": "d1"}, "some-other-secret-that-is-long-enough-32b", algorithm="HS256")
        self.assertIsNone(device_of_bearer(f"Bearer {forged}"))

    def test_a_token_with_an_unsupported_algorithm_is_no_device(self):
        unsigned = jwt.encode({"kind": "agent", "device": "d1"}, None, algorithm="none")
        self.assertIsNone(device_of_bearer(f"Bearer {unsigned}"))
