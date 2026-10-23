from types import SimpleNamespace
from unittest import TestCase

from apiserver.apierrors import errors
from apiserver.idp.github import GitHubError, RepoNotAccessible
from apiserver.idp.github_check import check_submission


class FakeApp:
    def __init__(self, commit_present=True, installed=True, failing=False):
        self.commit_present, self.installed, self.failing = commit_present, installed, failing

    def installation_token(self, repo):
        if not self.installed:
            raise RepoNotAccessible(repo)
        if self.failing:
            raise GitHubError("boom")
        return SimpleNamespace(token="t")

    def commit_exists(self, repo, sha):
        if self.failing:
            raise GitHubError("boom")
        return self.commit_present


def task(repository="https://github.com/acme/proj.git", commit="abc123"):
    return SimpleNamespace(id="t1", script=SimpleNamespace(repository=repository, version_num=commit))


class TestSubmissionCheck(TestCase):
    def test_a_repository_on_github_with_its_commit_is_accepted(self):
        check_submission(task(), FakeApp())

    def test_a_missing_commit_is_refused_with_a_clear_message(self):
        with self.assertRaises(errors.bad_request.ValidationError) as ctx:
            check_submission(task(), FakeApp(commit_present=False))
        self.assertIn("push it first", str(ctx.exception))

    def test_a_repository_without_the_app_is_refused(self):
        with self.assertRaises(errors.bad_request.ValidationError) as ctx:
            check_submission(task(), FakeApp(installed=False))
        self.assertIn("not installed", str(ctx.exception))

    def test_a_github_repository_without_a_recorded_commit_is_refused(self):
        with self.assertRaises(errors.bad_request.ValidationError):
            check_submission(task(commit=None), FakeApp())

    def test_a_failing_github_call_refuses_the_job(self):
        with self.assertRaises(errors.bad_request.ValidationError):
            check_submission(task(), FakeApp(failing=True))

    def test_a_repository_that_is_not_on_github_is_not_checked(self):
        check_submission(task(repository="https://gitlab.example.org/a/b.git"), None)

    def test_no_repository_is_not_checked(self):
        check_submission(task(repository=""), None)

    def test_a_github_repository_is_refused_when_the_app_is_not_configured(self):
        with self.assertRaises(errors.bad_request.ValidationError) as ctx:
            check_submission(task(), None)
        self.assertIn("GitHub", str(ctx.exception))

