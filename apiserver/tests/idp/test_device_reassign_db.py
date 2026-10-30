"""Database-level tests of devices.reassign: an admin moves a device to another enabled admin."""

import uuid
from types import SimpleNamespace

import pytest
from unittest import TestCase

from apiserver import database as db
from apiserver.apierrors import errors
from apiserver.apimodels.devices import ReassignRequest
from apiserver.database.model.audit import AuditEvent
from apiserver.database.model.auth import Role, User
from apiserver.database.model.device import Device
from apiserver.idp.checks import check_device
from apiserver.idp.users import upsert_idp_user
from apiserver.services import devices as svc
from apiserver.service_repo.auth import Identity

pytestmark = pytest.mark.integration  # needs MongoDB


class FakeCall:
    def __init__(self, ident):
        self.identity = ident
        self.result = SimpleNamespace(data=None, data_model=None)


class TestReassignDevice(TestCase):
    @classmethod
    def setUpClass(cls):
        db.db.initialize()

    def setUp(self):
        issuer = f"http://idp.test/reassign-{uuid.uuid4().hex}"
        self.admin_a = upsert_idp_user(issuer, "a", "a", ("admin",), "admin")
        self.admin_b = upsert_idp_user(issuer, "b", "b", ("admin",), "admin")
        self.user = upsert_idp_user(issuer, "c", "c", (), "admin")
        self.addCleanup(lambda: [u.delete() for u in (self.admin_a, self.admin_b, self.user)])
        self.device = Device(id=f"dev_{uuid.uuid4().hex}", user=self.admin_a.id, company=self.admin_a.company,
                             kind="agent", name="gpu-box", public_key="pem").save()
        self.addCleanup(self.device.delete)

    def _call(self, ident, user_id):
        call = FakeCall(ident)
        svc.reassign(call, self.admin_a.company, ReassignRequest(id=self.device.id, user=user_id))
        return call

    def _identity(self, user, role=Role.admin):
        return Identity(user=user.id, company=user.company, role=role)

    def test_an_admin_moves_the_device_and_the_key_stays(self):
        self._call(self._identity(self.admin_a), self.admin_b.id)
        moved = Device.objects(id=self.device.id).get()
        self.assertEqual(moved.user, self.admin_b.id)
        self.assertEqual(moved.public_key, "pem")
        self.assertFalse(moved.revoked)
        check_device(moved, User.objects(id=self.admin_b.id).get())
        event = AuditEvent.objects(action="device.reassigned", detail__device=self.device.id).first()
        self.assertIsNotNone(event)
        self.addCleanup(event.delete)

    def test_the_new_owner_must_be_an_admin(self):
        with self.assertRaises(errors.bad_request.InvalidId):
            self._call(self._identity(self.admin_a), self.user.id)
        self.assertEqual(Device.objects(id=self.device.id).get().user, self.admin_a.id)

    def test_a_non_admin_caller_is_refused(self):
        with self.assertRaises(errors.forbidden.RoleNotAllowed):
            self._call(self._identity(self.user, role=Role.user), self.admin_b.id)
        self.assertEqual(Device.objects(id=self.device.id).get().user, self.admin_a.id)

    def test_a_disabled_new_owner_is_refused(self):
        User.objects(id=self.admin_b.id).update_one(set__idp_disabled=True)
        with self.assertRaises(errors.bad_request.InvalidId):
            self._call(self._identity(self.admin_a), self.admin_b.id)

    def test_the_device_list_names_the_owner_and_flags_a_disabled_owner(self):
        call = FakeCall(self._identity(self.admin_a))
        listed = svc.get_all(call, self.admin_a.company, None)["devices"]
        mine = next(d for d in listed if d["id"] == self.device.id)
        self.assertEqual(mine["user"], self.admin_a.id)
        self.assertFalse(mine["owner_disabled"])
        self.assertEqual(mine["owner_name"], self.admin_a.name)

        User.objects(id=self.admin_a.id).update_one(set__idp_disabled=True)
        listed = svc.get_all(call, self.admin_a.company, None)["devices"]
        mine = next(d for d in listed if d["id"] == self.device.id)
        self.assertTrue(mine["owner_disabled"])
