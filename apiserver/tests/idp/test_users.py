import uuid
from unittest import TestCase

from apiserver.database.model.auth import Role
from apiserver.idp.users import role_for, user_id_for


class TestUserProvisioning(TestCase):
    def test_user_id_is_stable_for_the_same_identity(self):
        a = user_id_for("http://idp/realms/cairnml", "sub-1")
        b = user_id_for("http://idp/realms/cairnml", "sub-1")
        self.assertEqual(a, b)
        self.assertEqual(len(a), 32)

    def test_user_id_differs_between_subjects_and_issuers(self):
        self.assertNotEqual(user_id_for("http://idp/a", "sub-1"), user_id_for("http://idp/a", "sub-2"))
        self.assertNotEqual(user_id_for("http://idp/a", "sub-1"), user_id_for("http://idp/b", "sub-1"))

    def test_user_id_matches_the_documented_derivation(self):
        expected = uuid.uuid5(uuid.NAMESPACE_URL, "http://idp/realms/cairnml|sub-1").hex
        self.assertEqual(user_id_for("http://idp/realms/cairnml", "sub-1"), expected)

    def test_admin_role_comes_from_the_idp_admin_role(self):
        self.assertEqual(role_for(["admin", "offline_access"], "admin"), Role.admin)

    def test_everyone_else_is_a_plain_user(self):
        self.assertEqual(role_for(["offline_access"], "admin"), Role.user)
        self.assertEqual(role_for([], "admin"), Role.user)
