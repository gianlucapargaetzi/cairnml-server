# apiserver/tests/bll/test_workers_agent_info.py
from datetime import datetime
from uuid import uuid4

import pytest

from apiserver.apimodels.workers import WorkerEntry, IdNameEntry, StatusReportRequest
from apiserver.bll.workers import WorkerBLL, WorkerConversionHelper
from apiserver.tests.db import ensure_db

ensure_db()

worker_bll = WorkerBLL()


def register_worker(company, worker_id):
    now = datetime.utcnow()
    entry = WorkerEntry(
        key=worker_bll._get_worker_key(company, "owner1", worker_id),
        id=worker_id,
        user=IdNameEntry(id="owner1", name="owner1"),
        company=IdNameEntry(id=company, name=company),
        register_time=now,
        register_timeout=3600,
        last_activity_time=now,
    )
    worker_bll._save_worker(entry)


def report(company, worker_id, **fields):
    worker_bll.status_report(
        company_id=company,
        user_id="owner1",
        ip="10.0.0.1",
        report=StatusReportRequest(worker=worker_id, timestamp=1, **fields),
    )


def listed_entry(company, worker_id):
    entries = [w for w in worker_bll.get_all(company, last_seen=3600) if w.id == worker_id]
    assert len(entries) == 1
    return WorkerConversionHelper.from_worker_entry(entries[0]).worker


@pytest.mark.needs_redis
def test_status_report_stores_the_agent_version_and_run_mode():
    company = f"c-{uuid4().hex[:8]}"
    worker_id = f"w-{uuid4().hex[:8]}"
    register_worker(company, worker_id)

    report(company, worker_id, agent_version="1.2.3", run_mode="docker")

    listed = listed_entry(company, worker_id)
    assert listed.agent_version == "1.2.3"
    assert listed.run_mode == "docker"


@pytest.mark.needs_redis
def test_an_agent_that_does_not_send_them_leaves_the_fields_empty():
    company = f"c-{uuid4().hex[:8]}"
    worker_id = f"w-{uuid4().hex[:8]}"
    register_worker(company, worker_id)

    report(company, worker_id)

    listed = listed_entry(company, worker_id)
    assert listed.agent_version is None
    assert listed.run_mode is None


@pytest.mark.needs_redis
def test_a_later_report_without_the_fields_keeps_the_stored_values():
    company = f"c-{uuid4().hex[:8]}"
    worker_id = f"w-{uuid4().hex[:8]}"
    register_worker(company, worker_id)
    report(company, worker_id, agent_version="1.2.3", run_mode="venv")

    report(company, worker_id)

    listed = listed_entry(company, worker_id)
    assert listed.agent_version == "1.2.3"
    assert listed.run_mode == "venv"
