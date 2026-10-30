from unittest import TestCase, mock

import requests

from apiserver.apierrors import errors
from apiserver.idp import repo_visibility
from apiserver.idp.repo_visibility import check_readable, is_private


def response(status, content_type="application/x-git-upload-pack-advertisement"):
    return mock.Mock(status_code=status, headers={"Content-Type": content_type})


class TestIsPrivate(TestCase):
    def test_an_anonymous_advertisement_means_public(self):
        with mock.patch.object(repo_visibility.requests, "get", return_value=response(200)) as get:
            self.assertIs(is_private("https://gitlab.example.org/team/proj.git"), False)
        get.assert_called_once()
        self.assertEqual(get.call_args[0][0],
                         "https://gitlab.example.org/team/proj/info/refs?service=git-upload-pack")

    def test_the_repository_path_keeps_its_case(self):
        with mock.patch.object(repo_visibility.requests, "get", return_value=response(200)) as get:
            is_private("https://gitlab.example.org/Team/Proj")
        self.assertIn("/Team/Proj/info/refs", get.call_args[0][0])

    def test_a_refused_anonymous_read_means_private(self):
        for status in (401, 403, 404):
            with self.subTest(status=status):
                with mock.patch.object(repo_visibility.requests, "get", return_value=response(status)):
                    self.assertIs(is_private("https://gitlab.example.org/team/proj"), True)

    def test_a_sign_in_page_instead_of_an_advertisement_means_private(self):
        with mock.patch.object(repo_visibility.requests, "get", return_value=response(200, "text/html")):
            self.assertIs(is_private("https://gitlab.example.org/team/proj"), True)

    def test_a_host_that_cannot_be_asked_gives_no_answer(self):
        with mock.patch.object(repo_visibility.requests, "get", side_effect=requests.ConnectionError("down")):
            self.assertIsNone(is_private("https://gitlab.example.org/team/proj"))
        with mock.patch.object(repo_visibility.requests, "get", return_value=response(503)):
            self.assertIsNone(is_private("https://gitlab.example.org/team/proj"))

    def test_urls_that_are_not_http_are_not_asked(self):
        with mock.patch.object(repo_visibility.requests, "get") as get:
            self.assertIsNone(is_private("git@gitlab.example.org:team/proj.git"))
            self.assertIsNone(is_private("ssh://gitlab.example.org/team/proj"))
        get.assert_not_called()

    def test_user_information_in_the_url_is_not_sent_to_the_host(self):
        with mock.patch.object(repo_visibility.requests, "get", return_value=response(200)) as get:
            is_private("https://user:secret@gitlab.example.org/team/proj")
        self.assertNotIn("secret", get.call_args[0][0])
        self.assertIn("https://gitlab.example.org/team/proj/", get.call_args[0][0])


class TestCheckReadable(TestCase):
    def test_a_public_repository_is_accepted_without_credentials(self):
        with mock.patch.object(repo_visibility, "is_private", return_value=False), \
                mock.patch.object(repo_visibility.store, "find_credential") as find:
            check_readable("c1", "https://gitlab.example.org/team/proj")
        find.assert_not_called()

    def test_a_host_that_cannot_be_asked_is_accepted(self):
        with mock.patch.object(repo_visibility, "is_private", return_value=None):
            check_readable("c1", "https://gitlab.example.org/team/proj")

    def test_a_private_repository_without_credentials_is_refused_naming_the_host(self):
        with mock.patch.object(repo_visibility, "is_private", return_value=True), \
                mock.patch.object(repo_visibility.store, "find_credential", return_value=None):
            with self.assertRaises(errors.bad_request.ValidationError) as ctx:
                check_readable("c1", "https://gitlab.example.org/team/proj")
        self.assertIn("gitlab.example.org", str(ctx.exception))
        self.assertIn("git_credentials.set", str(ctx.exception))

    def test_a_private_repository_with_stored_credentials_is_accepted(self):
        with mock.patch.object(repo_visibility, "is_private", return_value=True), \
                mock.patch.object(repo_visibility.store, "find_credential", return_value=object()):
            check_readable("c1", "https://gitlab.example.org/team/proj")
