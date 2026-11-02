"""The cached dataset list: what datasets.list reads, the hourly rebuild, the refresh on a job token, and what a failure
keeps. The GitHub App is the fake from dataset_fakes; the snapshot is kept in the test database and the locks in Redis.
No test reaches the network.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import TestCase, mock

from apiserver.bll import dataset_cache as bll
from apiserver.database.model.settings import Settings
from apiserver.idp.github import GitHubError
from apiserver.services import datasets as service
from apiserver.tests.datachunks.dataset_fakes import DVC, FakeGitHub, good_files
from apiserver.tests.db import ensure_db
from cairnml.datasets import footprint as fp

COARS = "acme/cars"
OTHER = "acme/other"
REPOS = (COARS, OTHER)


def ago(**delta) -> str:
    return (datetime.now(timezone.utc) - timedelta(**delta)).isoformat()


def is_recent(stamp: str) -> bool:
    return datetime.fromisoformat(stamp) > datetime.now(timezone.utc) - timedelta(minutes=1)


class CacheTestCase(TestCase):
    def setUp(self):
        ensure_db()
        self.clear()
        self.addCleanup(self.clear)
        self.github = FakeGitHub({COARS: good_files(), OTHER: good_files()})

    @staticmethod
    def clear():
        Settings.objects(key=bll.SNAPSHOT_KEY).delete()
        bll._redis.delete(bll.LOCK_KEY, *(f"dataset_cache/repo/{repo}" for repo in REPOS))

    @staticmethod
    def store(**fields):
        """Writes the snapshot with some fields replaced, to set up a stale list or an old entry."""
        bll._write({**bll.read_snapshot(), **fields})

    def repository_entries(self):
        return {dataset["repository"]: dataset for dataset in bll.read_snapshot()["datasets"]}


class TestListReadsTheSnapshot(CacheTestCase):
    def test_an_empty_cache_lists_nothing_and_has_no_refresh_time(self):
        self.assertEqual(bll.cached_list(), {"datasets": [], "refreshed_at": None})

    def test_the_endpoint_answers_from_the_snapshot_and_makes_no_github_call(self):
        self.assertTrue(bll.refresh_if_due(self.github))
        call = SimpleNamespace(result=SimpleNamespace(data=None))
        with mock.patch("apiserver.idp.github.GitHubApp._send", side_effect=AssertionError("GitHub was called")), \
                mock.patch("apiserver.idp.github_check.build_from_config", side_effect=AssertionError("app built")):
            service.list_datasets(call, "company", None)
        self.assertEqual([d["repository"] for d in call.result.data["datasets"]], [COARS, OTHER])
        self.assertEqual(call.result.data["refreshed_at"], bll.read_snapshot()["refreshed_at"])
        self.assertIn("checked_at", call.result.data["datasets"][0])


class TestHourlyRebuild(CacheTestCase):
    def test_a_rebuild_of_the_whole_list_sets_refreshed_at_and_the_entries(self):
        self.assertTrue(bll.refresh_if_due(self.github))

        snapshot = bll.read_snapshot()
        self.assertIsNotNone(snapshot["refreshed_at"])
        self.assertEqual(snapshot["refreshed_at"], snapshot["attempted_at"])
        self.assertEqual([d["repository"] for d in snapshot["datasets"]], [COARS, OTHER])
        self.assertEqual(snapshot["datasets"][0]["head_commit"], "a" * 40)
        self.assertIsNotNone(snapshot["datasets"][0]["checked_at"])

    def test_a_list_rebuilt_within_the_hour_is_not_rebuilt(self):
        bll.refresh_if_due(self.github)
        reads = len(self.github.reads)

        self.assertFalse(bll.refresh_if_due(self.github))
        self.assertEqual(len(self.github.reads), reads)

    def test_a_list_whose_last_attempt_is_an_hour_old_is_rebuilt(self):
        self.store(attempted_at=ago(minutes=61), refreshed_at=ago(minutes=61))

        self.assertTrue(bll.refresh_if_due(self.github))
        self.assertTrue(is_recent(bll.read_snapshot()["refreshed_at"]))

    def test_a_second_process_does_not_refresh_while_the_lock_is_held(self):
        self.store(attempted_at=ago(hours=2))
        bll._redis.set(bll.LOCK_KEY, "another-process", ex=60)

        self.assertFalse(bll.refresh_if_due(self.github))
        self.assertEqual(self.github.reads, [])
        self.assertEqual(bll.read_snapshot()["datasets"], [])

    def test_the_lock_is_released_after_a_rebuild(self):
        bll.refresh_if_due(self.github)
        self.assertIsNone(bll._redis.get(bll.LOCK_KEY))


class TestRefreshOnDispatch(CacheTestCase):
    def test_a_repository_missing_from_the_list_is_refreshed(self):
        self.assertTrue(bll.refresh_repository(self.github, COARS))

        entry = self.repository_entries()[COARS]
        self.assertEqual(entry["repository"], COARS)
        self.assertIsNotNone(entry["checked_at"])

    def test_a_repository_older_than_an_hour_is_refreshed(self):
        self.github.repos[COARS]["DATASET.md"] = b"new text"
        self.store(datasets=[{**self.github_entry(COARS), "description": "old text", "checked_at": ago(hours=2)}])

        self.assertTrue(bll.refresh_repository(self.github, COARS))
        self.assertEqual(self.repository_entries()[COARS]["description"], "new text")

    def test_a_repository_checked_within_the_hour_is_not_refreshed(self):
        self.store(datasets=[{**self.github_entry(COARS), "checked_at": ago(minutes=10)}])
        self.github.reads.clear()

        self.assertFalse(bll.refresh_repository(self.github, COARS))
        self.assertEqual(self.github.reads, [])

    def test_a_repository_is_refreshed_at_most_once_in_five_minutes(self):
        self.assertTrue(bll.refresh_repository(self.github, COARS))
        self.store(datasets=[])  # as if the entry were gone again: the throttle still holds

        self.assertFalse(bll.refresh_repository(self.github, COARS))
        self.assertEqual(self.repository_entries(), {})

    def test_the_repository_can_be_refreshed_again_once_the_five_minutes_have_passed(self):
        bll.refresh_repository(self.github, COARS)
        self.store(datasets=[])
        bll._redis.delete(f"dataset_cache/repo/{COARS}")  # what the expiry of the key does after five minutes

        self.assertTrue(bll.refresh_repository(self.github, COARS))
        self.assertIn(COARS, self.repository_entries())

    def test_a_refresh_on_dispatch_does_not_move_the_time_of_the_whole_list(self):
        stamp = ago(minutes=30)
        self.store(refreshed_at=stamp)

        bll.refresh_repository(self.github, COARS)

        self.assertEqual(bll.read_snapshot()["refreshed_at"], stamp)

    def github_entry(self, repo):
        """The entry the rebuild writes for the repository, as the test's GitHub App sees it now (without checked_at)."""
        entries = {}
        bll._check_into(entries, self.github, repo, ago())
        return {key: value for key, value in entries[repo].items() if key != "checked_at"}


