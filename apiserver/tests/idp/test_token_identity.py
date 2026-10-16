"""A token's identity can be read back. Runs anywhere the server's modules import."""

from unittest import TestCase

from apiserver.service_repo.auth import Identity, Token


class TestTokenIdentity(TestCase):
    def test_the_identity_in_a_token_is_read_back(self):
        identity = Identity(user="user-1", company="company-1", role="user", user_name="alice", company_name="acme")
        encoded = Token.create_encoded_token(identity, expiration_sec=300)
        parsed = Token.decode_identity(encoded)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.user, "user-1")
        self.assertEqual(parsed.company, "company-1")
