"""Database-level test of the directory sync, run inside the server container."""

import uuid
from unittest import TestCase

from apiserver import database as db
from apiserver.database.model.auth import User
from apiserver.idp.checks import AccessDenied, check_device
from apiserver.idp.directory import DirectoryError, DirectoryUser
from apiserver.idp.user_sync import sync_once
from apiserver.idp.users import upsert_idp_user

ADMIN = "admin"


class StubDirectory:
    def __init__(self, answers):
        self.answers = answers

    def get_user(self, subject):
        # Users this test does not own are unknown to the stub: a failed lookup changes nothing.
        if subject not in self.answers:
            return DirectoryError("not in the stub")
        return self.answers[subject]


class TestUserSyncAgainstTheDatabase(TestCase):
    @classmethod
    def setUpClass(cls):
        db.db.initialize()

    def setUp(self):
        self.issuer = f"http://idp.test/db-{uuid.uuid4().hex}"
        self.leaver = upsert_idp_user(self.issuer, "leaver", "leaver", (), ADMIN)
        self.stayer = upsert_idp_user(self.issuer, "stayer", "stayer", (), ADMIN)
        self.addCleanup(lambda: [u.delete() for u in (self.leaver, self.stayer)])

    def test_disabled_in_the_idp_is_disabled_here_and_its_devices_are_refused(self):
        from apiserver.database.model.device import Device
        device = Device(id=f"dev_{uuid.uuid4().hex}", user=self.leaver.id, company=self.leaver.company,
                        kind="client", name="t", public_key="pem").save()
        self.addCleanup(device.delete)
        directory = StubDirectory({
            f"{self.issuer}|leaver": DirectoryUser(False, ()),
            f"{self.issuer}|stayer": DirectoryUser(True, (ADMIN,)),
        })
        sync_once(directory, ADMIN)
        leaver = User.objects(id=self.leaver.id).get()
        stayer = User.objects(id=self.stayer.id).get()
        self.assertTrue(leaver.idp_disabled)
        self.assertFalse(stayer.idp_disabled)
        self.assertEqual(stayer.role, "admin")
        with self.assertRaises(AccessDenied):
            check_device(Device.objects(id=device.id).get(), User.objects(id=leaver.id).get())
