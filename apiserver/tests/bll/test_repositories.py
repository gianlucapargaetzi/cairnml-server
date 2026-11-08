"""A repository a run uses is recorded at its first use, owned by that run's user. A GitHub App attached to it is the
owner's own or the company's, and is the only way a private repository is read."""
from datetime import datetime
from uuid import uuid4

import pytest

from apiserver.apierrors import errors
from apiserver.bll import repositories as bll
from apiserver.database.model.code_repository import CodeRepository
from apiserver.database.model.user_access import UserAccess
from apiserver.idp import git_credentials as store
from apiserver.tests.db import ensure_db

ensure_db()
REPO = "acme/proj"


@pytest.fixture
def company(monkeypatch):
    name = f"c-repo-{uuid4().hex[:8]}"
    monkeypatch.setattr(store, "key_material", lambda: "test-key-material")
    monkeypatch.setattr(bll.datasets_bll, "_can_read", lambda reader, repo: True)
    monkeypatch.setattr(bll.datasets_bll, "_reader", lambda entry, secret: object())
    yield name
    CodeRepository.objects(company=name).delete()
    UserAccess.objects(company=name).delete()


def app(company, entry_id, user, scope="user"):
    UserAccess(id=entry_id, company=company, user=user, kind="app", app_id="1", api_url=None, label=f"app {entry_id}",
               scope=scope, host="github.com", path="", secret=store.encrypt("pem", store.key_material()),
               created=datetime.utcnow()).save()
    return entry_id


def test_the_first_use_records_the_repository_for_its_run_user(company):
    record = bll.ensure(company, REPO, "u1", datetime.utcnow())
    assert record.owner == "u1" and record.credential is None
    again = bll.ensure(company, REPO, "u2", datetime.utcnow())
    assert again.id == record.id and again.owner == "u1"


def test_only_the_owner_or_an_admin_attaches_an_app(company):
    bll.ensure(company, REPO, "u1", datetime.utcnow())
    app(company, "app-u1", "u1")
    with pytest.raises(errors.forbidden.NoWritePermission):
        bll.set_credential(company, "u2", f"https://github.com/{REPO}", "app-u1", False)
    view = bll.set_credential(company, "u1", f"https://github.com/{REPO}", "app-u1", False)
    assert view["credential"] == "app-u1"


def test_a_repository_that_is_not_used_yet_has_no_record_to_attach_to(company):
    app(company, "app-u1", "u1")
    with pytest.raises(errors.bad_request.ValidationError, match="not used by any run"):
        bll.set_credential(company, "u1", f"https://github.com/{REPO}", "app-u1", False)


def test_a_personal_token_is_refused_as_the_repository_credential(company):
    UserAccess(id="tok", company=company, user="u1", kind="token", label="tok", scope="user", host="github.com",
               path="", secret="x", created=datetime.utcnow()).save()
    bll.ensure(company, REPO, "u1", datetime.utcnow())
    with pytest.raises(errors.bad_request.ValidationError, match="personal token"):
        bll.set_credential(company, "u1", f"https://github.com/{REPO}", "tok", False)


def test_a_job_reads_a_private_repository_only_with_the_app_attached(company):
    record = bll.ensure(company, REPO, "u1", datetime.utcnow())
    with pytest.raises(errors.bad_request.ValidationError, match="no GitHub App attached"):
        bll.credential_for_job(company, record, REPO)
    record.credential = app(company, "app-u1", "u1")
    record.save()
    entry, secret = bll.credential_for_job(company, record, REPO)
    assert entry.id == "app-u1" and secret == "pem"


def test_a_repository_can_be_listed_before_any_run_uses_it(company):
    app(company, "app-u1", "u1")
    view = bll.add(company, "u1", f"https://github.com/{REPO}", "app-u1")
    assert view["ref"] == REPO and view["owner"] == "u1" and view["credential"] == "app-u1"


def test_a_public_repository_is_listed_without_an_app(company):
    assert bll.add(company, "u1", f"https://github.com/{REPO}", None)["credential"] is None


def test_listing_a_repository_of_someone_else_is_refused(company):
    bll.ensure(company, REPO, "u1", datetime.utcnow())
    with pytest.raises(errors.bad_request.ValidationError, match="already listed by u1"):
        bll.add(company, "u2", f"https://github.com/{REPO}", None)


def test_listing_again_with_an_app_attaches_it_for_the_owner(company):
    bll.add(company, "u1", f"https://github.com/{REPO}", None)
    app(company, "app-u1", "u1")
    assert bll.add(company, "u1", f"https://github.com/{REPO}", "app-u1")["credential"] == "app-u1"
