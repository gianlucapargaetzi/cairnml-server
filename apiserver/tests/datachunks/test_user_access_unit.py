"""A user's own repository accesses: the rules that need no database or GitHub.

Run inside the server image: python -m unittest apiserver.tests.datachunks.test_user_access_unit
"""

from types import SimpleNamespace
from unittest import TestCase

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
                                secret="ENCRYPTED-VALUE")
        view = bll._view(entry)
        self.assertNotIn("secret", view)
        self.assertNotIn("ENCRYPTED-VALUE", str(view))


class TestWhoMayUse(TestCase):
    def test_no_user_has_no_accesses(self):
        self.assertEqual(bll.usable_for("company-1", None, "github.com", "acme/x"), [])
