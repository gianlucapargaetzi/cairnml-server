"""The directory is asked for the user's id, not the stored "issuer|id" subject. Runs anywhere."""

from types import SimpleNamespace
from unittest import TestCase

from apiserver.idp.user_sync import fetch_answers
from apiserver.idp.directory import DirectoryUser


class KeycloakLikeDirectory:
    """Knows users by their bare id only, as Keycloak's user endpoint does."""

    def __init__(self):
        self.asked = []

    def get_user(self, subject):
        self.asked.append(subject)
        if subject == "sub-1":
            return DirectoryUser(enabled=True, roles=("mlops-admin",))
        return None


class TestSubjectSentToTheDirectory(TestCase):
    def test_the_bare_id_is_asked_not_the_issuer_prefixed_subject(self):
        directory = KeycloakLikeDirectory()
        user = SimpleNamespace(idp_subject="http://mlops-idp:8180/realms/mlops|sub-1")
        answers = fetch_answers(directory, [user])
        self.assertEqual(directory.asked, ["sub-1"])
        self.assertIsNotNone(answers[user.idp_subject])
        self.assertTrue(answers[user.idp_subject].enabled)
