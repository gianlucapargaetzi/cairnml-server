from unittest import TestCase

from apiserver.config_repo import config


class TestIdpConfig(TestCase):
    def test_idp_defaults_load(self):
        self.assertEqual(config.get("apiserver.idp.admin_role"), "cairnml-admin")
        self.assertTrue(config.get("apiserver.idp.sync_enabled"))
        self.assertEqual(config.get("apiserver.idp.sync_interval_sec"), 60)
        self.assertEqual(config.get("apiserver.idp.access_token_ttl_sec"), 900)
        self.assertEqual(config.get("apiserver.idp.job_token_ttl_sec"), 3600)
        self.assertEqual(config.get("apiserver.idp.web_client_id"), "cairnml-web")
        self.assertEqual(config.get("apiserver.idp.sdk_client_id"), "cairnml-sdk")
        self.assertEqual(config.get("apiserver.idp.directory_client_id"), "cairnml-server")
