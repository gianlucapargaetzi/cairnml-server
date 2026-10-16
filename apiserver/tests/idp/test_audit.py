from unittest import TestCase

from apiserver.database.model.audit import AuditEvent
from apiserver.idp.audit import build_event


class TestAudit(TestCase):
    def test_event_keeps_actor_action_ip_and_detail(self):
        event = build_event("dev_1", "device.enrolled", ip="10.0.0.5", kind="client")
        self.assertEqual(event.actor, "dev_1")
        self.assertEqual(event.action, "device.enrolled")
        self.assertEqual(event.ip, "10.0.0.5")
        self.assertEqual(event.detail, {"kind": "client"})

    def test_event_without_ip_or_detail(self):
        event = build_event("u1", "web.login")
        self.assertIsNone(event.ip)
        self.assertEqual(event.detail, {})

    def test_action_is_indexed(self):
        self.assertIn("action", AuditEvent._meta["indexes"])
