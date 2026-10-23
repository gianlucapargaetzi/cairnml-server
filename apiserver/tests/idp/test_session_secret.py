"""The web session key comes from the secrets mount, and the repository's default is refused. Runs anywhere."""

import os
import tempfile
from unittest import TestCase
from unittest import mock

from apiserver.idp.secrets import DEFAULT_SESSION_SECRET, SecretMissing, session_secret


class TestSessionSecret(TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.env = mock.patch.dict(os.environ, {"CAIRNML_SECRETS_DIR": self.dir.name}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        os.environ.pop("CAIRNML_ALLOW_CONFIG_SECRET", None)

    def write(self, value):
        with open(os.path.join(self.dir.name, "session_secret"), "w", encoding="utf-8") as handle:
            handle.write(value)

    def test_the_key_is_read_from_the_secrets_folder(self):
        self.write("a-real-session-key\n")
        self.assertEqual(session_secret(), "a-real-session-key")

    def test_a_missing_key_is_an_error(self):
        with self.assertRaises(SecretMissing):
            session_secret()

    def test_the_repository_default_is_refused(self):
        self.write(DEFAULT_SESSION_SECRET + "\n")
        with self.assertRaises(ValueError):
            session_secret()
