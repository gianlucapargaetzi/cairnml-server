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