class TestFailuresKeepTheLastGoodEntries(CacheTestCase):
    def test_the_installations_that_cannot_be_read_keep_the_list_and_the_refreshed_time(self):
        bll.refresh_if_due(self.github)
        before = bll.read_snapshot()
        self.github.installed_repositories = mock.Mock(side_effect=GitHubError("installations listing failed: 502"))
        self.store(attempted_at=ago(hours=2))

        with self.assertLogs("apiserver.bll.dataset_cache", level="WARNING"):
            self.assertTrue(bll.refresh_if_due(self.github))

        after = bll.read_snapshot()
        self.assertEqual(after["datasets"], before["datasets"])
        self.assertEqual(after["refreshed_at"], before["refreshed_at"])
        self.assertTrue(is_recent(after["attempted_at"]))

    def test_a_repository_that_cannot_be_read_now_keeps_its_entry_and_the_time_of_the_list_does_not_move(self):
        bll.refresh_if_due(self.github)
        before = self.repository_entries()
        stamp = ago(hours=2)
        self.store(attempted_at=stamp, refreshed_at=stamp)
        self.github.failing.add(COARS)

        with self.assertLogs("apiserver.bll.dataset_cache", level="WARNING") as logs:
            self.assertFalse(bll.rebuild(self.github))

        self.assertIn("could not be checked", "".join(logs.output))
        self.assertEqual(self.repository_entries()[COARS], before[COARS])
        self.assertIn(OTHER, self.repository_entries())
        self.assertEqual(bll.read_snapshot()["refreshed_at"], stamp)

    def test_a_repository_whose_footprint_no_longer_holds_keeps_its_entry_and_the_list_still_counts_as_rebuilt(self):
        bll.refresh_if_due(self.github)
        before = self.repository_entries()[COARS]
        self.github.repos[COARS][fp.DVC_FILE] = DVC.replace("2048", "4096").encode()

        with self.assertLogs("apiserver.bll.dataset_cache", level="WARNING"):
            self.assertTrue(bll.rebuild(self.github))

        self.assertEqual(self.repository_entries()[COARS], before)

    def test_a_repository_confirmed_not_to_be_a_dataset_is_removed(self):
        bll.refresh_if_due(self.github)
        self.github.repos[COARS][fp.CONFIG_FILE] = None  # dataset.yaml is gone from its default branch

        with self.assertLogs("apiserver.bll.dataset_cache", level="INFO"):
            self.assertTrue(bll.rebuild(self.github))

        self.assertEqual(list(self.repository_entries()), [OTHER])

    def test_a_repository_the_app_is_no_longer_installed_on_is_removed(self):
        bll.refresh_if_due(self.github)
        del self.github.repos[COARS]

        bll.rebuild(self.github)

        self.assertEqual(list(self.repository_entries()), [OTHER])

    def test_a_repository_that_cannot_be_read_when_it_is_first_seen_is_not_added(self):
        self.github.failing.add(COARS)

        with self.assertLogs("apiserver.bll.dataset_cache", level="WARNING"):
            self.assertFalse(bll.rebuild(self.github))

        self.assertEqual(list(self.repository_entries()), [OTHER])
        self.assertIsNone(bll.read_snapshot()["refreshed_at"])
