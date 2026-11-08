# apiserver/tests/bll/test_share_writer_helpers.py
"""The writers' helpers refuse a deactivated caller, as share() and unshare() do."""
from datetime import datetime
from uuid import uuid4

import pytest

from apiserver.apierrors import errors
from apiserver.bll import access
from apiserver.database.model.auth import User
from apiserver.database.model.share import Share
from apiserver.tests.db import ensure_db

ensure_db()


@pytest.fixture
def company():
    return f"c-{uuid4().hex[:8]}"


@pytest.fixture
def users(company):
    User(id="active", name="active", company=company, created=datetime.utcnow()).save()
    User(id="gone", name="gone", company=company, created=datetime.utcnow(), idp_disabled=True).save()


def count(company):
    return Share.objects(company=company, resource_type="queue", resource_id="q1").count()


def test_a_deactivated_caller_cannot_record_a_share(company, users):
    with pytest.raises(errors.forbidden.NoWritePermission):
        access.record_grant(company, "gone", "queue", "q1", "public", actor="gone")
    assert count(company) == 0


def test_an_active_caller_records_a_share(company, users):
    access.record_grant(company, "active", "queue", "q1", "public", actor="active")
    assert count(company) == 1


def test_a_deactivated_caller_cannot_remove_a_share(company, users):
    access.record_grant(company, "active", "queue", "q1", "public", actor="active")
    with pytest.raises(errors.forbidden.NoWritePermission):
        access.remove_grant(company, "queue", "q1", "public", actor="gone")
    assert count(company) == 1


def test_a_deactivated_caller_cannot_drop_the_shares_of_a_resource(company, users):
    access.record_grant(company, "active", "queue", "q1", "public", actor="active")
    with pytest.raises(errors.forbidden.NoWritePermission):
        access.drop_resource_shares(company, "queue", "q1", actor="gone")
    assert count(company) == 1


def test_the_server_itself_may_write_without_an_actor(company, users):
    """The default queue is made by the server, with no user"""
    access.record_grant(company, "", "queue", "q1", "public", actor="")
    assert count(company) == 1
