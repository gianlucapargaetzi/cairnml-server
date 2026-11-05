"""A user's own repository accesses: the rules that need no database or GitHub.

Run inside the server image: python -m unittest apiserver.tests.datachunks.test_user_access_unit
"""

from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from apiserver.apierrors import errors
from apiserver.bll import user_access as bll

PEM = "-----BEGIN RSA PRIVATE KEY-----\nMIIE\n-----END RSA PRIVATE KEY-----"


class TestValidation(TestCase):
    def test_a_label_is_needed(self):
        with self.assertRaises(errors.bad_request.ValidationError):
            bll._label("   ")

    def test_a_token_is_needed(self):
        with self.assertRaises(errors.bad_request.ValidationError):
            bll.add_token("c", "u", "mine", "github.com", "", "   ")

    def test_an_app_id_is_a_number(self):
        with self.assertRaises(errors.bad_request.ValidationError):
            bll.add_app("c", "u", "app", "CairnML", "", PEM)

    def test_the_key_must_be_a_private_key_file(self):
        with self.assertRaises(errors.bad_request.ValidationError):
            bll.add_app("c", "u", "app", "12345", "", "not a key")

    def test_the_api_address_must_be_http_or_https(self):
        with self.assertRaises(errors.bad_request.ValidationError):
            bll.add_app("c", "u", "app", "12345", "ftp://example.org", PEM)


class TestNoSecretInAView(TestCase):
    def test_the_view_has_no_secret_field(self):
        entry = SimpleNamespace(id="a1", kind="token", label="mine", host="github.com", path="acme", app_id=None,
                                api_url=None, created=None, last_checked=None, last_state=None, last_message=None,
                                secret="ENCRYPTED-VALUE", scope="user")
        with patch.object(bll, "_granted_to", return_value=[]):
            view = bll._view(entry)
        self.assertNotIn("secret", view)
        self.assertNotIn("ENCRYPTED-VALUE", str(view))


class TestWhoMayUse(TestCase):
    def test_no_user_has_no_accesses(self):
        self.assertEqual(bll.usable_for("company-1", None, "github.com", "acme/x"), [])


class TestGrantRule(TestCase):
    def test_a_grant_applies_only_while_both_sides_are_members_and_active(self):
        self.assertTrue(bll.grant_applies("p1", "p1", True, True, True, True))

    def test_the_owner_leaving_the_project_stops_the_grant(self):
        self.assertFalse(bll.grant_applies("p1", "p1", False, True, True, True))

    def test_the_user_leaving_the_project_stops_the_grant(self):
        self.assertFalse(bll.grant_applies("p1", "p1", True, False, True, True))

    def test_a_deactivated_owner_or_user_stops_the_grant(self):
        self.assertFalse(bll.grant_applies("p1", "p1", True, True, False, True))
        self.assertFalse(bll.grant_applies("p1", "p1", True, True, True, False))

    def test_a_grant_to_another_project_does_not_apply(self):
        self.assertFalse(bll.grant_applies("p1", "p2", True, True, True, True))


class TestKeyFormat(TestCase):
    def test_a_malformed_key_is_a_validation_error_not_a_crash(self):
        with self.assertRaises(errors.bad_request.ValidationError):
            bll.add_app("c", "u", "app", "12345", "", "-----BEGIN PRIVATE KEY-----\nnot-base64!\n-----END PRIVATE KEY-----")
