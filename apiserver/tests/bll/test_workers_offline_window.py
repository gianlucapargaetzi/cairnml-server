# apiserver/tests/bll/test_workers_offline_window.py
from datetime import datetime, timedelta
from uuid import uuid4

import pytest

from apiserver.apimodels.workers import WorkerEntry, IdNameEntry
from apiserver.bll.workers import WorkerBLL
from apiserver.config_repo import config
from apiserver.tests.db import ensure_db

ensure_db()

worker_bll = WorkerBLL()


@pytest.fixture
def company():
    return f"c-{uuid4().hex[:8]}"


def report_worker(company, worker_id, reported_at, timeout):
    entry = WorkerEntry(
        key=worker_bll._get_worker_key(company, "owner1", worker_id),
        id=worker_id,
        user=IdNameEntry(id="owner1", name="owner1"),
        company=IdNameEntry(id=company, name=company),
        register_time=reported_at,
        register_timeout=timeout,
        last_activity_time=reported_at,
    )
    worker_bll._save_worker(entry)
    return entry


@pytest.mark.needs_redis
def test_default_worker_timeout_is_sixty_minutes():
    assert config.get("services.workers.default_worker_timeout_sec") == 3600
    assert config.get("apiserver.workers.default_timeout") == 3600


@pytest.mark.needs_redis
def test_worker_reported_59_minutes_ago_is_listed_and_61_minutes_ago_is_not(company):
    timeout = int(config.get("services.workers.default_worker_timeout_sec"))
    now = datetime.utcnow()
    recent = f"w-recent-{uuid4().hex[:8]}"
    stale = f"w-stale-{uuid4().hex[:8]}"
    report_worker(company, recent, now - timedelta(minutes=59), timeout)
    report_worker(company, stale, now - timedelta(minutes=61), timeout)

    listed = {w.id for w in worker_bll.get_all(company, last_seen=3600)}
    assert recent in listed
    assert stale not in listed
