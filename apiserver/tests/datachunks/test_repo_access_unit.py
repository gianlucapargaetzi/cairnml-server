"""How a repository is reached: public, GitHub App, stored token, or refused. Network and stored credentials are faked.

Run inside the server image: python -m unittest apiserver.tests.datachunks.test_repo_access_unit
"""

from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from apiserver.bll import repo_access as bll
from apiserver.idp.github import GitHubError, RepoNotAccessible

GITHUB = "https://github.com/acme/cars-data.git"


class FakeApp:
    def __init__(self, reachable=True):
        self.reachable = reachable

    def installation_token(self, repo):
        if not self.reachable:
            raise RepoNotAccessible(repo)
        return "app-token"


def resolve(repository, *, public=False, app=None, credential=None, token_ok=True, private=None, now=1000.0):
    bll._cache.clear()
    with patch.object(bll, "public_repository", return_value=public), \
            patch.object(bll, "build_from_config", return_value=app), \
            patch.object(bll.store, "find_credential", return_value=credential), \
            patch.object(bll.store, "secret_of", return_value="stored-token"), \
            patch.object(bll, "_token_works", return_value=token_ok), \
            patch.object(bll, "_app_label", return_value="CairnML"), \
            patch.object(bll, "is_private", return_value=private), \
            patch.object(bll.store, "host_and_path", return_value=("gitlab.example.org", "")):
        return bll.resolve_one("company-1", repository, now=now)


class TestGitHub(TestCase):
    def test_a_public_repository_needs_nothing(self):
        result = resolve(GITHUB, public=True)
        self.assertEqual((result["state"], result["via"]), ("public", ""))
        self.assertEqual(result["message"], "Publicly accessible")

    def test_a_private_repository_reached_by_the_app_names_the_app(self):
        result = resolve(GITHUB, app=FakeApp(reachable=True))
        self.assertEqual((result["state"], result["via"]), ("app", "CairnML"))
        self.assertEqual(result["message"], "Accessible via GitHub App CairnML")

    def test_a_token_is_used_when_the_app_does_not_reach_the_repository(self):
        credential = SimpleNamespace(host="github.com")
        result = resolve(GITHUB, app=FakeApp(reachable=False), credential=credential, token_ok=True)
        self.assertEqual((result["state"], result["via"]), ("token", "github.com"))

    def test_nothing_reaching_a_private_repository_is_refused_with_a_reason(self):
        result = resolve(GITHUB, app=FakeApp(reachable=False), credential=None)
        self.assertEqual(result["state"], "refused")
        self.assertIn("Ask an admin", result["message"])
        self.assertNotIn("stored-token", result["message"])

    def test_a_token_refused_by_github_is_reported_as_refused(self):
        credential = SimpleNamespace(host="github.com")
        result = resolve(GITHUB, app=None, credential=credential, token_ok=False)
        self.assertEqual(result["state"], "refused")
        self.assertIn("refused by GitHub", result["message"])

    def test_the_result_is_cached_for_ten_minutes(self):
        first = resolve(GITHUB, public=True, now=1000.0)
        with patch.object(bll, "public_repository", side_effect=AssertionError("asked again")):
            again = bll.resolve_one("company-1", GITHUB, now=1000.0 + bll.CACHE_SEC - 1)
        self.assertEqual(again, first)


class TestOtherHosts(TestCase):
    def test_a_public_repository_on_another_host_is_public(self):
        result = resolve("https://gitlab.example.org/team/cars.git", private=False)
        self.assertEqual(result["state"], "public")

    def test_a_private_repository_on_another_host_with_a_stored_token_is_recorded_not_verified(self):
        credential = SimpleNamespace(host="gitlab.example.org")
        result = resolve("https://gitlab.example.org/team/cars.git", private=True, credential=credential)
        self.assertEqual((result["state"], result["via"]), ("token", "gitlab.example.org"))
        self.assertIn("not verified", result["message"])

    def test_a_private_repository_on_another_host_without_credentials_is_refused(self):
        result = resolve("https://gitlab.example.org/team/cars.git", private=True, credential=None)
        self.assertEqual(result["state"], "refused")
