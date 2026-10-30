from types import SimpleNamespace
from unittest import TestCase

from apiserver.database.model.audit import RETENTION_DAYS, AuditEvent
from apiserver.idp.audit import build_event, client_address


class FakeCall:
    HEADER_REAL_IP = "X-Real-IP"
    HEADER_FORWARDED_FOR = "X-Forwarded-For"

    def __init__(self, headers=None, remote_addr="192.0.2.7"):
        self.headers = headers or {}
        self.remote_addr = remote_addr

    def get_header(self, header, default=None):
        return self.headers.get(header, default)

    @property
    def real_ip(self):
        return self.get_header(self.HEADER_FORWARDED_FOR) or self.get_header(self.HEADER_REAL_IP) or self.remote_addr


class TestClientAddress(TestCase):
    def test_the_proxy_set_address_is_used(self):
        self.assertEqual(client_address(FakeCall({"X-Real-IP": "10.0.0.5"})), "10.0.0.5")

    def test_a_forwarded_for_header_the_client_sent_does_not_replace_it(self):
        call = FakeCall({"X-Real-IP": "10.0.0.5", "X-Forwarded-For": "203.0.113.9"})
        self.assertEqual(client_address(call), "10.0.0.5")

    def test_without_the_proxy_header_the_connection_address_is_used(self):
        self.assertEqual(client_address(FakeCall()), "192.0.2.7")

    def test_the_address_fits_the_stored_field(self):
        self.assertLessEqual(len(client_address(FakeCall({"X-Real-IP": "1" * 200}))), 64)


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

    def test_event_keeps_its_company(self):
        event = build_event("u1", "web.login", ip="10.0.0.5", company="company-a")
        self.assertEqual(event.company, "company-a")

    def test_events_expire_after_the_retention_period(self):
        ttl = [spec for spec in AuditEvent._meta["index_specs"] if "expireAfterSeconds" in spec]
        self.assertEqual(ttl, [{"fields": [("ts", 1)], "expireAfterSeconds": RETENTION_DAYS * 24 * 60 * 60}])
        self.assertEqual(RETENTION_DAYS, 365)
