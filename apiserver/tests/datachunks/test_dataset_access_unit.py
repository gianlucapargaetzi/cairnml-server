"""Which installed repositories are datasets, and whether a dataset's footprint holds when it is read or listed.

Run inside the server image: python -m unittest apiserver.tests.datachunks.test_dataset_access_unit
"""

from types import SimpleNamespace
from unittest import TestCase

from apiserver.apierrors import errors
from apiserver.bll import dataset_access as bll
from apiserver.idp.github import GitHubError
from apiserver.tests.datachunks.dataset_fakes import ADAPTER, DVC, HEAD, FakeGitHub, good_files, local_files
from cairnml.datasets import footprint as fp

COARS = "acme/cars"


class TestGoldenVector(TestCase):
    def test_the_checksum_of_a_fixed_lock_is_the_same_in_the_sdk_and_on_the_server(self):
        dvc = fp.parse_dvc_file(DVC)
        lock = fp.build_lock(dvc, {"kind": "native"}, 3, {"image": "image"}, {"class": "class"}, "2.1.13")
        self.assertEqual(lock["footprint"], "275fe1c61757148538b8b2c964aac9c60daca857b04b8fce4e0dbf037118d2f4")


class TestVerifyDataset(TestCase):
    def test_a_dataset_with_a_valid_footprint_is_verified_at_its_head(self):
        branch, head, lock = bll.verify_dataset(FakeGitHub({COARS: good_files()}), COARS)
        self.assertEqual((branch, head), ("main", HEAD))
        self.assertEqual(lock["samples"], 3)

    def test_a_repository_without_dataset_yaml_is_not_a_dataset_and_the_message_says_so(self):
        files = good_files()
        files[fp.CONFIG_FILE] = None
        with self.assertRaises(bll.NotADataset) as ctx:
            bll.verify_dataset(FakeGitHub({COARS: files}), COARS)
        self.assertIn(
            "acme/cars is not a CairnML dataset: dataset.yaml is missing on its default branch", ctx.exception.msg)

    def test_a_repository_the_app_is_not_installed_on_is_refused_by_name(self):
        with self.assertRaisesRegex(errors.bad_request.ValidationError, "not installed on acme/cars"):
            bll.verify_dataset(FakeGitHub({}), COARS)

    def test_a_footprint_that_does_not_hold_is_refused(self):
        files = good_files(dvc=DVC.replace("2048", "4096"))
        with self.assertRaisesRegex(errors.bad_request.ValidationError, "footprint"):
            bll.verify_dataset(FakeGitHub({COARS: files}), COARS)

    def test_a_missing_lock_is_refused(self):
        files = good_files()
        files[fp.LOCK_FILE] = None
        with self.assertRaisesRegex(errors.bad_request.ValidationError, "dataset.lock.json is missing"):
            bll.verify_dataset(FakeGitHub({COARS: files}), COARS)

    def test_a_github_error_while_reading_is_refused_with_the_repository_named(self):
        github = FakeGitHub({COARS: good_files()})
        github.failing.add(COARS)
        with self.assertRaisesRegex(errors.bad_request.ValidationError, "GitHub could not be read for acme/cars"):
            bll.verify_dataset(github, COARS)


class TestLocalAdapter(TestCase):
    """A dataset with adapter: local: the adapter.py at the head is read, and the footprint checks its hash."""

    def test_a_local_dataset_whose_adapter_has_the_checked_hash_is_verified(self):
        github = FakeGitHub({COARS: local_files()})
        bll.verify_dataset(github, COARS)
        self.assertIn((COARS, fp.ADAPTER_FILE, HEAD), github.reads)

    def test_a_changed_adapter_is_refused_and_the_message_names_it(self):
        files = local_files(adapter=b"class Adapter:\n    changed = True\n", lock_adapter=ADAPTER)
        with self.assertRaisesRegex(errors.bad_request.ValidationError, "adapter.py"):
            bll.verify_dataset(FakeGitHub({COARS: files}), COARS)

    def test_a_missing_adapter_is_refused_and_the_message_names_it(self):
        with self.assertRaisesRegex(errors.bad_request.ValidationError, "adapter.py is missing"):
            bll.verify_dataset(FakeGitHub({COARS: local_files(adapter=None)}), COARS)

    def test_an_adapter_over_the_size_limit_is_refused_before_it_is_read(self):
        big = b"#" * (bll.ADAPTER_MAX_BYTES + 1)
        github = FakeGitHub({COARS: local_files(adapter=big)})
        with self.assertRaisesRegex(errors.bad_request.ValidationError, "adapter.py .* may be at most"):
            bll.verify_dataset(github, COARS)
        self.assertNotIn((COARS, fp.ADAPTER_FILE, HEAD), github.reads)

    def test_an_adapter_the_tree_lists_small_but_that_is_large_is_refused(self):
        github = FakeGitHub({COARS: local_files(adapter=b"#" * (bll.ADAPTER_MAX_BYTES + 1))})
        github.tree_overrides = {fp.ADAPTER_FILE: 10}
        with self.assertRaisesRegex(errors.bad_request.ValidationError, "adapter.py .* may be at most"):
            bll.verify_dataset(github, COARS)

    def test_a_native_dataset_does_not_read_an_adapter(self):
        github = FakeGitHub({COARS: good_files()})
        bll.verify_dataset(github, COARS)
        self.assertEqual([path for _repo, path, _ref in github.reads if path == fp.ADAPTER_FILE], [])


