import json
from unittest import TestCase

from apiserver.apierrors import errors
from apiserver.bll import dataset_details as bll


class FakeApp:
    """The part of the GitHub App client that the dataset details read."""

    def __init__(self, files=None, commits=None):
        self.files = files or {}
        self.commit_list = commits or []

    def default_branch_head(self, repo):
        return "main", "head-sha"

    def commit_exists(self, repo, sha):
        return sha in ("head-sha", "old-sha")

    def read_file(self, repo, path, ref=None):
        return self.files.get((ref or "head-sha", path))

    def list_commits(self, repo, ref, limit):
        return self.commit_list[:limit]


class TestDatasetDetails(TestCase):
    def test_an_own_dataset_shows_its_manifest_description_and_commits(self):
        manifest = json.dumps({"modalities": {"table": "table"}, "labels": {"energy": "json"},
                               "splits": {"train": 2000, "test": 500}, "samples": 2500}).encode()
        app = FakeApp(
            files={("head-sha", "manifest.json"): manifest, ("head-sha", "DATASET.md"): b"# Greenhouse"},
            commits=[{"sha": "head-sha", "message": "Noise", "author": "you", "date": "2026-10-30", "parents": []}],
        )
        details = bll.own_details(app, "acme/greenhouse", None)
        self.assertEqual(details["kind"], "own")
        self.assertEqual(details["name"], "greenhouse")
        self.assertEqual(details["revision"], "head-sha")
        self.assertEqual(details["metadata"]["splits"], {"train": 2000, "test": 500})
        self.assertEqual(details["metadata"]["samples"], 2500)
        self.assertEqual(details["description"], "# Greenhouse")
        self.assertEqual(details["commits"][0]["sha"], "head-sha")

    def test_missing_manifest_and_description_are_null(self):
        details = bll.own_details(FakeApp(), "acme/plain", None)
        self.assertIsNone(details["metadata"])
        self.assertIsNone(details["description"])

    def test_a_commit_selects_the_revision_and_must_exist(self):
        files = {("old-sha", "DATASET.md"): b"old text"}
        details = bll.own_details(FakeApp(files=files), "acme/greenhouse", "old-sha")
        self.assertEqual(details["revision"], "old-sha")
        self.assertEqual(details["description"], "old text")
        with self.assertRaises(errors.bad_request.ValidationError):
            bll.own_details(FakeApp(), "acme/greenhouse", "unknown")

    def test_an_invalid_manifest_is_refused(self):
        app = FakeApp(files={("head-sha", "manifest.json"): b"not json"})
        with self.assertRaises(errors.bad_request.ValidationError):
            bll.own_details(app, "acme/greenhouse", None)

    def test_a_registry_dataset_reads_its_metadata_file(self):
        original = bll.registry_bll.list_entries
        bll.registry_bll.list_entries = lambda app, registry: [
            {"name": "cifar10", "source": "huggingface", "link": "hf://datasets/uoft-cs/cifar10"}]
        try:
            app = FakeApp(files={("head-sha", "metadata/cifar10.json"): json.dumps(
                {"modalities": {"image": "image"}, "labels": {"class": "class"}, "splits": {"train": 50000}}).encode()})
            details = bll.registry_details(app, ["acme/registry"], "cifar10")
        finally:
            bll.registry_bll.list_entries = original
        self.assertEqual(details["kind"], "registry")
        self.assertEqual(details["source"], "huggingface")
        self.assertEqual(details["metadata"]["splits"], {"train": 50000})
        self.assertEqual(details["commits"], [])

    def test_owner_and_name_are_accepted_as_well(self):
        app = FakeApp(files={("head-sha", "DATASET.md"): b"text"})
        details = bll.details(app, repository="acme/greenhouse", registries=[], registry_name=None, commit=None)
        self.assertEqual(details["repository"], "acme/greenhouse")

    def test_a_dataset_is_found_in_a_later_registry_when_the_first_lacks_it(self):
        from apiserver.idp.github import GitHubError
        original = bll.registry_bll.list_entries

        def entries(app, registry):
            if registry == "acme/private":
                raise GitHubError("not accessible")
            if registry == "acme/public":
                return [{"name": "cars", "source": "web", "link": "https://example.org/cars"}]
            return []

        bll.registry_bll.list_entries = entries
        try:
            app = FakeApp(files={("head-sha", "metadata/cars.json"): json.dumps(
                {"modalities": {"image": "image"}, "labels": {}, "splits": {"train": 3}}).encode()})
            details = bll.registry_details(app, ["acme/private", "acme/public"], "cars")
        finally:
            bll.registry_bll.list_entries = original
        self.assertEqual(details["link"], "https://example.org/cars")

    def test_a_repository_must_be_on_github(self):
        with self.assertRaises(errors.bad_request.ValidationError):
            bll.details(FakeApp(), repository="https://example.org/x/y", registries=[], registry_name=None, commit=None)
