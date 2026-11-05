"""The public dataset registries: what the server lists from them, and that a dataset of our own is not in one.

Run inside the server image: python -m unittest apiserver.tests.datachunks.test_dataset_registry_unit
"""

from unittest import TestCase

from apiserver.bll import dataset_registry as bll
from apiserver.idp.github import GitHubError


class FakeApp:
    """Just enough of the GitHub App client: one registry folder."""

    def __init__(self, entries=None):
        self.entries = dict(entries or {})  # registry file name -> text
        self.github_down = False

    def read_dir_texts(self, repo, path):
        if self.github_down:
            raise GitHubError("folder read failed: 502")
        return dict(self.entries)


class TestListEntries(TestCase):
    def test_every_public_entry_is_listed_with_its_source_and_link_in_name_order(self):
        entries = {
            "coco-fashion.yaml": "source: huggingface\nlink: hf://datasets/org/coco\nadapter: coco_det\n",
            "cars.yaml": "# a comment\nsource: web\nlink: https://example.org/cars\n",
            "README.md": "not an entry",
        }
        result = bll.list_entries(app=FakeApp(entries=entries), registry="acme/public-datasets")
        self.assertEqual(result, [
            {"name": "cars", "source": "web", "link": "https://example.org/cars"},
            {"name": "coco-fashion", "source": "huggingface", "link": "hf://datasets/org/coco"},
        ])

    def test_an_entry_with_source_repo_is_ignored_with_a_logged_warning(self):
        entries = {
            "cars-data.yaml": "source: repo\nlink: https://github.com/acme/cars-data\n",
            "cars.yaml": "source: web\nlink: https://example.org/cars\n",
        }
        with self.assertLogs("apiserver.bll.dataset_registry", level="WARNING") as logs:
            result = bll.list_entries(app=FakeApp(entries=entries), registry="acme/public-datasets")
        self.assertEqual([entry["name"] for entry in result], ["cars"])
        self.assertIn("cars-data", "".join(logs.output))

    def test_an_unset_registry_lists_nothing(self):
        self.assertEqual(bll.list_entries(app=FakeApp(entries={"a.yaml": "source: web\n"}), registry=""), [])

    def test_github_being_down_is_raised_to_the_caller(self):
        app = FakeApp()
        app.github_down = True
        with self.assertRaises(GitHubError):
            bll.list_entries(app=app, registry="acme/public-datasets")

    def test_registration_is_gone_from_the_registry_module(self):
        for name in ("register", "allowed_repositories", "clear_allowed_cache", "entry_name", "entry_text"):
            self.assertFalse(hasattr(bll, name), name)


class TestNormalizeRepository(TestCase):
    def test_owner_and_name_are_kept(self):
        self.assertEqual(bll.normalize_repository("acme/public-datasets"), "acme/public-datasets")

    def test_a_github_address_is_reduced_to_owner_and_name(self):
        self.assertEqual(bll.normalize_repository("https://github.com/acme/public-datasets.git/"), "acme/public-datasets")
        self.assertEqual(bll.normalize_repository("  github.com/acme/public-datasets  "), "acme/public-datasets")

    def test_anything_else_is_refused(self):
        for text in ("", "acme", "acme/a/b", "acme/../x", "https://gitlab.com/acme/x", "acme/ x",
                     "git@gitlab.com:acme/x.git", "https://github.com/acme", "https://github.com/acme/x/tree/main"):
            self.assertIsNone(bll.normalize_repository(text), text)

    def test_every_github_url_form_names_the_same_repository(self):
        for text in (
            "https://github.com/acme/public-datasets.git",
            "https://github.com/acme/public-datasets",
            "http://www.github.com/acme/public-datasets/",
            "git@github.com:acme/public-datasets.git",
            "git@github.com:acme/public-datasets",
            "ssh://git@github.com/acme/public-datasets.git",
            "github.com/acme/public-datasets",
            "acme/public-datasets",
        ):
            self.assertEqual(bll.normalize_repository(text), "acme/public-datasets", text)

    def test_the_canonical_address_is_the_https_one(self):
        self.assertEqual(bll.canonical_url("acme/public-datasets"), "https://github.com/acme/public-datasets")


class TestPublicReader(TestCase):
    """Without a GitHub App the server still reads public repositories, and refuses private ones."""

    def test_a_public_repository_is_read_without_a_token(self):
        from apiserver.idp.github import PublicReader, _ANONYMOUS

        reader = PublicReader("https://api.github.com")
        reader._is_public = lambda repo: True
        self.assertEqual(reader._read_headers("acme/public-datasets"), _ANONYMOUS)

    def test_a_private_repository_is_refused_as_not_accessible(self):
        from apiserver.idp.github import PublicReader, RepoNotAccessible

        reader = PublicReader("https://api.github.com")
        reader._is_public = lambda repo: False
        with self.assertRaises(RepoNotAccessible):
            reader._read_headers("acme/private-datasets")
