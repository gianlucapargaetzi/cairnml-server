from types import SimpleNamespace
from uuid import uuid4

import pytest

from apiserver.apierrors import errors
from apiserver.database.model.auth import Role
from apiserver.database.model.dataset import Dataset
from apiserver.idp import github_check
from apiserver.services import datasets as svc
from apiserver.service_repo.auth import Identity
from apiserver.tests.db import ensure_db

ensure_db()

REPO = "https://github.com/acme/cars"


@pytest.fixture
def company():
    return f"c-{uuid4().hex[:8]}"


@pytest.fixture(autouse=True)
def clean_rows(company):
    yield
    Dataset.objects(company=company).delete()


class FakeApp:
    """The App reads a dataset's repository; every read is recorded so a test can see whether GitHub was asked."""

    def __init__(self):
        self.reads = []

    def default_branch_head(self, repo):
        self.reads.append(("head", repo))
        return "main", "head-sha"

    def read_file(self, repo, path, ref=None):
        self.reads.append(("read", repo, path))
        return b"the description" if path == "DATASET.md" else None

    def list_commits(self, repo, ref, limit):
        self.reads.append(("commits", repo))
        return []


class FakeCall:
    def __init__(self, company, user, role=Role.user, data=None):
        self.identity = Identity(user=user, company=company, role=role)
        self.data = data if data is not None else {}
        self.result = SimpleNamespace(data=None)


@pytest.fixture
def app(monkeypatch):
    fake = FakeApp()
    monkeypatch.setattr(github_check, "reader", lambda: fake)
    return fake


def user_only_dataset(company, owner="userA", repo="acme/cars"):
    Dataset(id=uuid4().hex, company=company, kind="repo", ref=repo, name=repo.split("/")[1], owner=owner,
            visibility="user").save()


def test_a_user_only_dataset_is_refused_to_another_user_even_when_the_app_is_installed(company, app):
    user_only_dataset(company)
    call = FakeCall(company, "userB", data={"repository": REPO})
    with pytest.raises(errors.bad_request.ValidationError, match="no such dataset"):
        svc.get_details(call, company, None)
    assert app.reads == []
    assert call.result.data is None


def test_the_owner_of_a_user_only_dataset_sees_its_details(company, app):
    user_only_dataset(company)
    call = FakeCall(company, "userA", data={"repository": REPO})
    svc.get_details(call, company, None)
    assert call.result.data["repository"] == "acme/cars"
    assert call.result.data["description"] == "the description"


def test_an_admin_sees_a_user_only_dataset_of_another_user(company, app):
    user_only_dataset(company)
    call = FakeCall(company, "userB", role=Role.admin, data={"repository": REPO})
    svc.get_details(call, company, None)
    assert call.result.data["repository"] == "acme/cars"


def test_the_repository_is_matched_case_insensitively(company, app):
    user_only_dataset(company)
    call = FakeCall(company, "userB", data={"repository": "https://github.com/ACME/Cars"})
    with pytest.raises(errors.bad_request.ValidationError, match="no such dataset"):
        svc.get_details(call, company, None)
    assert app.reads == []


def test_a_repository_with_no_kept_record_keeps_today_behaviour(company, app):
    call = FakeCall(company, "userB", data={"repository": REPO})
    svc.get_details(call, company, None)
    assert call.result.data["repository"] == "acme/cars"
    assert ("head", "acme/cars") in app.reads
