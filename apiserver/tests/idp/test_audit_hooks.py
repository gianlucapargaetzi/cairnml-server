"""Every security event writes an audit record. Run inside the server container against the live server."""

import uuid
from unittest import TestCase

import requests
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from apiserver import database as db
from apiserver.database.model.audit import AuditEvent
from apiserver.database.model.task.task import Task
from apiserver.idp.assertion import make_assertion
from apiserver.tests.idp.test_devices_api import API, PUBLIC_URL, call, idp_token
from apiserver.tests.idp.test_job_tokens_api import _agent, _new_task, _public_pem


def events(action, **detail_match):
    query = {"action": action}
    query.update({f"detail__{k}": v for k, v in detail_match.items()})
    return AuditEvent.objects(**query).count()


class TestAuditHooks(TestCase):
    @classmethod
    def setUpClass(cls):
        db.db.initialize()

    def test_enrolment_and_exchange_and_revoke_are_recorded(self):
        key = Ed25519PrivateKey.generate()
        device = call("devices.enroll", {"kind": "client", "public_key": _public_pem(key)},
                      token=idp_token(sub=f"audit-{uuid.uuid4().hex}")).json()["data"]["id"]
        self.assertGreaterEqual(events("device.enrolled", device=device), 1)
        token = call("devices.exchange", {"assertion": make_assertion(key, device, PUBLIC_URL)}).json()["data"]["access_token"]
        self.assertGreaterEqual(events("token.issued", device=device), 1)
        call("devices.revoke", {"id": device}, token=token)
        self.assertGreaterEqual(events("device.revoked", device=device), 1)

    def test_a_refused_exchange_is_recorded(self):
        key = Ed25519PrivateKey.generate()
        device = call("devices.enroll", {"kind": "client", "public_key": _public_pem(key)},
                      token=idp_token(sub=f"audit-{uuid.uuid4().hex}")).json()["data"]["id"]
        other = Ed25519PrivateKey.generate()
        call("devices.exchange", {"assertion": make_assertion(other, device, PUBLIC_URL)})
        self.assertGreaterEqual(events("token.refused", device=device), 1)

    def test_a_refused_agent_enrolment_is_recorded(self):
        key = Ed25519PrivateKey.generate()
        sub = f"audit-{uuid.uuid4().hex}"
        response = call("devices.enroll", {"kind": "agent", "public_key": _public_pem(key)}, token=idp_token(sub=sub))
        self.assertEqual(response.status_code, 403)
        self.assertGreaterEqual(events("device.enrol_refused"), 1)

    def test_a_refused_job_token_is_recorded(self):
        device, agent_token = _agent(f"audit-{uuid.uuid4().hex}")
        task = _new_task()
        self.addCleanup(lambda: Task.objects(id=task).delete())
        response = call("devices.job_token", {"task": task}, token=agent_token)
        self.assertEqual(response.status_code, 401)
        self.assertGreaterEqual(events("job_token.refused", device=device), 1)

    def test_a_cross_site_cookie_change_is_recorded(self):
        before = AuditEvent.objects(action="csrf.refused").count()
        requests.post(f"{API}/devices.get_all", json={}, headers={"Origin": "http://evil.example"},
                      cookies={"clearml_token_basic": "x"}, timeout=20)
        self.assertGreater(AuditEvent.objects(action="csrf.refused").count(), before)
