"""The cached list of the dataset repositories the GitHub App finds: read without a GitHub call, so that listing never calls GitHub.

The list is one snapshot, stored as JSON text in the Settings collection under one key. It is refreshed in two ways:

- A full rebuild once an hour. Each API server process runs a background thread that checks every minute whether a
  rebuild is due. A Redis lock lets one process rebuild at a time, and the hour is checked again under the lock, so
  the processes do not repeat one another's rebuild.
- A refresh of one repository when a job token is issued for it (devices.git_token): when the repository has no entry,
  or its entry is more than an hour old. Each repository is refreshed at most once every 5 minutes (a Redis key), so a
  burst of jobs cannot flood GitHub.

The job token itself still verifies the dataset live (dataset_access.verify_dataset). The snapshot only feeds the list
and these refreshes; it never answers a token request.

Failures keep the last good entries. An entry is removed only when GitHub confirms that the repository is not a dataset
(dataset.yaml missing at its root), or when the App is no longer installed on it. A transient failure (GitHub unreachable,
rate-limited, an installation that cannot be read) keeps the entry as it was. A footprint that no longer holds is logged
and keeps the entry too: the repository is still a dataset, only broken. refreshed_at is the time of the last full rebuild
in which no check failed transiently, so a stale list shows; checked_at is the time each entry was last checked.
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from threading import Thread
from time import sleep

from apiserver.apierrors import errors
from apiserver.bll import dataset_access
from apiserver.bll.dataset_access import GitHubUnreadable, NotADataset
from apiserver.database.model.settings import Settings
from apiserver.idp.github import GitHubApp, GitHubError
from apiserver.idp.github_check import build_from_config
from apiserver.redis_manager import redman

SNAPSHOT_KEY = "dataset_cache.snapshot"
FULL_REFRESH = timedelta(hours=1)  # a rebuild is due when the last attempt is this old
REPO_STALE = timedelta(hours=1)  # a repository entry older than this is refreshed when a job token is issued for it
REPO_MIN_GAP_SEC = 300  # a repository is refreshed at most this often, however many jobs ask for it
LOCK_KEY = "dataset_cache/refresh_lock"
LOCK_TTL_SEC = 1800  # a rebuild that runs longer than this lets another process start one
POLL_SEC = 60  # how often the background thread checks whether a rebuild is due
log = logging.getLogger(__name__)
_redis = redman.connection("apiserver")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def _older_than(stamp, age: timedelta) -> bool:
    """True when the ISO time is missing or at least ``age`` old."""
    if not stamp:
        return True
    return _now() - datetime.fromisoformat(stamp) >= age


def read_snapshot() -> dict:
    """The snapshot as stored: refreshed_at (last full rebuild with no transient failure), attempted_at (last full
    rebuild tried) and datasets (the entries, by repository name). Stored as JSON text because label names in a
    footprint are arbitrary keys, which Mongo does not take as field names."""
    text = Settings.get_by_key(SNAPSHOT_KEY)
    if not text:
        return {"refreshed_at": None, "attempted_at": None, "datasets": []}
    return json.loads(text)


def _write(snapshot: dict) -> None:
    Settings.set_or_add_value(SNAPSHOT_KEY, json.dumps(snapshot))


def _sorted(entries: dict) -> list:
    return sorted(entries.values(), key=lambda dataset: dataset["repository"].lower())


def cached_list() -> dict:
    """The cached entries and when the whole list was last rebuilt. No GitHub call."""
    snapshot = read_snapshot()
    return {"datasets": snapshot["datasets"], "refreshed_at": snapshot["refreshed_at"]}


def _check_into(entries: dict, app: GitHubApp, repo: str, now: str) -> bool:
    """Checks one installed repository and puts the result in ``entries``. False when GitHub could not be read for it;
    its last entry is then kept as it was."""
    try:
        entries[repo] = {**dataset_access.dataset_entry(app, repo), "checked_at": now}
    except NotADataset:
        # Confirmed: dataset.yaml is missing at the root of its default branch.
        entries.pop(repo, None)
        log.info("the repository %s is not a dataset any more; it is removed from the dataset list", repo)
    except GitHubUnreadable as exc:
        log.warning("the dataset %s could not be checked; its last entry is kept: %s", repo, exc)
        return False
    except errors.bad_request.ValidationError as exc:
        log.warning("the dataset %s does not pass its footprint check; its last entry is kept: %s", repo, exc.msg)
    return True


def rebuild(app: GitHubApp) -> bool:
    """Rebuilds the whole list from the App's installations: the repositories are read once (paged), then each one is
    checked. Returns True when no check failed transiently, which is when refreshed_at moves on.

    If the installations cannot be read, nothing changes but attempted_at."""
    now = _iso(_now())
    snapshot = read_snapshot()
    try:
        installed = app.installed_repositories()
    except GitHubError as exc:
        log.warning("the dataset list is not rebuilt: the App's installations could not be read: %s", exc)
        _write({**snapshot, "attempted_at": now})
        return False

    names = {item["repository"] for item in installed}
    entries = {dataset["repository"]: dataset for dataset in snapshot["datasets"]}
    complete = True
    for repo in sorted(names):
        complete = _check_into(entries, app, repo, now) and complete
    for repo in list(entries):
        if repo not in names:
            # The installations were read in full, so the App is no longer installed on it.
            del entries[repo]
    _write({
        "refreshed_at": now if complete else snapshot["refreshed_at"],
        "attempted_at": now,
        "datasets": _sorted(entries),
    })
    return complete


def refresh_if_due(app: GitHubApp) -> bool:
    """Rebuilds the list when the last attempt is an hour old. Returns True when this process rebuilt it.

    The Redis lock is held for the rebuild, so while one API server process holds it the others return at once. The
    hour is checked again under the lock, for a process that found the list stale just before another one rebuilt it.
    """
    if not _older_than(read_snapshot()["attempted_at"], FULL_REFRESH):
        return False
    if not _redis.set(LOCK_KEY, _iso(_now()), ex=LOCK_TTL_SEC, nx=True):
        return False  # another API server process is rebuilding the list now
    try:
        if not _older_than(read_snapshot()["attempted_at"], FULL_REFRESH):
            return False
        rebuild(app)
        return True
    finally:
        _redis.delete(LOCK_KEY)


def refresh_repository(app: GitHubApp, repo: str) -> bool:
    """Refreshes one repository's entry, for a job token issued for it: when it has no entry or an entry older than an
    hour, and at most once in five minutes. Returns True when it was checked. A failed check keeps the entry."""
    snapshot = read_snapshot()
    entry = next((dataset for dataset in snapshot["datasets"] if dataset["repository"] == repo), None)
    if entry is not None and not _older_than(entry.get("checked_at"), REPO_STALE):
        return False
    if not _redis.set(f"dataset_cache/repo/{repo}", _iso(_now()), ex=REPO_MIN_GAP_SEC, nx=True):
        return False  # checked a moment ago, by this job or by another
    now = _iso(_now())
    entries = {dataset["repository"]: dataset for dataset in snapshot["datasets"]}
    _check_into(entries, app, repo, now)
    _write({**snapshot, "datasets": _sorted(entries)})
    return True


class DatasetCacheRefresher(Thread):
    """The hourly rebuild, run by each API server process. The lock keeps the processes from rebuilding together."""

    def __init__(self):
        super().__init__(target=self._run, name="dataset-cache-refresher", daemon=True)

    @staticmethod
    def _run():
        while True:
            try:
                app = build_from_config()
                if app is not None:
                    refresh_if_due(app)
            except Exception:
                log.exception("the dataset list could not be refreshed")
            sleep(POLL_SEC)


refresher = DatasetCacheRefresher()
