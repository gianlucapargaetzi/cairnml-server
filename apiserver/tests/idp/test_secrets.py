import os
import tempfile
from unittest import TestCase, mock

from apiserver.idp.secrets import (
    SecretMissing,
    read_secret,
    refuse_default_secret,
)


class TestSecrets(TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self._env = mock.patch.dict(os.environ, {"CLEARML_SECRETS_DIR": self._dir.name})
        self._env.start()
        self.addCleanup(self._env.stop)

    def _write(self, name, content):
        with open(os.path.join(self._dir.name, name), "w") as f:
            f.write(content)

    def test_reads_secret_and_strips_one_trailing_newline(self):
        self._write("token_secret", "abc123\n")
        self.assertEqual(read_secret("token_secret"), "abc123")

    def test_keeps_secret_without_trailing_newline(self):
        self._write("token_secret", "abc123")
        self.assertEqual(read_secret("token_secret"), "abc123")

    def test_missing_secret_raises(self):
        with self.assertRaises(SecretMissing):
            read_secret("does_not_exist")

    def test_default_secret_is_refused(self):
        with self.assertRaises(ValueError):
            refuse_default_secret("Rq8FW84sSqVgq7WvBB_4EzNl9y8z8IGiDXX3C345_a5AZfcwZcwCIA", "Rq8FW84sSqVgq7WvBB_4EzNl9y8z8IGiDXX3C345_a5AZfcwZcwCIA")

    def test_custom_secret_is_accepted(self):
        refuse_default_secret("a-real-secret", "Rq8FW84sSqVgq7WvBB_4EzNl9y8z8IGiDXX3C345_a5AZfcwZcwCIA")
