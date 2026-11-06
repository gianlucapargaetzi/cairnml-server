import json
from types import SimpleNamespace
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

    def test_a_registry_entry_shows_its_stored_metadata_and_reads_nothing(self):
        entry = SimpleNamespace(name="cifar10", ref="acme/registry/cifar10", source="huggingface",
                                link="hf://datasets/uoft-cs/cifar10",
                                metadata=json.dumps({"modalities": {"image": "image"}, "labels": {"class": "class"},
                                                     "splits": {"train": 50000}, "samples": None}))
        details = bll.details(None, repository=None, registry_entry=entry, commit=None)
        self.assertEqual(details["kind"], "registry")
        self.assertEqual(details["registry"], "acme/registry")
        self.assertEqual(details["source"], "huggingface")
        self.assertEqual(details["metadata"]["splits"], {"train": 50000})
        self.assertEqual(details["commits"], [])

    def test_a_registry_entry_without_metadata_shows_none(self):
        entry = SimpleNamespace(name="cars", ref="acme/registry/cars", source="web", link="https://example.org/cars",
                                metadata="")
        self.assertIsNone(bll.registry_details(entry)["metadata"])

    def test_owner_and_name_are_accepted_as_well(self):
        app = FakeApp(files={("head-sha", "DATASET.md"): b"text"})
        details = bll.details(app, repository="acme/greenhouse", registry_entry=None, commit=None)
        self.assertEqual(details["repository"], "acme/greenhouse")

    def test_a_repository_must_be_on_github(self):
        with self.assertRaises(errors.bad_request.ValidationError):
            bll.details(FakeApp(), repository="https://example.org/x/y", registry_entry=None, commit=None)
