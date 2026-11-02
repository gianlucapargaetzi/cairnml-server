"""Registering a dataset in the registry: what the server checks first, and that it happens once.

Run inside the server image: python -m unittest apiserver.tests.datachunks.test_dataset_registry_unit
"""

import json
from unittest import TestCase

from apiserver.apierrors import errors
from cairnml.datasets import footprint as fp
from apiserver.bll import dataset_registry as bll
from apiserver.idp.github import GitHubError, RepoNotAccessible

DVC = "outs:\n- md5: 465a62c55506cd2fa10372c1f9ba4b5e.dir\n  size: 2048\n  nfiles: 3\n  hash: md5\n  path: data\n"
HEAD = "a" * 40


def good_files(dvc=DVC):
    lock = fp.build_lock(fp.parse_dvc_file(DVC), {"kind": "native"}, 3, {"image": "image"}, {"class": "class"}, "2.1.13")
    return {fp.DVC_FILE: dvc.encode(), fp.CONFIG_FILE: b"format: native\n", fp.LOCK_FILE: json.dumps(lock).encode()}


ADAPTER = b"class Adapter:\n    pass\n"


def local_files(adapter=ADAPTER, lock_adapter=ADAPTER):
    """A local dataset: dataset.yaml says adapter: local, and the lock was checked with ``lock_adapter``."""
    fmt = {"kind": "local", "name": "local", "adapter_sha256": fp.sha256_bytes(lock_adapter)}
    lock = fp.build_lock(fp.parse_dvc_file(DVC), fmt, 3, {"image": "image"}, {"class": "class"}, "2.1.13")
    files = {fp.DVC_FILE: DVC.encode(), fp.CONFIG_FILE: b"adapter: local\n", fp.LOCK_FILE: json.dumps(lock).encode()}
    if adapter is not None:
        files[fp.ADAPTER_FILE] = adapter
    return files


class FakeApp:
    """Just enough of the GitHub App client: one dataset repository and one registry."""

    def __init__(self, files=None, entries=None, pending=False, installed=True, write_ok=True):
        self.files = files if files is not None else good_files()
        self.entries = dict(entries or {})  # registry file name -> text
        self.pending = pending
        self.installed = installed
        self.write_ok = write_ok
        self.opened = []
        self.folder_reads = 0
        self.github_down = False
        self.tree_overrides = {}  # sizes that the tree lists on top of the files, for example a size GitHub reports
        self.reads = []  # every path read from the dataset repository, at the ref it was read at

    def default_branch_head(self, repo):
        if not self.installed:
            raise RepoNotAccessible(repo)
        return "main", HEAD

    def read_file(self, repo, path, ref=None):
        if repo == "acme/registry":
            return self.entries.get(path.split("/", 1)[1], None) and self.entries[path.split("/", 1)[1]].encode()
        self.reads.append((path, ref))
        return self.files.get(path)

    def tree_sizes(self, repo, ref):
        sizes = {"data.dvc": 120, "dataset.yaml": 20, "dataset.lock.json": 400}
        if self.files.get(fp.ADAPTER_FILE) is not None:
            sizes[fp.ADAPTER_FILE] = len(self.files[fp.ADAPTER_FILE])
        sizes.update(self.tree_overrides)
        return sizes

    def list_dir(self, repo, path, ref=None):
        return list(self.entries)

    def read_dir_texts(self, repo, path):
        self.folder_reads += 1
        if self.github_down:
            raise GitHubError("folder read failed: 502")
        return dict(self.entries)

    def branch_exists(self, repo, branch):
        return self.pending

    def open_registration_pull_request(self, repo, branch, path, content, message, title, body):
        if not self.write_ok:
            raise GitHubError("token request failed: 422")
        self.opened.append((repo, branch, path, content))
        return "https://github.com/acme/registry/pull/7"


def register(app, repository="https://github.com/acme/cars-data.git", name=None, owners=("acme",), registry="acme/registry"):
    return bll.register(app=app, repository=repository, name=name, registry=registry, allowed_owners=owners)


