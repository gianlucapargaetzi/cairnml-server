"""Which installed repositories are datasets, and whether a dataset's footprint holds when it is read or listed.

Run inside the server image: python -m unittest apiserver.tests.datachunks.test_dataset_access_unit
"""

from unittest import TestCase

from apiserver.apierrors import errors
from apiserver.bll import dataset_access as bll
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


class TestDatasetDescription(TestCase):
    def test_dataset_md_is_returned_when_the_repository_has_one(self):
        github = FakeGitHub({COARS: {**good_files(), "DATASET.md": b"# Cars\n\nPhotos of cars.\n"}})
        dataset = bll.dataset_entry(github, COARS)
        self.assertEqual(dataset["description"], "# Cars\n\nPhotos of cars.\n")
        self.assertFalse(dataset["description_truncated"])

    def test_dataset_md_is_capped_at_20_kb(self):
        text = b"x" * (30 * 1024)
        github = FakeGitHub({COARS: {**good_files(), "DATASET.md": text}})
        dataset = bll.dataset_entry(github, COARS)
        self.assertEqual(len(dataset["description"].encode()), bll.DESCRIPTION_MAX_BYTES)
        self.assertEqual(dataset["description"], "x" * bll.DESCRIPTION_MAX_BYTES)
        self.assertTrue(dataset["description_truncated"])

    def test_dataset_md_of_exactly_20_kb_is_not_marked_as_truncated(self):
        github = FakeGitHub({COARS: {**good_files(), "DATASET.md": b"y" * bll.DESCRIPTION_MAX_BYTES}})
        dataset = bll.dataset_entry(github, COARS)
        self.assertEqual(len(dataset["description"]), bll.DESCRIPTION_MAX_BYTES)
        self.assertFalse(dataset["description_truncated"])

    def test_a_missing_dataset_md_is_not_an_error(self):
        dataset = bll.dataset_entry(FakeGitHub({COARS: good_files()}), COARS)
        self.assertIsNone(dataset["description"])
        self.assertFalse(dataset["description_truncated"])

    def test_dataset_md_is_read_at_the_head_commit_of_the_default_branch(self):
        github = FakeGitHub({COARS: {**good_files(), "DATASET.md": b"text"}})
        bll.dataset_entry(github, COARS)
        self.assertIn((COARS, "DATASET.md", HEAD), github.reads)
