import re
from pathlib import Path
from unittest import TestCase

import yaml

from apiserver.config_repo import config

COMPOSE_FILE = Path(__file__).resolve().parents[3] / "deploy" / "compose.yaml"


def _compose_value(value):
    """The default of a compose interpolation ${NAME:-default}, or the value itself."""
    match = re.fullmatch(r"\$\{[A-Z_]+:-(.*)\}", str(value))
    return match.group(1) if match else str(value)


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

    def test_defaults_match_the_compose_stack(self):
        # deploy/compose.yaml sets these for the apiserver: its values are the defaults of apiserver.conf, so a
        # deployment that starts the image without the shipped compose behaves the same.
        with COMPOSE_FILE.open() as f:
            env = yaml.safe_load(f)["services"]["apiserver"]["environment"]
        self.assertEqual(config.get("apiserver.idp.admin_role"), env["CAIRNML__apiserver__idp__admin_role"])
        self.assertEqual(config.get("apiserver.idp.sync_enabled"),
                         env["CAIRNML__apiserver__idp__sync_enabled"] == "true")
        self.assertEqual(config.get("apiserver.auth.cookies.secure"),
                         _compose_value(env["CAIRNML__apiserver__auth__cookies__secure"]) == "true")
        self.assertEqual(config.get("apiserver.auth.custom_cookies.idp_session.settings.secure"),
                         _compose_value(env["CAIRNML__apiserver__auth__custom_cookies__idp_session__settings__secure"])
                         == "true")
