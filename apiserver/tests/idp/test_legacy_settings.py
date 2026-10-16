from unittest import TestCase

from apiserver.idp.legacy import check_no_legacy_settings, legacy_settings_in_use


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
        self.assertEqual(len(self.check({"CLEARML_ALLOW_CONFIG_SECRET": "1"})), 1)

    def test_both_are_allowed_in_a_test_run(self):
        env = {"CLEARML_TEST_MODE": "1", "CLEARML_ALLOW_CONFIG_SECRET": "1"}
        self.assertEqual(self.check(env, **{"apiserver.idp.jwks_file": "/tmp/jwks.json"}), [])
