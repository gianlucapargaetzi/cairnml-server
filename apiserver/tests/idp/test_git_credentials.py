import os
import uuid
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from apiserver.apierrors import errors
from apiserver.database.model.audit import AuditEvent
from apiserver.database.model.git_credential import GitCredential
from apiserver.idp import git_credentials as store
from apiserver.idp.secrets import read_secret
from apiserver.tests.db import ensure_db

KEY = "key-material-for-the-tests"
SECRET = "stored-value-for-the-tests"


class TestEncryption(TestCase):
    def test_a_value_survives_the_round_trip_and_is_not_stored_in_clear(self):
        token = store.encrypt(SECRET, KEY)
        self.assertNotIn(SECRET, token)
        self.assertEqual(store.decrypt(token, KEY), SECRET)

    def test_the_same_value_encrypts_differently_each_time(self):
        self.assertNotEqual(store.encrypt(SECRET, KEY), store.encrypt(SECRET, KEY))

    def test_another_key_cannot_decrypt_and_the_error_does_not_carry_the_value(self):
        token = store.encrypt(SECRET, KEY)
        with self.assertRaises(store.CredentialUnreadable) as ctx:
            store.decrypt(token, KEY + "other")
        self.assertNotIn(SECRET, str(ctx.exception))

    def test_a_tampered_token_is_refused(self):
        token = store.encrypt(SECRET, KEY)
        tampered = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
        with self.assertRaises(store.CredentialUnreadable):
            store.decrypt(tampered, KEY)

    def test_the_key_is_read_from_the_secrets_directory(self):
        with TemporaryDirectory() as folder, patch.dict(os.environ, {"CAIRNML_SECRETS_DIR": folder}):
            with open(os.path.join(folder, store.KEY_SECRET), "w") as handle:
                handle.write(KEY + "\n")
            self.assertEqual(store.key_material(), KEY)
            self.assertEqual(read_secret(store.KEY_SECRET), KEY)

    def test_a_missing_or_empty_key_refuses_the_store(self):
        with TemporaryDirectory() as folder, patch.dict(os.environ, {"CAIRNML_SECRETS_DIR": folder}):
            with self.assertRaises(errors.server_error.ConfigError):
                store.key_material()
            open(os.path.join(folder, store.KEY_SECRET), "w").close()
            with self.assertRaises(errors.server_error.ConfigError):
                store.key_material()


class TestHostsAndPaths(TestCase):
    def test_a_repository_url_gives_its_host_and_path(self):
        self.assertEqual(store.host_and_path("https://GitLab.Example.com/Team/Project.git"),
                         ("gitlab.example.com", "team/project"))

    def test_user_information_in_the_url_is_dropped(self):
        self.assertEqual(store.host_and_path("https://user:secret@git.example.org:8443/a/b"),
                         ("git.example.org:8443", "a/b"))

    def test_a_url_without_a_scheme_is_taken_as_https(self):
        self.assertEqual(store.host_and_path("gitea.local/ops/repo"), ("gitea.local", "ops/repo"))

    def test_a_host_must_be_a_host_name(self):
        for bad in ("https://gitlab.example.com", "gitlab example.com", "", "a/b"):
            with self.assertRaises(errors.bad_request.ValidationError):
                store.normalise_host(bad)

    def test_a_path_must_be_plain(self):
        self.assertEqual(store.normalise_path("/Team/"), "team")
        for bad in ("team/../x", "a//b", "team?x", "a b"):
            with self.assertRaises(errors.bad_request.ValidationError):
                store.normalise_path(bad)

    def test_the_most_specific_prefix_wins_and_prefixes_match_whole_segments(self):
        entries = [SimpleNamespace(path=""), SimpleNamespace(path="team"), SimpleNamespace(path="team/project")]
        self.assertEqual(store.most_specific(entries, "team/project/x").path, "team/project")
        self.assertEqual(store.most_specific(entries, "team/other").path, "team")
        self.assertEqual(store.most_specific(entries, "teams/other").path, "")
        self.assertIsNone(store.most_specific([SimpleNamespace(path="team")], "teams/x"))


