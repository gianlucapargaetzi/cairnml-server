"""Database-level test of the directory sync, run inside the server container."""

import pytest
import uuid
from unittest import TestCase

from apiserver import database as db
from apiserver.database.model.auth import User
from apiserver.idp.checks import AccessDenied, check_device
from apiserver.idp.directory import DirectoryError, DirectoryUser
from apiserver.idp.user_sync import import_missing, sync_once
from apiserver.idp.users import upsert_idp_user

pytestmark = pytest.mark.integration  # needs MongoDB, Elasticsearch or a running API server

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
            "leaver": DirectoryUser(False, ()),
            "stayer": DirectoryUser(True, (ADMIN,)),
        })
        sync_once(directory, ADMIN)
        leaver = User.objects(id=self.leaver.id).get()
        stayer = User.objects(id=self.stayer.id).get()
        self.assertTrue(leaver.idp_disabled)
        self.assertFalse(stayer.idp_disabled)
        self.assertEqual(stayer.role, "admin")
        with self.assertRaises(AccessDenied):
            check_device(Device.objects(id=device.id).get(), User.objects(id=leaver.id).get())

    def test_directory_users_without_a_cairnml_user_are_created_and_the_others_are_not(self):
        newcomer = f"newcomer-{uuid.uuid4().hex}"
        retired = f"retired-{uuid.uuid4().hex}"

        class ListingDirectory(StubDirectory):
            def list_users(self):
                return [("leaver", "leaver", True), ("stayer", "stayer", True),
                        (newcomer, "newcomer", True), (retired, "retired", False)]

        directory = ListingDirectory({
            "leaver": DirectoryUser(True, ()),
            "stayer": DirectoryUser(True, ()),
            newcomer: DirectoryUser(True, (ADMIN,)),
            retired: DirectoryUser(False, ()),
        })
        self.addCleanup(lambda: User.objects(idp_subject__in=[f"{self.issuer}|{newcomer}", f"{self.issuer}|{retired}"]).delete())
        created = import_missing(directory, self.issuer, ADMIN)
        self.assertEqual(created, 1)
        user = User.objects(idp_subject=f"{self.issuer}|{newcomer}").get()
        self.assertEqual(user.name, "newcomer")
        self.assertEqual(user.role, "admin")
        self.assertFalse(User.objects(idp_subject=f"{self.issuer}|{retired}").first())
        self.assertEqual(User.objects(idp_subject=f"{self.issuer}|stayer").count(), 1)
        self.assertEqual(import_missing(directory, self.issuer, ADMIN), 0)

    def test_disabling_an_owner_audits_its_live_devices(self):
        from apiserver.database.model.audit import AuditEvent
        from apiserver.database.model.device import Device
        live = Device(id=f"dev_{uuid.uuid4().hex}", user=self.leaver.id, company=self.leaver.company,
                      kind="agent", name="live", public_key="pem").save()
        revoked = Device(id=f"dev_{uuid.uuid4().hex}", user=self.leaver.id, company=self.leaver.company,
                         kind="agent", name="revoked", public_key="pem", revoked=True).save()
        self.addCleanup(lambda: [d.delete() for d in (live, revoked)])
        self.addCleanup(lambda: AuditEvent.objects(actor=self.leaver.id, action="owner.disabled").delete())
        directory = StubDirectory({
            "leaver": DirectoryUser(False, ()),
            "stayer": DirectoryUser(True, (ADMIN,)),
        })
        sync_once(directory, ADMIN)
        event = AuditEvent.objects(actor=self.leaver.id, action="owner.disabled").get()
        self.assertEqual(event.detail["devices"], [live.id])
