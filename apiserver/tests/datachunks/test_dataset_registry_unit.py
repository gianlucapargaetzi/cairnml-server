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