class TestCredentialStore(TestCase):
    @classmethod
    def setUpClass(cls):
        ensure_db()

    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        with open(os.path.join(folder.name, store.KEY_SECRET), "w") as handle:
            handle.write(KEY)
        env = patch.dict(os.environ, {"CAIRNML_SECRETS_DIR": folder.name})
        env.start()
        self.addCleanup(env.stop)
        self.company = f"company-{uuid.uuid4().hex}"
        self.actor = "tester-" + self.company
        self.addCleanup(lambda: GitCredential.objects(company=self.company).delete())
        self.addCleanup(lambda: AuditEvent.objects(actor=self.actor).delete())

    def test_the_stored_value_is_encrypted_and_the_answers_never_carry_it(self):
        view = store.set_credential(self.company, "gitlab.example.com", "team", "ci-bot", SECRET, self.actor)
        self.assertEqual(view, {"host": "gitlab.example.com", "path": "team", "username": "ci-bot",
                                "updated": view["updated"]})
        stored = GitCredential.objects(company=self.company).get()
        self.assertNotIn(SECRET, stored.secret)
        listed = store.list_credentials(self.company)
        self.assertEqual(len(listed), 1)
        self.assertNotIn(SECRET, repr(listed))
        self.assertEqual(set(listed[0]), {"host", "path", "username", "updated"})

    def test_setting_again_replaces_the_entry(self):
        store.set_credential(self.company, "gitlab.example.com", "team", "ci-bot", SECRET, self.actor)
        store.set_credential(self.company, "gitlab.example.com", "/Team/", "ci-bot-2", "new-value", self.actor)
        self.assertEqual(GitCredential.objects(company=self.company).count(), 1)
        entry = store.find_credential(self.company, "https://gitlab.example.com/team/project")
        self.assertEqual(entry.username, "ci-bot-2")
        self.assertEqual(store.secret_of(entry), "new-value")

    def test_the_lookup_for_a_repository_uses_the_most_specific_entry_of_the_company(self):
        store.set_credential(self.company, "gitlab.example.com", "", "all", "v-all", self.actor)
        store.set_credential(self.company, "gitlab.example.com", "team", "team", "v-team", self.actor)
        entry = store.find_credential(self.company, "https://gitlab.example.com/team/project.git")
        self.assertEqual((entry.username, store.secret_of(entry)), ("team", "v-team"))
        entry = store.find_credential(self.company, "https://gitlab.example.com/other/project")
        self.assertEqual((entry.username, store.secret_of(entry)), ("all", "v-all"))

    def test_a_repository_on_another_host_or_company_finds_nothing(self):
        store.set_credential(self.company, "gitlab.example.com", "", "u", SECRET, self.actor)
        self.assertIsNone(store.find_credential(self.company, "https://bitbucket.example.com/team/project"))
        self.assertIsNone(store.find_credential(self.company + "-other", "https://gitlab.example.com/team/project"))

    def test_deleting_removes_the_entry_and_is_audited_without_the_value(self):
        store.set_credential(self.company, "gitlab.example.com", "team", "u", SECRET, self.actor)
        self.assertEqual(store.delete_credential(self.company, "gitlab.example.com", "team", self.actor), 1)
        self.assertIsNone(store.find_credential(self.company, "https://gitlab.example.com/team/x"))
        self.assertEqual(store.delete_credential(self.company, "gitlab.example.com", "team", self.actor), 0)
        events = list(AuditEvent.objects(actor=self.actor).order_by("id"))
        self.assertEqual([event.action for event in events], ["git_credential.set", "git_credential.deleted"])
        self.assertNotIn(SECRET, repr([event.to_mongo().to_dict() for event in events]))

    def test_a_stored_credential_is_handed_out_for_the_task_and_audited(self):
        from apiserver.services.devices import _stored_token

        store.set_credential(self.company, "gitlab.example.com", "team", "ci-bot", SECRET, self.actor)
        task_id = f"task-{uuid.uuid4().hex}"
        answer = _stored_token(self.company, self.actor, task_id, "https://gitlab.example.com/team/project.git")
        self.assertEqual(answer, {"token": SECRET, "expires_at": None, "username": "ci-bot"})
        issued = AuditEvent.objects(actor=self.actor, action="git_token.issued").get()
        self.assertEqual(issued.detail["task"], task_id)
        self.assertEqual(issued.detail["host"], "gitlab.example.com")
        self.assertNotIn(SECRET, repr(issued.to_mongo().to_dict()))

    def test_a_host_without_a_credential_is_refused_and_named(self):
        from apiserver.services.devices import _stored_token

        with self.assertRaises(errors.bad_request.ValidationError) as ctx:
            _stored_token(self.company, self.actor, "task-x", "https://gitlab.example.com/team/project")
        self.assertIn("gitlab.example.com", str(ctx.exception))
        self.assertIn("git_credentials.set", str(ctx.exception))
