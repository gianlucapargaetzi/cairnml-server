"""The App's installations and the public check, against a fake HTTP session. No test reaches GitHub."""

from unittest import TestCase

from apiserver.idp import github
from apiserver.idp.github import GitHubApp, GitHubError


class Response:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body


class FakeHttp:
    """Answers by URL suffix and page number. Records every request."""

    def __init__(self, routes):
        self.routes = routes  # (url suffix, page or None) -> Response, or a function of the call returning one
        self.requests = []

    def request(self, method, url, headers=None, params=None, **kwargs):
        page = int((params or {}).get("page", 1)) if params else None
        self.requests.append((method, url, page, headers))
        for (suffix, route_page), response in self.routes.items():
            if url.endswith(suffix) and route_page in (page, None):
                return response
        return Response(404)


def app_with(http, clock=lambda: 0):
    app = GitHubApp("1", "not-a-key", "https://api.example", http=http, clock=clock)
    app._app_jwt = lambda: "app-jwt"  # no real key here: the App's own token is only needed by the calls under test
    return app


def repos(prefix, count):
    return [{"full_name": f"{prefix}/r{i}", "default_branch": "main", "private": True} for i in range(count)]


class TestInstalledRepositories(TestCase):
    def test_every_repository_of_every_installation_is_listed_with_its_default_branch(self):
        http = FakeHttp({
            ("/app/installations", None): Response(200, [{"id": 11}, {"id": 12}]),
            ("/app/installations/11/access_tokens", None): Response(201, {"token": "tok-11"}),
            ("/app/installations/12/access_tokens", None): Response(201, {"token": "tok-12"}),
            ("/installation/repositories", None): Response(200, {"repositories": [
                {"full_name": "acme/cars", "default_branch": "main", "private": True}]}),
        })
        app = app_with(http)
        listed = app.installed_repositories()
        self.assertEqual(sorted(item["repository"] for item in listed), ["acme/cars", "acme/cars"])
        self.assertEqual(listed[0]["default_branch"], "main")

    def test_a_listing_longer_than_one_page_is_read_page_by_page(self):
        first, second = repos("acme", 100), repos("acme-two", 3)
        http = FakeHttp({
            ("/app/installations", None): Response(200, [{"id": 11}]),
            ("/app/installations/11/access_tokens", None): Response(201, {"token": "tok"}),
            ("/installation/repositories", 1): Response(200, {"repositories": first}),
            ("/installation/repositories", 2): Response(200, {"repositories": second}),
        })
        listed = app_with(http).installed_repositories()
        self.assertEqual(len(listed), 103)
        pages = [page for method, url, page, _headers in http.requests if url.endswith("/installation/repositories")]
        self.assertEqual(pages, [1, 2])

    def test_the_repositories_are_listed_with_the_token_of_their_installation(self):
        http = FakeHttp({
            ("/app/installations", None): Response(200, [{"id": 11}]),
            ("/app/installations/11/access_tokens", None): Response(201, {"token": "tok-11"}),
            ("/installation/repositories", None): Response(200, {"repositories": repos("acme", 1)}),
        })
        app_with(http).installed_repositories()
        listing = [headers for _m, url, _p, headers in http.requests if url.endswith("/installation/repositories")]
        self.assertEqual(listing[0]["Authorization"], "Bearer tok-11")

    def test_a_failing_installation_listing_is_a_github_error(self):
        http = FakeHttp({("/app/installations", None): Response(500)})
        with self.assertRaises(GitHubError):
            app_with(http).installed_repositories()


class TestPublicRepository(TestCase):
    def setUp(self):
        github._public_answers.clear()

    def test_a_public_repository_is_public_and_asked_without_a_token(self):
        http = FakeHttp({("/repos/acme/open", None): Response(200, {"private": False})})
        self.assertTrue(github.public_repository("https://api.example", "acme/open", http=http))
        self.assertIsNone(http.requests[0][3].get("Authorization"))

    def test_a_private_repository_is_not_public(self):
        http = FakeHttp({("/repos/acme/cars", None): Response(404)})
        self.assertFalse(github.public_repository("https://api.example", "acme/cars", http=http))

    def test_the_answer_is_kept_for_ten_minutes(self):
        http = FakeHttp({("/repos/acme/open", None): Response(200, {"private": False})})
        now = [1000.0]
        clock = lambda: now[0]  # noqa: E731
        github.public_repository("https://api.example", "acme/open", clock=clock, http=http)
        github.public_repository("https://api.example", "acme/open", clock=clock, http=http)
        self.assertEqual(len(http.requests), 1)
        now[0] += 601
        github.public_repository("https://api.example", "acme/open", clock=clock, http=http)
        self.assertEqual(len(http.requests), 2)

    def test_a_failing_lookup_is_a_github_error_and_is_not_kept(self):
        http = FakeHttp({("/repos/acme/open", None): Response(403)})
        with self.assertRaises(GitHubError):
            github.public_repository("https://api.example", "acme/open", http=http)
        self.assertNotIn("acme/open", github._public_answers)

    def test_a_public_commit_is_found_without_a_token(self):
        http = FakeHttp({("/repos/acme/open/commits/abc", None): Response(200, {"sha": "abc"})})
        self.assertTrue(github.public_commit_exists("https://api.example", "acme/open", "abc", http=http))
        self.assertIsNone(http.requests[0][3].get("Authorization"))

    def test_a_commit_that_is_not_there_is_not_found(self):
        http = FakeHttp({("/repos/acme/open/commits/zzz", None): Response(422)})
        self.assertFalse(github.public_commit_exists("https://api.example", "acme/open", "zzz", http=http))
