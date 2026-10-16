from types import SimpleNamespace
from unittest import TestCase

from apiserver.idp.directory import DirectoryError, DirectoryUser
from apiserver.idp.user_sync import decide


def user(uid, disabled=False, role="user"):
    return SimpleNamespace(id=uid, idp_subject=f"iss|{uid}", idp_disabled=disabled, role=role)


class TestSyncDecisions(TestCase):
    def test_enabled_user_stays_enabled_and_takes_the_idp_role(self):
        actions = decide([user("u1", role="user")], {"iss|u1": DirectoryUser(True, ("admin",))}, "admin")
        self.assertEqual(actions, [("u1", False, "admin")])

    def test_disabled_user_is_disabled(self):
        actions = decide([user("u1")], {"iss|u1": DirectoryUser(False, ())}, "admin")
        self.assertEqual(actions, [("u1", True, "user")])

    def test_missing_user_is_disabled(self):
        actions = decide([user("u1")], {"iss|u1": None}, "admin")
        self.assertEqual(actions, [("u1", True, "user")])

    def test_failed_lookup_changes_nothing(self):
        actions = decide([user("u1")], {"iss|u1": DirectoryError("down")}, "admin")
        self.assertEqual(actions, [])

    def test_mass_anomaly_disables_nobody(self):
        users = [user("u1"), user("u2"), user("u3")]
        answers = {"iss|u1": None, "iss|u2": DirectoryUser(False, ()), "iss|u3": None}
        self.assertEqual(decide(users, answers, "admin"), [])

    def test_single_user_offboarded_is_still_disabled(self):
        actions = decide([user("u1")], {"iss|u1": None}, "admin")
        self.assertEqual(actions, [("u1", True, "user")])

    def test_already_disabled_user_is_left_alone_when_idp_agrees(self):
        actions = decide([user("u1", disabled=True)], {"iss|u1": DirectoryUser(False, ())}, "admin")
        self.assertEqual(actions, [])
