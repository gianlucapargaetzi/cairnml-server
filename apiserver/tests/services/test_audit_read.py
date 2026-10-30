from datetime import datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from apiserver.apierrors import errors
from apiserver.apimodels.audit import AuditListRequest
from apiserver.database.model.audit import AuditEvent
from apiserver.services import audit as audit_svc
from apiserver.tests.db import ensure_db

ensure_db()


class FakeCall:
    def __init__(self, role="admin"):
        self.result = SimpleNamespace(data=None)
        self.identity = SimpleNamespace(role=role)


COMPANY = "company-a"


@pytest.fixture
def actor():
    return f"actor-{uuid4().hex[:8]}"


@pytest.fixture
def events(actor):
    """Three events of one actor, one hour apart: the oldest first."""
    start = datetime.utcnow().replace(microsecond=0) - timedelta(hours=3)
    saved = []
    for i, action in enumerate(["web.login", "token.issued", "token.refused"]):
        event = AuditEvent(actor=actor, action=action, ip=f"10.0.0.{i}", company=COMPANY, ts=start + timedelta(hours=i))
        event.save()
        saved.append(event)
    yield saved
    for event in saved:
        event.delete()


def get_all(role="admin", company=COMPANY, **fields):
    return audit_svc.get_all(FakeCall(role), company, AuditListRequest(**fields))


def test_events_come_newest_first_with_the_client_address(actor, events):
    result = get_all(actor=actor)

    assert result["total"] == 3
    assert [event["action"] for event in result["events"]] == ["token.refused", "token.issued", "web.login"]
    assert [event["ip"] for event in result["events"]] == ["10.0.0.2", "10.0.0.1", "10.0.0.0"]


def test_filter_by_action(actor, events):
    result = get_all(actor=actor, action="token.issued")

    assert result["total"] == 1
    assert result["events"][0]["action"] == "token.issued"


def test_filter_by_time_range(actor, events):
    since = (events[1].ts).isoformat()
    until = (events[1].ts).isoformat()

    result = get_all(actor=actor, since=since, until=until)

    assert [event["action"] for event in result["events"]] == ["token.issued"]


def test_time_with_an_offset_is_read_as_utc(actor, events):
    # One hour after the last event's time, written with a +01:00 offset, is that event's own time in UTC
    local = (events[2].ts + timedelta(hours=1)).isoformat() + "+01:00"

    result = get_all(actor=actor, since=local)

    assert [event["action"] for event in result["events"]] == ["token.refused"]


def test_paging_keeps_the_total(actor, events):
    first = get_all(actor=actor, page=0, page_size=2)
    second = get_all(actor=actor, page=1, page_size=2)

    assert first["total"] == second["total"] == 3
    assert [event["action"] for event in first["events"]] == ["token.refused", "token.issued"]
    assert [event["action"] for event in second["events"]] == ["web.login"]


def test_a_bad_time_is_refused(actor):
    with pytest.raises(errors.bad_request.ValidationError):
        get_all(actor=actor, since="yesterday")


def test_each_event_names_its_company(actor, events):
    result = get_all(actor=actor)

    assert {event["company"] for event in result["events"]} == {COMPANY}


def test_an_admin_reads_only_the_events_of_their_company(actor, events):
    other = AuditEvent(actor=actor, action="web.login", company="company-b", ip="10.9.9.9")
    other.save()
    try:
        assert get_all(actor=actor)["total"] == 3
        assert get_all(actor=actor, company="company-b")["total"] == 1
    finally:
        other.delete()


def test_root_reads_the_events_of_every_company(actor, events):
    other = AuditEvent(actor=actor, action="web.login", company="company-b", ip="10.9.9.9")
    other.save()
    try:
        assert get_all(role="root", actor=actor)["total"] == 4
    finally:
        other.delete()
