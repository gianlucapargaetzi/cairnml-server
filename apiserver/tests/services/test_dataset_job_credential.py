"""A job reads a dataset with the credential the dataset holds, and no other access. It needs the dataset to be visible to
whoever queued the job, or allocated to its project. Project grants on credentials do not apply to datasets."""
from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from apiserver.apierrors import errors
from apiserver.bll import datasets as bll
from apiserver.database.model.dataset import Dataset
from apiserver.database.model.user_access import UserAccess
from apiserver.idp import git_credentials as store
from apiserver.services import devices
from apiserver.tests.db import ensure_db

ensure_db()
REPO = "acme/cars"


class FakeApp:
    def __init__(self, app_id, secret, api_url):
        self.secret = secret

    def installation_token(self, repo, permissions=None):
        return SimpleNamespace(token=f"tok-{self.secret}", expires_at=datetime(2030, 1, 1))


@pytest.fixture
def company():
    name = f"c-job-{uuid4().hex[:8]}"
    yield name
    Dataset.objects(company=name).delete()
    UserAccess.objects(company=name).delete()


@pytest.fixture(autouse=True)
def keys(monkeypatch):
    monkeypatch.setattr(store, "key_material", lambda: "test-key-material")
    monkeypatch.setattr(devices, "GitHubApp", FakeApp)
    monkeypatch.setattr(devices, "audit", lambda *args, **kwargs: None)


def credential(company, entry_id, user="u1", kind="app", scope="user", secret="s-app"):
    UserAccess(id=entry_id, company=company, user=user, kind=kind, label=f"label {entry_id}", scope=scope,
               host="github.com", path="", app_id="1", api_url=None,
               secret=store.encrypt(secret, store.key_material()), created=datetime.utcnow()).save()
    return entry_id


def dataset(company, owner, credential_id, kind="repo", ref=REPO):
    record = Dataset(id=uuid4().hex, company=company, kind=kind, ref=ref, name=ref.split("/")[-1], owner=owner,
                     visibility="user", credential=credential_id, state="ok", created=datetime.utcnow())
    record.save()
    return record


def test_the_job_reads_with_the_dataset_credential(company):
    credential_id = credential(company, "app-u1")
    record = dataset(company, "u1", credential_id)
    answer = devices._dataset_token(company, record, f"acme/cars", "agent", "task1", None, {}, layout=False)
    assert answer["token"] == "tok-s-app"


def test_a_dataset_with_no_credential_gives_a_job_no_token(company):
    record = dataset(company, "u1", None)
    with pytest.raises(errors.bad_request.ValidationError, match="has no credential"):
        devices._dataset_token(company, record, REPO, "agent", "task1", None, {}, layout=False)


def test_a_credential_of_someone_else_than_the_dataset_owner_is_not_used_by_jobs(company):
    credential_id = credential(company, "tok-u2", user="u2", kind="token", secret="ghp_u2")
    record = dataset(company, "u1", credential_id)
    with pytest.raises(errors.bad_request.ValidationError, match="belongs to someone else"):
        bll.job_credential(company, record, REPO)


def test_the_companys_credential_is_used_by_jobs(company):
    credential_id = credential(company, "company-app", user="admin1", scope="company")
    record = dataset(company, "u1", credential_id)
    assert devices._dataset_token(company, record, REPO, "agent", "task1", None, {}, layout=False)["token"] == "tok-s-app"


def test_a_job_may_read_only_what_the_one_who_queued_it_may_see(company):
    record = dataset(company, "u1", None)  # visibility user: the owner only
    assert bll.job_can_read(company, "u1", None, record) is True
    assert bll.job_can_read(company, "u2", None, record) is False
    assert bll.job_can_read(company, None, None, record) is False


def test_a_registry_is_read_with_its_credential_too(company):
    credential_id = credential(company, "app-u1")
    record = dataset(company, "u1", credential_id, kind="registry", ref="acme/registry")
    assert devices._dataset_token(company, record, "acme/registry", "agent", "task1", None, {}, layout=False)["token"]
