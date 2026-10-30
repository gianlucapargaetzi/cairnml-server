from pathlib import Path
from unittest import TestCase

import yaml

COMPOSE_FILE = Path(__file__).resolve().parents[2] / "deploy" / "compose.yaml"
DATASTORES = ("mongo", "redis", "elasticsearch")


class TestDeployCompose(TestCase):
    @classmethod
    def setUpClass(cls):
        with COMPOSE_FILE.open() as f:
            cls.compose = yaml.safe_load(f)
        cls.services = cls.compose["services"]

    def test_datastores_have_no_host_port_and_only_the_internal_network(self):
        for name in DATASTORES:
            service = self.services[name]
            self.assertNotIn("ports", service, name)
            self.assertEqual(list(service["networks"]), ["datastores"], name)
        self.assertTrue(self.compose["networks"]["datastores"]["internal"])

    def test_apiserver_reaches_the_datastores(self):
        self.assertIn("datastores", self.services["apiserver"]["networks"])
        self.assertIn("datastores", self.services["async-delete"]["networks"])

    def test_webserver_cannot_reach_the_datastores(self):
        self.assertNotIn("datastores", self.services["webserver"]["networks"])

    def test_elasticsearch_remote_reindex_is_not_allowed(self):
        env = self.services["elasticsearch"]["environment"]
        self.assertNotIn("reindex.remote.whitelist", env)
        self.assertNotIn("reindex.remote.whitelist", str(self.compose))

    def test_keycloak_admin_console_is_published_on_loopback_only(self):
        ports = self.services["cairnml-idp"].get("ports", [])
        self.assertEqual(len(ports), 1)
        self.assertTrue(ports[0].startswith("127.0.0.1:"), ports[0])
