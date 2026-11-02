import base64
import json
from unittest import TestCase

from apiserver.idp.github import GitHubApp


class Response:
    def __init__(self, status, body=None, content=b""):
        self.status_code = status
        self._body = body
        self.content = content

    def json(self):
        return self._body


class FakeHttp:
    """Answers the GitHub requests a public read makes. Any request that carries a token is recorded as a token request."""

    def __init__(self, routes):
        self.routes = routes
        self.authorized = []

    def request(self, method, url, headers=None, **kwargs):
        if headers and "Authorization" in headers:
            self.authorized.append(url)
        for suffix, response in self.routes.items():
            if url.endswith(suffix):
                return response
        return Response(404)


def app_with(http):
    return GitHubApp("1", "not-a-key", "https://api.example", http=http, clock=lambda: 0)


class TestPublicReads(TestCase):
    def test_a_public_repository_is_read_without_a_token(self):
        http = FakeHttp({
            "/repos/acme/registry": Response(200, {"private": False, "default_branch": "main"}),
            "/repos/acme/registry/commits/main": Response(200, {"sha": "abc"}),
        })
        app = app_with(http)
        self.assertEqual(app.default_branch_head("acme/registry"), ("main", "abc"))
        self.assertEqual(http.authorized, [])

    def test_a_private_repository_is_read_with_the_app(self):
        http = FakeHttp({"/repos/acme/secret": Response(404)})
        app = app_with(http)
        self.assertFalse(app._is_public("acme/secret"))
        with self.assertRaises(Exception):
            # no installation exists in the fake: the lookup reaches the App, which is what a private repository needs
            app.default_branch_head("acme/secret")

    def test_a_public_folder_is_read_file_by_file(self):
        listing = [{"name": "cars.yaml", "type": "file"}, {"name": "sub", "type": "dir"}]
        http = FakeHttp({
            "/repos/acme/registry": Response(200, {"private": False}),
            "/repos/acme/registry/contents/datasets": Response(200, listing),
            "/repos/acme/registry/contents/datasets/cars.yaml": Response(200, content=b"source: web\n"),
        })
        texts = app_with(http).read_dir_texts("acme/registry", "datasets")
        self.assertEqual(texts, {"cars.yaml": "source: web\n"})
        self.assertEqual(http.authorized, [])
