"""A dataset has a unique id and a name a run uses (its reference), unique in the company. References resolve to one
dataset: an id, a name, or the address of its repository. A job resolves only what its task's user may see."""
from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from apiserver.apierrors import errors
from apiserver.bll import datasets as bll
from apiserver.database.model.dataset import Dataset
from apiserver.services import datasets as svc
from apiserver.tests.db import ensure_db

ensure_db()
REPO = "https://github.com/acme/cars"


@pytest.fixture
def company():
    name = f"c-refs-{uuid4().hex[:8]}"
    yield name
    Dataset.objects(company=name).delete()


@pytest.fixture(autouse=True)
def public(monkeypatch):
    monkeypatch.setattr(bll, "_public", lambda repo: False)


def person(company, user, is_admin=False):
    return SimpleNamespace(data={}, result=SimpleNamespace(data=None),
                           identity=SimpleNamespace(user=user, company=company, role="admin" if is_admin else "user"),
                           auth=None)


def resolve_as(company, call, reference):
    call.data = {"ref": reference}
    svc.resolve(call, company, None)
    return call.result.data["dataset"]


def test_a_repository_is_referred_to_by_its_name(company):
    added = bll.add_repo(company, "u1", REPO)
    assert added["reference"] == "cars"


def test_a_name_resolves_to_one_dataset(company):
    bll.add_repo(company, "u1", REPO)
    call = person(company, "u1")
    assert resolve_as(company, call, "cars")["ref"] == "acme/cars"


def test_an_id_and_a_repository_address_resolve_to_the_same_dataset(company):
    added = bll.add_repo(company, "u1", REPO)
    call = person(company, "u1")
    assert resolve_as(company, call, added["id"])["id"] == added["id"]
    assert resolve_as(company, call, "https://github.com/acme/cars.git")["id"] == added["id"]


def test_a_second_dataset_with_the_same_name_is_refused(company):
    bll.add_repo(company, "u1", REPO)
    with pytest.raises(errors.bad_request.ValidationError, match="already used by acme/cars"):
        bll.add_repo(company, "u2", "https://github.com/other/cars")


def test_a_person_who_cannot_see_a_dataset_gets_no_such_dataset(company):
    added = bll.add_repo(company, "u1", REPO)
    with pytest.raises(errors.bad_request.ValidationError, match="no such dataset"):
        resolve_as(company, person(company, "u2"), added["id"])


def test_a_job_resolves_a_dataset_its_task_users_may_see(company, monkeypatch):
    added = bll.add_repo(company, "u1", REPO)
    task = SimpleNamespace(project=None, execution=SimpleNamespace(enqueued_by="u1"))
    monkeypatch.setattr(svc, "Task", SimpleNamespace(objects=lambda **kw: SimpleNamespace(first=lambda: task)))
    call = person(company, "u1")
    call.auth = SimpleNamespace(job_task="task1")
    assert resolve_as(company, call, "cars")["id"] == added["id"]


def test_a_job_does_not_resolve_a_dataset_its_task_users_may_not_see(company, monkeypatch):
    added = bll.add_repo(company, "u1", REPO)
    task = SimpleNamespace(project=None, execution=SimpleNamespace(enqueued_by="u2"))
    monkeypatch.setattr(svc, "Task", SimpleNamespace(objects=lambda **kw: SimpleNamespace(first=lambda: task)))
    call = person(company, "u2")
    call.auth = SimpleNamespace(job_task="task1")
    with pytest.raises(errors.bad_request.ValidationError, match="no such dataset"):
        resolve_as(company, call, added["id"])