class TestRegister(TestCase):
    def test_a_dataset_with_a_valid_footprint_is_added_through_a_pull_request(self):
        app = FakeApp()
        result = register(app)
        self.assertEqual(result, {"name": "cars-data", "repository": "acme/cars-data",
                                  "pull_request": "https://github.com/acme/registry/pull/7"})
        repo, branch, path, content = app.opened[0]
        self.assertEqual((repo, branch, path), ("acme/registry", "register/cars-data", "datasets/cars-data.yaml"))
        self.assertIn("source: repo", content)
        self.assertIn("link: https://github.com/acme/cars-data", content)
        self.assertNotIn("adapter:", content)

    def test_a_footprint_that_does_not_hold_is_refused_and_nothing_is_opened(self):
        app = FakeApp(files=good_files(dvc=DVC.replace("2048", "4096")))
        with self.assertRaisesRegex(errors.bad_request.ValidationError, "footprint"):
            register(app)
        self.assertEqual(app.opened, [])

    def test_a_missing_lock_is_refused(self):
        files = good_files()
        files[fp.LOCK_FILE] = None
        with self.assertRaisesRegex(errors.bad_request.ValidationError, "dataset.lock.json is missing"):
            register(FakeApp(files=files))

    def test_registration_happens_once(self):
        with self.assertRaisesRegex(errors.bad_request.ValidationError, "already registered"):
            register(FakeApp(entries={"cars-data.yaml": "source: repo\nlink: https://github.com/acme/cars-data\n"}))

    def test_the_same_repository_under_another_name_is_refused(self):
        entries = {"old-name.yaml": "source: repo\nlink: https://github.com/Acme/Cars-Data\n"}
        with self.assertRaisesRegex(errors.bad_request.ValidationError, "already registered as old-name"):
            register(FakeApp(entries=entries), name="new-name")

    def test_a_registration_that_is_waiting_in_a_pull_request_is_not_repeated(self):
        with self.assertRaisesRegex(errors.bad_request.ValidationError, "waiting in a pull request"):
            register(FakeApp(pending=True))

    def test_owners_that_are_not_allowed_are_refused(self):
        with self.assertRaisesRegex(errors.bad_request.ValidationError, "may not be registered"):
            register(FakeApp(), repository="https://github.com/evil/cars-data")
        with self.assertRaisesRegex(errors.bad_request.ValidationError, "may not be registered"):
            register(FakeApp(), owners=())

    def test_only_github_repositories_are_accepted(self):
        with self.assertRaisesRegex(errors.bad_request.ValidationError, "github.com"):
            register(FakeApp(), repository="https://example.com/acme/cars-data")

    def test_a_repository_without_the_app_is_refused_by_name(self):
        with self.assertRaisesRegex(errors.bad_request.ValidationError, "not installed on acme/cars-data"):
            register(FakeApp(installed=False))

    def test_an_app_without_write_access_to_the_registry_says_what_is_missing(self):
        with self.assertRaisesRegex(errors.bad_request.ValidationError, "needs write access"):
            register(FakeApp(write_ok=False))

    def test_a_server_without_a_registry_does_not_register(self):
        with self.assertRaisesRegex(errors.bad_request.ValidationError, "not configured"):
            register(FakeApp(), registry="")

    def test_names_are_checked(self):
        self.assertEqual(bll.entry_name("acme/Cars-Data.git", None), "cars-data")
        with self.assertRaises(errors.bad_request.ValidationError):
            bll.entry_name("acme/x", "Bad Name!")


class TestGoldenVector(TestCase):
    def test_the_checksum_of_a_fixed_lock_is_the_same_in_the_sdk_and_on_the_server(self):
        dvc = fp.parse_dvc_file(DVC)
        lock = fp.build_lock(dvc, {"kind": "native"}, 3, {"image": "image"}, {"class": "class"}, "2.1.13")
        self.assertEqual(lock["footprint"], "275fe1c61757148538b8b2c964aac9c60daca857b04b8fce4e0dbf037118d2f4")