class TestListDatasets(TestCase):
    def test_only_installed_repositories_with_a_dataset_yaml_and_a_holding_footprint_are_listed(self):
        github = FakeGitHub({
            "acme/cars": good_files(),
            "acme/notes": {"README.md": b"not a dataset"},
            "acme/broken": good_files(dvc=DVC.replace("2048", "4096")),
        })
        with self.assertLogs("apiserver.bll.dataset_access", level="WARNING") as logs:
            datasets = bll.list_datasets(github)
        self.assertEqual([d["repository"] for d in datasets], ["acme/cars"])
        self.assertIn("acme/broken", "".join(logs.output))

    def test_each_dataset_has_its_head_commit_branch_and_footprint_summary(self):
        github = FakeGitHub({"acme/cars": good_files(labels={"class": "class", "color": "text"})})
        dataset, = bll.list_datasets(github)
        self.assertEqual(dataset["repository"], "acme/cars")
        self.assertEqual(dataset["default_branch"], "main")
        self.assertEqual(dataset["head_commit"], HEAD)
        self.assertEqual(dataset["footprint"], {"format": "native", "samples": 3,
                                                "labels": {"class": "class", "color": "text"}})

    def test_the_list_is_sorted_by_repository_name(self):
        github = FakeGitHub({"acme/zeta": good_files(), "Acme/alpha": good_files(), "acme/mid": good_files()})
        self.assertEqual([d["repository"] for d in bll.list_datasets(github)],
                         ["Acme/alpha", "acme/mid", "acme/zeta"])

    def test_a_repository_that_cannot_be_read_now_is_left_out_and_the_others_are_listed(self):
        github = FakeGitHub({"acme/cars": good_files(), "acme/down": good_files()})
        github.failing.add("acme/down")
        with self.assertLogs("apiserver.bll.dataset_access", level="WARNING"):
            self.assertEqual([d["repository"] for d in bll.list_datasets(github)], ["acme/cars"])

    def test_no_installed_repository_gives_an_empty_list(self):
        self.assertEqual(bll.list_datasets(FakeGitHub({})), [])


class TestDatasetDescription(TestCase):
    def test_dataset_md_is_returned_when_the_repository_has_one(self):
        github = FakeGitHub({COARS: {**good_files(), "DATASET.md": b"# Cars\n\nPhotos of cars.\n"}})
        dataset, = bll.list_datasets(github)
        self.assertEqual(dataset["description"], "# Cars\n\nPhotos of cars.\n")
        self.assertFalse(dataset["description_truncated"])

    def test_dataset_md_is_capped_at_20_kb(self):
        text = b"x" * (30 * 1024)
        github = FakeGitHub({COARS: {**good_files(), "DATASET.md": text}})
        dataset, = bll.list_datasets(github)
        self.assertEqual(len(dataset["description"].encode()), bll.DESCRIPTION_MAX_BYTES)
        self.assertEqual(dataset["description"], "x" * bll.DESCRIPTION_MAX_BYTES)
        self.assertTrue(dataset["description_truncated"])

    def test_dataset_md_of_exactly_20_kb_is_not_marked_as_truncated(self):
        github = FakeGitHub({COARS: {**good_files(), "DATASET.md": b"y" * bll.DESCRIPTION_MAX_BYTES}})
        dataset, = bll.list_datasets(github)
        self.assertEqual(len(dataset["description"]), bll.DESCRIPTION_MAX_BYTES)
        self.assertFalse(dataset["description_truncated"])

    def test_a_missing_dataset_md_is_not_an_error(self):
        dataset, = bll.list_datasets(FakeGitHub({COARS: good_files()}))
        self.assertIsNone(dataset["description"])
        self.assertFalse(dataset["description_truncated"])

    def test_dataset_md_is_read_at_the_head_commit_of_the_default_branch(self):
        github = FakeGitHub({COARS: {**good_files(), "DATASET.md": b"text"}})
        bll.list_datasets(github)
        self.assertIn((COARS, "DATASET.md", HEAD), github.reads)


class TestDatasetsListEndpoint(TestCase):
    """datasets.list: the service answers with the datasets of the App, and an empty list without an App."""

    def call(self):
        return SimpleNamespace(result=SimpleNamespace(data=None))

    def test_the_endpoint_lists_the_datasets_of_the_app(self):
        from apiserver.services import datasets as service

        github = FakeGitHub({COARS: good_files(), "acme/notes": {"README.md": b"x"}})
        original = service.build_from_config
        service.build_from_config = lambda: github
        try:
            call = self.call()
            service.list_datasets(call, "company", None)
        finally:
            service.build_from_config = original
        self.assertEqual([d["repository"] for d in call.result.data["datasets"]], [COARS])

    def test_without_the_app_no_dataset_is_listed(self):
        from apiserver.services import datasets as service

        original = service.build_from_config
        service.build_from_config = lambda: None
        try:
            call = self.call()
            service.list_datasets(call, "company", None)
        finally:
            service.build_from_config = original
        self.assertEqual(call.result.data, {"datasets": []})

    def test_a_github_failure_while_listing_is_a_bad_request(self):
        from apiserver.services import datasets as service

        class Down(FakeGitHub):
            def installed_repositories(self):
                raise GitHubError("installations listing failed: 502")

        original = service.build_from_config
        service.build_from_config = lambda: Down()
        try:
            with self.assertRaisesRegex(errors.bad_request.ValidationError, "GitHub request failed"):
                service.list_datasets(self.call(), "company", None)
        finally:
            service.build_from_config = original
