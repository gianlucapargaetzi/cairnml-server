"""The details of a dataset of ours are read through the credential it holds. A repository with no commit yet is shown as
empty, not as unreadable."""
from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from apiserver.bll import dataset_details
from apiserver.bll import datasets as bll
from apiserver.database.model.dataset import Dataset
from apiserver.database.model.user_access import UserAccess
from apiserver.idp import git_credentials as store
from apiserver.idp.github import GitHubError, PublicReader
from apiserver.tests.db import ensure_db

ensure_db()
REPO = "acme/empty"


class EmptyRepo:
    """A repository the reader can see, with no commit: the branch lookup fails, the repository lookup answers."""

    def default_branch_head(self, repo):
        raise GitHubError("branch lookup failed: 409")

    def repository_visible(self, repo):
        return True


class GoneRepo(EmptyRepo):
    def repository_visible(self, repo):
        return False


@pytest.fixture
def company():
    name = f"c-details-{uuid4().hex[:8]}"
    yield name
    Dataset.objects(company=name).delete()
    UserAccess.objects(company=name).delete()


@pytest.fixture(autouse=True)
def keys(monkeypatch):
    monkeypatch.setattr(store, "key_material", lambda: "test-key-material")


def test_an_empty_repository_is_shown_as_empty(monkeypatch):
    details = dataset_details.own_details(EmptyRepo(), REPO, None)
    assert details["kind"] == "own" and details["revision"] is None and details["commits"] == []
    assert details["metadata"] is None and details["description"] is None


def test_an_empty_repository_that_is_hidden_is_still_an_error():
    with pytest.raises(Exception, match="could not be read"):
        dataset_details.own_details(GoneRepo(), REPO, None)


def test_a_dataset_without_a_credential_is_read_as_a_public_repository(company):
    record = Dataset(id=uuid4().hex, company=company, kind="repo", ref=REPO, name="empty", owner="u1",
                     visibility="user", credential=None, state="ok", created=datetime.utcnow())
    record.save()
    assert isinstance(bll.reader_for_dataset(company, record, REPO), PublicReader)


def test_a_dataset_is_read_with_its_own_credential(company, monkeypatch):
    UserAccess(id="app-u1", company=company, user="u1", kind="app", label="App", scope="user", app_id="7",
               api_url=None, secret=store.encrypt("pem", store.key_material()), created=datetime.utcnow()).save()
    record = Dataset(id=uuid4().hex, company=company, kind="repo", ref=REPO, name="empty", owner="u1",
                     visibility="user", credential="app-u1", state="ok", created=datetime.utcnow())
    record.save()
    made = []
    monkeypatch.setattr(bll, "GitHubApp", lambda app_id, key, api: made.append((app_id, key)) or SimpleNamespace())
    bll.reader_for_dataset(company, record, REPO)
    assert made == [("7", "pem")]