class TestAllowedRepositories(TestCase):
    """The registry is the only list of dataset repositories: a job gets a token for the registry and its entries."""

    def setUp(self):
        bll.clear_allowed_cache()

    def test_the_registry_itself_and_the_repositories_of_its_own_entries_are_allowed(self):
        app = FakeApp(entries={
            "cars.yaml": "source: repo\nlink: https://github.com/Acme/Cars-Data\n",
            "coco.yaml": "source: huggingface\nlink: hf://datasets/org/coco\nadapter: coco\n",
            "notes.txt": "source: repo\nlink: https://github.com/acme/not-an-entry\n",
        })
        self.assertEqual(bll.allowed_repositories(app, "acme/registry"), {"acme/registry", "acme/cars-data"})

    def test_no_registry_means_nothing_is_allowed(self):
        self.assertEqual(bll.allowed_repositories(FakeApp(), ""), set())

    def test_the_whole_folder_is_read_in_one_request_however_many_entries_there_are(self):
        app = FakeApp(entries={f"d{i}.yaml": f"source: repo\nlink: https://github.com/acme/d{i}\n" for i in range(40)})
        self.assertEqual(len(bll.allowed_repositories(app, "acme/registry")), 41)
        self.assertEqual(app.folder_reads, 1)

    def test_concurrent_callers_refresh_the_answer_once(self):
        import threading

        app = FakeApp(entries={"cars.yaml": "source: repo\nlink: https://github.com/acme/cars-data\n"})
        results = []
        threads = [threading.Thread(target=lambda: results.append(bll.allowed_repositories(app, "acme/registry")))
                   for _ in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(app.folder_reads, 1)
        self.assertTrue(all(result == results[0] for result in results))

    def test_when_github_cannot_be_reached_the_last_answer_keeps_being_used(self):
        app = FakeApp(entries={"cars.yaml": "source: repo\nlink: https://github.com/acme/cars-data\n"})
        now = [1000.0]
        first = bll.allowed_repositories(app, "acme/registry", clock=lambda: now[0])
        app.github_down = True
        now[0] += 61
        self.assertEqual(bll.allowed_repositories(app, "acme/registry", clock=lambda: now[0]), first)
        reads = app.folder_reads
        now[0] += 5  # still inside the short retry pause: GitHub is not asked again
        self.assertEqual(bll.allowed_repositories(app, "acme/registry", clock=lambda: now[0]), first)
        self.assertEqual(app.folder_reads, reads)

    def test_without_an_earlier_answer_a_github_failure_is_raised(self):
        app = FakeApp()
        app.github_down = True
        with self.assertRaises(GitHubError):
            bll.allowed_repositories(app, "acme/registry")

    def test_a_new_entry_is_seen_after_a_minute(self):
        app = FakeApp(entries={"cars.yaml": "source: repo\nlink: https://github.com/acme/cars-data\n"})
        now = [1000.0]
        first = bll.allowed_repositories(app, "acme/registry", clock=lambda: now[0])
        app.entries["more.yaml"] = "source: repo\nlink: https://github.com/acme/more-data\n"
        self.assertEqual(bll.allowed_repositories(app, "acme/registry", clock=lambda: now[0]), first)
        now[0] += 61
        self.assertIn("acme/more-data", bll.allowed_repositories(app, "acme/registry", clock=lambda: now[0]))


class TestListEntries(TestCase):
    def test_every_entry_is_listed_with_its_source_and_link_in_name_order(self):
        entries = {
            "coco-fashion.yaml": "source: huggingface\nlink: hf://datasets/org/coco\nadapter: coco_det\n",
            "cars-data.yaml": "# a comment\nsource: repo\nlink: https://github.com/acme/cars-data\n",
            "README.md": "not an entry",
        }
        result = bll.list_entries(app=FakeApp(entries=entries), registry="acme/registry")
        self.assertEqual(result, [
            {"name": "cars-data", "source": "repo", "link": "https://github.com/acme/cars-data"},
            {"name": "coco-fashion", "source": "huggingface", "link": "hf://datasets/org/coco"},
        ])

    def test_an_unset_registry_lists_nothing(self):
        self.assertEqual(bll.list_entries(app=FakeApp(entries={"a.yaml": "source: repo\n"}), registry=""), [])

    def test_github_being_down_is_raised_to_the_caller(self):
        app = FakeApp()
        app.github_down = True
        with self.assertRaises(GitHubError):
            bll.list_entries(app=app, registry="acme/registry")


class TestLocalAdapter(TestCase):
    """A dataset with adapter: local: the server reads adapter.py at the registered commit and the footprint checks its hash."""

    def test_a_local_dataset_whose_adapter_has_the_checked_hash_is_registered(self):
        app = FakeApp(files=local_files())
        result = register(app)
        self.assertEqual(result["name"], "cars-data")
        self.assertEqual(len(app.opened), 1)
        self.assertIn((fp.ADAPTER_FILE, HEAD), app.reads)

    def test_a_changed_adapter_is_refused_and_the_message_names_it(self):
        app = FakeApp(files=local_files(adapter=b"class Adapter:\n    changed = True\n", lock_adapter=ADAPTER))
        with self.assertRaisesRegex(errors.bad_request.ValidationError, "adapter.py"):
            register(app)
        self.assertEqual(app.opened, [])

    def test_a_missing_adapter_is_refused_and_the_message_names_it(self):
        app = FakeApp(files=local_files(adapter=None))
        with self.assertRaisesRegex(errors.bad_request.ValidationError, "adapter.py is missing"):
            register(app)
        self.assertEqual(app.opened, [])

    def test_an_adapter_over_the_size_limit_is_refused_before_it_is_read(self):
        big = b"#" * (bll.ADAPTER_MAX_BYTES + 1)
        app = FakeApp(files=local_files(adapter=big))
        with self.assertRaisesRegex(errors.bad_request.ValidationError, "adapter.py .* may be at most"):
            register(app)
        self.assertNotIn((fp.ADAPTER_FILE, HEAD), app.reads)
        self.assertEqual(app.opened, [])

    def test_an_adapter_that_the_tree_lists_small_but_that_is_large_is_refused(self):
        app = FakeApp(files=local_files(adapter=b"#" * (bll.ADAPTER_MAX_BYTES + 1)))
        app.tree_overrides = {fp.ADAPTER_FILE: 10}
        with self.assertRaisesRegex(errors.bad_request.ValidationError, "adapter.py .* may be at most"):
            register(app)
        self.assertEqual(app.opened, [])

    def test_an_adapter_at_the_size_limit_is_read(self):
        app = FakeApp(files=local_files(adapter=b"#" * bll.ADAPTER_MAX_BYTES, lock_adapter=b"#" * bll.ADAPTER_MAX_BYTES))
        register(app)
        self.assertEqual(len(app.opened), 1)

    def test_a_native_dataset_does_not_read_an_adapter(self):
        app = FakeApp()
        register(app)
        self.assertEqual([path for path, _ref in app.reads if path == fp.ADAPTER_FILE], [])
        self.assertEqual(len(app.opened), 1)

    def test_a_registry_adapter_dataset_does_not_read_an_adapter(self):
        files = good_files()
        lock = fp.build_lock(fp.parse_dvc_file(DVC), {"kind": "adapter", "name": "coco", "registry": "acme/adapters",
                                                      "registry_commit": "b" * 40}, 3, {"image": "image"},
                             {"class": "class"}, "2.1.13")
        files[fp.CONFIG_FILE] = b"adapter: coco\nregistry: acme/adapters\n"
        files[fp.LOCK_FILE] = json.dumps(lock).encode()
        app = FakeApp(files=files)
        register(app)
        self.assertEqual([path for path, _ref in app.reads if path == fp.ADAPTER_FILE], [])
        self.assertEqual(len(app.opened), 1)

    def test_a_native_dataset_with_an_adapter_at_the_top_is_refused_as_before(self):
        files = good_files()
        files[fp.ADAPTER_FILE] = ADAPTER
        with self.assertRaisesRegex(errors.bad_request.ValidationError, "adapter.py is tracked by git"):
            register(FakeApp(files=files))
