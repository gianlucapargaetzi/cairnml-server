import pytest
from unittest import TestCase
from uuid import uuid4

from apiserver import database as db
from apiserver.database.model.auth import Role, User
from apiserver.database.model.user import User as Profile
from apiserver.idp.users import upsert_idp_user, user_id_for

pytestmark = pytest.mark.integration  # needs MongoDB, Elasticsearch or a running API server

ADMIN = "admin"


class TestUpsertIdpUser(TestCase):
    @classmethod
    def setUpClass(cls):
        db.db.initialize()

    def setUp(self):
        self.issuer = f"http://idp/{uuid4().hex}"
        self.subject = uuid4().hex
        self.user_id = user_id_for(self.issuer, self.subject)

    def tearDown(self):
        User.objects(id=self.user_id).delete()
        Profile.objects(id=self.user_id).delete()

    def test_first_sign_in_creates_user_and_profile(self):
        user = upsert_idp_user(self.issuer, self.subject, "alice", (), ADMIN)

        self.assertEqual(user.id, self.user_id)
        self.assertEqual(User.objects(id=self.user_id).get().role, Role.user)
        self.assertIsNotNone(Profile.objects(id=self.user_id).first())

    def test_later_sign_in_updates_role_and_name_without_a_second_user(self):
        upsert_idp_user(self.issuer, self.subject, "alice", (), ADMIN)
        upsert_idp_user(self.issuer, self.subject, "alice-renamed", (ADMIN,), ADMIN)

        stored = User.objects(id=self.user_id).get()
        self.assertEqual(stored.role, Role.admin)
        self.assertEqual(stored.name, "alice-renamed")
        self.assertEqual(User.objects(idp_subject=f"{self.issuer}|{self.subject}").count(), 1)


def test_a_second_user_the_idp_maps_to_admin_stays_a_user_while_there_is_an_admin():
    """There is one admin account: it owns the shared repositories and the company's credentials."""
    from apiserver.config.info import get_default_company
    from apiserver.database.model.auth import Role, User
    from apiserver.idp.users import single_admin, upsert_idp_user

    company = get_default_company()
    first = upsert_idp_user("https://idp", "admin-1", "root-admin", ["cairn-admin"], "cairn-admin")
    second = upsert_idp_user("https://idp", "admin-2", "would-be-admin", ["cairn-admin"], "cairn-admin")
    try:
        assert first.role == Role.admin and second.role == Role.user
        assert upsert_idp_user("https://idp", "admin-1", "root-admin", ["cairn-admin"], "cairn-admin").role == Role.admin
        assert single_admin(second.id, Role.admin, company) == Role.user and single_admin(second.id, Role.user, company) == Role.user
    finally:
        User.objects(id__in=[first.id, second.id]).delete()
