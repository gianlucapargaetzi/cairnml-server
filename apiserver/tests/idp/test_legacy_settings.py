import pytest
from unittest import TestCase

from apiserver.idp.legacy import check_no_legacy_settings, legacy_settings_in_use

pytestmark = pytest.mark.integration  # needs MongoDB, Elasticsearch or a running API server


def settings(**values):
    return lambda key, default=None: values.get(key, default)


class TestLegacySettings(TestCase):
    def test_clean_settings_start(self):
        self.assertEqual(legacy_settings_in_use(settings()), [])
        check_no_legacy_settings(settings())

    def test_access_keys_refuse_to_start(self):
        get = settings(**{"secure.credentials": {"tests": {"role": "user"}}})
        self.assertEqual(len(legacy_settings_in_use(get)), 1)
        with self.assertRaises(RuntimeError):
            check_no_legacy_settings(get)

    def test_fixed_users_refuse_to_start(self):
        get = settings(**{"apiserver.auth.fixed_users.enabled": True})
        with self.assertRaises(RuntimeError) as ctx:
            check_no_legacy_settings(get)
        self.assertIn("fixed users", str(ctx.exception))

    def test_disabled_fixed_users_start(self):
        get = settings(**{"apiserver.auth.fixed_users.enabled": False})
        check_no_legacy_settings(get)


class TestTestOnlySettings(TestCase):
    def check(self, env, **values):
        from apiserver.idp.legacy import test_only_settings_in_use

        return test_only_settings_in_use(settings(**values), env)

    def test_no_test_settings_in_production(self):
        self.assertEqual(self.check({}), [])

    def test_a_fixed_key_set_needs_a_test_run(self):
        problems = self.check({}, **{"apiserver.idp.jwks_file": "/tmp/jwks.json"})
        self.assertEqual(len(problems), 1)

    def test_the_config_secret_switch_needs_a_test_run(self):
        self.assertEqual(len(self.check({"CAIRNML_ALLOW_CONFIG_SECRET": "1"})), 1)

    def test_both_are_allowed_in_a_test_run(self):
        env = {"CAIRNML_TEST_MODE": "1", "CAIRNML_ALLOW_CONFIG_SECRET": "1"}
        self.assertEqual(self.check(env, **{"apiserver.idp.jwks_file": "/tmp/jwks.json"}), [])


class TestCookieSettings(TestCase):
    def check(self, url, secure):
        from apiserver.idp.legacy import cookie_settings_problems

        return cookie_settings_problems(settings(**{"apiserver.idp.public_url": url,
                                                     "apiserver.auth.cookies.secure": secure}))

    def test_https_with_a_secure_cookie_starts(self):
        self.assertEqual(self.check("https://cairnml.example", True), [])

    def test_https_with_an_insecure_cookie_is_refused(self):
        self.assertEqual(len(self.check("https://cairnml.example", False)), 1)

    def test_plain_http_may_use_an_insecure_cookie(self):
        self.assertEqual(self.check("http://cairnml:8008", False), [])
