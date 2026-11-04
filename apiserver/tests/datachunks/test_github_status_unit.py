"""The GitHub App check: the state and the banner message for each outcome.

Run inside the server image: python -m unittest apiserver.tests.datachunks.test_github_status_unit
"""

from unittest import TestCase
from unittest.mock import patch

from apiserver.bll import github_status
from apiserver.idp.github import GitHubError, GitHubRefused
from apiserver.idp.secrets import SecretMissing


class FakeConfig:
    def __init__(self, app_id="12345"):
        self.app_id = app_id

    def get(self, key, default=None):
        if key == "apiserver.idp.github_app_id":
            return self.app_id
        return default


class FakeApp:
    def __init__(self, info=None, info_error=None, repositories=None, repositories_error=None):
        self.info = info if info is not None else {"slug": "cairnml", "name": "CairnML", "id": 12345}
        self.info_error = info_error
        self.repositories = repositories if repositories is not None else [{"repository": "acme/a"}]
        self.repositories_error = repositories_error

    def app_info(self):
        if self.info_error:
            raise self.info_error
        return self.info

    def installed_repositories(self):
        if self.repositories_error:
            raise self.repositories_error
        return self.repositories


class TestRunCheck(TestCase):
    def run_with(self, app=None, build_error=None, app_id="12345", registries=None, entries=None, unreadable=()):
        registries = ["acme/public-datasets", "acme/private-datasets"] if registries is None else registries
        entries = {} if entries is None else entries

        def list_entries(app, registry):
            if registry in unreadable:
                raise GitHubError(f"folder read failed for {registry}")
            return entries.get(registry, [])

        def build():
            if build_error:
                raise build_error
            return app

        with patch("apiserver.config_repo.config", FakeConfig(app_id)), \
                patch("apiserver.bll.github_status.build_from_config", side_effect=build), \
                patch("apiserver.bll.dataset_registry.configured_registries", return_value=registries), \
                patch("apiserver.bll.dataset_registry.list_entries", side_effect=list_entries):
            return github_status.run_check()

    def test_no_app_id_is_not_configured(self):
        result = self.run_with(app=FakeApp(), app_id="")
        self.assertEqual(result["state"], "not_configured")
        self.assertIn("GITHUB_APP_ID is not set", result["message"])

    def test_a_missing_key_is_refused_with_the_secret_named(self):
        result = self.run_with(build_error=SecretMissing("secret file 'github_app_key' is missing"))
        self.assertEqual(result["state"], "refused")
        self.assertTrue(result["message"].startswith("GitHub App Access Refused"))
        self.assertIn("github_app_key", result["message"])
        self.assertFalse(result["steps"][0]["ok"])

    def test_github_refusing_the_key_is_refused_and_stops_the_check(self):
        result = self.run_with(app=FakeApp(info_error=GitHubRefused("GitHub did not accept the App ID or its key (401)")))
        self.assertEqual(result["state"], "refused")
        self.assertEqual(result["message"], "GitHub App Access Refused: GitHub did not accept the App ID or its key (401)")
        self.assertEqual([s["name"] for s in result["steps"]], ["configured", "app"])

    def test_github_down_is_unreachable_not_refused(self):
        result = self.run_with(app=FakeApp(info_error=GitHubError("GitHub transport error")))
        self.assertEqual(result["state"], "unreachable")
        self.assertTrue(result["message"].startswith("GitHub could not be reached"))

    def test_working_app_reports_registries_and_their_datasets(self):
        entries = {
            "acme/public-datasets": [{"name": "coco"}, {"name": "cars"}],
            "acme/private-datasets": [{"name": "fashion"}],
        }
        result = self.run_with(app=FakeApp(), entries=entries)
        self.assertEqual(result["state"], "ok")
        self.assertEqual(result["message"], "GitHub App works. Registered 2 dataset registries with 3 datasets.")
        self.assertEqual(result["datasets"], 3)
        self.assertEqual(result["app"]["slug"], "cairnml")
        self.assertEqual(result["repositories"], [{"repository": "acme/a"}])

    def test_an_unreadable_registry_is_counted_and_does_not_fail_the_check(self):
        result = self.run_with(app=FakeApp(), entries={"acme/public-datasets": [{"name": "coco"}]},
                               unreadable=("acme/private-datasets",))
        self.assertEqual(result["state"], "ok")
        self.assertEqual(result["message"],
                         "GitHub App works. Registered 2 dataset registries with 1 dataset (1 could not be read).")
        failed = [r for r in result["registries"] if not r["ok"]]
        self.assertEqual([r["repository"] for r in failed], ["acme/private-datasets"])

    def test_no_registries_is_stated_plainly(self):
        result = self.run_with(app=FakeApp(), registries=[])
        self.assertEqual(result["message"], "GitHub App works. Registered 0 dataset registries with 0 datasets.")


class TestUserView(TestCase):
    def test_a_refused_app_tells_a_user_to_contact_the_admin_without_the_reason(self):
        view = github_status.user_view({"state": "refused", "message": "GitHub App Access Refused: secret file 'x' missing",
                                        "steps": [{"name": "configured"}], "checked_at": "t"})
        self.assertEqual(view["state"], "refused")
        self.assertEqual(view["message"], "GitHub App Access Refused. If something does not work, contact your admin.")
        self.assertNotIn("secret", view["message"])
        self.assertEqual(view["steps"], [])

    def test_a_working_app_keeps_its_counts(self):
        view = github_status.user_view({"state": "ok", "message": "GitHub App works. Registered 1 dataset registry with 2 datasets."})
        self.assertEqual(view["message"], "GitHub App works. Registered 1 dataset registry with 2 datasets.")

    def test_before_the_first_check_the_user_sees_that(self):
        self.assertEqual(github_status.user_view({"state": "unknown"})["message"], "The GitHub App has not been checked yet.")
