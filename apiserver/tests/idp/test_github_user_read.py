# apiserver/tests/idp/test_github_user_read.py
"""Whether a user's own GitHub token reads a repository: a 200 is yes, a refusal of the token or of the repository is no,
and any other answer is a GitHub error. No network: the HTTP session is a fake."""
from unittest import TestCase

from apiserver.idp.github import GitHubError, repository_readable_with


class Response:
    def __init__(self, status):
        self.status_code = status


class FakeHttp:
    def __init__(self, status):
        self.status = status
        self.calls = []

    def request(self, method, url, headers=None, **kwargs):
        self.calls.append((method, url, headers))
        return Response(self.status)


class TestRepositoryReadableWith(TestCase):
    def test_a_200_means_the_token_reads_the_repository(self):
        http = FakeHttp(200)
        self.assertTrue(repository_readable_with("https://api.example", "ghp_x", "acme/found", http=http))
        method, url, headers = http.calls[0]
        self.assertEqual((method, url), ("GET", "https://api.example/repos/acme/found"))
        self.assertEqual(headers["Authorization"], "Bearer ghp_x")

    def test_a_404_or_403_or_401_means_the_token_does_not_read_it(self):
        for status in (401, 403, 404):
            with self.subTest(status=status):
                self.assertFalse(repository_readable_with("https://api.example", "ghp_x", "acme/found",
                                                          http=FakeHttp(status)))

    def test_any_other_answer_is_a_github_error(self):
        with self.assertRaises(GitHubError):
            repository_readable_with("https://api.example", "ghp_x", "acme/found", http=FakeHttp(500))
