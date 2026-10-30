from unittest import TestCase

from apiserver.idp.tokens import issue_access_token, read_claims, renewed_web_token


class TestWebSessionRenewal(TestCase):
    def web_token(self):
        return issue_access_token("u1", "c1", "", kind="web", role="admin", user_name="Ann")

    def test_fresh_web_session_is_not_renewed(self):
        claims = read_claims(self.web_token())
        self.assertIsNone(renewed_web_token(self.web_token(), now=claims["iat"] + 10))

    def test_web_session_past_half_its_life_is_renewed_for_the_same_user(self):
        token = self.web_token()
        claims = read_claims(token)
        now = claims["iat"] + 0.6 * (claims["exp"] - claims["iat"])
        renewed = renewed_web_token(token, now=now)
        self.assertIsNotNone(renewed)
        new_claims = read_claims(renewed)
        self.assertEqual(new_claims["kind"], "web")
        self.assertEqual(new_claims["identity"]["user"], "u1")
        self.assertEqual(new_claims["identity"]["company"], "c1")
        self.assertEqual(new_claims["identity"]["role"], "admin")
        # a full new lifetime, issued now (not from the old token's expiry)
        self.assertGreaterEqual(new_claims["exp"] - new_claims["iat"], 800)
        self.assertGreaterEqual(new_claims["exp"], claims["exp"])

    def test_device_and_job_tokens_are_never_renewed_here(self):
        token = issue_access_token("u1", "c1", "dev_test", kind="client", role="user")
        claims = read_claims(token)
        now = claims["iat"] + 0.9 * (claims["exp"] - claims["iat"])
        self.assertIsNone(renewed_web_token(token, now=now))

    def test_forged_token_is_not_renewed(self):
        token = self.web_token()
        head, body, sig = token.split(".")
        forged = ".".join([head, body, sig[:-2] + ("AA" if not sig.endswith("AA") else "BB")])
        self.assertIsNone(renewed_web_token(forged))
