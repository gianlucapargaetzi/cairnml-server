from types import SimpleNamespace
from unittest import TestCase, mock

from apiserver.apierrors import errors
from apiserver.bll import repositories
from apiserver.idp.access_refused import AccessRefused
from apiserver.idp.github import GitHubError
from apiserver.idp.github_check import GITHUB_APP_DOCS, check_submission


class FakeApp:
    def __init__(self, commit_present=True, failing=False):
        self.commit_present, self.failing = commit_present, failing

    def commit_exists(self, repo, sha):
        if self.failing:
            raise GitHubError("boom")
        return self.commit_present


def task(repository="https://github.com/acme/proj.git", commit="abc123"):
    return SimpleNamespace(id="t1", company="c1", user="u1", project="p1",
                           script=SimpleNamespace(repository=repository, version_num=commit))


class TestSubmissionCheck(TestCase):
    """The check asks the resolver (tested in tests/bll/test_repositories); here the resolver is faked."""

    def check(self, state=repositories.ASSIGNED, host="github.com", app=None, message="", **task_kwargs):
        repo = SimpleNamespace(host=host, path="acme/proj")
        resolution = repositories.Resolution(state, SimpleNamespace(kind="app") if app is not None else None, message)
        with mock.patch.object(repositories, "record_use_quietly", return_value=repo), \
                mock.patch.object(repositories, "resolve", return_value=resolution), \
                mock.patch.object(repositories, "_app_of", return_value=app), \
                mock.patch("apiserver.bll.user_access.root_project", return_value="p1"):
            check_submission(task(**task_kwargs))

    def test_a_repository_read_with_a_credential_and_its_commit_is_accepted(self):
        self.check(app=FakeApp())

    def test_a_missing_commit_is_refused_with_a_clear_message(self):
        with self.assertRaisesRegex(errors.bad_request.ValidationError, "push it first"):
            self.check(app=FakeApp(commit_present=False))

    def test_an_unreachable_repository_is_refused_with_the_reason_and_the_steps(self):
        with self.assertRaises(AccessRefused) as ctx:
            self.check(state=repositories.UNREACHABLE, message="The repository is private and no credential is assigned")
        self.assertIn("no credential is assigned", str(ctx.exception))
        self.assertIn(GITHUB_APP_DOCS, str(ctx.exception))

    def test_a_github_repository_without_a_recorded_commit_is_refused(self):
        with self.assertRaises(errors.bad_request.ValidationError):
            self.check(app=FakeApp(), commit=None)

    def test_a_failing_github_call_refuses_the_job(self):
        with self.assertRaises(errors.bad_request.ValidationError):
            self.check(app=FakeApp(failing=True))

    def test_a_repository_cloned_without_a_credential_is_accepted_and_its_commit_is_not_looked_up(self):
        self.check(state=repositories.ANONYMOUS, app=None)

    def test_an_unknown_answer_never_refuses_the_job(self):
        self.check(state=repositories.UNKNOWN, app=None)

    def test_another_host_is_checked_by_the_resolver_only(self):
        self.check(host="gitlab.example.org")
        with self.assertRaises(AccessRefused) as ctx:
            self.check(state=repositories.UNREACHABLE, host="gitlab.example.org", message="private")
        self.assertNotIn(GITHUB_APP_DOCS, str(ctx.exception))

    def test_no_repository_is_not_checked(self):
        with mock.patch.object(repositories, "record_use_quietly") as record:
            check_submission(task(repository=""))
        record.assert_not_called()

    def test_an_address_that_names_no_hosted_repository_is_not_checked(self):
        with mock.patch.object(repositories, "record_use_quietly", return_value=None):
            check_submission(task(repository="/home/me/code"))
