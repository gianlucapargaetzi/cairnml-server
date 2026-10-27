from unittest import TestCase

from apiserver.idp.oidc import OidcError, OidcVerifier
from apiserver.tests.idp.idp_stub import FakeIdp

CLIENT = "cairnml-sdk"


class TestOidcVerifier(TestCase):
    def setUp(self):
        self.idp = FakeIdp()
        self.verifier = OidcVerifier(FakeIdp.ISSUER, CLIENT, jwks=self.idp.jwks())

    def test_valid_token_yields_identity_and_roles(self):
        token = self.idp.token(sub="u1", username="alice", roles=("admin",))
        identity = self.verifier.verify(token)
        self.assertEqual(identity.subject, "u1")
        self.assertEqual(identity.username, "alice")
        self.assertEqual(identity.roles, ("admin",))

    def test_another_realm_is_refused(self):
        with self.assertRaises(OidcError):
            self.verifier.verify(self.idp.token(iss="http://evil.test/realms/other"))

    def test_the_same_realm_at_another_address_is_accepted(self):
        # A token names the identity provider's address as the client reached it (through the proxy), which differs from
        # the server's own. The signature is still checked against the realm's keys.
        token = self.idp.token(sub="u1", iss="http://192.168.1.20/realms/cairnml")
        self.assertEqual(self.verifier.verify(token).subject, "u1")

    def test_token_for_another_client_is_refused(self):
        with self.assertRaises(OidcError):
            self.verifier.verify(self.idp.token(azp="some-other-app"))

    def test_expired_token_is_refused(self):
        with self.assertRaises(OidcError):
            self.verifier.verify(self.idp.token(run_in=-600))

    def test_token_signed_by_another_key_is_refused(self):
        with self.assertRaises(OidcError):
            self.verifier.verify(self.idp.token(key=self.idp.other_key()))

    def test_unknown_key_id_is_refused(self):
        with self.assertRaises(OidcError):
            self.verifier.verify(self.idp.token(kid="unknown"))

    def test_nonce_must_match(self):
        token = self.idp.token(extra={"nonce": "n-1"})
        self.verifier.verify_nonce(token, "n-1")
        with self.assertRaises(OidcError):
            self.verifier.verify_nonce(token, "n-2")
