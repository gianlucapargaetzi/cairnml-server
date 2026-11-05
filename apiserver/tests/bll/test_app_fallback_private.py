# apiserver/tests/bll/test_app_fallback_private.py
"""A repository with a kept record is never re-added as an App Public row. The App fallback applies only when no record
exists for the repository, so a private repository the App also finds stays private to its record."""
from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from apiserver.apierrors import errors
from apiserver.bll import datasets as bll
from apiserver.database.model.dataset import Dataset
from apiserver.database.model.user_access import UserAccess
from apiserver.services import datasets as service
from apiserver.tests.db import ensure_db

ensure_db()

APP_OWNER = "admin1"
OWNER_A = "owner_a"
OTHER_B = "user_b"
REPO = "acme/private"


@pytest.fixture
def company(monkeypatch):
    """A company of its own: the App is installed, the App finds the private repository, and A owns a private record."""
    name = f"c-{uuid4().hex[:8]}"
    UserAccess(id=f"app-{name}", company=name, user=APP_OWNER, kind="app", label="App", scope="company",
               secret="x", created=datetime.utcnow()).save()
    Dataset(id=f"ds-{name}", company=name, kind="repo", ref=REPO, name="private", owner=OWNER_A,
            visibility="user", credential=None, state="ok", state_message="", created=datetime.utcnow(),
            updated=datetime.utcnow(), last_checked=datetime.utcnow()).save()
    monkeypatch.setattr(bll.dataset_cache, "cached_list", lambda: {"datasets": [{"repository": REPO}]})
    yield name
    Dataset.objects(company=name).delete()
    UserAccess.objects(company=name).delete()


class FakeApp:
    def installation_token(self, repo, permissions=None):
        raise AssertionError("no token may be issued for a private repository B has no share on")


def person(user):
    return SimpleNamespace(
        data={}, identity=SimpleNamespace(user=user, role="user"),
        auth=SimpleNamespace(token_kind="client", job_task=None), real_ip="127.0.0.1",
        get_header=lambda header, default=None: default, HEADER_REAL_IP="X-Real-IP",
    )


@pytest.fixture
def github(monkeypatch):
    app = FakeApp()
    monkeypatch.setattr(service, "build_from_config", lambda: app)
    monkeypatch.setattr(service, "public_repo", lambda repo: False)
    monkeypatch.setattr(service, "audit", lambda *args, **kwargs: None)
    monkeypatch.setattr(service, "client_address", lambda call: "127.0.0.1")
    return app


def test_the_level_on_a_repository_with_a_record_is_not_the_app_fallback(company):
    assert bll.held_on_repository(company, OTHER_B, False, REPO) == (None, "dataset")


def test_a_user_with_no_share_on_a_private_recorded_repository_is_refused_as_not_found(company, github):
    call = person(OTHER_B)
    call.data = {"repository": f"https://github.com/{REPO}", "level": "read"}

    with pytest.raises(errors.bad_request.ValidationError) as refused:
        service.git_token(call, company, None)
    assert "no such dataset" in refused.value.msg


def test_a_private_recorded_repository_is_not_listed_for_a_user_with_no_share(company):
    rows = bll.list_visible(company, OTHER_B, False)

    assert [row for row in rows if row["ref"].lower() == REPO] == []
